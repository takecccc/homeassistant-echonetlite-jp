"""Numbers for settable numeric ECHONET Lite properties."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EchonetConfigEntry, EchonetCoordinator
from .echonet.codec import CodecError, encode
from .echonet.device import EchonetDevice
from .entity import EchonetEntity, async_setup_platform
from .properties import PropertySpec

PARALLEL_UPDATES = 1
_NUMBER_DEVICE_CLASSES = {c.value for c in NumberDeviceClass}


async def async_setup_entry(
    hass: HomeAssistant, entry: EchonetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_platform(entry, async_add_entities, Platform.NUMBER, EchonetNumber)


class EchonetNumber(EchonetEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: EchonetCoordinator, device: EchonetDevice, spec: PropertySpec
    ) -> None:
        super().__init__(coordinator, device, spec)
        schema = spec.schema
        self._attr_native_unit_of_measurement = spec.unit
        # Number and sensor device classes share most values (temperature, power, ...).
        device_class = spec.device_class
        self._attr_device_class = (
            NumberDeviceClass(device_class)
            if device_class is not None and device_class in _NUMBER_DEVICE_CLASSES
            else None
        )
        if schema.get("type") == "level":
            self._attr_native_min_value = 1
            self._attr_native_max_value = schema.get("maximum", 1)
            self._attr_native_step = 1
        else:
            multiple = schema.get("multiple") or 1
            self._attr_native_step = multiple
            self._attr_native_min_value = schema.get("minimum", 0) * multiple
            self._attr_native_max_value = schema.get("maximum", 0xFFFF) * multiple

    @property
    def native_value(self) -> float | None:
        value = self.current_value
        return value if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        assert self.spec.epc is not None
        if self.spec.schema.get("type") == "level" or float(value).is_integer():
            value = int(value)
        try:
            edt = encode(self.spec.schema, value)
        except CodecError as err:
            raise ServiceValidationError(str(err)) from err
        await self.coordinator.async_set(self.device, self.spec.epc, edt)
