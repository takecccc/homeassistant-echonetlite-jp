"""Entity mapping from MRA definitions, checked against real class files."""

from __future__ import annotations

import datetime as dt

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, Platform

from custom_components.echonetlite_jp.echonet.client import ObjectInfo, PropertyMaps
from custom_components.echonetlite_jp.echonet.device import EchonetDevice
from custom_components.echonetlite_jp.echonet.mra import MraCatalog
from custom_components.echonetlite_jp.echonet.protocol import EOJ
from custom_components.echonetlite_jp.properties import (
    PropertySpec,
    device_specs,
    read_value,
)

CATALOG = MraCatalog(quirks_dir=None)


def _device(
    eoj: str, values: dict[int, bytes], settable: frozenset[int] = frozenset()
) -> EchonetDevice:
    parsed = EOJ.parse(eoj)
    maps = PropertyMaps(get=frozenset(values), set=settable)
    return EchonetDevice(
        node_uid="uid",
        info=ObjectInfo(host="192.0.2.1", eoj=parsed, maps=maps),
        class_def=CATALOG.get_class(parsed.class_code),
        values=values,
    )


def _specs(device: EchonetDevice, language: str = "en") -> dict[str, PropertySpec]:
    return {spec.key: spec for spec in device_specs(device, language)}


def test_smart_meter() -> None:
    device = _device(
        "028801",
        {
            0xD3: (1).to_bytes(4, "big"),
            0xE0: (123456).to_bytes(4, "big"),
            0xE1: b"\x01",
            0xE7: (-250).to_bytes(4, "big", signed=True),
            0xEA: bytes.fromhex("07EA0A0D0C1E00") + (7000).to_bytes(4, "big"),
        },
    )
    specs = _specs(device)

    energy = specs["e0"]
    assert (energy.device_class, energy.state_class) == (
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
    )
    assert read_value(device, energy, "en") == pytest.approx(12345.6)

    power = specs["e7"]
    assert power.device_class == SensorDeviceClass.POWER
    assert power.state_class == SensorStateClass.MEASUREMENT
    assert read_value(device, power, "en") == -250

    unit = specs["e1"]
    assert unit.entity_category == EntityCategory.DIAGNOSTIC
    assert read_value(device, unit, "en") == 0.1

    # 30-minute reading: a timestamp and a value
    timestamp = specs["ea_dateAndTime"]
    assert timestamp.device_class == SensorDeviceClass.TIMESTAMP
    value = read_value(device, timestamp, "en")
    assert isinstance(value, dt.datetime)
    assert (value.hour, value.minute) == (12, 30)
    assert value.tzinfo is not None
    reading = next(s for k, s in specs.items() if k.startswith("ea_") and k != "ea_dateAndTime")
    assert read_value(device, reading, "en") == pytest.approx(700.0)


def test_gas_and_water_meters() -> None:
    gas = _device("028201", {0xE0: (12345).to_bytes(4, "big")})
    spec = _specs(gas)["e0"]
    assert spec.device_class == SensorDeviceClass.GAS
    assert spec.state_class == SensorStateClass.TOTAL_INCREASING
    assert spec.unit == "m³"
    assert spec.precision == 3
    assert read_value(gas, spec, "en") == pytest.approx(12.345)

    water = _device("028101", {0xE0: (5).to_bytes(4, "big"), 0xE1: b"\x01"})
    spec = _specs(water)["e0"]
    assert spec.device_class == SensorDeviceClass.WATER
    assert read_value(water, spec, "en") == pytest.approx(0.5)


def test_temperature_sensor_precision_and_no_data() -> None:
    device = _device("001101", {0xE0: (215).to_bytes(2, "big")})
    spec = _specs(device)["e0"]
    assert spec.device_class == SensorDeviceClass.TEMPERATURE
    assert spec.precision == 1
    assert read_value(device, spec, "en") == pytest.approx(21.5)
    device.values[0xE0] = bytes.fromhex("7FFF")  # overflow code
    assert read_value(device, spec, "en") is None


def test_air_conditioner_controls() -> None:
    device = _device(
        "013001",
        {0x80: b"\x30", 0xA0: b"\x41", 0xB0: b"\x42", 0xB3: b"\x1a", 0xC6: b"\x01"},
        settable=frozenset({0x80, 0xA0, 0xB0, 0xB3}),
    )
    specs = _specs(device, "ja")
    assert specs["80"].platform is Platform.SWITCH
    assert specs["80"].name is None
    assert specs["b0"].platform is Platform.SELECT
    assert "冷房" in (specs["b0"].options or ())
    assert read_value(device, specs["b0"], "ja") == "冷房"
    assert specs["b3"].platform is Platform.NUMBER
    assert specs["a0"].platform is Platform.NUMBER
    assert read_value(device, specs["a0"], "ja") is None  # 0x41 = "auto", not a level
    device.values[0xA0] = b"\x33"
    assert read_value(device, specs["a0"], "ja") == 3
    # bitmaps are not mapped yet: disabled raw sensor
    assert specs["c6"].raw and not specs["c6"].enabled_default
    assert read_value(device, specs["c6"], "ja") == "01"


@pytest.mark.parametrize(
    ("edt", "ja", "en"),
    [
        (b"\x00", "未設定", "Not set"),
        (b"\xff", "不定", "Indefinite"),
        (b"\x08", "リビング", "Living room"),
        (b"\x0a", "リビング 2", "Living room 2"),
        (b"\x6c", "車庫 4", "Garage 4"),
        (b"\x01" + bytes(16), "位置情報", "Position information"),
    ],
)
def test_installation_location(edt: bytes, ja: str, en: str) -> None:
    device = _device("028701", {0x81: edt})
    spec = _specs(device)["81"]
    assert spec.entity_category == EntityCategory.DIAGNOSTIC
    assert read_value(device, spec, "ja") == ja
    assert read_value(device, spec, "en") == en


def test_unknown_epc_is_disabled_raw_sensor() -> None:
    device = _device("028701", {0xF5: b"\x00\xff"})
    spec = _specs(device)["f5"]
    assert spec.raw and not spec.enabled_default
    assert spec.object_id == "epc_f5"
    assert read_value(device, spec, "en") == "00FF"
    del device.values[0xF5]
    assert read_value(device, spec, "en") is None


def test_unknown_class_keeps_super_class_properties() -> None:
    device = _device("0FFF01", {0x80: b"\x30", 0x88: b"\x42"})
    specs = _specs(device)
    assert specs["80"].platform is Platform.BINARY_SENSOR
    assert specs["88"].device_class == "problem"
