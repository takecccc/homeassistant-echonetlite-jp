"""A minimal ECHONET Lite node that answers GET/SETC over UDP on loopback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from custom_components.echonetlite_jp.echonet.protocol import (
    EOJ,
    ESV,
    NODE_PROFILE,
    Frame,
    Property,
    encode_property_map,
)

FAKE_HOST = "127.0.0.2"
FAKE_UID = bytes.fromhex("FE00000B") + bytes(range(1, 14))
PANASONIC = bytes.fromhex("00000B")


@dataclass
class FakeObject:
    values: dict[int, bytes]
    settable: set[int] = field(default_factory=set)
    # Called with (epc, edt) after a successful SET.
    on_set: Callable[[int, bytes], None] | None = None
    # Dynamic values computed on GET.
    getters: dict[int, Callable[[], bytes]] = field(default_factory=dict)

    def get(self, epc: int) -> bytes | None:
        if epc in self.getters:
            return self.getters[epc]()
        if epc == 0x9F:
            return encode_property_map(set(self.values) | set(self.getters) | {0x9D, 0x9E, 0x9F})
        if epc == 0x9E:
            return encode_property_map(self.settable)
        if epc == 0x9D:
            return encode_property_map(set())
        return self.values.get(epc)


class FakeNode(asyncio.DatagramProtocol):
    def __init__(self, objects: dict[EOJ, FakeObject], *, uid: bytes = FAKE_UID) -> None:
        self.objects = objects
        self.uid = uid
        self.max_properties: int | None = None  # drop requests with more properties
        self.silent = False
        self.reject_multi = False  # answer multi-property GETs with an empty Get_SNA
        self.drop = 0  # silently drop this many upcoming requests
        self.ignored_epcs: set[int] = set()  # never answer requests for these
        self.delay = 0.0  # seconds before answering
        self.in_flight = 0
        self.max_in_flight = 0
        self.requests: list[Frame] = []
        self._transport: asyncio.DatagramTransport | None = None
        self.port = 0

    async def start(self, host: str = FAKE_HOST, port: int = 0) -> int:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]
        return self.port

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def notify(self, eoj: EOJ, epc: int, edt: bytes, addr: tuple[str, int]) -> None:
        assert self._transport is not None
        frame = Frame(0, eoj, EOJ(0x05, 0xFF, 0x01), ESV.INF, (Property(epc, edt),))
        self._transport.sendto(frame.encode(), addr)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        frame = Frame.decode(data)
        self.requests.append(frame)
        if self.silent:
            return
        if any(p.epc in self.ignored_epcs for p in frame.properties):
            return
        if self.drop > 0:
            self.drop -= 1
            return
        if self.max_properties is not None and len(frame.properties) > self.max_properties:
            return
        obj = self._node_profile() if frame.deoj == NODE_PROFILE else self.objects.get(frame.deoj)
        if obj is None:
            return
        reply = self._handle(obj, frame)
        if reply is None:
            return
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        asyncio.get_running_loop().call_later(self.delay, self._reply, reply, addr)

    def _reply(self, reply: Frame, addr: tuple[str, int]) -> None:
        self.in_flight -= 1
        if self._transport is not None:
            self._transport.sendto(reply.encode(), addr)

    def _handle(self, obj: FakeObject, frame: Frame) -> Frame | None:
        props: list[Property] = []
        ok = True
        if frame.esv == ESV.GET and self.reject_multi and len(frame.properties) > 1:
            props = [Property(p.epc) for p in frame.properties]
            return Frame(frame.tid, frame.deoj, frame.seoj, ESV.GET_SNA, tuple(props))
        if frame.esv == ESV.GET:
            for prop in frame.properties:
                value = obj.get(prop.epc)
                ok &= value is not None
                props.append(Property(prop.epc, value or b""))
            esv = ESV.GET_RES if ok else ESV.GET_SNA
        elif frame.esv == ESV.SETC:
            for prop in frame.properties:
                if prop.epc in obj.settable:
                    obj.values[prop.epc] = prop.edt
                    if obj.on_set is not None:
                        obj.on_set(prop.epc, prop.edt)
                    props.append(Property(prop.epc))
                else:
                    ok = False
                    props.append(prop)
            esv = ESV.SET_RES if ok else ESV.SETC_SNA
        else:
            return None
        return Frame(frame.tid, frame.deoj, frame.seoj, esv, tuple(props))

    def _node_profile(self) -> FakeObject:
        instances = [eoj for eoj in self.objects]
        instance_list = bytes([len(instances)]) + b"".join(bytes(e) for e in instances)
        return FakeObject({0x80: b"\x30", 0x83: self.uid, 0x8A: PANASONIC, 0xD6: instance_list})
