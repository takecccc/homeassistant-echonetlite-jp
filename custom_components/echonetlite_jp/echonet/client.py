"""High-level ECHONET Lite operations on top of the transport."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .errors import EchonetError, EchonetRequestRejected, EchonetTimeoutError
from .manufacturers import MANUFACTURERS
from .protocol import (
    EOJ,
    ESV,
    NODE_PROFILE,
    Frame,
    Property,
    decode_instance_list,
    decode_property_map,
)
from .transport import EchonetTransport

_LOGGER = logging.getLogger(__name__)

EPC_INSTALLATION_LOCATION = 0x81
EPC_UID = 0x83
EPC_MANUFACTURER = 0x8A
EPC_PRODUCT_CODE = 0x8C
EPC_SERIAL_NUMBER = 0x8D
EPC_INF_MAP = 0x9D
EPC_SET_MAP = 0x9E
EPC_GET_MAP = 0x9F
EPC_INSTANCE_LIST_NOTIFICATION = 0xD5
EPC_INSTANCE_LIST = 0xD6

IDENTIFICATION_EPCS = frozenset(
    {EPC_UID, EPC_MANUFACTURER, EPC_PRODUCT_CODE, EPC_SERIAL_NUMBER, 0x82, 0x8B}
)
PROPERTY_MAP_EPCS = frozenset({EPC_INF_MAP, EPC_SET_MAP, EPC_GET_MAP})


@dataclass(frozen=True)
class NodeInfo:
    host: str
    uid: str | None
    manufacturer: str | None
    manufacturer_code: int | None
    instances: tuple[EOJ, ...]


@dataclass(frozen=True)
class PropertyMaps:
    get: frozenset[int] = frozenset()
    set: frozenset[int] = frozenset()
    inf: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ObjectInfo:
    host: str
    eoj: EOJ
    maps: PropertyMaps
    uid: str | None = None
    manufacturer: str | None = None
    manufacturer_code: int | None = None
    product_code: str | None = None
    serial_number: str | None = None


@dataclass
class GetResult:
    values: dict[int, bytes] = field(default_factory=dict)
    missing: set[int] = field(default_factory=set)


class EchonetClient:
    def __init__(
        self,
        transport: EchonetTransport,
        *,
        timeout: float = 3.0,
        initial_timeout: float = 1.0,
        min_timeout: float = 0.3,
        retries: int = 2,
        max_properties: int = 24,
    ) -> None:
        self.transport = transport
        self._timeout = timeout
        self._initial_timeout = min(initial_timeout, timeout)
        self._min_timeout = min(min_timeout, timeout)
        self._retries = retries
        self._max_properties = max(1, max_properties)
        self._rtt: dict[str, _RoundTrip] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def timeout_for(self, host: str) -> float:
        """Timeout for a first attempt, learned from the host's response times.

        Wired nodes typically answer in milliseconds but occasionally drop a
        request; waiting the full timeout each time would stall polling.
        """
        rtt = self._rtt.get(host)
        if rtt is None:
            # Unknown node: start moderately; retries back off up to the full timeout.
            return self._initial_timeout
        return min(self._timeout, max(self._min_timeout, rtt.timeout))

    async def discover(self, wait: float = 3.0, sends: int = 3) -> list[str]:
        """Return hosts that answer a multicast instance-list request.

        The request is sent ``sends`` times: on a real network a single
        multicast misses a given node roughly one time in eight.
        """
        responses = await self.transport.multicast(
            NODE_PROFILE, ESV.GET, [Property(EPC_INSTANCE_LIST)], wait=wait, sends=sends
        )
        return sorted({host for host, _ in responses}, key=_host_sort_key)

    async def get_node(self, host: str) -> NodeInfo:
        result = await self.get(host, NODE_PROFILE, [EPC_UID, EPC_MANUFACTURER, EPC_INSTANCE_LIST])
        if EPC_INSTANCE_LIST not in result.values:
            raise EchonetError(f"{host} did not report its instance list")
        return NodeInfo(
            host=host,
            uid=_uid(result.values.get(EPC_UID)),
            manufacturer=manufacturer_name(result.values.get(EPC_MANUFACTURER)),
            manufacturer_code=manufacturer_code(result.values.get(EPC_MANUFACTURER)),
            instances=tuple(decode_instance_list(result.values[EPC_INSTANCE_LIST])),
        )

    async def get_object(self, host: str, eoj: EOJ) -> ObjectInfo:
        result = await self.get(
            host,
            eoj,
            [
                EPC_GET_MAP,
                EPC_SET_MAP,
                EPC_INF_MAP,
                EPC_UID,
                EPC_MANUFACTURER,
                EPC_PRODUCT_CODE,
                EPC_SERIAL_NUMBER,
            ],
        )
        values = result.values
        return ObjectInfo(
            host=host,
            eoj=eoj,
            maps=PropertyMaps(
                get=decode_property_map(values.get(EPC_GET_MAP, b"")),
                set=decode_property_map(values.get(EPC_SET_MAP, b"")),
                inf=decode_property_map(values.get(EPC_INF_MAP, b"")),
            ),
            uid=_uid(values.get(EPC_UID)),
            manufacturer=manufacturer_name(values.get(EPC_MANUFACTURER)),
            manufacturer_code=manufacturer_code(values.get(EPC_MANUFACTURER)),
            product_code=_ascii(values.get(EPC_PRODUCT_CODE)),
            serial_number=_ascii(values.get(EPC_SERIAL_NUMBER)),
        )

    async def get(
        self, host: str, eoj: EOJ, epcs: Iterable[int], *, max_properties: int | None = None
    ) -> GetResult:
        """GET many properties, splitting into chunks and falling back to one
        property per request for nodes that reject or drop large requests.

        ``max_properties`` lowers the chunk size for nodes known to need it.
        Raises EchonetTimeoutError only when the node does not answer at all.
        """
        ordered = list(dict.fromkeys(epcs))
        result = GetResult()
        answered = False
        size = max(1, min(max_properties or self._max_properties, self._max_properties))
        for start in range(0, len(ordered), size):
            chunk = ordered[start : start + size]
            try:
                frame = await self._request(host, eoj, ESV.GET, [Property(e) for e in chunk])
            except EchonetTimeoutError:
                if len(chunk) == 1 and not answered:
                    raise
                frame = None
            if frame is not None:
                answered = True
                got = _collect(frame, result)
                # A Get_SNA normally carries every property that could be read.
                # Some nodes reject the whole request instead; only then ask
                # for the properties one by one.
                if got or len(chunk) == 1:
                    result.missing.update(set(chunk) - got)
                    continue
                retry = chunk
            else:
                retry = chunk
            for epc in retry:
                try:
                    single = await self._request(host, eoj, ESV.GET, [Property(epc)])
                except EchonetTimeoutError:
                    if not answered:
                        raise
                    result.missing.add(epc)
                    continue
                answered = True
                if epc not in _collect(single, result):
                    result.missing.add(epc)
        return result

    async def get_one(self, host: str, eoj: EOJ, epc: int) -> bytes:
        frame = await self._request(host, eoj, ESV.GET, [Property(epc)])
        for prop in frame.properties:
            if prop.epc == epc and prop.edt:
                return prop.edt
        raise EchonetRequestRejected(f"GET 0x{epc:02X} rejected by {host} {eoj}", [epc])

    async def set(self, host: str, eoj: EOJ, values: Mapping[int, bytes]) -> None:
        """SET with response (SetC). Raises EchonetRequestRejected on SetC_SNA."""
        frame = await self._request(
            host, eoj, ESV.SETC, [Property(epc, edt) for epc, edt in values.items()]
        )
        if frame.esv != ESV.SET_RES:
            # In SetC_SNA, accepted properties come back with PDC=0.
            rejected = [p.epc for p in frame.properties if p.edt] or list(values)
            raise EchonetRequestRejected(
                f"SET {', '.join(f'0x{e:02X}' for e in rejected)} rejected by {host} {eoj}",
                rejected,
            )

    async def _request(self, host: str, eoj: EOJ, esv: ESV, props: list[Property]) -> Frame:
        # One outstanding request per node: many nodes cannot handle more, and
        # it keeps writes from interleaving with a poll.
        async with self._locks.setdefault(host, asyncio.Lock()):
            loop = asyncio.get_running_loop()
            for attempt in range(self._retries + 1):
                # Back off on retries in case the node is slower than learned.
                timeout = min(self._timeout, self.timeout_for(host) * 2**attempt)
                started = loop.time()
                try:
                    frame = await self.transport.request(host, eoj, esv, props, timeout=timeout)
                except EchonetTimeoutError:
                    if attempt == self._retries:
                        raise
                    _LOGGER.debug("no response from %s %s in %.1fs; retrying", host, eoj, timeout)
                    continue
                self._rtt.setdefault(host, _RoundTrip()).update(loop.time() - started)
                return frame
        raise AssertionError("unreachable")


@dataclass
class _RoundTrip:
    """Smoothed round-trip time estimate (RFC 6298)."""

    srtt: float | None = None
    rttvar: float = 0.0

    def update(self, sample: float) -> None:
        if self.srtt is None:
            self.srtt, self.rttvar = sample, sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample

    @property
    def timeout(self) -> float:
        assert self.srtt is not None
        return self.srtt + 4 * self.rttvar


def manufacturer_code(edt: bytes | None) -> int | None:
    if not edt or len(edt) != 3:
        return None
    return int.from_bytes(edt, "big")


def manufacturer_name(edt: bytes | None) -> str | None:
    code = manufacturer_code(edt)
    if code is None:
        return None
    return MANUFACTURERS.get(code, f"0x{code:06X}")


def _collect(frame: Frame, result: GetResult) -> set[int]:
    got: set[int] = set()
    for prop in frame.properties:
        if prop.edt:
            result.values[prop.epc] = prop.edt
            got.add(prop.epc)
    return got


def _uid(edt: bytes | None) -> str | None:
    # 0x83: 0xFE + manufacturer code (3) + unique id (13). All-zero means "unset".
    if not edt or not any(edt[1:]):
        return None
    return edt.hex().upper()


def _ascii(edt: bytes | None) -> str | None:
    if not edt:
        return None
    text = edt.decode("ascii", errors="ignore").strip("\x00 ").strip()
    return text or None


def _host_sort_key(host: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in host.split("."))
    except ValueError:
        return (999,)
