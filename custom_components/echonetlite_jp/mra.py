from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class MRAClassResolver:
    def __init__(self, mra_dir: str = "") -> None:
        self._class_names: dict[str, str] = {}
        self._class_props: dict[str, dict[int, dict[str, Any]]] = {}
        self._definitions: dict[str, dict[str, Any]] = {}
        self._loaded = False

        path = self._resolve_mra_dir(mra_dir)
        if path is not None:
            self._load(path)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def resolve_class_name(self, eoj: str) -> str | None:
        code = self._class_code_from_eoj(eoj)
        return self._class_names.get(code)

    def resolve_property(self, eoj: str, epc: int) -> dict[str, Any] | None:
        class_code = self._class_code_from_eoj(eoj)
        if class_code in self._class_props and epc in self._class_props[class_code]:
            return self._class_props[class_code][epc]
        if "0000" in self._class_props and epc in self._class_props["0000"]:
            return self._class_props["0000"][epc]
        return None

    def _resolve_mra_dir(self, mra_dir: str) -> Path | None:
        if mra_dir.strip():
            path = Path(mra_dir).expanduser()
            if path.exists() and path.is_dir():
                return path
            return None

        # Auto-detect bundled path under integration dir.
        for dirname in ("mra_data", "mra"):
            default_path = Path(__file__).parent / dirname
            if default_path.exists() and default_path.is_dir():
                return default_path
        return None

    def _load(self, root: Path) -> None:
        self._load_definitions(root)
        for path in root.rglob("*.json"):
            class_code = self._class_code_from_path(path)
            if class_code is None:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            class_name = self._extract_class_name(data)
            if class_name and class_code not in self._class_names:
                self._class_names[class_code] = class_name
            prop_map = self._extract_prop_map(data)
            if prop_map:
                current = self._class_props.setdefault(class_code, {})
                for epc, info in prop_map.items():
                    if epc not in current:
                        current[epc] = info
        self._loaded = bool(self._class_names)

    def _load_definitions(self, root: Path) -> None:
        path = root / "definitions" / "definitions.json"
        if not path.exists() or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        definitions = data.get("definitions", {})
        if not isinstance(definitions, dict):
            return
        for key, value in definitions.items():
            if isinstance(key, str) and isinstance(value, dict):
                self._definitions[key] = value

    @staticmethod
    def _class_code_from_path(path: Path) -> str | None:
        matches = re.findall(r"0x([0-9A-Fa-f]{4})", str(path))
        if matches:
            return matches[-1].upper()
        stem_match = re.fullmatch(r"([0-9A-Fa-f]{4})", path.stem)
        if stem_match:
            return stem_match.group(1).upper()
        return None

    @staticmethod
    def _class_code_from_eoj(eoj: str) -> str:
        raw = eoj.strip().upper().removeprefix("0X")
        if len(raw) < 4:
            return ""
        return raw[:4]

    @staticmethod
    def _extract_class_name(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        class_name = data.get("className")
        if isinstance(class_name, str) and class_name.strip():
            return class_name.strip()
        if isinstance(class_name, dict):
            for key in ("ja", "JA", "en", "EN"):
                value = class_name.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _extract_prop_map(self, data: Any) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        if not isinstance(data, dict):
            return out
        el_props = data.get("elProperties")
        if not isinstance(el_props, list):
            return out
        atomic_targets: set[int] = set()
        rows: list[tuple[int, dict[str, Any]]] = []
        for prop in el_props:
            if not isinstance(prop, dict):
                continue
            epc = self._parse_epc(prop.get("epc"))
            if epc is None:
                continue
            info = self._extract_prop_info(prop)
            atomic_epc = self._parse_epc(prop.get("atomic"))
            if atomic_epc is not None:
                info["atomic"] = f"0x{atomic_epc:02X}"
                atomic_targets.add(atomic_epc)
            rows.append((epc, info))

        for epc, info in rows:
            info["is_atomic_helper"] = epc in atomic_targets
            if epc not in out:
                out[epc] = info
        return out

    def _extract_prop_info(self, prop: dict[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self._extract_name(prop.get("propertyName")),
            "description": self._extract_name(prop.get("descriptions")),
            "short_name": str(prop.get("shortName") or "").strip(),
            "access_rule": prop.get("accessRule", {}),
            "type": "",
            "format": "",
            "unit": None,
            "multiple": None,
            "minimum": None,
            "maximum": None,
            "base": None,
            "enum": {},
        }
        if not info["name"] and info["short_name"]:
            info["name"] = info["short_name"]
        refs = self._collect_refs(prop.get("data"))
        info["refs"] = refs

        schema: dict[str, Any] | None = None
        for ref_key in refs:
            candidate = self._definitions.get(ref_key)
            if not isinstance(candidate, dict):
                continue
            c_type = candidate.get("type")
            if c_type in {"number", "state", "level", "time", "date-time", "raw"}:
                schema = candidate
                break
        if schema is None:
            return info

        value_type = str(schema.get("type") or "").strip()
        if value_type:
            info["type"] = value_type
        value_format = str(schema.get("format") or "").strip()
        if value_format:
            info["format"] = value_format
        if "unit" in schema and isinstance(schema.get("unit"), str):
            info["unit"] = schema.get("unit")
        if "multiple" in schema and isinstance(schema.get("multiple"), (int, float)):
            info["multiple"] = schema.get("multiple")
        if "minimum" in schema and isinstance(schema.get("minimum"), (int, float)):
            info["minimum"] = schema.get("minimum")
        if "maximum" in schema and isinstance(schema.get("maximum"), (int, float)):
            info["maximum"] = schema.get("maximum")
        if "base" in schema and isinstance(schema.get("base"), str):
            info["base"] = schema.get("base")
        info["enum"] = self._extract_enum_map(schema)
        return info

    @staticmethod
    def _parse_epc(value: Any) -> int | None:
        if not isinstance(value, str):
            return None
        s = value.strip().lower()
        if re.fullmatch(r"0x[0-9a-f]{2}", s):
            return int(s, 16)
        return None

    @classmethod
    def _collect_refs(cls, node: Any) -> list[str]:
        out: list[str] = []
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                key = cls._ref_key(ref)
                if key:
                    out.append(key)
            for value in node.values():
                out.extend(cls._collect_refs(value))
        elif isinstance(node, list):
            for value in node:
                out.extend(cls._collect_refs(value))
        # Keep insertion order, remove duplicates.
        seen: set[str] = set()
        uniq: list[str] = []
        for item in out:
            if item in seen:
                continue
            seen.add(item)
            uniq.append(item)
        return uniq

    @staticmethod
    def _ref_key(ref: str) -> str | None:
        if not ref.startswith("#/definitions/"):
            return None
        key = ref.removeprefix("#/definitions/").strip()
        return key or None

    @staticmethod
    def _extract_name(node: Any) -> str:
        if isinstance(node, str) and node.strip():
            return node.strip()
        if isinstance(node, dict):
            for key in ("ja", "JA", "en", "EN"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @classmethod
    def _extract_enum_map(cls, schema: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        values = schema.get("enum")
        if not isinstance(values, list):
            return out
        for item in values:
            if not isinstance(item, dict):
                continue
            edt = item.get("edt")
            if not isinstance(edt, str):
                continue
            token = cls._normalize_edt_token(edt)
            if not token:
                continue
            desc = cls._extract_name(item.get("descriptions"))
            if not desc:
                name = item.get("name")
                if isinstance(name, str):
                    desc = name.strip()
            if desc:
                out[token] = desc
        return out

    @staticmethod
    def _normalize_edt_token(edt: str) -> str:
        token = edt.strip().upper()
        if token.startswith("0X"):
            token = token[2:]
        if not token:
            return ""
        if len(token) % 2 != 0:
            token = f"0{token}"
        return token
