from __future__ import annotations

import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HemsEchonetCoordinator

_EPC_KEY_RE = re.compile(r"^0x[0-9A-Fa-f]{2}$")
_KNOWN_ONOFF_EPCS = {"0X80"}
_ON_LIKE = {"1", "01", "30", "41", "ON", "TRUE"}
_OFF_LIKE = {"0", "00", "31", "42", "OFF", "FALSE"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HemsEchonetCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_entity_keys: set[tuple[str, str]] = set()

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

    def current_entity_keys() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for target_key, data in coordinator.data.items():
            payload = data.get("payload", {})
            set_map = _epc_keys_from_map(data.get("set_map", []))
            payload_map = payload if isinstance(payload, dict) else {}
            for epc_key in set_map:
                raw_value = payload_map.get(epc_key)
                if _looks_like_on_off(epc_key, raw_value):
                    pairs.append((target_key, epc_key))
        return sorted(set(pairs))

    def add_new_entities() -> None:
        new_pairs = [pair for pair in current_entity_keys() if pair not in known_entity_keys]
        if not new_pairs:
            return
        for pair in new_pairs:
            known_entity_keys.add(pair)
        async_add_entities(
            [HemsEchonetEpcSwitch(coordinator, target_key, epc_key) for target_key, epc_key in new_pairs]
        )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class HemsEchonetEpcSwitch(CoordinatorEntity[HemsEchonetCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsEchonetCoordinator, target_key: str, epc_key: str) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._epc_key = _normalize_epc_key(epc_key) or epc_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}-{self._epc_key}-switch"
        self._value_override: Any = None
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        data = self.coordinator.data.get(self._target_key, {})
        manufacturer = str(data.get("manufacturer") or "").strip()
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        eoj = str(data.get("eoj") or "unknown")
        base_parts = [
            p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p
        ]
        base = " ".join(base_parts) if base_parts else f"ECHONET {eoj}"
        return f"{base} {self._epc_key}"

    @property
    def available(self) -> bool:
        data = self.coordinator.data.get(self._target_key, {})
        set_map = data.get("set_map", [])
        return self._epc_key in _epc_keys_from_map(set_map)

    @property
    def is_on(self) -> bool | None:
        raw_value = self._current_raw_value()
        if raw_value is None:
            return None
        result = _to_bool(raw_value)
        return result

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        raw_value = self._current_raw_value()
        return {
            "host": data.get("host"),
            "eoj": data.get("eoj"),
            "uid": data.get("uid"),
            "epc": self._epc_key,
            "raw_value": raw_value,
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._async_set_value(True)
        except HomeAssistantError:
            on_hex, _off_hex = self._resolve_on_off_edt()
            await self._async_set(on_hex)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._async_set_value(False)
        except HomeAssistantError:
            _on_hex, off_hex = self._resolve_on_off_edt()
            await self._async_set(off_hex)

    def _current_raw_value(self) -> Any:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        if isinstance(payload, dict) and self._epc_key in payload:
            return payload[self._epc_key]
        return self._value_override

    def _resolve_on_off_edt(self) -> tuple[str, str]:
        current = self._current_raw_value()
        token = _normalize_token(current)
        if token in {"41", "42"}:
            return "41", "42"
        if token in {"01", "00"}:
            return "01", "00"
        return "30", "31"

    async def _async_set(self, edt_hex: str) -> None:
        try:
            value = await self.coordinator.client.async_set_epc(self._target_key, self._epc_key, edt_hex)
            self._value_override = value
            self._last_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_error) from exc
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def _async_set_value(self, value: Any) -> None:
        try:
            updated = await self.coordinator.client.async_set_epc_value(
                self._target_key, self._epc_key, value
            )
            self._value_override = updated
            self._last_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_error) from exc
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()


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


def _looks_like_on_off(epc_key: str, raw_value: Any) -> bool:
    if epc_key.upper() in _KNOWN_ONOFF_EPCS:
        return True
    token = _normalize_token(raw_value)
    return token in _ON_LIKE or token in _OFF_LIKE


def _to_bool(raw_value: Any) -> bool | None:
    token = _normalize_token(raw_value)
    if token in _ON_LIKE:
        return True
    if token in _OFF_LIKE:
        return False
    return None


def _normalize_token(raw_value: Any) -> str:
    if isinstance(raw_value, bool):
        return "TRUE" if raw_value else "FALSE"
    if isinstance(raw_value, int):
        return f"{raw_value:02X}"
    if raw_value is None:
        return ""
    s = str(raw_value).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s


def _normalize_epc_key(value: str) -> str | None:
    raw = value.strip()
    if not _EPC_KEY_RE.fullmatch(raw):
        return None
    try:
        epc = int(raw, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"
