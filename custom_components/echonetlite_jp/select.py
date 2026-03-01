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
from .entity_filter import EntityFilterOptions
from .entity_filter import should_register_epc

_EPC_KEY_RE = re.compile(r"^0x[0-9A-Fa-f]{2}$")
_INSTALL_LOCATION_LABELS = {
    "00": "未設定",
    "08": "リビング",
    "10": "ダイニング",
    "18": "キッチン",
    "20": "浴室",
    "28": "洗面所/脱衣所",
    "30": "トイレ",
    "38": "廊下",
    "40": "部屋",
    "48": "階段",
    "50": "玄関",
    "58": "納戸",
    "60": "庭",
    "68": "車庫",
    "70": "ベランダ",
    "78": "その他",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HemsEchonetCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_entity_keys: set[tuple[str, str]] = set()
    filter_options = EntityFilterOptions.from_entry(entry)

    def current_entity_keys() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for target_key, data in coordinator.data.items():
            set_map = _epc_keys_from_map(data.get("set_map", []))
            eoj = str(data.get("eoj") or "").strip()
            if not eoj:
                continue
            for epc_key in set_map:
                if not should_register_epc(coordinator.client, eoj, epc_key, filter_options):
                    continue
                meta = coordinator.client.resolve_epc_metadata_by_eoj(eoj, epc_key)
                if not isinstance(meta, dict):
                    continue
                enum_map = _select_enum_map(epc_key, meta)
                if not isinstance(enum_map, dict) or len(enum_map) == 0:
                    continue
                if _is_binary_onoff_enum(enum_map):
                    # ON/OFF の2値は switch プラットフォーム側で扱う。
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
        if self._epc_key == "0x81" and token:
            token = token[-2:]
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
        enum_map = self._enum_map()
        token = _token_by_option(enum_map, option)
        if not token:
            raise HomeAssistantError(f"invalid option: {option}")
        try:
            updated = await self.coordinator.client.async_set_epc(
                self._target_key, self._epc_key, token
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
        return _select_enum_map(self._epc_key, meta)


def _select_enum_map(epc_key: str, meta: dict[str, Any]) -> dict[str, Any]:
    if epc_key == "0x81":
        return dict(_INSTALL_LOCATION_LABELS)
    if str(meta.get("type") or "").strip().lower() != "state":
        return {}
    enum_map = meta.get("enum", {})
    if isinstance(enum_map, dict):
        return enum_map
    return {}


def _token_by_option(enum_map: dict[str, Any], option: str) -> str | None:
    wanted = option.strip().lower()
    for token, label in enum_map.items():
        if not isinstance(label, str):
            continue
        if label.strip().lower() == wanted:
            return token
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


def _is_binary_onoff_enum(enum_map: dict[str, Any]) -> bool:
    if len(enum_map) != 2:
        return False
    on_like = {"on", "true", "1"}
    off_like = {"off", "false", "0"}
    seen_on = False
    seen_off = False
    for label in enum_map.values():
        if not isinstance(label, str):
            return False
        norm = label.strip().lower()
        if norm in on_like:
            seen_on = True
        elif norm in off_like:
            seen_off = True
        else:
            return False
    return seen_on and seen_off


def _normalize_epc_key(value: str) -> str | None:
    raw = value.strip()
    if not _EPC_KEY_RE.fullmatch(raw):
        return None
    try:
        epc = int(raw, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"
