from __future__ import annotations

import asyncio
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
        self._root: Path | None = self._resolve_mra_dir(mra_dir)

        # Avoid blocking file I/O in HA event loop. In that case, caller should
        # invoke ensure_loaded() from an executor/to_thread context.
        if self._root is not None and not self._is_in_running_loop():
            self._load(self._root)

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

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._root is None:
            return
        self._load(self._root)

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
    def _is_in_running_loop() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

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
            # Prefer later definitions in a class file (usually newer release ranges).
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
            "coefficient": [],
            "enum": {},
            "no_data_codes": [],
            "object_fields": [],
        }
        if not info["name"] and info["short_name"]:
            info["name"] = info["short_name"]
        refs = self._collect_refs(prop.get("data"))
        info["refs"] = refs
        info["object_fields"] = self._extract_object_fields(prop.get("data"))

        data_node = prop.get("data")
        number_schema = self._pick_schema_by_type(data_node, {"number", "level"})
        state_schema = self._pick_schema_by_type(data_node, {"state"})
        temporal_schema = self._pick_schema_by_type(data_node, {"time", "date", "date-time"})
        schema: dict[str, Any] | None = number_schema or state_schema or temporal_schema
        if schema is None:
            for ref_key in refs:
                candidate = self._definitions.get(ref_key)
                if not isinstance(candidate, dict):
                    continue
                c_type = candidate.get("type")
                if c_type in {"number", "state", "level", "time", "date", "date-time", "raw"}:
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
        if info["multiple"] is None and isinstance(schema.get("multipleOf"), (int, float)):
            info["multiple"] = schema.get("multipleOf")
        coefficient = schema.get("coefficient")
        if isinstance(coefficient, list):
            info["coefficient"] = [str(x) for x in coefficient if isinstance(x, str)]
        if "minimum" in schema and isinstance(schema.get("minimum"), (int, float)):
            info["minimum"] = schema.get("minimum")
        if "maximum" in schema and isinstance(schema.get("maximum"), (int, float)):
            info["maximum"] = schema.get("maximum")
        if "base" in schema and isinstance(schema.get("base"), str):
            info["base"] = schema.get("base")
        info["enum"] = self._extract_enum_map(schema)
        info["no_data_codes"] = self._extract_no_data_codes(state_schema)
        return info

    def _extract_object_fields(self, data_node: Any) -> list[dict[str, Any]]:
        schema = self._resolve_schema(data_node)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return []
        properties = schema.get("properties")
        if not isinstance(properties, list):
            return []
        out: list[dict[str, Any]] = []
        for item in properties:
            if not isinstance(item, dict):
                continue
            short_name = str(item.get("shortName") or "").strip()
            if not short_name:
                continue
            element_name = self._extract_name(item.get("elementName"))
            element_node = item.get("element")
            number_schema = self._pick_schema_by_type(element_node, {"number", "level"})
            state_schema = self._pick_schema_by_type(element_node, {"state"})
            temporal_schema = self._pick_schema_by_type(element_node, {"time", "date", "date-time"})
            selected = number_schema or state_schema or temporal_schema
            if not isinstance(selected, dict):
                continue

            value_type = str(selected.get("type") or "").strip().lower()
            if value_type not in {"number", "level", "state", "time", "date", "date-time"}:
                continue
            fmt = str(selected.get("format") or "").strip()
            size = self._schema_size_bytes(selected)
            if size is None:
                continue
            unit = selected.get("unit")
            multiple = selected.get("multiple")
            if multiple is None:
                multiple = selected.get("multipleOf")
            coefficient = selected.get("coefficient")
            if not isinstance(coefficient, list):
                coefficient = []
            no_data_codes = self._extract_no_data_codes(state_schema)

            out.append(
                {
                    "key": short_name,
                    "name": element_name or short_name,
                    "type": value_type,
                    "format": fmt,
                    "size": size,
                    "unit": unit if isinstance(unit, str) else None,
                    "multiple": multiple if isinstance(multiple, (int, float)) else None,
                    "coefficient": [str(x) for x in coefficient if isinstance(x, str)],
                    "enum": self._extract_enum_map(state_schema) if isinstance(state_schema, dict) else {},
                    "no_data_codes": no_data_codes,
                }
            )
        return out

    def _resolve_schema(self, node: Any) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            return None
        ref = node.get("$ref")
        if isinstance(ref, str):
            key = self._ref_key(ref)
            if key and key in self._definitions:
                resolved = self._definitions[key]
                if isinstance(resolved, dict):
                    # Keep attributes declared beside $ref
                    # (e.g. coefficient/multiple overrides in oneOf items).
                    merged = dict(resolved)
                    for k, v in node.items():
                        if k == "$ref":
                            continue
                        if isinstance(merged.get(k), dict) and isinstance(v, dict):
                            merged[k] = dict(merged[k]) | v
                        else:
                            merged[k] = v
                    return merged
        if "oneOf" in node and isinstance(node["oneOf"], list):
            for candidate in node["oneOf"]:
                resolved = self._resolve_schema(candidate)
                if isinstance(resolved, dict):
                    return resolved
        return node

    def _pick_schema_by_type(self, node: Any, wanted: set[str]) -> dict[str, Any] | None:
        if isinstance(node, list):
            for item in node:
                found = self._pick_schema_by_type(item, wanted)
                if isinstance(found, dict):
                    return found
            return None
        if not isinstance(node, dict):
            return None

        one_of = node.get("oneOf")
        if isinstance(one_of, list):
            for candidate in one_of:
                found = self._pick_schema_by_type(candidate, wanted)
                if isinstance(found, dict):
                    return found

        resolved = self._resolve_schema(node)
        if not isinstance(resolved, dict):
            return None
        if str(resolved.get("type") or "").strip().lower() in wanted:
            return resolved
        return None

    @staticmethod
    def _schema_size_bytes(schema: dict[str, Any]) -> int | None:
        fmt = str(schema.get("format") or "").strip().lower()
        if fmt in {"uint8", "int8"}:
            return 1
        if fmt in {"uint16", "int16"}:
            return 2
        if fmt in {"uint32", "int32"}:
            return 4
        size = schema.get("size")
        if isinstance(size, int) and size > 0:
            return size
        min_size = schema.get("minSize")
        max_size = schema.get("maxSize")
        if isinstance(min_size, int) and isinstance(max_size, int) and min_size == max_size and min_size > 0:
            return min_size
        return None

    @staticmethod
    def _extract_no_data_codes(schema: dict[str, Any] | None) -> list[int]:
        if not isinstance(schema, dict):
            return []
        values = schema.get("enum")
        if not isinstance(values, list):
            return []
        out: list[int] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            edt = item.get("edt")
            if not isinstance(edt, str):
                continue
            token = edt.strip().lower()
            if token.startswith("0x"):
                token = token[2:]
            if not token:
                continue
            try:
                out.append(int(token, 16))
            except ValueError:
                continue
        return sorted(set(out))

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
