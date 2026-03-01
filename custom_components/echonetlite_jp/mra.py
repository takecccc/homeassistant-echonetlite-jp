from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class MRAClassResolver:
    def __init__(self, mra_dir: str = "") -> None:
        self._class_names: dict[str, str] = {}
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
        self._loaded = bool(self._class_names)

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
