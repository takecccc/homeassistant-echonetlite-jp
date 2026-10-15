"""Power distribution board metering (class 0x0287).

Per-circuit values are published as list properties: a "channel range" EPC
is SET first, then the matching list EPC is read (an atomic pair in the MRA).
The individual channel EPCs 0xD0-0xEF only cover 32 channels and many boards
do not implement them, so the lists are used whenever available.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..codec import CodecError, coefficient_lookup, decode, primary
from ..device import DerivedSensor, EchonetDevice, SensorGroup
from ..errors import EchonetError
from ..mra import ClassDef, Text

if TYPE_CHECKING:
    from ..client import EchonetClient

_LOGGER = logging.getLogger(__name__)

CHANNEL_EPCS = frozenset(range(0xD0, 0xF0))
_MAX_CHANNELS = 252  # upper bound of the channel count properties in the MRA
_KEY_RE = re.compile(r"^(ch|duplex)(\d+)_([a-z_]+)$")


@dataclass(frozen=True)
class _Field:
    key: str
    item_name: str | None  # shortName inside an object item; None for scalar items
    name: Text
    unit: str
    cumulative: bool


@dataclass(frozen=True)
class _ChannelList:
    prefix: str  # derived keys are "<prefix><channel>_<field key>"
    label: Text
    count_epc: int
    range_epc: int
    list_epc: int
    fields: tuple[_Field, ...]


_SIMPLEX = Text("計測チャンネル", "Channel ")
_DUPLEX = Text("双方向計測チャンネル", "Duplex channel ")
_LISTS = (
    _ChannelList(
        "ch",
        _SIMPLEX,
        0xB1,
        0xB2,
        0xB3,
        (_Field("energy", None, Text("積算電力量", "energy"), "kWh", True),),
    ),
    _ChannelList(
        "ch",
        _SIMPLEX,
        0xB1,
        0xB6,
        0xB7,
        (_Field("power", None, Text("瞬時電力", "power"), "W", False),),
    ),
    _ChannelList(
        "duplex",
        _DUPLEX,
        0xB8,
        0xB9,
        0xBA,
        (
            _Field(
                "energy_normal",
                "normalDirectionElectricEnergy",
                Text("積算電力量(正方向)", "energy (normal direction)"),
                "kWh",
                True,
            ),
            _Field(
                "energy_reverse",
                "reverseDirectionElectricEnergy",
                Text("積算電力量(逆方向)", "energy (reverse direction)"),
                "kWh",
                True,
            ),
        ),
    ),
    _ChannelList(
        "duplex",
        _DUPLEX,
        0xB8,
        0xBD,
        0xBE,
        (_Field("power", None, Text("瞬時電力", "power"), "W", False),),
    ),
)


def _key(lst: _ChannelList, channel: int, fld: _Field) -> str:
    return f"{lst.prefix}{channel}_{fld.key}"


def _sensor(lst: _ChannelList, channel: int, fld: _Field) -> DerivedSensor:
    return DerivedSensor(
        key=_key(lst, channel, fld),
        name=Text(
            f"{lst.label.ja}{channel} {fld.name.ja}", f"{lst.label.en}{channel} {fld.name.en}"
        ),
        unit=fld.unit,
        total_increasing=fld.cumulative,
        group=SensorGroup(lst.prefix, channel, lst.label),
        measure=fld.name,
    )


class PowerDistributionBoard:
    class_code = 0x0287

    def handled_epcs(self, device: EchonetDevice) -> frozenset[int]:
        if any(lst.list_epc in device.info.maps.get for lst in _LISTS):
            return CHANNEL_EPCS
        return frozenset()

    async def async_poll(self, client: EchonetClient, device: EchonetDevice) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for lst in _LISTS:
            if lst.list_epc not in device.info.maps.get:
                continue
            try:
                channels = await _read_list(client, device, lst)
            except EchonetError as err:
                _LOGGER.debug("%s: reading 0x%02X failed: %s", device.id, lst.list_epc, err)
                continue
            for channel, item in channels.items():
                for fld in lst.fields:
                    value = item.get(fld.item_name) if fld.item_name else item
                    out[_key(lst, channel, fld)] = (
                        value if isinstance(value, (int, float)) else None
                    )
        return out

    def sensors(self, device: EchonetDevice) -> list[DerivedSensor]:
        """Sensors for every channel the board reports, read or not.

        Channels come from the channel counts (0xB1/0xB8) so that the entities
        exist from the start even when a lossy list read has not succeeded yet;
        channels seen in readings are added too, for boards without counts.
        """
        channels: dict[tuple[str, str], set[int]] = defaultdict(set)
        for lst in _LISTS:
            count = device.decode(lst.count_epc)
            if lst.list_epc in device.info.maps.get and isinstance(count, int):
                for fld in lst.fields:
                    channels[lst.prefix, fld.key].update(range(1, min(count, _MAX_CHANNELS) + 1))
        for key in device.derived:
            if match := _KEY_RE.match(key):
                channels[match[1], match[3]].add(int(match[2]))
        return [
            _sensor(lst, channel, fld)
            for lst in _LISTS
            for fld in lst.fields
            for channel in sorted(channels.get((lst.prefix, fld.key), ()))
        ]


async def _read_list(
    client: EchonetClient, device: EchonetDevice, lst: _ChannelList
) -> dict[int, Any]:
    count = device.decode(lst.count_epc)
    count = count if isinstance(count, int) else None
    max_range = _max_range(device.class_def, lst.range_epc)
    can_set = lst.range_epc in device.info.maps.set

    channels: dict[int, Any] = {}
    start = 1
    while True:
        requested: int | None = None
        if can_set and count is not None:
            requested = min(max_range, count - start + 1)
            try:
                await client.set(
                    device.host, device.eoj, {lst.range_epc: bytes([start, requested])}
                )
            except EchonetError as err:
                # Some boards reject the range but still answer with a default list.
                _LOGGER.debug("%s: range 0x%02X rejected: %s", device.id, lst.range_epc, err)
                can_set = False
                requested = None
        edt = await client.get_one(device.host, device.eoj, lst.list_epc)
        first, items = _decode_list(device, lst.list_epc, edt)
        if first is None:
            first = start
        for offset, item in enumerate(items):
            channel = first + offset
            if count is None or channel <= count:
                channels[channel] = item
        if requested is None or count is None:
            break
        start += requested
        if start > count:
            break
    return channels


def _decode_list(device: EchonetDevice, epc: int, edt: bytes) -> tuple[int | None, list[Any]]:
    """Return (start channel, items). Falls back to a header-less payload,
    which some boards send instead of start(1) + range(1) + items."""
    prop = device.class_def.get(epc)
    if prop is None:
        return None, []
    lookup = coefficient_lookup(device.class_def, device.values)
    try:
        decoded = decode(prop.schema, edt, lookup)
    except CodecError:
        decoded = {}
    items = next((v for v in decoded.values() if isinstance(v, list)), None)
    if items is not None:
        start = decoded.get("startChannel")
        return (start if isinstance(start, int) else None), items
    array_schema = prop.schema.get("properties", [{}])[-1].get("element", {})
    try:
        return None, decode(array_schema, edt, lookup)
    except CodecError:
        return None, []


def _max_range(class_def: ClassDef, range_epc: int) -> int:
    prop = class_def.get(range_epc)
    try:
        maximum = primary(prop.schema["properties"][1]["element"]).get("maximum")  # type: ignore[union-attr]
    except (AttributeError, KeyError, IndexError, TypeError):
        maximum = None
    return maximum if isinstance(maximum, int) else 30
