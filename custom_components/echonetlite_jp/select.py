"""Selects for settable multi-state ECHONET Lite properties."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EchonetConfigEntry, EchonetCoordinator
from .echonet.device import EchonetDevice
from .entity import EchonetEntity, async_setup_platform
from .properties import PropertySpec, find_option

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: EchonetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_platform(entry, async_add_entities, Platform.SELECT, EchonetSelect)


class EchonetSelect(EchonetEntity, SelectEntity):
    def __init__(
        self, coordinator: EchonetCoordinator, device: EchonetDevice, spec: PropertySpec
    ) -> None:
        super().__init__(coordinator, device, spec)
        self._attr_options = list(spec.options or ())

    @property
    def current_option(self) -> str | None:
        return self.value

    async def async_select_option(self, option: str) -> None:
        item = find_option(self.spec, option, self.coordinator.language)
        if item is None or self.spec.epc is None:
            raise ServiceValidationError(f"invalid option: {option}")
        await self.coordinator.async_set(self.device, self.spec.epc, item.edt)
