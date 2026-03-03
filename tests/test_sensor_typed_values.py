from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorStateClass

from custom_components.echonetlite_jp.sensor import HemsEchonetEpcSensor


class _FakeClient:
    def __init__(self, meta):
        self._meta = meta

    def resolve_epc_metadata_by_eoj(self, eoj: str, epc_key: str):
        return self._meta


class _FakeCoordinator:
    def __init__(self, data, meta):
        self.data = data
        self.client = _FakeClient(meta)
        self.last_update_success = True

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


def test_typed_sensor_does_not_fallback_to_raw_string_for_invalid_value() -> None:
    # Reproduces the "float('7E')" path: temperature-like sensor with raw hex string.
    coordinator = _FakeCoordinator(
        data={
            "t": {
                "eoj": "027B01",
                "payload": {"0xE2": "7E"},
                "get_map": ["0xE2"],
                "set_map": [],
            }
        },
        meta={
            "name": "室内温度計測値",
            "type": "",
            "unit": "Celsius",
            "format": "int8",
            "no_data_codes": [126],
        },
    )

    sensor = HemsEchonetEpcSensor(coordinator, "t", "0xE2")
    assert sensor.native_value is None


def test_c0_applies_c2_coefficient_and_remains_available() -> None:
    coordinator = _FakeCoordinator(
        data={
            "t": {
                "eoj": "028701",
                # raw=0x01000000 (16,777,216), C2=0x0C (x1000)
                # expected=16,777,216,000
                "payload": {"0xC0": "01000000", "0xC2": "0C"},
                "get_map": ["0xC0", "0xC2"],
                "set_map": [],
            }
        },
        meta={
            "name": "積算電力量計測値 (正方向)",
            "type": "number",
            "unit": "kWh",
            "format": "uint32",
            "minimum": 0,
            "maximum": 99999999,
            "multiple": 1,
            "coefficient": ["0xC2"],
            "no_data_codes": [0xFFFFFFFE],
        },
    )

    sensor = HemsEchonetEpcSensor(coordinator, "t", "0xC0")
    assert sensor.available is True
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    assert sensor.native_value == 16777216000.0


def test_c1_applies_c2_fractional_coefficient() -> None:
    coordinator = _FakeCoordinator(
        data={
            "t": {
                "eoj": "028701",
                # raw=100, C2=0x01 (x0.1) -> 10.0
                "payload": {"0xC1": "00000064", "0xC2": "01"},
                "get_map": ["0xC1", "0xC2"],
                "set_map": [],
            }
        },
        meta={
            "name": "積算電力量計測値 (逆方向)",
            "type": "number",
            "unit": "kWh",
            "format": "uint32",
            "minimum": 0,
            "maximum": 99999999,
            "multiple": 1,
            "coefficient": ["0xC2"],
            "no_data_codes": [0xFFFFFFFE],
        },
    )

    sensor = HemsEchonetEpcSensor(coordinator, "t", "0xC1")
    assert sensor.available is True
    assert sensor.device_class == SensorDeviceClass.ENERGY
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    assert sensor.native_value == 10.0


def test_c0_applies_c2_when_code_is_decimal_string() -> None:
    coordinator = _FakeCoordinator(
        data={
            "t": {
                "eoj": "028701",
                # C2 decimal string "10" should be interpreted as enum code 0x0A => x10
                "payload": {"0xC0": "00000064", "0xC2": "10"},
                "get_map": ["0xC0", "0xC2"],
                "set_map": [],
            }
        },
        meta={
            "name": "積算電力量計測値 (正方向)",
            "type": "number",
            "unit": "kWh",
            "format": "uint32",
            "minimum": 0,
            "maximum": 99999999,
            "multiple": 1,
            "coefficient": ["0xC2"],
            "no_data_codes": [0xFFFFFFFE],
        },
    )

    sensor = HemsEchonetEpcSensor(coordinator, "t", "0xC0")
    assert sensor.native_value == 1000.0
