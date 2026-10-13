from __future__ import annotations

import datetime as dt

import pytest

from custom_components.echonetlite_jp.echonet.codec import (
    CodecError,
    EnumValue,
    decode,
    decode_property,
    encode,
    enum_values,
    fixed_size,
    is_binary_state,
    primary,
)
from custom_components.echonetlite_jp.echonet.mra import MraCatalog

CATALOG = MraCatalog()
PDB = CATALOG.get_class(0x0287)
AIRCON = CATALOG.get_class(0x0130)
SMART_METER = CATALOG.get_class(0x0288)


def test_catalog_metadata() -> None:
    assert PDB.name.ja == "分電盤メータリング"
    assert PDB.name.get("en") == "Power distribution board metering"
    # super class properties are merged in
    assert PDB.get(0x80) is not None
    assert {0xB2, 0xB3, 0xC3, 0xC5} <= PDB.atomic_epcs
    assert CATALOG.version != "unknown"


def test_unknown_class_falls_back_to_super_class() -> None:
    unknown = CATALOG.get_class(0x0FFF)
    assert not unknown.known
    assert unknown.get(0x80) is not None


def test_operation_status_is_binary() -> None:
    schema = primary(PDB.get(0x80).schema)
    assert is_binary_state(schema)
    on = decode(schema, b"\x30")
    assert isinstance(on, EnumValue) and on.is_on is True
    assert decode(schema, b"\x31").is_on is False
    assert encode(schema, True) == b"\x30"
    assert encode(schema, "false") == b"\x31"


def test_number_with_unit_coefficient() -> None:
    # 0xC0 cumulative energy x 0xC2 unit (0x01 -> 0.1 kWh)
    values = {0xC0: (100).to_bytes(4, "big"), 0xC2: b"\x01"}
    assert decode_property(PDB, 0xC0, values) == 10.0
    values[0xC2] = b"\x0c"  # x1000
    assert decode_property(PDB, 0xC0, values) == 100000.0
    del values[0xC2]
    assert decode_property(PDB, 0xC0, values) == 100


def test_number_no_data_code_decodes_as_state() -> None:
    value = decode_property(PDB, 0xC0, {0xC0: bytes.fromhex("FFFFFFFE")})
    assert isinstance(value, EnumValue) and value.name == "noData"


def test_signed_number_with_multiple() -> None:
    # 0xC7 instantaneous current: R/T phase int16 x 0.1 A
    value = decode_property(PDB, 0xC7, {0xC7: bytes.fromhex("0064FF9C")})
    assert value == {"rPhase": 10.0, "tPhase": -10.0}


def test_object_field_with_invalid_value_becomes_none() -> None:
    value = decode_property(PDB, 0xC7, {0xC7: bytes.fromhex("7FFE0001")})
    assert isinstance(value["rPhase"], EnumValue)
    assert value["tPhase"] == pytest.approx(0.1)


def test_temperature_out_of_range_is_special_state() -> None:
    # 0xBB room temperature: 0x7E = "overflow"/unmeasurable code
    value = decode_property(AIRCON, 0xBB, {0xBB: b"\x7e"})
    assert isinstance(value, EnumValue)
    assert decode_property(AIRCON, 0xBB, {0xBB: b"\x19"}) == 25


def test_temporal_values() -> None:
    assert decode_property(PDB, 0x97, {0x97: bytes([13, 5])}) == dt.time(13, 5)
    assert decode_property(PDB, 0x98, {0x98: bytes.fromhex("07EA0A0D")}) == dt.date(2026, 10, 13)
    with pytest.raises(CodecError):
        decode_property(PDB, 0x98, {0x98: bytes.fromhex("07EA0D0D")})


