"""Data update coordinator for ECHONET Lite JP."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, REDISCOVERY_INTERVAL
from .echonet.client import EchonetError
from .echonet.device import EchonetDevice
from .echonet.network import EchonetNetwork
from .echonet.protocol import Frame
from .echonet.transport import EchonetTimeoutError

_LOGGER = logging.getLogger(__name__)

type EchonetConfigEntry = ConfigEntry[EchonetCoordinator]


class EchonetCoordinator(DataUpdateCoordinator[dict[str, EchonetDevice]]):
    """Polls every known object; rediscovers the network periodically."""

    config_entry: EchonetConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EchonetConfigEntry,
        network: EchonetNetwork,
        scan_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval,
        )
        self.network = network
        self._next_discovery = dt_util.utcnow()

    @property
    def language(self) -> str:
        return self.hass.config.language

    async def _async_update_data(self) -> dict[str, EchonetDevice]:
        now = dt_util.utcnow()
        if now >= self._next_discovery or self.network.rediscovery_requested:
            try:
                new = await self.network.async_discover()
            except OSError as err:
                raise UpdateFailed(f"discovery failed: {err}") from err
            for device in new:
                _LOGGER.debug(
                    "found %s at %s (%s)", device.id, device.host, device.class_def.name.en
                )
            self._next_discovery = now + REDISCOVERY_INTERVAL
        if not self.network.devices:
            # Try again on the next poll instead of waiting for the interval.
            self._next_discovery = now
            raise UpdateFailed("no ECHONET Lite devices found")
        await self.network.async_poll()
        return dict(self.network.devices)

    @callback
    def async_handle_notification(self, host: str, frame: Frame) -> None:
        """Apply property notifications (INF) pushed by devices."""
        # Devices are updated in place; notify listeners without rescheduling
        # the next poll (frequent notifications would otherwise starve it).
        if self.network.handle_notification(host, frame) is not None and self.data is not None:
            self.async_update_listeners()

    async def async_set(self, device: EchonetDevice, epc: int, edt: bytes) -> None:
        """Write a property and read it back."""
        client = self.network.client
        try:
            await client.set(device.host, device.eoj, {epc: edt})
            device.values[epc] = await client.get_one(device.host, device.eoj, epc)
        except EchonetTimeoutError as err:
            raise HomeAssistantError(f"{device.host} did not respond") from err
        except EchonetError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_set_updated_data(dict(self.network.devices))
