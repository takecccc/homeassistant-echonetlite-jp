from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HemsEchonetCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HemsEchonetCoordinator = hass.data[DOMAIN][entry.entry_id]
    target_keys = [t.key for t in coordinator.client.targets]
    if not target_keys:
        target_keys = list(coordinator.data.keys())
    entities = [HemsEchonetRawSensor(coordinator, key) for key in target_keys]
    async_add_entities(entities)


class HemsEchonetRawSensor(CoordinatorEntity[HemsEchonetCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HemsEchonetCoordinator, target_key: str) -> None:
        super().__init__(coordinator)
        self._target_key = target_key
        self._attr_unique_id = f"{DOMAIN}-{target_key}"

    @property
    def name(self) -> str:
        data = self.coordinator.data.get(self._target_key, {})
        manufacturer = str(data.get("manufacturer") or "").strip()
        device_name = str(data.get("device_name") or "").strip()
        eoj_desc = str(data.get("eoj_desc") or "").strip()
        eoj = str(data.get("eoj") or "unknown")

        parts = [p for p in (manufacturer, device_name, f"{eoj_desc} ({eoj})" if eoj_desc else eoj) if p]
        return " ".join(parts) if parts else f"ECHONET {eoj}"

    @property
    def native_value(self) -> int:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            return 0
        return len([k for k in payload.keys() if k != "_errors"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._target_key, {})
        payload = data.get("payload", {})
        return {
            "host": data.get("host"),
            "eoj": data.get("eoj"),
            "uid": data.get("uid"),
            "payload": payload,
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
