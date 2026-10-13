"""Diagnostics support for ECHONET Lite JP."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import EchonetConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EchonetConfigEntry
) -> dict[str, Any]:
    network = entry.runtime_data.network
    # Node UIDs identify the physical unit; replace them with stable aliases.
    aliases: dict[str, str] = {}
    for device in network.devices.values():
        aliases.setdefault(device.node_uid, f"node{len(aliases) + 1}")
    return {
        "options": dict(entry.options),
        "mra_version": await hass.async_add_executor_job(lambda: network.catalog.version),
        "devices": [
            {
                "node": aliases[device.node_uid],
                "host": device.host,
                "eoj": str(device.eoj),
                "class": device.class_def.name.en,
                "class_known": device.class_def.known,
                "manufacturer": device.info.manufacturer,
                "manufacturer_code": (
                    f"0x{device.info.manufacturer_code:06X}"
                    if device.info.manufacturer_code is not None
                    else None
                ),
                "product_code": device.info.product_code,
                "quirks": list(device.class_def.quirks),
                "available": device.available,
                "get_map": [f"0x{e:02X}" for e in sorted(device.info.maps.get)],
                "set_map": [f"0x{e:02X}" for e in sorted(device.info.maps.set)],
                "inf_map": [f"0x{e:02X}" for e in sorted(device.info.maps.inf)],
                "values": {f"0x{e:02X}": v.hex().upper() for e, v in sorted(device.values.items())},
                "derived": device.derived,
            }
            for device in network.devices.values()
        ],
    }
