"""Command-line tool for poking at ECHONET Lite devices without Home Assistant.

Uses the same core as the integration. Examples:

    uv run scripts/echonet_cli.py scan
    uv run scripts/echonet_cli.py dump --host 192.168.1.20
    uv run scripts/echonet_cli.py get 192.168.1.20 028701 0xC0 0xC2
    uv run scripts/echonet_cli.py set 192.168.1.20 013001 0x80 31
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "echonetlite_jp")
)

from echonet.client import EPC_INSTALLATION_LOCATION, EchonetClient
from echonet.codec import to_plain
from echonet.device import EchonetDevice
from echonet.errors import EchonetError
from echonet.location import decode_location
from echonet.mra import MraCatalog
from echonet.network import EchonetNetwork
from echonet.protocol import EOJ
from echonet.quirks import DeviceKey
from echonet.transport import EchonetTransport


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interface", help="local IPv4 address used for multicast")
    parser.add_argument("--timeout", type=float, default=3.0, help="request timeout (s)")
    parser.add_argument("--lang", default="ja", choices=("ja", "en"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list nodes, objects and property maps")
    scan.add_argument("--host", action="append", help="query this host instead of multicast")
    scan.add_argument("--wait", type=float, default=3.0, help="multicast wait (s)")

    dump = sub.add_parser("dump", help="read every property of every object as JSON")
    dump.add_argument("--host", action="append")
    dump.add_argument("--wait", type=float, default=3.0)

    get = sub.add_parser("get", help="read properties of one object")
    get.add_argument("host")
    get.add_argument("eoj", type=EOJ.parse)
    get.add_argument("epcs", nargs="*", type=lambda v: int(v, 16), help="EPCs (default: all)")

    set_ = sub.add_parser("set", help="write raw EDT to a property")
    set_.add_argument("host")
    set_.add_argument("eoj", type=EOJ.parse)
    set_.add_argument("epc", type=lambda v: int(v, 16))
    set_.add_argument("edt", type=lambda v: bytes.fromhex(v.lower().removeprefix("0x")))

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return asyncio.run(_run(args))
    except (EchonetError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace) -> int:
    transport = EchonetTransport(interface=args.interface)
    await transport.open()
    try:
        client = EchonetClient(transport, timeout=args.timeout)
        catalog = MraCatalog()
        if args.command in ("scan", "dump"):
            network = EchonetNetwork(
                client,
                catalog,
                hosts=args.host,
                discovery_wait=args.wait,
                include_user_defined=True,
            )
            await network.async_discover()
            if args.command == "dump":
                await network.async_poll()
                print(
                    json.dumps(
                        [_describe(d, args.lang) for d in network.devices.values()],
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
            else:
                _print_scan(network, args.lang)
            return 0
        if args.command == "set":
            await client.set(args.host, args.eoj, {args.epc: args.edt})
            print(f"0x{args.epc:02X} <- {args.edt.hex().upper()}: OK")
            return 0
        info = await client.get_object(args.host, args.eoj)
        key = DeviceKey(args.eoj.class_code, info.manufacturer_code, info.product_code)
        device = EchonetDevice("cli", info, catalog.get_device_class(key))
        # Read everything readable so that coefficients (e.g. units) apply.
        result = await client.get(args.host, args.eoj, {*info.maps.get, *args.epcs})
        device.values = result.values
        for epc in args.epcs or sorted(info.maps.get):
            print(_format_property(device, epc, args.lang))
        return 0
    finally:
        transport.close()


def _print_scan(network: EchonetNetwork, lang: str) -> None:
    if not network.devices:
        print("no devices found")
    for device in sorted(network.devices.values(), key=lambda d: (d.host, d.eoj)):
        maps = device.info.maps
        print(f"{device.host}  {device.eoj}  {device.class_def.name.get(lang)}")
        code = device.info.manufacturer_code
        print(
            f"    uid={device.node_uid} maker={device.info.manufacturer}"
            f" ({f'0x{code:06X}' if code is not None else '-'})"
            f" product={device.info.product_code}"
        )
        if device.class_def.quirks:
            print(f"    quirks={', '.join(device.class_def.quirks)}")
        print(f"    get={_epcs(maps.get)}\n    set={_epcs(maps.set)}\n    inf={_epcs(maps.inf)}")


def _describe(device: EchonetDevice, lang: str) -> dict[str, Any]:
    props = {}
    for epc, edt in sorted(device.values.items()):
        prop = device.class_def.get(epc)
        props[f"0x{epc:02X}"] = {
            "name": prop.name.get(lang) if prop else None,
            "edt": edt.hex().upper(),
            "value": _value(device, epc, lang),
        }
    return {
        "host": device.host,
        "eoj": str(device.eoj),
        "class": device.class_def.name.get(lang),
        "uid": device.node_uid,
        "manufacturer": device.info.manufacturer,
        "product_code": device.info.product_code,
        "quirks": list(device.class_def.quirks),
        "properties": props,
        "derived": device.derived,
    }


def _format_property(device: EchonetDevice, epc: int, lang: str) -> str:
    prop = device.class_def.get(epc)
    name = prop.name.get(lang) if prop else "?"
    edt = device.values.get(epc)
    if edt is None:
        return f"0x{epc:02X} {name}: (no value)"
    return f"0x{epc:02X} {name}: {edt.hex().upper()} -> {_value(device, epc, lang)}"


def _value(device: EchonetDevice, epc: int, lang: str) -> Any:
    if epc == EPC_INSTALLATION_LOCATION:
        location = decode_location(device.values.get(epc, b""))
        return location.get(lang) if location else None
    return to_plain(device.decode(epc), lang)


def _epcs(epcs: frozenset[int]) -> str:
    return " ".join(f"{e:02X}" for e in sorted(epcs)) or "-"


if __name__ == "__main__":
    sys.exit(main())
