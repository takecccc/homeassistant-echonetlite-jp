"""Config flow for ECHONET Lite JP."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_GROUP_NAMES,
    CONF_HOSTS,
    CONF_INTERFACE,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_WAIT,
    DOMAIN,
)
from .coordinator import EchonetConfigEntry
from .echonet.client import EchonetClient
from .echonet.errors import EchonetError
from .echonet.transport import EchonetTransport

_LOGGER = logging.getLogger(__name__)


def _form(fields: dict[Any, Any]) -> Any:
    """A form schema. Typed Any: Home Assistant 2026 swaps voluptuous for
    probatio at runtime, so the static voluptuous types do not match."""
    return vol.Schema(fields)


def _schema(defaults: dict[str, Any]) -> Any:
    return _form(
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
            except EchonetError:
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
    def __init__(self) -> None:
        self._device_id: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["connection", "group_names"])

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults = dict(self.config_entry.options)
        if user_input is not None:
            options, errors = _parse(user_input)
            if not errors:
                return self.async_create_entry(data={**self.config_entry.options, **options})
            defaults = options
        return self.async_show_form(
            step_id="connection", data_schema=_schema(defaults), errors=errors
        )

    async def async_step_group_names(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the device whose circuits (sensor groups) to name."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        coordinator = self.config_entry.runtime_data
        devices = {
            device.id: device
            for device in coordinator.network.devices.values()
            if device.sensor_groups()
        }
        if not devices:
            return self.async_abort(reason="no_groups")
        if user_input is not None or len(devices) == 1:
            self._device_id = user_input["device"] if user_input else next(iter(devices))
            return await self.async_step_group_names_edit()
        language = coordinator.language
        options = [
            selector.SelectOptionDict(
                value=device.id,
                label=f"{device.class_def.name.get(language)} ({device.host})",
            )
            for device in devices.values()
        ]
        return self.async_show_form(
            step_id="group_names",
            data_schema=_form(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_group_names_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One name per line for each kind of group; line N names number N."""
        coordinator = self.config_entry.runtime_data
        assert self._device_id is not None
        device = coordinator.network.devices[self._device_id]
        groups = device.sensor_groups()
        all_names: dict[str, dict[str, list[str]]] = self.config_entry.options.get(
            CONF_GROUP_NAMES, {}
        )
        current = all_names.get(self._device_id, {})
        errors: dict[str, str] = {}
        if user_input is not None:
            names: dict[str, list[str]] = {}
            for kind, (_label, count) in groups.items():
                lines = [line.strip() for line in str(user_input.get(kind, "")).splitlines()]
                while lines and not lines[-1]:
                    lines.pop()
                if len(lines) > count:
                    errors[kind] = "too_many_names"
                elif lines:
                    names[kind] = lines
            if not errors:
                updated = {**all_names, self._device_id: names}
                if not names:
                    updated.pop(self._device_id)
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_GROUP_NAMES: updated}
                )
        multiline = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        schema = _form(
            {
                vol.Optional(kind, default="\n".join(current.get(kind, []))): multiline
                for kind in groups
            }
        )
        language = coordinator.language
        summary = ", ".join(
            f"{label.get(language).strip()}: {count}" for label, count in groups.values()
        )
        return self.async_show_form(
            step_id="group_names_edit",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device": device.class_def.name.get(language),
                "groups": summary,
            },
        )
