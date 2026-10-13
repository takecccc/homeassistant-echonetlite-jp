"""Switches for settable two-state ECHONET Lite properties."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import EchonetConfigEntry
from .echonet.codec import encode
from .entity import EchonetEntity, async_setup_platform

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: EchonetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_platform(entry, async_add_entities, Platform.SWITCH, EchonetSwitch)


class EchonetSwitch(EchonetEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        return self.value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)

    async def _async_write(self, on: bool) -> None:
        assert self.spec.epc is not None
        await self.coordinator.async_set(self.device, self.spec.epc, encode(self.spec.schema, on))
