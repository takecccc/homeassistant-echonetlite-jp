"""ECHONET Lite frame format (EHD1=0x10, EHD2=0x81 "specified message format")."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

ECHONET_PORT = 3610
MULTICAST_ADDRESS = "224.0.23.0"

_EHD = b"\x10\x81"
_HEADER_SIZE = 12  # EHD(2) TID(2) SEOJ(3) DEOJ(3) ESV(1) OPC(1)


class ESV(IntEnum):
    """ECHONET Lite service codes."""

    SETI = 0x60
    SETC = 0x61
    GET = 0x62
    INF_REQ = 0x63
    SETGET = 0x6E
    SET_RES = 0x71
    GET_RES = 0x72
    INF = 0x73
    INFC = 0x74
    INFC_RES = 0x7A
    SETGET_RES = 0x7E
    SETI_SNA = 0x50
    SETC_SNA = 0x51
    GET_SNA = 0x52
    INF_SNA = 0x53
    SETGET_SNA = 0x5E


class FrameError(ValueError):
    """Raised for datagrams that are not valid ECHONET Lite frames."""


@dataclass(frozen=True, order=True)
class EOJ:
    """ECHONET object identifier: class group code, class code, instance code."""

    group: int
    cls: int
    instance: int

    @classmethod
    def parse(cls, value: str) -> EOJ:
        raw = value.strip().lower().removeprefix("0x")
        if len(raw) != 6:
            raise ValueError(f"EOJ must be 3 bytes of hex: {value!r}")
        data = bytes.fromhex(raw)
        return cls(data[0], data[1], data[2])

    @classmethod
    def from_bytes(cls, data: bytes) -> EOJ:
        if len(data) != 3:
            raise ValueError("EOJ must be 3 bytes")
        return cls(data[0], data[1], data[2])

    def __bytes__(self) -> bytes:
        return bytes((self.group, self.cls, self.instance))

    def __str__(self) -> str:
        return bytes(self).hex().upper()

    @property
    def class_code(self) -> int:
        """Two-byte class code (group << 8 | class), e.g. 0x0287."""
        return self.group << 8 | self.cls

    @property
    def is_node_profile(self) -> bool:
        return self.group == 0x0E and self.cls == 0xF0


NODE_PROFILE = EOJ(0x0E, 0xF0, 0x01)
CONTROLLER = EOJ(0x05, 0xFF, 0x01)


@dataclass(frozen=True)
class Property:
    """One EPC/EDT pair. An empty EDT means PDC=0."""

    epc: int
    edt: bytes = b""


@dataclass(frozen=True)
class Frame:
    tid: int
    seoj: EOJ
    deoj: EOJ
    esv: int
    properties: tuple[Property, ...] = field(default_factory=tuple)

    def encode(self) -> bytes:
        if len(self.properties) > 0xFF:
            raise ValueError("too many properties in one frame")
        out = bytearray(_EHD)
        out += self.tid.to_bytes(2, "big")
        out += bytes(self.seoj)
        out += bytes(self.deoj)
        out.append(self.esv)
        out.append(len(self.properties))
        for prop in self.properties:
            if len(prop.edt) > 0xFF:
                raise ValueError(f"EDT too long for EPC 0x{prop.epc:02X}")
            out.append(prop.epc)
            out.append(len(prop.edt))
            out += prop.edt
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        if len(data) < _HEADER_SIZE or data[:2] != _EHD:
            raise FrameError("not an ECHONET Lite frame")
        tid = int.from_bytes(data[2:4], "big")
        seoj = EOJ.from_bytes(data[4:7])
        deoj = EOJ.from_bytes(data[7:10])
        esv = data[10]
        opc = data[11]
        props: list[Property] = []
        offset = _HEADER_SIZE
        # SetGet responses carry a second OPC block; we only consume the first.
        for _ in range(opc):
            if offset + 2 > len(data):
                raise FrameError("truncated property header")
            epc = data[offset]
            pdc = data[offset + 1]
            offset += 2
            if offset + pdc > len(data):
                raise FrameError("truncated property data")
            props.append(Property(epc, bytes(data[offset : offset + pdc])))
            offset += pdc
        return cls(tid, seoj, deoj, esv, tuple(props))


def decode_property_map(edt: bytes) -> frozenset[int]:
    """Decode a property map (EPC 0x9D/0x9E/0x9F).

    Fewer than 16 properties are listed directly. Otherwise the 16 bytes after
    the count form a bitmap where bit ``b`` of byte ``i`` stands for EPC
    ``0x80 + 0x10 * b + i``.
    """
    if not edt:
        return frozenset()
    count = edt[0]
    if count < 16:
        return frozenset(edt[1 : 1 + count])
    epcs: set[int] = set()
    for i, byte in enumerate(edt[1:17]):
        for bit in range(8):
            if byte & (1 << bit):
                epcs.add(0x80 + 0x10 * bit + i)
    return frozenset(epcs)


def encode_property_map(epcs: set[int] | frozenset[int]) -> bytes:
    ordered = sorted(epcs)
    if len(ordered) < 16:
        return bytes([len(ordered), *ordered])
    bitmap = bytearray(16)
    for epc in ordered:
        bitmap[(epc - 0x80) & 0x0F] |= 1 << ((epc - 0x80) >> 4)
    return bytes([len(ordered)]) + bytes(bitmap)


def decode_instance_list(edt: bytes) -> list[EOJ]:
    """Decode the node profile instance list (EPC 0xD5/0xD6)."""
    if not edt:
        return []
    count = edt[0]
    body = edt[1:]
    return [EOJ.from_bytes(body[i * 3 : i * 3 + 3]) for i in range(min(count, len(body) // 3))]
