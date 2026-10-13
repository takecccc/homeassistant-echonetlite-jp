"""Power distribution board metering (class 0x0287).

Per-circuit values are published as list properties: a "channel range" EPC
is SET first, then the matching list EPC is read (an atomic pair in the MRA).
The individual channel EPCs 0xD0-0xEF only cover 32 channels and many boards
do not implement them, so the lists are used whenever available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..codec import CodecError, decode, primary
from ..device import DerivedSensor, EchonetDevice
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
    prefix: str
    count_epc: int
    range_epc: int
    list_epc: int
    fields: tuple[_Field, ...]


_LISTS = (
    _ChannelList(
        "ch", 0xB1, 0xB2, 0xB3, (_Field("energy", None, Text("積算電力量", "energy"), "kWh", True),)
    ),
    _ChannelList(
        "ch", 0xB1, 0xB6, 0xB7, (_Field("power", None, Text("瞬時電力", "power"), "W", False),)
    ),
    _ChannelList(
        "duplex",
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
        "duplex", 0xB8, 0xBD, 0xBE, (_Field("power", None, Text("瞬時電力", "power"), "W", False),)
    ),
)
_FIELDS = {(lst.prefix, f.key): f for lst in _LISTS for f in lst.fields}
_PREFIX_LABEL = {
    "ch": Text("計測チャンネル", "Channel "),
    "duplex": Text("双方向計測チャンネル", "Duplex channel "),
}


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
                    out[f"{lst.prefix}{channel}_{fld.key}"] = (
                        value if isinstance(value, (int, float)) else None
                    )
        return out

    def sensors(self, device: EchonetDevice) -> list[DerivedSensor]:
        """Sensors for every channel the board reports, read or not.

        Channels come from the channel counts (0xB1/0xB8) so that the entities
        exist from the start even when a lossy list read has not succeeded yet;
        channels seen in readings are added too, for boards without counts.
        """
        keys = dict.fromkeys(self._expected_keys(device))
        keys.update(dict.fromkeys(k for k in device.derived if _KEY_RE.match(k)))
        out: list[DerivedSensor] = []
        for key in keys:
            match = _KEY_RE.match(key)
            if not match or (fld := _FIELDS.get((match[1], match[3]))) is None:
                continue
            label = _PREFIX_LABEL[match[1]]
            channel = match[2]
            out.append(
                DerivedSensor(
                    key=key,
                    name=Text(
                        f"{label.ja}{channel} {fld.name.ja}", f"{label.en}{channel} {fld.name.en}"
                    ),
                    unit=fld.unit,
                    total_increasing=fld.cumulative,
                )
            )
        return out

    @staticmethod
    def _expected_keys(device: EchonetDevice) -> list[str]:
        keys: list[str] = []
        for lst in _LISTS:
            if lst.list_epc not in device.info.maps.get:
                continue
            count = device.decode(lst.count_epc)
            if not isinstance(count, int):
                continue
            for channel in range(1, min(count, _MAX_CHANNELS) + 1):
                keys.extend(f"{lst.prefix}{channel}_{fld.key}" for fld in lst.fields)
        return keys


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
    lookup = _coefficient_lookup(device)
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


def _coefficient_lookup(device: EchonetDevice) -> Any:
    def lookup(ref: int) -> float | None:
        value = device.decode(ref)
        return float(value) if isinstance(value, (int, float)) else None

    return lookup


def _max_range(class_def: ClassDef, range_epc: int) -> int:
    prop = class_def.get(range_epc)
    try:
        maximum = primary(prop.schema["properties"][1]["element"]).get("maximum")  # type: ignore[union-attr]
    except (AttributeError, KeyError, IndexError, TypeError):
        maximum = None
    return maximum if isinstance(maximum, int) else 30
