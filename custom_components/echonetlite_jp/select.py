from __future__ import annotations

import re
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HemsEchonetCoordinator

_EPC_KEY_RE = re.compile(r"^0x[0-9A-Fa-f]{2}$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HemsEchonetCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_entity_keys: set[tuple[str, str]] = set()

    def current_entity_keys() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for target_key, data in coordinator.data.items():
            set_map = _epc_keys_from_map(data.get("set_map", []))
            eoj = str(data.get("eoj") or "").strip()
            if not eoj:
                continue
            for epc_key in set_map:
                meta = coordinator.client.resolve_epc_metadata_by_eoj(eoj, epc_key)
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("type") or "").strip().lower() != "state":
                    continue
                enum_map = meta.get("enum", {})
                if not isinstance(enum_map, dict) or len(enum_map) == 0:
                    continue
                pairs.append((target_key, epc_key))
        return sorted(set(pairs))

    def add_new_entities() -> None:
        new_pairs = [pair for pair in current_entity_keys() if pair not in known_entity_keys]
        if not new_pairs:
            return
        for pair in new_pairs:
            known_entity_keys.add(pair)
        async_add_entities(
            [HemsEchonetEpcSelect(coordinator, target_key, epc_key) for target_key, epc_key in new_pairs]
        )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class HemsEchonetEpcSelect(CoordinatorEntity[HemsEchonetCoordinator], SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsEchonetCoordinator, target_key: str, epc_key: str) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._epc_key = _normalize_epc_key(epc_key) or epc_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}-{self._epc_key}-select"
        self._value_override: Any = None
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        data = self.coordinator.data.get(self._target_key, {})
        manufacturer = str(data.get("manufacturer") or "").strip()
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        eoj = str(data.get("eoj") or "unknown")
        prop_name = str((self._meta() or {}).get("name") or "").strip()
        base_parts = [
            p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p
        ]
        base = " ".join(base_parts) if base_parts else f"ECHONET {eoj}"
        return f"{base} {prop_name or self._epc_key}"

    @property
    def available(self) -> bool:
        data = self.coordinator.data.get(self._target_key, {})
        return self._epc_key in _epc_keys_from_map(data.get("set_map", []))

    @property
    def options(self) -> list[str]:
        enum_map = self._enum_map()
        seen: set[str] = set()
        out: list[str] = []
        for label in enum_map.values():
            if not isinstance(label, str):
                continue
            item = label.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    @property
    def current_option(self) -> str | None:
        enum_map = self._enum_map()
        token = _normalize_hex_token(self._current_raw_value())
        if token and token in enum_map:
            label = enum_map[token]
            if isinstance(label, str) and label.strip():
                return label.strip()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        return {
            "host": data.get("host"),
            "eoj": data.get("eoj"),
            "uid": data.get("uid"),
            "epc": self._epc_key,
            "raw_value": self._current_raw_value(),
            "last_error": self._last_error,
            "errors": data.get("errors", []),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        uid = str(data.get("uid") or "unknown")
        eoj = str(data.get("eoj") or "unknown")
        device_key = f"{uid}-{eoj}"
        manufacturer = str(data.get("manufacturer") or "").strip() or "ECHONET Lite"
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        label_parts = [p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p]
        device_label = " ".join(label_parts) if label_parts else f"{manufacturer} {eoj}"
        return {
            "identifiers": {(DOMAIN, device_key)},
            "name": device_label,
            "manufacturer": manufacturer,
            "model": eoj_desc or eoj,
        }

    async def async_select_option(self, option: str) -> None:
        try:
            updated = await self.coordinator.client.async_set_epc_value(
                self._target_key, self._epc_key, option
            )
            self._value_override = updated
            self._last_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_error) from exc
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    def _current_raw_value(self) -> Any:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        if isinstance(payload, dict):
            value = payload.get(self._epc_key)
            if value is not None:
                return value
        return self._value_override

    def _meta(self) -> dict[str, Any] | None:
        data = self.coordinator.data.get(self._target_key, {})
        eoj = str(data.get("eoj") or "").strip()
        if not eoj:
            return None
        meta = self.coordinator.client.resolve_epc_metadata_by_eoj(eoj, self._epc_key)
        if isinstance(meta, dict):
            return meta
        return None

    def _enum_map(self) -> dict[str, Any]:
        meta = self._meta() or {}
        enum_map = meta.get("enum", {})
        if isinstance(enum_map, dict):
            return enum_map
        return {}


def _epc_keys_from_map(values: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    for epc_key in values:
        if isinstance(epc_key, str) and _EPC_KEY_RE.fullmatch(epc_key):
            normalized = _normalize_epc_key(epc_key)
            if normalized:
                out.append(normalized)
    return out


def _normalize_hex_token(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "01" if value else "00"
    if isinstance(value, int):
        return f"{value:02X}"
    token = str(value).strip().upper()
    if token.startswith("0X"):
        token = token[2:]
    token = token.replace(" ", "")
    if not token:
        return ""
    if len(token) % 2 != 0:
        token = f"0{token}"
    if not re.fullmatch(r"[0-9A-F]+", token):
        return ""
    return token


def _normalize_epc_key(value: str) -> str | None:
    raw = value.strip()
    if not _EPC_KEY_RE.fullmatch(raw):
        return None
    try:
        epc = int(raw, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"
