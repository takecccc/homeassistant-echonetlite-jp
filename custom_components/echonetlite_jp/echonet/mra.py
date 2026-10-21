"""ECHONET Machine Readable Appendix (MRA) catalog.

The MRA describes every device class as JSON. This module loads the class
files lazily, resolves ``$ref`` pointers into ``definitions.json``, merges
the super class (0x0000) properties into each device class and applies
device-specific quirks on top.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Any

from .quirks import DEFAULT_QUIRKS_DIR, UNCHANGED, DeviceKey, Quirk, QuirkError, load_quirks

_LOGGER = logging.getLogger(__name__)

DEFAULT_MRA_DIR = Path(__file__).resolve().parent.parent / "mra_data"
SUPER_CLASS = 0x0000
NODE_PROFILE_CLASS = 0x0EF0

Schema = dict[str, Any]


@dataclass(frozen=True)
class Text:
    """Text in the two languages the MRA provides."""

    ja: str
    en: str

    @classmethod
    def from_json(cls, node: Any, fallback: str = "") -> Text:
        if isinstance(node, str):
            return cls(node, node)
        if isinstance(node, dict):
            ja = str(node.get("ja") or "").strip()
            en = str(node.get("en") or "").strip()
            return cls(ja or en or fallback, en or ja or fallback)
        return cls(fallback, fallback)

    def get(self, language: str) -> str:
        return self.ja if language.lower().startswith("ja") else self.en


@dataclass(frozen=True)
class PropertyDef:
    epc: int
    name: Text
    short_name: str
    description: Text
    schema: Schema
    get: bool
    set: bool
    inf: bool
    atomic: int | None = None
    # Home Assistant presentation hints from quirks ("enabled", "category").
    hints: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ClassDef:
    class_code: int
    name: Text
    short_name: str
    properties: dict[int, PropertyDef]
    known: bool = True
    # Set by quirks; see quirks.py.
    max_properties: int | None = None
    extension: str | None = UNCHANGED
    quirks: tuple[str, ...] = ()

    def get(self, epc: int) -> PropertyDef | None:
        return self.properties.get(epc)

    @cached_property
    def coefficient_epcs(self) -> frozenset[int]:
        """EPCs other properties refer to as coefficients (units, multipliers)."""
        found: set[int] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for ref in node.get("coefficient") or []:
                    if (epc := _parse_epc(ref)) is not None:
                        found.add(epc)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for prop in self.properties.values():
            walk(prop.schema)
        return frozenset(found)

    @property
    def atomic_epcs(self) -> frozenset[int]:
        """EPCs that only make sense as part of an atomic set/get pair."""
        out: set[int] = set()
        for prop in self.properties.values():
            if prop.atomic is not None:
                out.add(prop.epc)
                out.add(prop.atomic)
        return frozenset(out)


class MraCatalog:
    """Thread-safe, lazily loaded view of an MRA directory.

    File I/O happens on first access of a class; call :meth:`get_class` from an
    executor when running inside an event loop.
    """

    def __init__(
        self, root: Path | str | None = None, *, quirks_dir: Path | str | None = DEFAULT_QUIRKS_DIR
    ) -> None:
        self._root = Path(root) if root else DEFAULT_MRA_DIR
        self._quirks_dir = Path(quirks_dir) if quirks_dir else None
        self._lock = threading.Lock()
        self._definitions: dict[str, Schema] | None = None
        self._super: dict[int, PropertyDef] | None = None
        self._classes: dict[int, ClassDef] = {}
        self._quirks: list[Quirk] | None = None
        self._devices: dict[DeviceKey, ClassDef] = {}

    @property
    def version(self) -> str:
        try:
            meta = json.loads((self._root / "metaData.json").read_text(encoding="utf-8"))
            return str(meta["metaData"]["dataVersion"])
        except (OSError, KeyError, ValueError):
            return "unknown"

    def get_class(self, class_code: int) -> ClassDef:
        cached = self._classes.get(class_code)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._classes.get(class_code)
            if cached is None:
                cached = self._load_class(class_code)
                self._classes[class_code] = cached
            return cached

    def get_device_class(self, key: DeviceKey) -> ClassDef:
        """The class definition for one device: MRA plus matching quirks."""
        cached = self._devices.get(key)
        if cached is not None:
            return cached
        base = self.get_class(key.class_code)
        with self._lock:
            if self._quirks is None:
                self._quirks = load_quirks(self._quirks_dir) if self._quirks_dir else []
            matching = sorted(
                (q for q in self._quirks if q.matches(key)), key=lambda q: q.specificity
            )
            class_def = base
            for quirk in matching:
                class_def = self._apply_quirk(class_def, quirk)
            self._devices[key] = class_def
            return class_def

    def _apply_quirk(self, class_def: ClassDef, quirk: Quirk) -> ClassDef:
        props = dict(class_def.properties)
        for raw in quirk.properties:
            epc = int(raw["epc"], 16)
            if raw.get("remove"):
                props.pop(epc, None)
                continue
            try:
                props[epc] = self._property(raw, epc, base=props.get(epc))
            except QuirkError as err:
                _LOGGER.error("quirk %s: %s", quirk.source, err)
        return replace(
            class_def,
            name=Text.from_json(quirk.class_name) if quirk.class_name else class_def.name,
            short_name=quirk.short_name or class_def.short_name,
            properties=props,
            known=class_def.known or bool(quirk.class_name),
            max_properties=quirk.max_properties or class_def.max_properties,
            extension=class_def.extension if quirk.extension == UNCHANGED else quirk.extension,
            quirks=(*class_def.quirks, quirk.source),
        )

    def _load_class(self, class_code: int) -> ClassDef:
        super_props = self._super_properties()
        if class_code == SUPER_CLASS:
            path = self._root / "superClass" / "0x0000.json"
        elif class_code == NODE_PROFILE_CLASS:
            path = self._root / "nodeProfile" / "0x0EF0.json"
        else:
            path = self._root / "devices" / f"0x{class_code:04X}.json"
        data = self._read(path)
        if data is None:
            return ClassDef(
                class_code=class_code,
                name=Text.from_json(None, f"0x{class_code:04X}"),
                short_name="",
                properties=dict(super_props),
                known=False,
            )
        props = dict(super_props)
        props.update(self._parse_properties(data))
        return ClassDef(
            class_code=class_code,
            name=Text.from_json(data.get("className"), f"0x{class_code:04X}"),
            short_name=str(data.get("shortName") or ""),
            properties=props,
        )

    def _super_properties(self) -> dict[int, PropertyDef]:
        if self._super is None:
            data = self._read(self._root / "superClass" / "0x0000.json")
            self._super = self._parse_properties(data) if data else {}
        return self._super

    def _parse_properties(self, data: dict[str, Any]) -> dict[int, PropertyDef]:
        out: dict[int, PropertyDef] = {}
        latest: set[int] = set()
        for raw in data.get("elProperties") or []:
            epc = _parse_epc(raw.get("epc"))
            if epc is None:
                continue
            is_latest = (raw.get("validRelease") or {}).get("to") == "latest"
            # A class file may list several revisions of one EPC; keep the current one.
            if epc in latest and not is_latest:
                continue
            out[epc] = self._property(raw, epc, quirk=False)
            if is_latest:
                latest.add(epc)
        return out

    def _property(
        self, raw: dict[str, Any], epc: int, base: PropertyDef | None = None, *, quirk: bool = True
    ) -> PropertyDef:
        """Build a property from MRA JSON. For quirks, keys missing from ``raw``
        are taken from ``base`` so that only part of a property is overridden."""
        if quirk and base is None and "data" not in raw:
            raise QuirkError(f"0x{epc:02X} is not in the MRA, so 'data' is required")

        def pick(key: str, convert: Any, inherited: Any) -> Any:
            return convert(raw.get(key)) if key in raw or base is None else inherited

        short_name = str(raw.get("shortName") or (base.short_name if base else ""))
        if short_name == "DEL":  # data quirk in the super class (0x97)
            short_name = ""
        if "accessRule" in raw or base is None:
            access = raw.get("accessRule") or {}
            get, set_, inf = (
                access.get(op, "notApplicable") != "notApplicable" for op in ("get", "set", "inf")
            )
        else:
            get, set_, inf = base.get, base.set, base.inf
        return PropertyDef(
            epc=epc,
            name=pick(
                "propertyName",
                lambda v: Text.from_json(v, short_name or f"0x{epc:02X}"),
                base and base.name,
            ),
            short_name=short_name,
            description=pick("descriptions", Text.from_json, base and base.description),
            schema=pick("data", lambda v: self._resolve(v or {}), base and base.schema),
            get=get,
            set=set_,
            inf=inf,
            atomic=pick("atomic", _parse_epc, base and base.atomic),
            hints={**(base.hints if base else {}), **raw.get("homeassistant", {})},
        )

    def _resolve(self, node: Any) -> Any:
        """Inline ``$ref`` targets; keys beside a ``$ref`` override the target."""
        if isinstance(node, list):
            return [self._resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            target = self._defs().get(ref.removeprefix("#/definitions/"))
            if isinstance(target, dict):
                out.update(self._resolve(target))
        for key, value in node.items():
            if key == "$ref":
                continue
            out[key] = self._resolve(value)
        if "multipleOf" in out and "multiple" not in out:
            out["multiple"] = out.pop("multipleOf")
        return out

    def _defs(self) -> dict[str, Schema]:
        if self._definitions is None:
            data = self._read(self._root / "definitions" / "definitions.json") or {}
            defs = data.get("definitions")
            self._definitions = defs if isinstance(defs, dict) else {}
        return self._definitions

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


def _parse_epc(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        epc = int(value, 16)
    except ValueError:
        return None
    return epc if 0x80 <= epc <= 0xFF else None
