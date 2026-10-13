"""ECHONET Machine Readable Appendix (MRA) catalog.

The MRA describes every device class as JSON. This module loads the class
files lazily, resolves ``$ref`` pointers into ``definitions.json`` and merges
the super class (0x0000) properties into each device class.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class ClassDef:
    class_code: int
    name: Text
    short_name: str
    properties: dict[int, PropertyDef]
    known: bool = True

    def get(self, epc: int) -> PropertyDef | None:
        return self.properties.get(epc)

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

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root else DEFAULT_MRA_DIR
        self._lock = threading.Lock()
        self._definitions: dict[str, Schema] | None = None
        self._super: dict[int, PropertyDef] | None = None
        self._classes: dict[int, ClassDef] = {}

    @property
    def version(self) -> str:
        try:
            meta = json.loads((self._root / "metaData.json").read_text(encoding="utf-8"))
            return str(meta["metaData"]["dataVersion"])
        except (OSError, KeyError, ValueError):
            return "unknown"

    def is_loaded(self, class_code: int) -> bool:
        return class_code in self._classes

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
            access = raw.get("accessRule") or {}
            short_name = str(raw.get("shortName") or "")
            if short_name == "DEL":  # data quirk in the super class (0x97)
                short_name = ""
            out[epc] = PropertyDef(
                epc=epc,
                name=Text.from_json(raw.get("propertyName"), short_name or f"0x{epc:02X}"),
                short_name=short_name,
                description=Text.from_json(raw.get("descriptions")),
                schema=self._resolve(raw.get("data") or {}),
                get=access.get("get", "notApplicable") != "notApplicable",
                set=access.get("set", "notApplicable") != "notApplicable",
                inf=access.get("inf", "notApplicable") != "notApplicable",
                atomic=_parse_epc(raw.get("atomic")),
            )
            if is_latest:
                latest.add(epc)
        return out

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
