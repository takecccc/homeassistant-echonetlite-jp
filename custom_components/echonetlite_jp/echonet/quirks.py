"""Device-specific corrections and additions to the MRA ("quirks").

A quirk is a JSON file that matches devices by class code and, optionally,
manufacturer code and product code (EPC 0x8C). Its ``elProperties`` use the
MRA property format, so vendor-specific EPCs are described exactly like
standard ones. See ``quirks/README.md`` for the format.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_QUIRKS_DIR = Path(__file__).resolve().parent.parent / "quirks"

# Value of Quirk.extension when the file does not mention an extension.
UNCHANGED = "unchanged"

_TOP_LEVEL_KEYS = {
    "description",
    "match",
    "className",
    "shortName",
    "maxProperties",
    "extension",
    "elProperties",
}
_MATCH_KEYS = {"eoj", "manufacturer", "productCode"}
_HA_KEYS = {"enabled", "category"}
_CATEGORIES = {None, "diagnostic", "config"}


class QuirkError(ValueError):
    """A quirk file is malformed."""


@dataclass(frozen=True)
class DeviceKey:
    """The identity quirks are matched against."""

    class_code: int
    manufacturer_code: int | None = None
    product_code: str | None = None


@dataclass(frozen=True)
class Quirk:
    source: str
    class_code: int
    manufacturer_code: int | None
    product_code: str | None
    class_name: Any = None
    short_name: str | None = None
    max_properties: int | None = None
    extension: str | None = UNCHANGED
    properties: tuple[dict[str, Any], ...] = ()

    def matches(self, key: DeviceKey) -> bool:
        if key.class_code != self.class_code:
            return False
        if self.manufacturer_code is not None and key.manufacturer_code != self.manufacturer_code:
            return False
        return self.product_code is None or key.product_code == self.product_code

    @property
    def specificity(self) -> int:
        return (self.manufacturer_code is not None) + 2 * (self.product_code is not None)


def load_quirks(directory: Path) -> list[Quirk]:
    """Load every ``*.json`` in ``directory``; malformed files are logged and skipped."""
    quirks: list[Quirk] = []
    if not directory.is_dir():
        return quirks
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            quirks.append(parse_quirk(data, path.name))
        except (OSError, ValueError) as err:
            _LOGGER.error("ignoring quirk %s: %s", path.name, err)
    return quirks


def parse_quirk(data: Any, source: str) -> Quirk:
    if not isinstance(data, dict):
        raise QuirkError("top level must be an object")
    _check_keys(data, _TOP_LEVEL_KEYS, "top level")
    match = data.get("match")
    if not isinstance(match, dict):
        raise QuirkError("'match' is required")
    _check_keys(match, _MATCH_KEYS, "match")

    eoj = _hex(match.get("eoj"), "match.eoj")
    if eoj is None or not 0 <= eoj <= 0xFFFF:
        raise QuirkError("match.eoj must be a class code such as 0x0287")
    manufacturer = _hex(match.get("manufacturer"), "match.manufacturer")
    product = match.get("productCode")
    if product is not None and (not isinstance(product, str) or not product.strip()):
        raise QuirkError("match.productCode must be a non-empty string")

    max_properties = data.get("maxProperties")
    if max_properties is not None and (
        not isinstance(max_properties, int)
        or isinstance(max_properties, bool)
        or max_properties < 1
    ):
        raise QuirkError("maxProperties must be a positive integer")

    extension = data.get("extension", UNCHANGED)
    if extension is not None and not isinstance(extension, str):
        raise QuirkError("extension must be a name or null")

    properties = data.get("elProperties", [])
    if not isinstance(properties, list):
        raise QuirkError("elProperties must be a list")
    seen: set[int] = set()
    for prop in properties:
        epc = _check_property(prop)
        if epc in seen:
            raise QuirkError(f"EPC 0x{epc:02X} is listed twice")
        seen.add(epc)

    return Quirk(
        source=source,
        class_code=eoj,
        manufacturer_code=manufacturer,
        product_code=product.strip() if product else None,
        class_name=data.get("className"),
        short_name=data.get("shortName"),
        max_properties=max_properties,
        extension=extension,
        properties=tuple(properties),
    )


def _check_property(prop: Any) -> int:
    if not isinstance(prop, dict):
        raise QuirkError("each elProperties entry must be an object")
    epc = _hex(prop.get("epc"), "epc")
    if epc is None or not 0x80 <= epc <= 0xFF:
        raise QuirkError(f"invalid epc {prop.get('epc')!r}")
    if prop.get("remove") is True:
        if set(prop) - {"epc", "remove"}:
            raise QuirkError(f"0x{epc:02X}: 'remove' cannot be combined with other keys")
        return epc
    if "data" in prop and not isinstance(prop["data"], dict):
        raise QuirkError(f"0x{epc:02X}: data must be an object")
    hints = prop.get("homeassistant", {})
    if not isinstance(hints, dict):
        raise QuirkError(f"0x{epc:02X}: homeassistant must be an object")
    _check_keys(hints, _HA_KEYS, f"0x{epc:02X}.homeassistant")
    if "enabled" in hints and not isinstance(hints["enabled"], bool):
        raise QuirkError(f"0x{epc:02X}: homeassistant.enabled must be true or false")
    if hints.get("category") not in _CATEGORIES:
        raise QuirkError(f"0x{epc:02X}: homeassistant.category must be diagnostic, config or null")
    return epc


def _check_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise QuirkError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")


def _hex(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QuirkError(f'{where} must be a hex string such as "0x0B"')
    try:
        return int(value, 16)
    except ValueError as err:
        raise QuirkError(f"{where} is not hex: {value!r}") from err
