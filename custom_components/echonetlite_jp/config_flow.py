"""Config flow for ECHONET Lite JP."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_HOSTS, CONF_INTERFACE, DEFAULT_SCAN_INTERVAL, DISCOVERY_WAIT, DOMAIN
from .coordinator import EchonetConfigEntry
from .echonet.client import EchonetClient, EchonetError
from .echonet.transport import EchonetTimeoutError, EchonetTransport

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_HOSTS, default=", ".join(defaults.get(CONF_HOSTS, []))
            ): selector.TextSelector(),
            vol.Optional(CONF_INTERFACE, default=defaults.get(CONF_INTERFACE, "")): str,
            vol.Optional(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=3600,
                    step=1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _parse(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    hosts = [h.strip() for h in str(user_input.get(CONF_HOSTS, "")).split(",") if h.strip()]
    for host in hosts:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            errors[CONF_HOSTS] = "invalid_host"
    interface = str(user_input.get(CONF_INTERFACE, "")).strip()
    if interface:
        try:
            ipaddress.IPv4Address(interface)
        except ValueError:
            errors[CONF_INTERFACE] = "invalid_interface"
    options = {
        CONF_HOSTS: hosts,
        CONF_INTERFACE: interface,
        CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
    }
    return options, errors


async def async_probe(hosts: list[str], interface: str) -> int:
    """Return how many nodes answer. Raises OSError if the port is unavailable."""
    transport = EchonetTransport(interface=interface or None)
    await transport.open()
    try:
        client = EchonetClient(transport)
        if not hosts:
            return len(await client.discover(DISCOVERY_WAIT))
        found = 0
        for host in hosts:
            try:
                await client.get_node(host)
                found += 1
            except (EchonetError, EchonetTimeoutError):
                _LOGGER.debug("no ECHONET Lite node at %s", host)
        return found
    finally:
        transport.close()


class EchonetConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            options, errors = _parse(user_input)
            defaults = options
            if not errors:
                try:
                    found = await async_probe(options[CONF_HOSTS], options[CONF_INTERFACE])
                except OSError:
                    _LOGGER.exception("cannot open UDP port 3610")
                    errors["base"] = "cannot_bind"
                else:
                    if found == 0:
                        errors["base"] = "no_devices_found"
                    else:
                        return self.async_create_entry(
                            title="ECHONET Lite", data={}, options=options
                        )
        return self.async_show_form(step_id="user", data_schema=_schema(defaults), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: EchonetConfigEntry) -> OptionsFlow:
        return EchonetOptionsFlow()


class EchonetOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults = dict(self.config_entry.options)
        if user_input is not None:
            options, errors = _parse(user_input)
            if not errors:
                return self.async_create_entry(data=options)
            defaults = options
        return self.async_show_form(step_id="init", data_schema=_schema(defaults), errors=errors)
