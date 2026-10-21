"""Decode and encode EDT bytes according to (resolved) MRA schemas."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .mra import ClassDef, Schema, Text

_INT_SIZES = {"int8": 1, "uint8": 1, "int16": 2, "uint16": 2, "int32": 4, "uint32": 4}
_BINARY_NAMES = ({"true", "false"}, {"on", "off"})

CoefficientLookup = Callable[[int], float | None]


class CodecError(ValueError):
    """EDT does not match the schema, or a value cannot be encoded."""


@dataclass(frozen=True)
class EnumValue:
    edt: bytes
    name: str
    label: Text

    @property
    def is_on(self) -> bool | None:
        if self.name in ("true", "on"):
            return True
        if self.name in ("false", "off"):
            return False
        return None


# ---------------------------------------------------------------- schema shape


def alternatives(schema: Schema) -> list[Schema]:
    """Flatten ``oneOf`` into a list of concrete schemas."""
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        out: list[Schema] = []
        for alt in one_of:
            if isinstance(alt, dict):
                out.extend(alternatives(alt))
        return out
    return [schema]


def primary(schema: Schema) -> Schema:
    """The schema describing the normal value; ``state`` alternatives of a
    ``oneOf`` with another type are special codes such as "no data"."""
    alts = alternatives(schema)
    for alt in alts:
        if alt.get("type") != "state":
            return alt
    if len(alts) == 1:
        return alts[0]
    merged: list[Any] = []
    for alt in alts:
        merged.extend(alt.get("enum") or [])
    return {"type": "state", "size": alts[0].get("size"), "enum": merged}


def enum_values(schema: Schema) -> list[EnumValue]:
    out: list[EnumValue] = []
    for item in schema.get("enum") or []:
        edt = _parse_hex(item.get("edt"))
        if edt is None:
            continue
        name = str(item.get("name") or "")
        out.append(EnumValue(edt, name, Text.from_json(item.get("descriptions"), name)))
    return out


def is_binary_state(schema: Schema) -> bool:
    if schema.get("type") != "state":
        return False
    names = {e.name for e in enum_values(schema)}
    return any(names == candidate for candidate in _BINARY_NAMES)


def fixed_size(schema: Schema) -> int | None:
    """Byte length of values matching ``schema`` when it is fixed."""
    if "oneOf" in schema:
        for alt in alternatives(schema):
            size = fixed_size(alt)
            if size is not None:
                return size
        return None
    kind = schema.get("type")
    if kind == "number":
        return _INT_SIZES.get(str(schema.get("format")))
    if kind == "level":
        return 1
    if kind in ("state", "numericValue", "bitmap"):
        size = schema.get("size")
        return size if isinstance(size, int) and size > 0 else None
    if kind == "date":
        return int(schema.get("size") or 4)
    if kind == "time":
        return int(schema.get("size") or 3)
    if kind == "date-time":
        return int(schema.get("size") or 7)
    if kind == "raw":
        lo, hi = schema.get("minSize"), schema.get("maxSize")
        return lo if isinstance(lo, int) and lo == hi else None
    if kind == "object":
        total = 0
        for element in schema.get("properties") or []:
            size = fixed_size(element.get("element") or {})
            if size is None:
                return None
            total += size
        return total
    return None


# ---------------------------------------------------------------- decoding


def decode(schema: Schema, edt: bytes, coefficient: CoefficientLookup | None = None) -> Any:
    """Decode ``edt``. Returns int/float, EnumValue, date/time/datetime, bytes,
    dict (object/bitmap) or list (array). Raises CodecError on mismatch."""
    if "oneOf" in schema:
        for alt in alternatives(schema):
            try:
                return decode(alt, edt, coefficient)
            except CodecError:
                continue
        raise CodecError(f"no alternative matches {edt.hex()}")

    kind = schema.get("type")
    decoder = _DECODERS.get(str(kind))
    if decoder is None:
        raise CodecError(f"unsupported type {kind!r}")
    return decoder(schema, edt, coefficient)


def _decode_number(schema: Schema, edt: bytes, coefficient: CoefficientLookup | None) -> Any:
    fmt = str(schema.get("format"))
    size = _INT_SIZES.get(fmt)
    if size is None or len(edt) != size:
        raise CodecError(f"expected {size} bytes for {fmt}")
    raw = int.from_bytes(edt, "big", signed=fmt.startswith("int"))
    lo, hi = schema.get("minimum"), schema.get("maximum")
    if (isinstance(lo, (int, float)) and raw < lo) or (isinstance(hi, (int, float)) and raw > hi):
        raise CodecError(f"{raw} out of range")
    value: float = raw
    multiple = schema.get("multiple")
    if isinstance(multiple, (int, float)) and multiple != 1:
        value = raw * multiple
    for ref in schema.get("coefficient") or []:
        epc = _parse_hex(ref)
        factor = coefficient(int.from_bytes(epc, "big")) if (epc and coefficient) else None
        if factor is not None:
            value = value * factor
    if isinstance(value, float):
        return round(value, 9)
    return value


def _decode_state(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> EnumValue:
    for item in enum_values(schema):
        if item.edt == edt:
            return item
    raise CodecError(f"unknown state {edt.hex()}")


def _decode_level(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> int:
    base = _parse_hex(schema.get("base"))
    maximum = schema.get("maximum")
    if len(edt) != 1 or base is None or not isinstance(maximum, int):
        raise CodecError("invalid level")
    level = edt[0] - base[0] + 1
    if not 1 <= level <= maximum:
        raise CodecError(f"level {level} out of range")
    return level


def _decode_numeric_value(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> float:
    for item in schema.get("enum") or []:
        if _parse_hex(item.get("edt")) == edt:
            return float(item["numericValue"])
    raise CodecError(f"unknown numeric code {edt.hex()}")


def _decode_date(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> dt.date:
    if len(edt) != 4:
        raise CodecError("date must be 4 bytes")
    try:
        return dt.date(int.from_bytes(edt[:2], "big"), edt[2], edt[3])
    except ValueError as err:
        raise CodecError(str(err)) from err


def _decode_time(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> dt.time:
    if len(edt) not in (2, 3):
        raise CodecError("time must be 2 or 3 bytes")
    try:
        return dt.time(edt[0], edt[1], edt[2] if len(edt) == 3 else 0)
    except ValueError as err:
        raise CodecError(str(err)) from err


def _decode_datetime(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> dt.datetime:
    if len(edt) not in (6, 7):
        raise CodecError("date-time must be 6 or 7 bytes")
    try:
        return dt.datetime(
            int.from_bytes(edt[:2], "big"),
            edt[2],
            edt[3],
            edt[4],
            edt[5],
            edt[6] if len(edt) == 7 else 0,
        )
    except ValueError as err:
        raise CodecError(str(err)) from err


def _decode_raw(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> bytes:
    lo, hi = schema.get("minSize"), schema.get("maxSize")
    if (isinstance(lo, int) and len(edt) < lo) or (isinstance(hi, int) and len(edt) > hi):
        raise CodecError("raw size out of range")
    return edt


def _decode_bitmap(schema: Schema, edt: bytes, _: CoefficientLookup | None) -> dict[str, Any]:
    size = schema.get("size")
    if isinstance(size, int) and len(edt) != size:
        raise CodecError("bitmap size mismatch")
    out: dict[str, Any] = {}
    for bitmap in schema.get("bitmaps") or []:
        position = bitmap.get("position") or {}
        index = position.get("index", 0)
        mask = int(str(position.get("bitMask", "0b0")), 0)
        if not mask or index >= len(edt):
            continue
        shift = (mask & -mask).bit_length() - 1
        bits = (edt[index] & mask) >> shift
        value: Any = bits
        for item in enum_values(bitmap.get("value") or {}):
            if int.from_bytes(item.edt, "big") == bits:
                value = item
                break
        out[str(bitmap.get("name"))] = value
    return out


def _decode_object(
    schema: Schema, edt: bytes, coefficient: CoefficientLookup | None
) -> dict[str, Any]:
    elements = schema.get("properties") or []
    out: dict[str, Any] = {}
    offset = 0
    for idx, element in enumerate(elements):
        sub = element.get("element") or {}
        size = fixed_size(sub)
        if size is None:
            if idx != len(elements) - 1:
                raise CodecError("variable-size element in the middle of an object")
            size = len(edt) - offset
        chunk = edt[offset : offset + size]
        if len(chunk) != size:
            raise CodecError("object shorter than its schema")
        offset += size
        try:
            out[str(element.get("shortName"))] = decode(sub, chunk, coefficient)
        except CodecError:
            out[str(element.get("shortName"))] = None
    if offset != len(edt):
        raise CodecError("object longer than its schema")
    return out


def _decode_array(schema: Schema, edt: bytes, coefficient: CoefficientLookup | None) -> list[Any]:
    item_size = schema.get("itemSize")
    items = schema.get("items") or {}
    if not isinstance(item_size, int) or item_size <= 0 or len(edt) % item_size:
        raise CodecError("array length mismatch")
    out: list[Any] = []
    for start in range(0, len(edt), item_size):
        try:
            out.append(decode(items, edt[start : start + item_size], coefficient))
        except CodecError:
            out.append(None)
    return out


_DECODERS: dict[str, Callable[[Schema, bytes, CoefficientLookup | None], Any]] = {
    "number": _decode_number,
    "state": _decode_state,
    "level": _decode_level,
    "numericValue": _decode_numeric_value,
    "date": _decode_date,
    "time": _decode_time,
    "date-time": _decode_datetime,
    "raw": _decode_raw,
    "bitmap": _decode_bitmap,
    "object": _decode_object,
    "array": _decode_array,
}


def coefficient_lookup(
    class_def: ClassDef,
    values: Mapping[int, bytes],
    readable: Collection[int] = frozenset(),
) -> CoefficientLookup:
    """Resolve coefficient references (e.g. 0xC2 "unit for cumulative energy")
    from a device's other property values.

    A coefficient the device does not have counts as 1. One it has
    (``readable``) but whose value is not known raises CodecError: scaling by 1
    instead would report e.g. cumulative energy 100 times too large.
    """

    def lookup(ref: int) -> float | None:
        ref_prop = class_def.get(ref)
        ref_edt = values.get(ref)
        value: Any = None
        if ref_prop is not None and ref_edt is not None:
            try:
                value = decode(ref_prop.schema, ref_edt)  # coefficients have no coefficients
            except CodecError:
                value = None
        if isinstance(value, (int, float)):
            return float(value)
        if ref in readable:
            raise CodecError(f"coefficient 0x{ref:02X} is not known")
        return None

    return lookup


def decode_property(
    class_def: ClassDef,
    epc: int,
    values: Mapping[int, bytes],
    readable: Collection[int] = frozenset(),
) -> Any:
    """Decode a property of a device, applying coefficients from its other
    values (``readable``: the EPCs the device can report, see coefficient_lookup)."""
    prop = class_def.get(epc)
    edt = values.get(epc)
    if prop is None or edt is None:
        raise CodecError(f"no definition or value for 0x{epc:02X}")
    return decode(prop.schema, edt, coefficient_lookup(class_def, values, readable))


def to_plain(value: Any, language: str) -> Any:
    """A decoded value as JSON-friendly data (labels, hex, ISO dates)."""
    if isinstance(value, EnumValue):
        return value.label.get(language)
    if isinstance(value, dict):
        return {key: to_plain(item, language) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item, language) for item in value]
    if isinstance(value, bytes):
        return value.hex().upper()
    if isinstance(value, (dt.date, dt.time)):  # datetime is a date
        return value.isoformat()
    return value


# ---------------------------------------------------------------- encoding


def encode(schema: Schema, value: Any) -> bytes:
    """Encode a user value for SET. Supports number, level, state and raw."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    errors: list[str] = []
    for alt in alternatives(schema):
        try:
            return _encode_one(alt, value)
        except CodecError as err:
            errors.append(str(err))
    raise CodecError("; ".join(errors) or "cannot encode value")


