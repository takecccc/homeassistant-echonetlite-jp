"""Sensors for readable ECHONET Lite properties."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EchonetConfigEntry, EchonetCoordinator
from .echonet.device import EchonetDevice
from .entity import EchonetEntity, async_setup_platform
from .properties import PropertySpec

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: EchonetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_platform(entry, async_add_entities, Platform.SENSOR, EchonetSensor)


class EchonetSensor(EchonetEntity, SensorEntity):
    def __init__(
        self, coordinator: EchonetCoordinator, device: EchonetDevice, spec: PropertySpec
    ) -> None:
        super().__init__(coordinator, device, spec)
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = (
            SensorDeviceClass(spec.device_class) if spec.device_class else None
        )
        self._attr_state_class = spec.state_class
        self._attr_suggested_display_precision = spec.precision
        self._attr_options = list(spec.options) if spec.options is not None else None

    @property
    def native_value(self) -> Any:
        return self.current_value
