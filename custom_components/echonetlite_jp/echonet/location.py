"""Installation location (EPC 0x81), defined by the ECHONET Lite specification.

One byte: bits 7-3 are the location code and bits 2-0 a number (0 = none).
0x00 means not set, 0xFF indefinite, and 0x01 followed by 16 bytes is a
free-form position.
"""

from __future__ import annotations

from .mra import Text

_LOCATIONS = {
    0x01: Text("リビング", "Living room"),
    0x02: Text("ダイニング", "Dining room"),
    0x03: Text("キッチン", "Kitchen"),
    0x04: Text("浴室", "Bathroom"),
    0x05: Text("トイレ", "Lavatory"),
    0x06: Text("洗面所", "Washroom"),
    0x07: Text("廊下", "Passageway"),
    0x08: Text("部屋", "Room"),
    0x09: Text("階段", "Stairway"),
    0x0A: Text("玄関", "Front door"),
    0x0B: Text("納戸", "Storeroom"),
    0x0C: Text("庭", "Garden"),
    0x0D: Text("車庫", "Garage"),
    0x0E: Text("ベランダ", "Veranda"),
    0x0F: Text("その他", "Other"),
}
_NOT_SET = Text("未設定", "Not set")
_INDEFINITE = Text("不定", "Indefinite")
_POSITION = Text("位置情報", "Position information")


def decode_location(edt: bytes) -> Text | None:
    if not edt:
        return None
    code = edt[0]
    if len(edt) != 1 or code == 0x01:
        return _POSITION
    if code == 0x00:
        return _NOT_SET
    if code == 0xFF:
        return _INDEFINITE
    place = _LOCATIONS.get(code >> 3)
    if place is None:
        return Text(f"0x{code:02X}", f"0x{code:02X}")
    number = code & 0x07
    if not number:
        return place
    return Text(f"{place.ja} {number}", f"{place.en} {number}")
