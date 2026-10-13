from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.echonetlite_jp import async_migrate_entry
from custom_components.echonetlite_jp.const import DOMAIN

from .fake_node import FakeNode
from .helpers import AIRCON, PDB, aircon, aircon_device, board, setup_integration

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "loopback_sockets")


@pytest.fixture
def fake_node() -> FakeNode:
    return FakeNode({AIRCON: aircon(), PDB: board()})


async def test_entities(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("switch.home_air_conditioner").state == STATE_ON
    assert hass.states.get("binary_sensor.home_air_conditioner_fault_status").state == STATE_OFF
    assert hass.states.get("select.home_air_conditioner_operation_mode").state == "Cooling"
    number = hass.states.get("number.home_air_conditioner_target_temperature")
    assert number.state == "26"
    assert number.attributes["unit_of_measurement"] == "°C"
    assert hass.states.get("number.home_air_conditioner_air_flow_level").state == "3"
    room = hass.states.get("sensor.home_air_conditioner_room_temperature")
    assert room.state == "25"
    assert room.attributes["device_class"] == "temperature"
    assert (
        room.attributes["friendly_name"]
        == "Home air conditioner Measured value of room temperature"
    )

    energy = hass.states.get(
        "sensor.power_distribution_board_metering_normal_direction_cumulative_electric_energy"
    )
    assert energy.state == "123.4"
    assert energy.attributes["state_class"] == "total_increasing"
    assert hass.states.get("sensor.power_distribution_board_metering_ch1_energy").state == "10.0"
    assert hass.states.get("sensor.power_distribution_board_metering_ch2_power").state == "20"

    ent_reg = er.async_get(hass)
    location = ent_reg.async_get("sensor.home_air_conditioner_installation_location")
    assert location.entity_category == "diagnostic"
    assert (
        hass.states.get("sensor.home_air_conditioner_installation_location").state
        == "Living room 1"
    )
    vendor = ent_reg.async_get("sensor.home_air_conditioner_epc_f5")
    assert vendor.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    device = aircon_device(hass)
    assert device.manufacturer == "Panasonic"
    assert device.model_id == "CS-X400D"


async def test_japanese_names(hass: HomeAssistant, fake_node: FakeNode) -> None:
    await setup_integration(hass, fake_node, language="ja")
    state = hass.states.get("sensor.home_air_conditioner_room_temperature")
    assert state.attributes["friendly_name"] == "家庭用エアコン 室内温度計測値"
    assert hass.states.get("select.home_air_conditioner_operation_mode").state == "冷房"
    channel = hass.states.get("sensor.power_distribution_board_metering_ch2_energy")
    assert channel.attributes["friendly_name"] == "分電盤メータリング 計測チャンネル2 積算電力量"


async def test_controls(hass: HomeAssistant, fake_node: FakeNode) -> None:
    await setup_integration(hass, fake_node)
    values = fake_node.objects[AIRCON].values

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.home_air_conditioner"}, blocking=True
    )
    assert values[0x80] == b"\x31"
    assert hass.states.get("switch.home_air_conditioner").state == STATE_OFF

    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.home_air_conditioner_operation_mode", "option": "Heating"},
        blocking=True,
    )
    assert values[0xB0] == b"\x43"

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.home_air_conditioner_target_temperature", "value": 22},
        blocking=True,
    )
    assert values[0xB3] == b"\x16"
    assert hass.states.get("number.home_air_conditioner_target_temperature").state == "22"


async def test_unavailable_and_recovery(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    coordinator = entry.runtime_data
    fake_node.silent = True
    fake_node.requests.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.home_air_conditioner").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.power_distribution_board_metering_ch1_energy").state == (
        STATE_UNAVAILABLE
    )
    # the second object on the silent node is not polled
    assert {frame.deoj for frame in fake_node.requests} == {AIRCON}

    fake_node.silent = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.home_air_conditioner").state == STATE_ON


