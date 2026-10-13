from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.echonetlite_jp.diagnostics import async_get_config_entry_diagnostics

from .fake_node import FakeNode
from .helpers import AIRCON, aircon, setup_integration

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "loopback_sockets")


@pytest.fixture
def fake_node() -> FakeNode:
    return FakeNode({AIRCON: aircon()})


async def test_diagnostics(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    [device] = diag["devices"]
    assert device["eoj"] == "013001"
    assert device["class"] == "Home air conditioner"
    assert device["values"]["0xBB"] == "19"
    assert "0xB3" in device["set_map"]
    assert diag["mra_version"] != "unknown"
