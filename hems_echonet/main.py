from __future__ import annotations

import argparse
import binascii
import ipaddress
import socket
import struct
import time
from dataclasses import dataclass

ECHONET_PORT = 3610
ECHONET_MULTICAST = "224.0.23.0"
EHD1 = 0x10
EHD2 = 0x81

# Controller class object (used as sender object)
SEOJ_CONTROLLER = bytes.fromhex("05FF01")
# Node profile class object
EOJ_NODE_PROFILE = bytes.fromhex("0EF001")

ESV_GET = 0x62
ESV_GET_RES = 0x72
ESV_INF = 0x73


@dataclass
class EchonetFrame:
    tid: int
    seoj: bytes
    deoj: bytes
    esv: int
    opc: int
    epc: int
    pdc: int
    edt: bytes


def hex3(value: bytes) -> str:
    return value.hex().upper()


def parse_hex_byte(value: str, name: str) -> int:
    s = value.strip().lower().removeprefix("0x")
    if len(s) != 2:
        raise ValueError(f"{name} must be 1 byte hex (e.g. 80)")
    return int(s, 16)


def parse_hex_eoj(value: str) -> bytes:
    s = value.strip().lower().removeprefix("0x")
    if len(s) != 6:
        raise ValueError("EOJ must be 3 bytes hex (e.g. 028801)")
    return bytes.fromhex(s)


def build_get_frame(tid: int, deoj: bytes, epc: int) -> bytes:
    return b"".join(
        [
            bytes([EHD1, EHD2]),
            struct.pack(">H", tid),
            SEOJ_CONTROLLER,
            deoj,
            bytes([ESV_GET]),
            bytes([0x01]),  # OPC
            bytes([epc]),
            bytes([0x00]),  # PDC
        ]
    )


def parse_frame(packet: bytes) -> EchonetFrame:
    if len(packet) < 14:
        raise ValueError("frame too short")
    if packet[0] != EHD1 or packet[1] != EHD2:
        raise ValueError("not ECHONET Lite format 1")

    tid = struct.unpack(">H", packet[2:4])[0]
    seoj = packet[4:7]
    deoj = packet[7:10]
    esv = packet[10]
    opc = packet[11]

    if opc < 1:
        raise ValueError("OPC=0 not supported in this script")
    epc = packet[12]
    pdc = packet[13]
    if len(packet) < 14 + pdc:
        raise ValueError("invalid PDC")
    edt = packet[14 : 14 + pdc]

    return EchonetFrame(
        tid=tid,
        seoj=seoj,
        deoj=deoj,
        esv=esv,
        opc=opc,
        epc=epc,
        pdc=pdc,
        edt=edt,
    )