def test_smart_meter_multiple_coefficients() -> None:
    # 0xE0 x 0xD3 (coefficient) x 0xE1 (unit; 0x02 -> 0.01 kWh)
    values = {
        0xE0: (12345).to_bytes(4, "big"),
        0xD3: (10).to_bytes(4, "big"),
        0xE1: b"\x02",
    }
    assert decode_property(SMART_METER, 0xE0, values) == pytest.approx(1234.5)


def test_list_property_decodes_items() -> None:
    edt = bytes([1, 2]) + (5).to_bytes(4, "big") + bytes.fromhex("FFFFFFFE")
    value = decode_property(PDB, 0xB3, {0xB3: edt, 0xC2: b"\x00"})
    assert value["startChannel"] == 1
    assert value["range"] == 2
    assert value["electricEnergy"][0] == 5
    assert value["electricEnergy"][1].name == "noData"


def test_encode_number_scales_and_checks_range() -> None:
    schema = primary(AIRCON.get(0xB3).schema)  # temperature setting 0..50 C
    assert encode(schema, 26) == b"\x1a"
    with pytest.raises(CodecError):
        encode(schema, 99)
    with pytest.raises(CodecError):
        encode(schema, 25.5)


def test_encode_state_by_label() -> None:
    schema = primary(AIRCON.get(0xB0).schema)  # operation mode
    cooling = next(e for e in enum_values(schema) if e.name == "cooling")
    assert encode(schema, cooling.label.ja) == cooling.edt
    assert encode(schema, "cooling") == cooling.edt
    with pytest.raises(CodecError):
        encode(schema, "no such mode")


def test_level() -> None:
    schema = primary(AIRCON.get(0xA0).schema)
    assert schema["type"] == "level"
    assert decode(schema, b"\x31") == 1
    assert decode(schema, b"\x38") == 8
    with pytest.raises(CodecError):
        decode(schema, b"\x39")
    assert encode(schema, 8) == b"\x38"
    with pytest.raises(CodecError):
        encode(schema, 9)


def test_bitmap() -> None:
    schema = primary(AIRCON.get(0xC6).schema)
    value = decode(schema, b"\x01")
    first = next(iter(value.values()))
    assert isinstance(first, EnumValue)
    with pytest.raises(CodecError):
        decode(schema, b"\x01\x00")


def test_date_time_and_raw() -> None:
    assert decode({"type": "date-time"}, bytes.fromhex("07EA0A0D0C1E05")) == dt.datetime(
        2026, 10, 13, 12, 30, 5
    )
    assert decode({"type": "date-time", "size": 6}, bytes.fromhex("07EA0A0D0C1E")) == dt.datetime(
        2026, 10, 13, 12, 30
    )
    with pytest.raises(CodecError):
        decode({"type": "date-time"}, bytes.fromhex("07EA0A0D0C"))
    raw = {"type": "raw", "minSize": 1, "maxSize": 2}
    assert decode(raw, b"\x01") == b"\x01"
    with pytest.raises(CodecError):
        decode(raw, b"\x01\x02\x03")
    assert encode(raw, "0x0A0B") == b"\x0a\x0b"
    with pytest.raises(CodecError):
        encode(raw, "zz")


def test_fixed_size() -> None:
    assert fixed_size(PDB.get(0xC7).schema) == 4
    assert fixed_size({"type": "date"}) == 4
    assert fixed_size({"type": "time", "size": 2}) == 2
    assert fixed_size({"type": "raw", "minSize": 1, "maxSize": 17}) is None
    assert fixed_size(PDB.get(0xB3).schema) is None  # ends with a variable-length list


def test_encode_rejects_unsupported_values() -> None:
    with pytest.raises(CodecError):
        encode({"type": "date"}, dt.date(2026, 1, 1))
    with pytest.raises(CodecError):
        encode(primary(AIRCON.get(0xB3).schema), True)
    assert encode({"type": "number", "format": "uint8"}, b"\x05") == b"\x05"


def test_unsupported_type() -> None:
    with pytest.raises(CodecError):
        decode({"type": "mystery"}, b"\x00")
