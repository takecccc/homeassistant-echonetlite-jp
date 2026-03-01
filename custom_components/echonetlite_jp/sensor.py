from __future__ import annotations

import json
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HemsEchonetCoordinator
from .entity_filter import EntityFilterOptions
from .entity_filter import should_register_epc

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
_KWH_UNIT_COEFFICIENT = {
    "00": 1.0,
    "01": 0.1,
    "02": 0.01,
    "03": 0.001,
    "04": 0.0001,
    "0A": 10.0,
    "0B": 100.0,
    "0C": 1000.0,
    "0D": 10000.0,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HemsEchonetCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_entity_keys: set[tuple[str, str, str]] = set()
    filter_options = EntityFilterOptions.from_entry(entry)
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("get_epc", {}, "async_get_epc")
    platform.async_register_entity_service("set_epc", {"edt": cv.string}, "async_set_epc")
    platform.async_register_entity_service("set_epc_value", {"value": cv.match_all}, "async_set_epc_value")

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

    def current_entity_keys() -> list[tuple[str, str, str]]:
        pairs: list[tuple[str, str, str]] = []
        for target_key, data in coordinator.data.items():
            eoj = str(data.get("eoj") or "").strip()
            if not eoj:
                continue
            payload = data.get("payload", {})
            get_map = data.get("get_map", [])
            set_map = data.get("set_map", [])
            if not isinstance(payload, dict):
                payload = {}
            for epc_key in payload.keys():
                if isinstance(epc_key, str) and _EPC_KEY_RE.fullmatch(epc_key):
                    normalized = _normalize_epc_key(epc_key)
                    if normalized and should_register_epc(
                        coordinator.client, eoj, normalized, filter_options
                    ):
                        meta = coordinator.client.resolve_epc_metadata_by_eoj(eoj, normalized)
                        fields = _composite_field_specs(normalized, meta)
                        if fields:
                            for field in fields:
                                pairs.append((target_key, normalized, field["key"]))
                        else:
                            pairs.append((target_key, normalized, "base"))
            for epc_key in _epc_keys_from_map(get_map):
                if should_register_epc(coordinator.client, eoj, epc_key, filter_options):
                    meta = coordinator.client.resolve_epc_metadata_by_eoj(eoj, epc_key)
                    fields = _composite_field_specs(epc_key, meta)
                    if fields:
                        for field in fields:
                            pairs.append((target_key, epc_key, field["key"]))
                    else:
                        pairs.append((target_key, epc_key, "base"))
            for epc_key in _epc_keys_from_map(set_map):
                if should_register_epc(coordinator.client, eoj, epc_key, filter_options):
                    meta = coordinator.client.resolve_epc_metadata_by_eoj(eoj, epc_key)
                    fields = _composite_field_specs(epc_key, meta)
                    if fields:
                        for field in fields:
                            pairs.append((target_key, epc_key, field["key"]))
                    else:
                        pairs.append((target_key, epc_key, "base"))
        return sorted(set(pairs))

    def add_new_entities() -> None:
        new_pairs = [pair for pair in current_entity_keys() if pair not in known_entity_keys]
        if not new_pairs:
            return
        for pair in new_pairs:
            known_entity_keys.add(pair)
        entities: list[SensorEntity] = []
        for target_key, epc_key, field_key in new_pairs:
            if field_key == "base":
                entities.append(HemsEchonetEpcSensor(coordinator, target_key, epc_key))
            else:
                entities.append(HemsEchonetCompositeFieldSensor(coordinator, target_key, epc_key, field_key))
        async_add_entities(entities)

    # Add entities discovered during initial refresh.
    add_new_entities()

    # Also add entities when periodic refresh discovers new EOJs/hosts/EPCs.
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class HemsEchonetEpcSensor(CoordinatorEntity[HemsEchonetCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsEchonetCoordinator, target_key: str, epc_key: str) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._epc_key = _normalize_epc_key(epc_key) or epc_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}-{self._epc_key}"
        self._value_override: Any = None
        self._last_get_error: str | None = None
        self._last_set_error: str | None = None

    @property
    def name(self) -> str:
        data = self.coordinator.data.get(self._target_key, {})
        manufacturer = str(data.get("manufacturer") or "").strip()
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        eoj = str(data.get("eoj") or "unknown")
        meta = self._meta()
        prop_name = str((meta or {}).get("name") or "").strip()

        base_parts = [
            p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p
        ]
        base = " ".join(base_parts) if base_parts else f"ECHONET {eoj}"
        suffix = prop_name if prop_name else self._epc_key
        return f"{base} {suffix}"

    @property
    def available(self) -> bool:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        in_payload = isinstance(payload, dict) and self._epc_key in payload
        return in_payload or self._epc_supported(data)

    @property
    def native_value(self) -> Any:
        value = self._current_raw_value()
        if value is None:
            return None
        decoded = self._decode_value(value)
        if decoded is not None:
            return decoded
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return value[:255]
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        meta = self._meta()
        if not isinstance(meta, dict):
            return None
        unit = meta.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            return None
        return _UNIT_MAP.get(unit.strip(), unit.strip())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        raw_value = None
        if isinstance(payload, dict):
            raw_value = payload.get(self._epc_key)
        get_map = self._epc_keys_from_map(data.get("get_map", []))
        set_map = self._epc_keys_from_map(data.get("set_map", []))
        gettable = self._epc_key in get_map
        settable = self._epc_key in set_map
        meta = self._meta()
        decoded = self._decode_value(raw_value)
        composite = self._decode_composite(raw_value)

        return {
            "host": data.get("host"),
            "eoj": data.get("eoj"),
            "uid": data.get("uid"),
            "epc": self._epc_key,
            "gettable": gettable,
            "settable": settable,
            "get_map": get_map,
            "set_map": set_map,
            "raw_value": raw_value,
            "decoded_value": decoded,
            "decoded_composite": composite,
            "value_override": self._value_override,
            "raw_value_json": json.dumps(raw_value, ensure_ascii=False, sort_keys=True, default=str)
            if raw_value is not None
            else None,
            "mra_name": (meta or {}).get("name") if isinstance(meta, dict) else None,
            "mra_type": (meta or {}).get("type") if isinstance(meta, dict) else None,
            "mra_unit": (meta or {}).get("unit") if isinstance(meta, dict) else None,
            "mra_multiple": (meta or {}).get("multiple") if isinstance(meta, dict) else None,
            "last_get_error": self._last_get_error,
            "last_set_error": self._last_set_error,
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

    async def async_get_epc(self) -> None:
        try:
            value = await self.coordinator.client.async_get_epc(self._target_key, self._epc_key)
            self._value_override = value
            self._last_get_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_get_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_get_error) from exc
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_set_epc(self, edt: str) -> None:
        try:
            value = await self.coordinator.client.async_set_epc(self._target_key, self._epc_key, edt)
            self._value_override = value
            self._last_set_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_set_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_set_error) from exc
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_set_epc_value(self, value: Any) -> None:
        try:
            updated = await self.coordinator.client.async_set_epc_value(
                self._target_key, self._epc_key, value
            )
            self._value_override = updated
            self._last_set_error = None
        except Exception as exc:  # noqa: BLE001
            self._last_set_error = f"{type(exc).__name__}: {exc}"
            raise HomeAssistantError(self._last_set_error) from exc
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

    def _decode_value(self, raw_value: Any) -> Any:
        if raw_value is None:
            return None
        composite = self._decode_composite(raw_value)
        if composite is not None:
            return composite.get("display")
        meta = self._meta()
        if not isinstance(meta, dict):
            return None
        value_type = str(meta.get("type") or "").strip().lower()
        if value_type == "state":
            enum_map = meta.get("enum", {})
            if isinstance(enum_map, dict):
                token = _normalize_hex_token(raw_value)
                if token and token in enum_map:
                    return enum_map[token]
            return None
        if value_type in {"number", "level"}:
            number = _decode_number(raw_value, str(meta.get("format") or ""))
            if number is None:
                return None
            multiple = meta.get("multiple")
            if isinstance(multiple, (int, float)):
                number = float(number) * float(multiple)
            if isinstance(number, float):
                number = round(number, 6)
            return number
        return None

    def _decode_composite(self, raw_value: Any) -> dict[str, Any] | None:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        payload_map = payload if isinstance(payload, dict) else {}
        meta = self._meta() or {}
        return _decode_composite_values(self._epc_key, meta, raw_value, payload_map)

    def _epc_supported(self, data: dict[str, Any]) -> bool:
        get_map = self._epc_keys_from_map(data.get("get_map", []))
        set_map = self._epc_keys_from_map(data.get("set_map", []))
        return self._epc_key in get_map or self._epc_key in set_map

    @staticmethod
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


