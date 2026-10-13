from __future__ import annotations

import pytest

from custom_components.echonetlite_jp.echonet.protocol import (
    EOJ,
    ESV,
    Frame,
    FrameError,
    Property,
    decode_instance_list,
    decode_property_map,
    encode_property_map,
)


def test_frame_round_trip() -> None:
    frame = Frame(
        0x1234,
        EOJ(0x05, 0xFF, 0x01),
        EOJ(0x02, 0x87, 0x01),
        ESV.GET,
        (Property(0x80), Property(0xB2, b"\x01\x10")),
    )
    data = frame.encode()
    assert data.hex() == "1081123405ff0102870162028000b202" + "0110"
    assert Frame.decode(data) == frame


@pytest.mark.parametrize("data", [b"", b"\x10\x82" + bytes(10), bytes.fromhex("10810001")])
def test_frame_rejects_garbage(data: bytes) -> None:
    with pytest.raises(FrameError):
        Frame.decode(data)


def test_frame_rejects_truncated_property() -> None:
    data = bytes.fromhex("1081000105ff010287017201800530")
    with pytest.raises(FrameError):
        Frame.decode(data)


def test_eoj_parsing_and_formatting() -> None:
    eoj = EOJ.parse("0x028701")
    assert eoj == EOJ(0x02, 0x87, 0x01)
    assert str(eoj) == "028701"
    assert eoj.class_code == 0x0287
    with pytest.raises(ValueError):
        EOJ.parse("0287")


@pytest.mark.parametrize(
    "epcs",
    [
        set(),
        {0x80, 0x9F},
        set(range(0x80, 0x90)),
        {0x80, 0x81, 0x9D, 0x9E, 0x9F, *range(0xD0, 0xF0)},
    ],
)
def test_property_map_round_trip(epcs: set[int]) -> None:
    assert decode_property_map(encode_property_map(epcs)) == epcs


def test_property_map_bitmap_layout() -> None:
    # 16+ properties use a bitmap: byte i, bit b -> EPC 0x80 + 0x10*b + i
    edt = bytes([17, 0x01 | 0x80] + [0] * 15)
    assert decode_property_map(edt) == {0x80, 0xF0}


def test_instance_list() -> None:
    edt = bytes.fromhex("02 028701 027901")
    assert decode_instance_list(edt) == [EOJ(2, 0x87, 1), EOJ(2, 0x79, 1)]
