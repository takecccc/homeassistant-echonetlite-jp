"""The ECHONET Lite JP integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_HOSTS,
    CONF_INTERFACE,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_WAIT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EchonetConfigEntry, EchonetCoordinator
from .echonet.client import EchonetClient
from .echonet.mra import MraCatalog
from .echonet.network import EchonetNetwork
from .echonet.transport import EchonetTransport
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EchonetConfigEntry) -> bool:
    options = entry.options
    transport = EchonetTransport(interface=options.get(CONF_INTERFACE) or None)
    try:
        await transport.open()
    except OSError as err:
        raise ConfigEntryNotReady(f"cannot listen on UDP port 3610: {err}") from err
    entry.async_on_unload(transport.close)

    network = EchonetNetwork(
        EchonetClient(transport),
        MraCatalog(),
        hosts=options.get(CONF_HOSTS) or None,
        discovery_wait=DISCOVERY_WAIT,
    )
    coordinator = EchonetCoordinator(
        hass,
        entry,
        network,
        timedelta(seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
    )
    entry.async_on_unload(transport.add_listener(coordinator.async_handle_notification))
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: EchonetConfigEntry) -> bool:
    if entry.version == 1:
        # 0.x stored everything in data; only the host and scan interval carry over.
        host = str(entry.data.get("host") or "").strip()
        options = {
            CONF_HOSTS: [host] if host else [],
            CONF_INTERFACE: "",
            CONF_SCAN_INTERVAL: int(entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        }
        hass.config_entries.async_update_entry(entry, data={}, options=options, version=2)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EchonetConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: EchonetConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
