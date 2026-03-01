from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_CIDR
from .const import CONF_DISCOVERY_WAIT
from .const import CONF_EOJ
from .const import CONF_EXCLUDE_UNKNOWN_EPCS
from .const import CONF_LISTEN_HOST
from .const import CONF_LISTEN_PORT
from .const import CONF_MAX_OPC
from .const import CONF_MRA_DIR
from .const import CONF_REDISCOVER_ON_ERROR
from .const import CONF_REFRESH_INTERVAL
from .const import CONF_TIMEOUT
from .const import DEFAULT_CIDR
from .const import DEFAULT_DISCOVERY_WAIT
from .const import DEFAULT_EOJ
from .const import DEFAULT_EXCLUDE_UNKNOWN_EPCS
from .const import DEFAULT_LISTEN_HOST
from .const import DEFAULT_LISTEN_PORT
from .const import DEFAULT_MAX_OPC
from .const import DEFAULT_MRA_DIR
from .const import DEFAULT_REDISCOVER_ON_ERROR
from .const import DEFAULT_REFRESH_INTERVAL
from .const import DEFAULT_SCAN_INTERVAL
from .const import DEFAULT_TIMEOUT
from .const import DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input.get(CONF_HOST, "")).strip()
            cidr = str(user_input.get(CONF_CIDR, "")).strip()
            if host:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    errors[CONF_HOST] = "invalid_host"
            if cidr:
                try:
                    ipaddress.ip_network(cidr, strict=False)
                except ValueError:
                    errors[CONF_CIDR] = "invalid_cidr"

            if not errors:
                return self.async_create_entry(title="ECHONET Lite JP", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(CONF_HOST, default=""): str,
                vol.Optional(CONF_EOJ, default=DEFAULT_EOJ): str,
                vol.Optional(CONF_CIDR, default=DEFAULT_CIDR): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.Coerce(int),
                vol.Optional(CONF_LISTEN_HOST, default=DEFAULT_LISTEN_HOST): str,
                vol.Optional(CONF_LISTEN_PORT, default=DEFAULT_LISTEN_PORT): vol.Coerce(int),
                vol.Optional(CONF_DISCOVERY_WAIT, default=DEFAULT_DISCOVERY_WAIT): vol.Coerce(float),
                vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.Coerce(float),
                vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL): vol.Coerce(float),
                vol.Optional(CONF_MAX_OPC, default=DEFAULT_MAX_OPC): vol.Coerce(int),
                vol.Optional(CONF_MRA_DIR, default=DEFAULT_MRA_DIR): str,
                vol.Optional(
                    CONF_REDISCOVER_ON_ERROR,
                    default=DEFAULT_REDISCOVER_ON_ERROR,
                ): bool,
                vol.Optional(
                    CONF_EXCLUDE_UNKNOWN_EPCS,
                    default=DEFAULT_EXCLUDE_UNKNOWN_EPCS,
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
