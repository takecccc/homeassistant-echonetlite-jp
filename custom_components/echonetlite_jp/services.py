"""Services for raw property access (debugging and unsupported properties)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import EchonetCoordinator
from .echonet.codec import to_plain
from .echonet.device import EchonetDevice
from .echonet.errors import EchonetError

SERVICE_GET_PROPERTY = "get_property"
SERVICE_SET_PROPERTY = "set_property"
ATTR_DEVICE_ID = "device_id"
ATTR_EPC = "epc"
ATTR_EDT = "edt"


def _epc(value: Any) -> int:
    try:
        epc = value if isinstance(value, int) else int(str(value), 16)
    except ValueError as err:
        raise vol.Invalid("EPC must be hex such as 0x80") from err
    if not 0x80 <= epc <= 0xFF:
        raise vol.Invalid("EPC must be between 0x80 and 0xFF")
    return epc


def _edt(value: Any) -> bytes:
    token = str(value).strip().lower().removeprefix("0x").replace(" ", "")
    try:
        edt = bytes.fromhex(token)
    except ValueError as err:
        raise vol.Invalid("EDT must be a hex string") from err
    if not edt:
        raise vol.Invalid("EDT must not be empty")
    return edt


_BASE = {vol.Required(ATTR_DEVICE_ID): cv.string, vol.Required(ATTR_EPC): _epc}
# Typed as Any: Home Assistant 2026 replaces voluptuous with probatio at runtime
# (homeassistant/__init__.py), so the static voluptuous types do not match.
GET_SCHEMA: Any = vol.Schema(_BASE)
SET_SCHEMA: Any = vol.Schema({**_BASE, vol.Required(ATTR_EDT): _edt})


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def get_property(call: ServiceCall) -> ServiceResponse:
        coordinator, device = _resolve(hass, call.data[ATTR_DEVICE_ID])
        epc: int = call.data[ATTR_EPC]
        try:
            edt = await coordinator.network.client.get_one(device.host, device.eoj, epc)
        except EchonetError as err:
            raise HomeAssistantError(f"GET 0x{epc:02X} failed: {err}") from err
        device.values[epc] = edt
        prop = device.class_def.get(epc)
        return {
            "epc": f"0x{epc:02X}",
            "name": prop.name.get(coordinator.language) if prop else None,
            "edt": edt.hex().upper(),
            "value": to_plain(device.decode(epc), coordinator.language),
        }

    async def set_property(call: ServiceCall) -> None:
        coordinator, device = _resolve(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.async_set(device, call.data[ATTR_EPC], call.data[ATTR_EDT])

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PROPERTY,
        get_property,
        schema=GET_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_SET_PROPERTY, set_property, schema=SET_SCHEMA)


def _resolve(hass: HomeAssistant, device_id: str) -> tuple[EchonetCoordinator, EchonetDevice]:
    entry_device = dr.async_get(hass).async_get(device_id)
    if entry_device is None:
        raise ServiceValidationError(f"unknown device: {device_id}")
    for domain, identifier in entry_device.identifiers:
        if domain != DOMAIN:
            continue
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator = entry.runtime_data
            if (device := coordinator.network.devices.get(identifier)) is not None:
                return coordinator, device
    raise ServiceValidationError(f"{device_id} is not a loaded ECHONET Lite device")
