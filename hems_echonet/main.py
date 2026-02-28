from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
from datetime import datetime, timezone
from typing import Any


def parse_eoj(eoj: str) -> tuple[int, int, int]:
    raw = eoj.strip().lower().removeprefix("0x")
    if len(raw) != 6:
        raise ValueError("EOJ must be 3-byte hex (example: 028801)")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def parse_eoj_candidates(raw: str) -> list[str]:
    if not raw.strip():
        return []
    items = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not items:
        return []
    parsed: list[str] = []
    for item in items:
        gc, cc, ci = parse_eoj(item)
        parsed.append(f"{gc:02X}{cc:02X}{ci:02X}")
    return sorted(set(parsed))


def normalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def filter_by_cidr(hosts: list[str], cidr: str) -> list[str]:
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version != 4:
        raise ValueError("Only IPv4 CIDR is supported for scan-hosts.")
    filtered: list[str] = []
    for host in hosts:
        if ipaddress.ip_address(host) in network:
            filtered.append(host)
    return filtered


def list_eojs_for_host(state: dict[str, Any], host: str) -> list[str]:
    instances = state.get(host, {}).get("instances", {})
    eojs: list[str] = []
    for eoj_gc, by_cc in instances.items():
        for eoj_cc, by_ci in by_cc.items():
            for eoj_ci in by_ci.keys():
                eojs.append(f"{int(eoj_gc):02X}{int(eoj_cc):02X}{int(eoj_ci):02X}")
    eojs.sort()
    return eojs


def describe_eoj(eoj: str) -> str:
    from pychonet.lib.eojx import EOJX_CLASS
    from pychonet.lib.eojx import EOJX_GROUP

    gc, cc, _ci = parse_eoj(eoj)
    group_name = EOJX_GROUP.get(gc, f"Unknown group 0x{gc:02X}")
    class_name = EOJX_CLASS.get(gc, {}).get(cc, f"Unknown class 0x{cc:02X}")
    return f"{group_name} / {class_name}"


def describe_epc(eoj: str, epc: int) -> str:
    from pychonet.lib.epc import EPC_CODE

    gc, cc, _ci = parse_eoj(eoj)
    name = EPC_CODE.get(gc, {}).get(cc, {}).get(epc)
    if name is None:
        return f"0x{epc:02X}(unknown)"
    return f"0x{epc:02X}({name})"


def format_epc_lines(eoj: str, epcs: list[int]) -> list[str]:
    if not epcs:
        return ["      - (none)"]
    return [f"      - {describe_epc(eoj, epc)}" for epc in epcs]


def get_instance_maps(state: dict[str, Any], host: str, eoj: str) -> tuple[list[int], list[int], list[int]]:
    gc, cc, ci = parse_eoj(eoj)
    instance = state.get(host, {}).get("instances", {}).get(gc, {}).get(cc, {}).get(ci, {})
    stat_map = instance.get(0x9D, []) or []
    set_map = instance.get(0x9E, []) or []
    get_map = instance.get(0x9F, []) or []
    return list(stat_map), list(set_map), list(get_map)


def get_instance_state(
    state: dict[str, Any], host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int
) -> dict[int, Any]:
    return (
        state.get(host, {})
        .get("instances", {})
        .get(eoj_gc, {})
        .get(eoj_cc, {})
        .get(eoj_ci, {})
    )


async def fetch_current_raw_payload(
    client: Any, host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int
) -> dict[str, Any]:
    from pychonet.lib.const import GET

    state = getattr(client, "_state", {})
    instance = get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
    get_map = sorted(set(instance.get(0x9F, []) or []))
    if not get_map:
        return {}

    payload: dict[str, Any] = {}
    failures: list[str] = []
    for epc in get_map:
        key = f"0x{epc:02X}"
        try:
            ok = await client.echonetMessage(
                host,
                eoj_gc,
                eoj_cc,
                eoj_ci,
                GET,
                [{"EPC": epc}],
            )
            if not ok:
                failures.append(f"{key}:timeout")
                continue
            state = getattr(client, "_state", {})
            instance = get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
            value = instance.get(epc)
            if isinstance(value, (bytes, bytearray)):
                payload[key] = value.hex().upper()
            else:
                payload[key] = value
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{key}:{type(exc).__name__}")

    if failures:
        payload["_errors"] = failures
    return payload


