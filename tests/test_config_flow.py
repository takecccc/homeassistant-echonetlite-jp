from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.echonetlite_jp.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

PROBE = "custom_components.echonetlite_jp.config_flow.async_probe"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.echonetlite_jp.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


async def test_create_entry_with_discovery(hass: HomeAssistant) -> None:
    flow_id = await _start(hass)
    with patch(PROBE, AsyncMock(return_value=2)) as probe:
        result = await hass.config_entries.flow.async_configure(flow_id, {"scan_interval": 60})
    probe.assert_awaited_once_with([], "")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert result["options"] == {"hosts": [], "interface": "", "scan_interval": 60}


async def test_create_entry_with_hosts(hass: HomeAssistant) -> None:
    flow_id = await _start(hass)
    with patch(PROBE, AsyncMock(return_value=1)) as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"hosts": "192.168.1.10, 192.168.1.11", "interface": "192.168.1.2"}
        )
    probe.assert_awaited_once_with(["192.168.1.10", "192.168.1.11"], "192.168.1.2")
    assert result["options"]["hosts"] == ["192.168.1.10", "192.168.1.11"]


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({"hosts": "192.168.1.300"}, {"hosts": "invalid_host"}),
        ({"interface": "eth0"}, {"interface": "invalid_interface"}),
    ],
)
async def test_invalid_input(hass: HomeAssistant, user_input: dict, errors: dict) -> None:
    flow_id = await _start(hass)
    with patch(PROBE, AsyncMock(return_value=1)) as probe:
        result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    probe.assert_not_awaited()
    assert result["errors"] == errors


@pytest.mark.parametrize(
    ("probe", "error"),
    [
        (AsyncMock(return_value=0), "no_devices_found"),
        (AsyncMock(side_effect=OSError), "cannot_bind"),
    ],
)
async def test_probe_errors(hass: HomeAssistant, probe: AsyncMock, error: str) -> None:
    flow_id = await _start(hass)
    with patch(PROBE, probe):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    with patch(PROBE, AsyncMock(return_value=1)):
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, version=2).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, options={"hosts": [], "interface": "", "scan_interval": 30}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"hosts": "bad", "interface": "", "scan_interval": 30}
    )
    assert result["errors"] == {"hosts": "invalid_host"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"hosts": "10.0.0.5", "interface": "", "scan_interval": 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"hosts": ["10.0.0.5"], "interface": "", "scan_interval": 15}


async def test_probe_against_a_node(loopback_sockets: None) -> None:
    """async_probe itself, against FakeNode (unicast and 'multicast')."""
    from functools import partial

    from custom_components.echonetlite_jp.config_flow import async_probe
    from custom_components.echonetlite_jp.echonet.client import EchonetClient
    from custom_components.echonetlite_jp.echonet.protocol import EOJ
    from custom_components.echonetlite_jp.echonet.transport import EchonetTransport

    from .fake_node import FAKE_HOST, FakeNode, FakeObject

    node = FakeNode({EOJ(1, 0x30, 1): FakeObject({0x80: b"\x30"})})
    port = await node.start()
    transport = partial(EchonetTransport, bind_host="127.0.0.1", port=port, multicast=False)
    client = partial(EchonetClient, timeout=0.2, retries=0)
    try:
        with (
            patch("custom_components.echonetlite_jp.config_flow.EchonetTransport", transport),
            patch("custom_components.echonetlite_jp.config_flow.EchonetClient", client),
            patch("custom_components.echonetlite_jp.config_flow.DISCOVERY_WAIT", 0.2),
            patch(
                "custom_components.echonetlite_jp.echonet.transport.MULTICAST_ADDRESS", FAKE_HOST
            ),
        ):
            assert await async_probe([FAKE_HOST], "") == 1
            assert await async_probe([], "") == 1
            node.silent = True
            assert await async_probe([FAKE_HOST], "") == 0
    finally:
        node.stop()