class HemsEchonetCompositeFieldSensor(CoordinatorEntity[HemsEchonetCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HemsEchonetCoordinator, target_key: str, epc_key: str, field_key: str
    ) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._epc_key = _normalize_epc_key(epc_key) or epc_key
        self._field_key = field_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}-{self._epc_key}-{self._field_key}"

    @property
    def name(self) -> str:
        data = self.coordinator.data.get(self._target_key, {})
        manufacturer = str(data.get("manufacturer") or "").strip()
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        eoj = str(data.get("eoj") or "unknown")
        meta = self._meta() or {}
        base_parts = [
            p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p
        ]
        base = " ".join(base_parts) if base_parts else f"ECHONET {eoj}"
        field = _composite_field_info(self._epc_key, meta, self._field_key)
        field_name = str(field.get("name") or self._field_key)
        return f"{base} {field_name}"

    @property
    def native_value(self) -> float | None:
        composite = self._composite_data()
        if composite is None:
            return None
        value = composite.get(self._field_key)
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        meta = self._meta() or {}
        field = _composite_field_info(self._epc_key, meta, self._field_key)
        unit = field.get("unit")
        if not isinstance(unit, str):
            return None
        return unit

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        return {
            "host": data.get("host"),
            "eoj": data.get("eoj"),
            "uid": data.get("uid"),
            "epc": self._epc_key,
            "composite_field": self._field_key,
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

    def _meta(self) -> dict[str, Any] | None:
        data = self.coordinator.data.get(self._target_key, {})
        eoj = str(data.get("eoj") or "").strip()
        if not eoj:
            return None
        meta = self.coordinator.client.resolve_epc_metadata_by_eoj(eoj, self._epc_key)
        if isinstance(meta, dict):
            return meta
        return None

    def _composite_data(self) -> dict[str, Any] | None:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            return None
        raw_value = payload.get(self._epc_key)
        if raw_value is None:
            return None
        return _decode_composite_values(self._epc_key, self._meta() or {}, raw_value, payload)


def _composite_field_specs(epc_key: str, meta: dict[str, Any] | None) -> list[dict[str, str]]:
    short_name = str((meta or {}).get("short_name") or "").strip().lower()
    if short_name == "instantaneouscurrent" or epc_key == "0xC7":
        return [
            {"key": "r_phase_a", "name": "R Phase Current", "unit": "A"},
            {"key": "t_phase_a", "name": "T Phase Current", "unit": "A"},
        ]
    if short_name == "instantaneousvoltage" or epc_key == "0xC8":
        return [
            {"key": "r_s_v", "name": "R-S Voltage", "unit": "V"},
            {"key": "s_t_v", "name": "S-T Voltage", "unit": "V"},
        ]
    if short_name == "measurementchannel1":
        return [
            {"key": "electric_energy_kwh", "name": "Channel1 Electric Energy", "unit": "kWh"},
            {"key": "current_r_phase_a", "name": "Channel1 R Phase Current", "unit": "A"},
            {"key": "current_t_phase_a", "name": "Channel1 T Phase Current", "unit": "A"},
        ]
    return []


def _composite_field_info(epc_key: str, meta: dict[str, Any], field_key: str) -> dict[str, str]:
    for item in _composite_field_specs(epc_key, meta):
        if item["key"] == field_key:
            return item
    return {"key": field_key, "name": field_key, "unit": ""}


def _decode_composite_values(
    epc_key: str, meta: dict[str, Any], raw_value: Any, payload: dict[str, Any]
) -> dict[str, Any] | None:
    token = _normalize_hex_token(raw_value)
    if not token:
        return None
    short_name = str(meta.get("short_name") or "").strip().lower()

    if (short_name == "instantaneouscurrent" or epc_key == "0xC7") and len(token) == 8:
        raw = bytes.fromhex(token)
        r = int.from_bytes(raw[0:2], byteorder="big", signed=True)
        t = int.from_bytes(raw[2:4], byteorder="big", signed=True)
        r_value = None if r == 0x7FFE else round(r * 0.1, 1)
        t_value = None if t == 0x7FFE else round(t * 0.1, 1)
        return {
            "type": "instantaneous_current",
            "r_phase_a": r_value,
            "t_phase_a": t_value,
            "display": f"R:{_fmt_num(r_value, 'A')} T:{_fmt_num(t_value, 'A')}",
        }

    if (short_name == "instantaneousvoltage" or epc_key == "0xC8") and len(token) == 8:
        raw = bytes.fromhex(token)
        rs = int.from_bytes(raw[0:2], byteorder="big", signed=False)
        st = int.from_bytes(raw[2:4], byteorder="big", signed=False)
        rs_value = None if rs == 0xFFFE else round(rs * 0.1, 1)
        st_value = None if st == 0xFFFE else round(st * 0.1, 1)
        return {
            "type": "instantaneous_voltage",
            "r_s_v": rs_value,
            "s_t_v": st_value,
            "display": f"R-S:{_fmt_num(rs_value, 'V')} S-T:{_fmt_num(st_value, 'V')}",
        }

    if short_name == "measurementchannel1" and len(token) == 16:
        raw = bytes.fromhex(token)
        energy_raw = int.from_bytes(raw[0:4], byteorder="big", signed=False)
        current_r_raw = int.from_bytes(raw[4:6], byteorder="big", signed=True)
        current_t_raw = int.from_bytes(raw[6:8], byteorder="big", signed=True)
        energy = None if energy_raw == 0xFFFFFFFE else _apply_kwh_unit_from_payload(energy_raw, payload)
        current_r = None if current_r_raw == 0x7FFE else round(current_r_raw * 0.1, 1)
        current_t = None if current_t_raw == 0x7FFE else round(current_t_raw * 0.1, 1)
        return {
            "type": "measurement_channel_1",
            "electric_energy_kwh": energy,
            "current_r_phase_a": current_r,
            "current_t_phase_a": current_t,
            "display": (
                f"E:{_fmt_num(energy, 'kWh')} "
                f"R:{_fmt_num(current_r, 'A')} "
                f"T:{_fmt_num(current_t, 'A')}"
            ),
        }
    return None


def _apply_kwh_unit_from_payload(value: int, payload: dict[str, Any]) -> float:
    c2_raw = payload.get("0xC2")
    token = _normalize_hex_token(c2_raw)
    if not token:
        return float(value)
    coef = _KWH_UNIT_COEFFICIENT.get(token[-2:])
    if coef is None:
        return float(value)
    return round(float(value) * coef, 6)


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


def _decode_number(value: Any, fmt: str) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    token = _normalize_hex_token(value)
    if not token:
        return None
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return None

    fmt_norm = fmt.strip().lower()
    expected_len = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4}.get(
        fmt_norm
    )
    if expected_len is not None and len(raw) != expected_len:
        return None
    signed = fmt_norm.startswith("int")
    return int.from_bytes(raw, byteorder="big", signed=signed)


def _fmt_num(value: float | int | None, unit: str) -> str:
    if value is None:
        return "N/A"
    return f"{value}{unit}"


def _normalize_epc_key(value: str) -> str | None:
    raw = value.strip()
    if not _EPC_KEY_RE.fullmatch(raw):
        return None
    try:
        epc = int(raw, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"