async def test_notification(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    fake_node.requests.clear()
    fake_node.notify(AIRCON, 0x80, b"\x31", ("127.0.0.1", fake_node.port))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if hass.states.get("switch.home_air_conditioner").state == STATE_OFF:
            break
    assert hass.states.get("switch.home_air_conditioner").state == STATE_OFF
    assert fake_node.requests == []  # applied from the INF frame, no poll needed
    assert entry.runtime_data.network.devices


async def test_services(hass: HomeAssistant, fake_node: FakeNode) -> None:
    await setup_integration(hass, fake_node)
    device = aircon_device(hass)

    response = await hass.services.async_call(
        DOMAIN,
        "get_property",
        {"device_id": device.id, "epc": "0xBB"},
        blocking=True,
        return_response=True,
    )
    assert response == {
        "epc": "0xBB",
        "name": "Measured value of room temperature",
        "edt": "19",
        "value": 25,
    }

    await hass.services.async_call(
        DOMAIN, "set_property", {"device_id": device.id, "epc": "0xB3", "edt": "18"}, blocking=True
    )
    assert fake_node.objects[AIRCON].values[0xB3] == b"\x18"


async def test_unload_releases_port(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    transport = entry.runtime_data.network.client.transport
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not transport.is_open


async def test_setup_retries_without_devices(hass: HomeAssistant, fake_node: FakeNode) -> None:
    fake_node.silent = True
    entry = await setup_integration(hass, fake_node)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_migrate_v1(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data={"host": "192.168.1.5", "scan_interval": 60}
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.data == {}
    assert entry.options == {"hosts": ["192.168.1.5"], "interface": "", "scan_interval": 60}


async def test_quirks(hass: HomeAssistant, fake_node: FakeNode, tmp_path: Path) -> None:
    quirk = {
        "match": {"eoj": "0x0130", "manufacturer": "0x00000B"},
        "elProperties": [
            {
                "epc": "0xF5",
                "propertyName": {"ja": "独自値", "en": "Vendor value"},
                "shortName": "vendorValue",
                "accessRule": {"get": "required", "set": "notApplicable", "inf": "optional"},
                "data": {"type": "number", "format": "uint16", "minimum": 0, "maximum": 65533},
            },
            {"epc": "0xBB", "homeassistant": {"enabled": False}},
            {"epc": "0xB0", "remove": True},
        ],
    }
    (tmp_path / "aircon.json").write_text(json.dumps(quirk), encoding="utf-8")
    await setup_integration(hass, fake_node, quirks_dir=tmp_path)

    assert hass.states.get("sensor.home_air_conditioner_vendor_value").state == "258"
    ent_reg = er.async_get(hass)
    room = ent_reg.async_get("sensor.home_air_conditioner_room_temperature")
    assert room.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert ent_reg.async_get("select.home_air_conditioner_operation_mode") is None


async def test_requested_rediscovery_is_rate_limited(
    hass: HomeAssistant, fake_node: FakeNode
) -> None:
    entry = await setup_integration(hass, fake_node)
    coordinator = entry.runtime_data
    network = coordinator.network
    with patch.object(network, "async_discover", wraps=network.async_discover) as discover:
        network.rediscovery_requested = True
        await coordinator.async_refresh()
        assert discover.call_count == 0  # just discovered during setup

        coordinator._last_discovery -= timedelta(minutes=6)
        await coordinator.async_refresh()
        assert discover.call_count == 1
        assert not network.rediscovery_requested


async def test_clock_properties_are_disabled_by_default(
    hass: HomeAssistant, fake_node: FakeNode
) -> None:
    fake_node.objects[AIRCON].values[0x97] = bytes([12, 30])
    await setup_integration(hass, fake_node)
    clock = er.async_get(hass).async_get("sensor.home_air_conditioner_current_time_setting")
    assert clock.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_unload_during_poll(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    coordinator = entry.runtime_data
    fake_node.delay = 0.1
    refresh = hass.async_create_task(coordinator.async_refresh())
    await asyncio.sleep(0.02)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await refresh  # ends as a normal failed update, not an unexpected exception
    assert not coordinator.last_update_success
    await asyncio.sleep(0.15)  # let FakeNode's delayed reply fire


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"epc": "0x10"}, "between 0x80 and 0xFF"),
        ({"epc": "zz"}, "EPC must be hex"),
    ],
)
async def test_service_validation(
    hass: HomeAssistant, fake_node: FakeNode, data: dict, error: str
) -> None:
    await setup_integration(hass, fake_node)
    device = aircon_device(hass)
    with pytest.raises(vol.Invalid, match=error):
        await hass.services.async_call(
            DOMAIN,
            "get_property",
            {"device_id": device.id, **data},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(vol.Invalid, match="EDT must be a hex string"):
        await hass.services.async_call(
            DOMAIN,
            "set_property",
            {"device_id": device.id, "epc": "0x80", "edt": "xyz"},
            blocking=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_property",
            {"device_id": "missing", "epc": "0x80"},
            blocking=True,
            return_response=True,
        )


async def test_service_reports_rejected_set(hass: HomeAssistant, fake_node: FakeNode) -> None:
    await setup_integration(hass, fake_node)
    device = aircon_device(hass)
    with pytest.raises(HomeAssistantError, match="rejected"):
        await hass.services.async_call(
            DOMAIN,
            "set_property",
            {"device_id": device.id, "epc": "0xBB", "edt": "10"},
            blocking=True,
        )


async def test_channel_sensors_are_created_before_values_arrive(
    hass: HomeAssistant, fake_node: FakeNode
) -> None:
    fake_node.ignored_epcs = {0xB3, 0xB7}
    entry = await setup_integration(hass, fake_node)
    state = hass.states.get("sensor.power_distribution_board_metering_ch2_energy")
    assert state is not None and state.state == "unknown"

    fake_node.ignored_epcs = set()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.power_distribution_board_metering_ch2_energy").state == "20.0"


async def test_early_rediscovery_after_startup(hass: HomeAssistant, fake_node: FakeNode) -> None:
    entry = await setup_integration(hass, fake_node)
    coordinator = entry.runtime_data
    assert coordinator._last_discovery is not None
    assert coordinator._next_discovery - coordinator._last_discovery == timedelta(minutes=2)

    coordinator._next_discovery = coordinator._last_discovery  # due now
    await coordinator.async_refresh()
    assert coordinator._next_discovery - coordinator._last_discovery == timedelta(minutes=30)
