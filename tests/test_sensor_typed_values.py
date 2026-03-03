from __future__ import annotations

from types import SimpleNamespace

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
