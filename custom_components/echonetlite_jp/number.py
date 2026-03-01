from __future__ import annotations

import re
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HemsEchonetCoordinator

_EPC_KEY_RE = re.compile(r"^0x[0-9A-Fa-f]{2}$")
_UNIT_MAP = {
    "Celsius": "°C",
    "celsius": "°C",
    "degreeCelsius": "°C",
    "minutes": "min",
    "minute": "min",
    "hour": "h",
    "hours": "h",
    "second": "s",
    "seconds": "s",
}


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
            for epc_key in set_map:
                meta = coordinator.client.resolve_epc_metadata(target_key, epc_key)
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("type") or "").strip().lower() != "number":
                    continue
                if not str(meta.get("format") or "").strip():
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
            [HemsEchonetEpcNumber(coordinator, target_key, epc_key) for target_key, epc_key in new_pairs]
        )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class HemsEchonetEpcNumber(CoordinatorEntity[HemsEchonetCoordinator], NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsEchonetCoordinator, target_key: str, epc_key: str) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._epc_key = _normalize_epc_key(epc_key) or epc_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}-{self._epc_key}-number"
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
    def native_value(self) -> float | None:
        decoded = _decode_numeric(self._current_raw_value(), self._meta() or {})
        if decoded is None:
            return None
        return float(decoded)

    @property
    def native_min_value(self) -> float:
        meta = self._meta() or {}
        minimum = meta.get("minimum")
        multiple = meta.get("multiple")
        if not isinstance(minimum, (int, float)):
            return 0.0
        if isinstance(multiple, (int, float)):
            return float(minimum) * float(multiple)
        return float(minimum)

    @property
    def native_max_value(self) -> float:
        meta = self._meta() or {}
        maximum = meta.get("maximum")
        multiple = meta.get("multiple")
        if not isinstance(maximum, (int, float)):
            return 100.0
        if isinstance(multiple, (int, float)):
            return float(maximum) * float(multiple)
        return float(maximum)

    @property
    def native_step(self) -> float:
        meta = self._meta() or {}
        multiple = meta.get("multiple")
        if isinstance(multiple, (int, float)) and multiple > 0:
            return float(multiple)
        return 1.0

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = (self._meta() or {}).get("unit")
        if not isinstance(unit, str) or not unit.strip():
            return None
        return _UNIT_MAP.get(unit.strip(), unit.strip())

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

    async def async_set_native_value(self, value: float) -> None:
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

    def _current_raw_value(self) -> Any:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        if isinstance(payload, dict):
            value = payload.get(self._epc_key)
            if value is not None:
                return value
        return self._value_override

    def _meta(self) -> dict[str, Any] | None:
        meta = self.coordinator.client.resolve_epc_metadata(self._target_key, self._epc_key)
        if isinstance(meta, dict):
            return meta
        return None


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


def _decode_numeric(raw_value: Any, meta: dict[str, Any]) -> float | None:
    fmt = str(meta.get("format") or "").strip().lower()
    if fmt not in {"uint8", "int8", "uint16", "int16", "uint32", "int32"}:
        return None
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        number = float(raw_value)
    else:
        token = _normalize_hex_token(raw_value)
        if not token:
            return None
        try:
            raw = bytes.fromhex(token)
        except ValueError:
            return None
        expected_len = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4}[fmt]
        if len(raw) != expected_len:
            return None
        signed = fmt.startswith("int")
        number = float(int.from_bytes(raw, byteorder="big", signed=signed))

    multiple = meta.get("multiple")
    if isinstance(multiple, (int, float)):
        number *= float(multiple)
    return number


def _normalize_epc_key(value: str) -> str | None:
    raw = value.strip()
    if not _EPC_KEY_RE.fullmatch(raw):
        return None
    try:
        epc = int(raw, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"
