"""Binary sensors for read-only two-state ECHONET Lite properties."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    async_setup_platform(entry, async_add_entities, Platform.BINARY_SENSOR, EchonetBinarySensor)


class EchonetBinarySensor(EchonetEntity, BinarySensorEntity):
    def __init__(
        self, coordinator: EchonetCoordinator, device: EchonetDevice, spec: PropertySpec
    ) -> None:
        super().__init__(coordinator, device, spec)
        self._attr_device_class = (
            BinarySensorDeviceClass(spec.device_class) if spec.device_class else None
        )

    @property
    def is_on(self) -> bool | None:
        value = self.current_value
        return value if isinstance(value, bool) else None
