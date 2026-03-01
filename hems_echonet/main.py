from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
from pathlib import Path
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


class MRAResolver:
    def __init__(self, root_dir: str = "") -> None:
        self._loaded = False
        self._class_epc_info: dict[str, dict[int, dict[str, str]]] = {}
        if root_dir.strip():
            self._load(Path(root_dir))

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def class_count(self) -> int:
        return len(self._class_epc_info)

    def resolve(self, eoj: str, epc: int) -> dict[str, str] | None:
        gc, cc, _ci = parse_eoj(eoj)
        class_code = f"{gc:02X}{cc:02X}"
        if class_code in self._class_epc_info and epc in self._class_epc_info[class_code]:
            return self._class_epc_info[class_code][epc]
        # Fallback to super class definition when available.
        if "0000" in self._class_epc_info and epc in self._class_epc_info["0000"]:
            return self._class_epc_info["0000"][epc]
        return None

    def _load(self, root: Path) -> None:
        if not root.exists() or not root.is_dir():
            return
        for path in root.rglob("*.json"):
            class_code = self._class_code_from_path(path)
            if class_code is None:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            epc_map = self._extract_epc_map(data)
            if not epc_map:
                continue
            current = self._class_epc_info.setdefault(class_code, {})
            for epc, info in epc_map.items():
                if epc not in current:
                    current[epc] = info
        self._loaded = bool(self._class_epc_info)

    @staticmethod
    def _class_code_from_path(path: Path) -> str | None:
        matches = re.findall(r"0x([0-9A-Fa-f]{4})", str(path))
        if matches:
            return matches[-1].upper()
        stem_match = re.fullmatch(r"([0-9A-Fa-f]{4})", path.stem)
        if stem_match:
            return stem_match.group(1).upper()
        return None

    @classmethod
    def _extract_epc_map(cls, data: Any) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        if not isinstance(data, dict):
            return out

        # Common MRA layout: "elProperties": [{ "epc": "0x80", "propertyName": {...} }, ...]
        el_props = data.get("elProperties")
        if isinstance(el_props, list):
            for prop in el_props:
                if not isinstance(prop, dict):
                    continue
                epc = cls._parse_epc(prop.get("epc"))
                if epc is None:
                    continue
                name = cls._extract_name(prop)
                description = cls._extract_description(prop)
                if name:
                    out[epc] = {"name": name, "description": description}

        # Generic fallback: recursively search for dicts containing epc + name-like fields.
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                epc = cls._parse_epc(
                    node.get("epc") or node.get("propertyCode") or node.get("code")
                )
                if epc is not None:
                    name = cls._extract_name(node)
                    description = cls._extract_description(node)
                    if name and epc not in out:
                        out[epc] = {"name": name, "description": description}
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return out

    @staticmethod
    def _parse_epc(value: Any) -> int | None:
        if isinstance(value, int):
            return value if 0 <= value <= 0xFF else None
        if isinstance(value, str):
            s = value.strip().upper()
            if s.startswith("0X"):
                s = s[2:]
            if re.fullmatch(r"[0-9A-F]{2}", s):
                return int(s, 16)
        return None

    @staticmethod
    def _extract_name(node: dict[str, Any]) -> str | None:
        candidates = [
            node.get("propertyName"),
            node.get("name"),
            node.get("shortName"),
            node.get("property"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, dict):
                for key in ("ja", "JA", "en", "EN"):
                    val = candidate.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        return None

    @staticmethod
    def _extract_description(node: dict[str, Any]) -> str:
        candidate = node.get("descriptions") or node.get("description")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            for key in ("ja", "JA", "en", "EN"):
                val = candidate.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""


def resolve_epc_info(eoj: str, epc: int, mra: MRAResolver | None = None) -> dict[str, str]:
    from pychonet.lib.epc import EPC_CODE

    if mra is not None:
        mra_info = mra.resolve(eoj, epc)
        if mra_info is not None:
            return {
                "name": mra_info.get("name", ""),
                "description": mra_info.get("description", ""),
                "source": "mra",
            }

    gc, cc, _ci = parse_eoj(eoj)
    name = EPC_CODE.get(gc, {}).get(cc, {}).get(epc)
    if name is None:
        return {"name": "unknown", "description": "", "source": "none"}
    return {"name": name, "description": "", "source": "pychonet"}


def format_epc_lines(eoj: str, epcs: list[int], mra: MRAResolver | None = None) -> list[str]:
    if not epcs:
        return ["      - (none)"]
    lines: list[str] = []
    for epc in epcs:
        info = resolve_epc_info(eoj, epc, mra)
        base = f"      - 0x{epc:02X}({info['name']})"
        if info["description"]:
            base += f" : {info['description']}"
        lines.append(base)
    return lines


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
    client: Any,
    host: str,
    eoj_gc: int,
    eoj_cc: int,
    eoj_ci: int,
    max_opc_per_request: int,
) -> dict[str, Any]:
    from pychonet.lib.const import GET

    state = getattr(client, "_state", {})
    instance = get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
    get_map = sorted(set(instance.get(0x9F, []) or []))
    if not get_map:
        return {}

    payload: dict[str, Any] = {}
    failures: list[str] = []
    batch_size = max(1, max_opc_per_request)
    for start in range(0, len(get_map), batch_size):
        chunk = get_map[start : start + batch_size]
        opc = [{"EPC": epc} for epc in chunk]
        try:
            ok = await client.echonetMessage(
                host,
                eoj_gc,
                eoj_cc,
                eoj_ci,
                GET,
                opc,
            )
            if not ok:
                raise TimeoutError("chunk timeout")
            state = getattr(client, "_state", {})
            instance = get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
            for epc in chunk:
                key = f"0x{epc:02X}"
                value = instance.get(epc)
                if isinstance(value, (bytes, bytearray)):
                    payload[key] = value.hex().upper()
                else:
                    payload[key] = value
        except Exception:
            # Fallback to single EPC requests for this failed chunk.
            for epc in chunk:
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
                continue

    for epc in get_map:
        key = f"0x{epc:02X}"
        if key in payload:
            continue
        failures.append(f"{key}:no-data")

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
    state = getattr(client, "_state", {})
    instance = get_instance_state(state, args.host, eoj_gc, eoj_cc, eoj_ci)
    get_map_size = len(set(instance.get(0x9F, []) or []))
    use_raw_mode = get_map_size > args.max_update_opc
    if use_raw_mode:
        print(
            "collect info: raw EPC mode enabled "
            f"(get-map size={get_map_size}, max-update-opc={args.max_update_opc})"
        )

    while True:
        if use_raw_mode:
            try:
                payload = await fetch_current_raw_payload(
                    client,
                    args.host,
                    eoj_gc,
                    eoj_cc,
                    eoj_ci,
                    args.max_update_opc,
                )
                if not payload:
                    payload = {"value": "no data (empty get-map)"}
            except Exception as raw_exc:  # noqa: BLE001
                print(f"collect error: {type(raw_exc).__name__}: {raw_exc}")
                if args.once:
                    break
                await asyncio.sleep(args.interval)
                continue
        else:
            try:
                payload = await device.update()
                if not isinstance(payload, dict):
                    payload = {"value": str(payload)}
            except Exception as exc:  # noqa: BLE001
                print(
                    "collect info: switching to raw EPC mode "
                    f"because update() failed ({type(exc).__name__}: {exc})"
                )
                use_raw_mode = True
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
    mra = MRAResolver(args.mra_dir)
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
    if args.mra_dir:
        mra_path = str(Path(args.mra_dir).resolve())
        if mra.loaded:
            print(f"mra loaded: classes={mra.class_count} dir={mra_path}")
        else:
            print(f"mra not loaded from dir={mra_path}")

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
            for line in format_epc_lines(eoj, stat_map, mra):
                print(line)
            print("    set-map(0x9E):")
            for line in format_epc_lines(eoj, set_map, mra):
                print(line)
            print("    get-map(0x9F):")
            for line in format_epc_lines(eoj, get_map, mra):
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
    p_collect.add_argument(
        "--max-update-opc",
        type=int,
        default=24,
        help="Max OPC count per GET request in raw mode; also used as update()->raw switch threshold",
    )
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
    p_scan.add_argument(
        "--mra-dir",
        default="",
        help="Optional path to extracted MRA JSON directory for EPC name resolution",
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