def _encode_one(schema: Schema, value: Any) -> bytes:
    kind = schema.get("type")
    if kind == "number":
        fmt = str(schema.get("format"))
        size = _INT_SIZES.get(fmt)
        if size is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError("a number is required")
        multiple = schema.get("multiple")
        scaled = value / multiple if isinstance(multiple, (int, float)) and multiple else value
        if not math.isfinite(scaled) or abs(scaled - round(scaled)) > 1e-6:
            raise CodecError(f"{value} is not a multiple of {multiple}")
        raw = round(scaled)
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if (isinstance(lo, (int, float)) and raw < lo) or (
            isinstance(hi, (int, float)) and raw > hi
        ):
            raise CodecError(f"{value} out of range")
        return raw.to_bytes(size, "big", signed=fmt.startswith("int"))
    if kind == "level":
        base = _parse_hex(schema.get("base"))
        maximum = schema.get("maximum")
        if base is None or not isinstance(value, int) or not 1 <= value <= int(maximum or 0):
            raise CodecError("invalid level")
        return bytes([base[0] + value - 1])
    if kind == "state":
        for item in enum_values(schema):
            if value == item or value in (item.name, item.label.ja, item.label.en):
                return item.edt
            if isinstance(value, bool) and item.is_on is value:
                return item.edt
        raise CodecError(f"{value!r} is not a valid state")
    if kind == "raw" and isinstance(value, str):
        edt = _parse_hex(value)
        if edt is None:
            raise CodecError("raw value must be hex")
        return edt
    raise CodecError(f"setting {kind!r} values is not supported")


def _parse_hex(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower().removeprefix("0x").replace(" ", "")
    if len(token) % 2:
        token = "0" + token
    try:
        return bytes.fromhex(token)
    except ValueError:
        return None
