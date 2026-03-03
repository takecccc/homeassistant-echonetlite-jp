from __future__ import annotations

from custom_components.echonetlite_jp.mra import MRAClassResolver


def test_temporal_property_type_is_resolved() -> None:
    mra = MRAClassResolver("custom_components/echonetlite_jp/mra_data")
    meta = mra.resolve_property("027B01", 0x97)  # current time setting
    assert isinstance(meta, dict)
    assert meta.get("type") == "time"


def test_number_property_collects_no_data_codes_from_oneof_state() -> None:
    mra = MRAClassResolver("custom_components/echonetlite_jp/mra_data")
    meta = mra.resolve_property("027B01", 0xE2)  # measuredRoomTemperature
    assert isinstance(meta, dict)
    assert 126 in (meta.get("no_data_codes") or [])


def test_0287_c0_keeps_c2_coefficient_from_ref_overlay() -> None:
    mra = MRAClassResolver("custom_components/echonetlite_jp/mra_data")
    meta = mra.resolve_property("028701", 0xC0)
    assert isinstance(meta, dict)
    assert "0xC2" in (meta.get("coefficient") or [])