async def collect_loop(args: argparse.Namespace) -> int:
    from pychonet import ECHONETAPIClient
    from pychonet import Factory
    from pychonet.lib.udpserver import UDPServer

    eoj_gc, eoj_cc, eoj_ci = parse_eoj(args.eoj)

    udp = UDPServer()
    loop = asyncio.get_running_loop()
    udp.run(args.listen_host, args.listen_port, loop=loop)
    client = ECHONETAPIClient(server=udp)

    await client.discover(args.host)
    await client.getAllPropertyMaps(args.host, eoj_gc, eoj_cc, eoj_ci)
    device = Factory(args.host, client, eoj_gc, eoj_cc, eoj_ci)

    while True:
        try:
            payload = await device.update()
            if not isinstance(payload, dict):
                payload = {"value": str(payload)}
        except Exception as exc:  # noqa: BLE001
            print(f"collect warn: update() failed ({type(exc).__name__}: {exc}), fallback to raw EPC")
            try:
                payload = await fetch_current_raw_payload(client, args.host, eoj_gc, eoj_cc, eoj_ci)
                if not payload:
                    payload = {"value": "no data (empty get-map)"}
            except Exception as raw_exc:  # noqa: BLE001
                print(f"collect error: {type(raw_exc).__name__}: {raw_exc}")
                if args.once:
                    break
                await asyncio.sleep(args.interval)
                continue

        try:
            now = datetime.now(timezone.utc).isoformat()
            print(f"host={args.host} eoj={args.eoj} at={now}")
            print(json.dumps(normalize_json(payload), ensure_ascii=False, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            print(f"collect error: {type(exc).__name__}: {exc}")

        if args.once:
            break
        await asyncio.sleep(args.interval)
    return 0


async def scan_hosts_loop(args: argparse.Namespace) -> int:
    from pychonet import ECHONETAPIClient
    from pychonet.lib.const import ENL_MULTICAST_ADDRESS
    from pychonet.lib.udpserver import UDPServer

    eoj_candidates = parse_eoj_candidates(args.eoj)
    udp = UDPServer()
    loop = asyncio.get_running_loop()
    udp.run(args.listen_host, args.listen_port, loop=loop)
    client = ECHONETAPIClient(server=udp)
    discovered_hosts: set[str] = set()

    async def on_unknown_host(host: str) -> None:
        if host in {ENL_MULTICAST_ADDRESS, args.listen_host}:
            return
        discovered_hosts.add(host)

    client._discover_callback = on_unknown_host

    # Multicast discovery to 224.0.23.0, then inspect discovered hosts.
    if args.verbose:
        print(
            "multicast discovery started "
            f"(wait={args.discovery_wait:.1f}s, target=224.0.23.0:3610)"
        )
    discover_task = asyncio.create_task(client.discover())
    step = 0.5
    elapsed = 0.0
    while elapsed < args.discovery_wait:
        await asyncio.sleep(step)
        elapsed = min(args.discovery_wait, elapsed + step)
        if args.verbose:
            current = len(discovered_hosts)
            print(
                f"discovery progress: {elapsed:.1f}/{args.discovery_wait:.1f}s "
                f"responded_hosts={current}"
            )

    if not discover_task.done():
        discover_task.cancel()
        try:
            await discover_task
        except asyncio.CancelledError:
            pass
    else:
        # Consume result/exception to avoid task warnings.
        _ = discover_task.exception()
    state = getattr(client, "_state", {})
    initial_hosts = set(discovered_hosts)
    hosts = sorted(initial_hosts)

    if args.cidr:
        hosts = filter_by_cidr(hosts, args.cidr)
    if args.limit and args.limit > 0:
        hosts = hosts[: args.limit]

    if not hosts:
        message = "no ECHONET host responded to multicast discovery"
        if args.cidr:
            message += f" in {args.cidr}"
        print(message)
        return 1

    if args.verbose:
        print(f"enriching node profile by unicast discover(host): {len(hosts)} host(s)")
    for idx, host in enumerate(hosts, start=1):
        try:
            await asyncio.wait_for(client.discover(host), timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            if args.verbose:
                print(f"discover(host) failed host={host} ({type(exc).__name__}: {exc})")
        if args.verbose and idx % 20 == 0:
            print(f"discover(host) progress: {idx}/{len(hosts)}")

    state = getattr(client, "_state", {})

    for host in hosts:
        eojs = list_eojs_for_host(state, host)
        print(f"discovered host={host}")
        if not eojs:
            print("  - (no EOJ instances)")
            continue
        for eoj in eojs:
            print(f"  - eoj={eoj} desc={describe_eoj(eoj)}")
            try:
                gc, cc, ci = parse_eoj(eoj)
                await asyncio.wait_for(
                    client.getAllPropertyMaps(host, gc, cc, ci),
                    timeout=args.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                if args.verbose:
                    print(f"    maps: failed ({type(exc).__name__}: {exc})")
                continue

            state = getattr(client, "_state", {})
            stat_map, set_map, get_map = get_instance_maps(state, host, eoj)
            stat_map = sorted(set(stat_map))
            set_map = sorted(set(set_map))
            get_map = sorted(set(get_map))
            print("    inf-map(0x9D):")
            for line in format_epc_lines(eoj, stat_map):
                print(line)
            print("    set-map(0x9E):")
            for line in format_epc_lines(eoj, set_map):
                print(line)
            print("    get-map(0x9F):")
            for line in format_epc_lines(eoj, get_map):
                print(line)

    if not eoj_candidates:
        print("object listing complete")
        return 0

    listed = 0
    for host in hosts:
        eojs = list_eojs_for_host(state, host)
        if eoj_candidates:
            eojs = [eoj for eoj in eojs if eoj in eoj_candidates]
            if not eojs:
                continue
        listed += 1
        print(host)

    if listed == 0:
        if eoj_candidates:
            print(f"no host matched eojs={eoj_candidates}")
        else:
            print("no host has instance list")
        return 1
    print(f"scan complete: {listed} host(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and display current ECHONET Lite values with pychonet"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Fetch and display current values")
    p_collect.add_argument("--host", required=True, help="Target device IPv4 address")
    p_collect.add_argument("--eoj", default="028801", help="Target EOJ hex (default: 028801)")
    p_collect.add_argument("--listen-host", default="0.0.0.0", help="UDP bind host")
    p_collect.add_argument("--listen-port", type=int, default=3610, help="UDP bind port")
    p_collect.add_argument("--interval", type=float, default=30.0, help="Polling interval seconds")
    p_collect.add_argument("--once", action="store_true", help="Collect only once")
    p_collect.set_defaults(func=None)

    p_scan = sub.add_parser(
        "scan-hosts",
        help="Discover hosts by multicast and list reachable ECHONET hosts",
    )
    p_scan.add_argument(
        "--cidr",
        default="",
        help="Optional IPv4 CIDR filter for discovered hosts (example: 192.168.1.0/24)",
    )
    p_scan.add_argument(
        "--eoj",
        default="",
        help="Optional EOJ filter list (comma-separated). Omit to list all discovered objects.",
    )
    p_scan.add_argument("--listen-host", default="0.0.0.0", help="UDP bind host")
    p_scan.add_argument("--listen-port", type=int, default=3610, help="UDP bind port")
    p_scan.add_argument(
        "--discovery-wait",
        type=float,
        default=2.0,
        help="Seconds to wait for multicast discovery responses",
    )
    p_scan.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Timeout seconds for discover(host) and getAllPropertyMaps",
    )
    p_scan.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max discovered hosts to list (0 = all discovered hosts)",
    )
    p_scan.add_argument("--verbose", action="store_true", help="Show multicast discovery progress")
    p_scan.set_defaults(func=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "collect":
        return asyncio.run(collect_loop(args))
    if args.cmd == "scan-hosts":
        return asyncio.run(scan_hosts_loop(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
