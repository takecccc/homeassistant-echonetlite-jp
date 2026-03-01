from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_EXCLUDE_AUXILIARY_EPCS
from .const import CONF_EXCLUDE_METADATA_EPCS
from .const import CONF_EXCLUDE_RANGE_EPCS
from .const import CONF_EXCLUDE_UNKNOWN_EPCS
from .const import DEFAULT_EXCLUDE_AUXILIARY_EPCS
from .const import DEFAULT_EXCLUDE_METADATA_EPCS
from .const import DEFAULT_EXCLUDE_RANGE_EPCS
from .const import DEFAULT_EXCLUDE_UNKNOWN_EPCS

_PROPERTY_MAP_EPCS = {"0x9D", "0x9E", "0x9F"}
_METADATA_EPCS = {"0x82", "0x83", "0x8A", "0x8B", "0x8C", "0x8D", "0x8E"}
_RANGE_KEYWORDS = {
    "range",
    "query",
    "listsetting",
    "channelrange",
    "acquisitionstart",
    "startchannel",
    "履歴収集日",
    "範囲指定",
    "範囲",
    "取得開始",
}
_AUX_KEYWORDS = {
    "unit",
    "coefficient",
    "multiplier",
    "factor",
    "係数",
    "単位",
    "補正",
}


@dataclass(frozen=True)
class EntityFilterOptions:
    exclude_unknown_epcs: bool
    exclude_metadata_epcs: bool
    exclude_range_epcs: bool
    exclude_auxiliary_epcs: bool

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> "EntityFilterOptions":
        merged = dict(entry.data) | dict(entry.options)
        return cls(
            exclude_unknown_epcs=bool(
                merged.get(CONF_EXCLUDE_UNKNOWN_EPCS, DEFAULT_EXCLUDE_UNKNOWN_EPCS)
            ),
            exclude_metadata_epcs=bool(
                merged.get(CONF_EXCLUDE_METADATA_EPCS, DEFAULT_EXCLUDE_METADATA_EPCS)
            ),
            exclude_range_epcs=bool(merged.get(CONF_EXCLUDE_RANGE_EPCS, DEFAULT_EXCLUDE_RANGE_EPCS)),
            exclude_auxiliary_epcs=bool(
                merged.get(CONF_EXCLUDE_AUXILIARY_EPCS, DEFAULT_EXCLUDE_AUXILIARY_EPCS)
            ),
        )


def should_register_epc(client: Any, eoj: str, epc_key: str, options: EntityFilterOptions) -> bool:
    if epc_key in _PROPERTY_MAP_EPCS:
        return False

    meta = client.resolve_epc_metadata_by_eoj(eoj, epc_key)
    name = ""
    if isinstance(meta, dict):
        name = str(meta.get("name") or "").strip()

    if options.exclude_unknown_epcs and not name:
        return False
    if options.exclude_metadata_epcs and epc_key in _METADATA_EPCS:
        return False
    if not isinstance(meta, dict):
        return True
    if options.exclude_range_epcs and _looks_like_range_or_query(meta):
        return False
    if options.exclude_auxiliary_epcs and _looks_like_auxiliary(meta):
        return False
    return True


def _looks_like_range_or_query(meta: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(meta.get("name") or ""),
            str(meta.get("short_name") or ""),
            str(meta.get("description") or ""),
        ]
    ).replace(" ", "").lower()
    return any(keyword.lower() in haystack for keyword in _RANGE_KEYWORDS)


def _looks_like_auxiliary(meta: dict[str, Any]) -> bool:
    if bool(meta.get("is_atomic_helper", False)):
        return True
    haystack = " ".join(
        [
            str(meta.get("name") or ""),
            str(meta.get("short_name") or ""),
            str(meta.get("description") or ""),
        ]
    ).replace(" ", "").lower()
    return any(keyword.lower() in haystack for keyword in _AUX_KEYWORDS)