def open_socket(timeout_sec: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", ECHONET_PORT))
    sock.settimeout(timeout_sec)

    # Join ECHONET Lite multicast group to receive discovery responses.
    mreq = struct.pack("=4s4s", socket.inet_aton(ECHONET_MULTICAST), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


def send_get(sock: socket.socket, host: str, deoj: bytes, epc: int, tid: int) -> None:
    frame = build_get_frame(tid=tid, deoj=deoj, epc=epc)
    sock.sendto(frame, (host, ECHONET_PORT))


def recv_until_timeout(sock: socket.socket, seconds: float) -> list[tuple[bytes, tuple[str, int]]]:
    deadline = time.monotonic() + seconds
    out: list[tuple[bytes, tuple[str, int]]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        sock.settimeout(remaining)
        try:
            packet, addr = sock.recvfrom(2048)
            out.append((packet, addr))
        except TimeoutError:
            return out


def decode_instance_list_s(edt: bytes) -> list[str]:
    if not edt:
        return []
    count = edt[0]
    body = edt[1:]
    if len(body) < count * 3:
        return []
    return [body[i : i + 3].hex().upper() for i in range(0, count * 3, 3)]


def decode_property_map(edt: bytes) -> list[int]:
    if not edt:
        return []
    count = edt[0]
    if count <= 16:
        return list(edt[1 : 1 + count])

    # Bitmap format for 0x80-0xFF region
    if len(edt) < 17:
        return []
    bitmap = edt[1:17]
    epcs: list[int] = []
    for i in range(16):
        for bit in range(8):
            if bitmap[i] & (1 << bit):
                epc = 0x80 + i * 8 + bit
                epcs.append(epc)
    return epcs


def cmd_discover(args: argparse.Namespace) -> int:
    tid = args.tid
    with open_socket(timeout_sec=args.timeout) as sock:
        send_get(sock, ECHONET_MULTICAST, EOJ_NODE_PROFILE, epc=0xD6, tid=tid)
        packets = recv_until_timeout(sock, args.timeout)

    if not packets:
        print("No response. Check network segment, multicast reachability, and device power.")
        return 1

    seen = set()
    for packet, (host, _) in packets:
        try:
            frame = parse_frame(packet)
        except ValueError:
            continue
        key = (host, frame.seoj, frame.epc, frame.esv, frame.edt)
        if key in seen:
            continue
        seen.add(key)

        if frame.epc == 0xD6 and frame.esv in (ESV_GET_RES, ESV_INF):
            instances = decode_instance_list_s(frame.edt)
            print(f"{host} SEOJ={hex3(frame.seoj)} instances={instances}")
        else:
            print(
                f"{host} SEOJ={hex3(frame.seoj)} ESV=0x{frame.esv:02X} "
                f"EPC=0x{frame.epc:02X} EDT={binascii.hexlify(frame.edt).decode().upper()}"
            )
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    ipaddress.ip_address(args.host)
    deoj = parse_hex_eoj(args.deoj)
    epc = parse_hex_byte(args.epc, "EPC")

    with open_socket(timeout_sec=args.timeout) as sock:
        send_get(sock, args.host, deoj, epc=epc, tid=args.tid)
        packets = recv_until_timeout(sock, args.timeout)

    for packet, (host, _) in packets:
        if host != args.host:
            continue
        try:
            frame = parse_frame(packet)
        except ValueError:
            continue
        if frame.tid != args.tid:
            continue
        if frame.epc != epc:
            continue

        edt_hex = frame.edt.hex().upper()
        print(
            f"host={host} SEOJ={hex3(frame.seoj)} DEOJ={hex3(frame.deoj)} "
            f"ESV=0x{frame.esv:02X} EPC=0x{frame.epc:02X} EDT={edt_hex}"
        )
        return 0

    print("No matching response. Confirm EOJ/EPC, host IP, and timeout.")
    return 1


def cmd_get_map(args: argparse.Namespace) -> int:
    ipaddress.ip_address(args.host)
    deoj = parse_hex_eoj(args.deoj)
    epcs = [0x9D, 0x9E, 0x9F]

    rc = 0
    for epc in epcs:
        with open_socket(timeout_sec=args.timeout) as sock:
            send_get(sock, args.host, deoj, epc=epc, tid=args.tid + epc)
            packets = recv_until_timeout(sock, args.timeout)

        matched = False
        for packet, (host, _) in packets:
            if host != args.host:
                continue
            try:
                frame = parse_frame(packet)
            except ValueError:
                continue
            if frame.epc != epc:
                continue
            props = decode_property_map(frame.edt)
            print(f"EPC 0x{epc:02X}: {[f'0x{x:02X}' for x in props]}")
            matched = True
            break
        if not matched:
            print(f"EPC 0x{epc:02X}: no response")
            rc = 1
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ECHONET Lite helper for local HEMS devices")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_discover = sub.add_parser("discover", help="Discover nodes and instance list (EPC 0xD6)")
    p_discover.add_argument("--timeout", type=float, default=3.0)
    p_discover.add_argument("--tid", type=lambda x: int(x, 0), default=0x1001)
    p_discover.set_defaults(func=cmd_discover)

    p_get = sub.add_parser("get", help="Get one EPC from target EOJ")
    p_get.add_argument("--host", required=True, help="Target IPv4 address")
    p_get.add_argument("--deoj", required=True, help="Target EOJ (example: 028801)")
    p_get.add_argument("--epc", required=True, help="Target EPC (example: 80)")
    p_get.add_argument("--timeout", type=float, default=2.0)
    p_get.add_argument("--tid", type=lambda x: int(x, 0), default=0x2001)
    p_get.set_defaults(func=cmd_get)

    p_map = sub.add_parser("get-map", help="Get property maps EPC 0x9D/0x9E/0x9F")
    p_map.add_argument("--host", required=True, help="Target IPv4 address")
    p_map.add_argument("--deoj", required=True, help="Target EOJ (example: 028801)")
    p_map.add_argument("--timeout", type=float, default=2.0)
    p_map.add_argument("--tid", type=lambda x: int(x, 0), default=0x3000)
    p_map.set_defaults(func=cmd_get_map)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
