from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import sqlite3
import time
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


def init_registry_db(conn: Any) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_uid TEXT PRIMARY KEY,
            manufacturer TEXT,
            product_code TEXT,
            serial_number TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
        )
        cur.execute(
        """
        CREATE TABLE IF NOT EXISTS device_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_uid TEXT NOT NULL,
            ip TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(device_uid, ip),
            FOREIGN KEY(device_uid) REFERENCES devices(device_uid)
        )
        """
        )
        cur.execute(
        """
        CREATE TABLE IF NOT EXISTS samples_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT NOT NULL,
            device_uid TEXT NOT NULL,
            ip TEXT NOT NULL,
            eoj TEXT NOT NULL,
            epc_code SMALLINT,
            epc_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            FOREIGN KEY(device_uid) REFERENCES devices(device_uid)
        )
        """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_raw_uid_time ON samples_raw(device_uid, collected_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_addresses_uid_active ON device_addresses(device_uid, active)"
        )
    finally:
        cur.close()
    conn.commit()


def resolve_device_uid(state: dict[str, Any], host: str) -> str:
    node = state.get(host, {})
    uid = node.get("uid")
    if uid is None or uid == "":
        # Fallback: still deterministic per host in worst case.
        return f"host:{host}"
    return str(uid)


def upsert_device_registry(
    conn: Any,
    state: dict[str, Any],
    host: str,
    now_iso: str,
) -> str:
    node = state.get(host, {})
    uid = resolve_device_uid(state, host)
    manufacturer = node.get("manufacturer")
    product_code = node.get("product_code")
    serial_number = None

    if isinstance(manufacturer, (dict, list)):
        manufacturer = json.dumps(manufacturer, ensure_ascii=False)
    if manufacturer is not None:
        manufacturer = str(manufacturer)
    if product_code is not None:
        product_code = str(product_code)

    cur = conn.cursor()
    try:
        cur.execute(
        """
        INSERT INTO devices(device_uid, manufacturer, product_code, serial_number, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_uid) DO UPDATE SET
            manufacturer=COALESCE(EXCLUDED.manufacturer, devices.manufacturer),
            product_code=COALESCE(EXCLUDED.product_code, devices.product_code),
            serial_number=COALESCE(EXCLUDED.serial_number, devices.serial_number),
            last_seen=EXCLUDED.last_seen
        """,
        (uid, manufacturer, product_code, serial_number, now_iso, now_iso),
        )
        cur.execute("UPDATE device_addresses SET active=0 WHERE device_uid=?", (uid,))
        cur.execute(
        """
        INSERT INTO device_addresses(device_uid, ip, first_seen, last_seen, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(device_uid, ip) DO UPDATE SET
            last_seen=EXCLUDED.last_seen,
            active=1
        """,
        (uid, host, now_iso, now_iso),
        )
    finally:
        cur.close()
    conn.commit()
    return uid


def parse_epc_key(key: Any) -> tuple[int | None, str]:
    if isinstance(key, int):
        return key, f"0x{key:02X}"
    if isinstance(key, str):
        s = key.strip()
        if re.fullmatch(r"0x[0-9A-Fa-f]{2}", s):
            return int(s, 16), s.upper()
        return None, s
    return None, str(key)


def save_raw_samples(
    conn: Any,
    collected_at: str,
    device_uid: str,
    host: str,
    eoj: str,
    payload: dict[str, Any],
) -> int:
    rows = 0
    cur = conn.cursor()
    try:
        for k, v in payload.items():
            epc_code, epc_key = parse_epc_key(k)
            cur.execute(
            """
            INSERT INTO samples_raw(collected_at, device_uid, ip, eoj, epc_code, epc_key, value_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collected_at,
                device_uid,
                host,
                eoj.upper(),
                epc_code,
                epc_key,
                json.dumps(normalize_json(v), ensure_ascii=False),
            ),
            )
            rows += 1
    finally:
        cur.close()
    conn.commit()
    return rows


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


def describe_eoj(eoj: str, mra: "MRAResolver | None" = None) -> str:
    from pychonet.lib.eojx import EOJX_CLASS
    from pychonet.lib.eojx import EOJX_GROUP

    gc, cc, _ci = parse_eoj(eoj)
    group_name = EOJX_GROUP.get(gc, f"Unknown group 0x{gc:02X}")
    if mra is not None:
        class_name = mra.resolve_class_name(eoj)
        if class_name:
            return f"{group_name} / {class_name}"
    class_name = EOJX_CLASS.get(gc, {}).get(cc, f"Unknown class 0x{cc:02X}")
    return f"{group_name} / {class_name}"


class MRAResolver:
    def __init__(self, root_dir: str = "") -> None:
        self._loaded = False
        self._class_epc_info: dict[str, dict[int, dict[str, str]]] = {}
        self._class_names: dict[str, str] = {}
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

    def resolve_class_name(self, eoj: str) -> str | None:
        gc, cc, _ci = parse_eoj(eoj)
        return self._class_names.get(f"{gc:02X}{cc:02X}")

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
            class_name = self._extract_class_name(data)
            if class_name and class_code not in self._class_names:
                self._class_names[class_code] = class_name
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
    def _extract_class_name(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        class_name = data.get("className")
        if isinstance(class_name, str) and class_name.strip():
            return class_name.strip()
        if isinstance(class_name, dict):
            for key in ("ja", "JA", "en", "EN"):
                value = class_name.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

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


async def refresh_device_profile(
    client: Any,
    host: str,
    eoj_gc: int,
    eoj_cc: int,
    eoj_ci: int,
    timeout_sec: float,
) -> None:
    await asyncio.wait_for(client.discover(host), timeout=timeout_sec)
    await asyncio.wait_for(client.getAllPropertyMaps(host, eoj_gc, eoj_cc, eoj_ci), timeout=timeout_sec)


async def discover_hosts_for_collect(client: Any, args: argparse.Namespace) -> list[str]:
    from pychonet.lib.const import ENL_MULTICAST_ADDRESS

    if args.host:
        return [args.host]

    discovered_hosts: set[str] = set()

    async def on_unknown_host(host: str) -> None:
        if host in {ENL_MULTICAST_ADDRESS, args.listen_host}:
            return
        discovered_hosts.add(host)

    client._discover_callback = on_unknown_host
    if args.verbose:
        print(
            "collect discovery started "
            f"(wait={args.discovery_wait:.1f}s, target=224.0.23.0:3610)"
        )
    discover_task = asyncio.create_task(client.discover())
    step = 0.5
    elapsed = 0.0
    while elapsed < args.discovery_wait:
        await asyncio.sleep(step)
        elapsed = min(args.discovery_wait, elapsed + step)
        if args.verbose:
            print(
                f"collect discovery progress: {elapsed:.1f}/{args.discovery_wait:.1f}s "
                f"responded_hosts={len(discovered_hosts)}"
            )
    if not discover_task.done():
        discover_task.cancel()
        try:
            await discover_task
        except asyncio.CancelledError:
            pass
    return sorted(discovered_hosts)


async def discover_targets_for_collect(
    client: Any, hosts: list[str], eoj_filter: list[str], args: argparse.Namespace
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for host in hosts:
        try:
            await asyncio.wait_for(client.discover(host), timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            if args.verbose:
                print(f"collect warn: discover(host) failed host={host} ({type(exc).__name__}: {exc})")
            continue
        state = getattr(client, "_state", {})
        eojs = list_eojs_for_host(state, host)
        if eoj_filter:
            eojs = [e for e in eojs if e in eoj_filter]
        for eoj in eojs:
            gc, cc, ci = parse_eoj(eoj)
            try:
                await asyncio.wait_for(client.getAllPropertyMaps(host, gc, cc, ci), timeout=args.timeout)
                targets.append((host, eoj))
            except Exception as exc:  # noqa: BLE001
                if args.verbose:
                    print(
                        f"collect warn: getAllPropertyMaps failed host={host} eoj={eoj} "
                        f"({type(exc).__name__}: {exc})"
                    )
    return targets


async def collect_loop(args: argparse.Namespace) -> int:
    from pychonet import ECHONETAPIClient
    from pychonet.lib.udpserver import UDPServer

    udp = UDPServer()
    loop = asyncio.get_running_loop()
    udp.run(args.listen_host, args.listen_port, loop=loop)
    client = ECHONETAPIClient(server=udp)
    eoj_filter = parse_eoj_candidates(args.eoj)
    hosts = await discover_hosts_for_collect(client, args)
    if args.cidr:
        hosts = filter_by_cidr(hosts, args.cidr)
    if not hosts:
        print("collect error: no hosts discovered")
        return 1
    targets = await discover_targets_for_collect(client, hosts, eoj_filter, args)
    if not targets:
        print("collect error: no collectable EOJ targets discovered")
        return 1
    if args.verbose:
        print(f"collect targets: {len(targets)}")
        for host, eoj in targets:
            print(f"  - host={host} eoj={eoj}")

    conn: sqlite3.Connection | None = None
    uid_by_host: dict[str, str] = {}
    if args.db_path:
        conn = sqlite3.connect(args.db_path)
        init_registry_db(conn)
        state = getattr(client, "_state", {})
        now_iso = datetime.now(timezone.utc).isoformat()
        for host in hosts:
            uid = upsert_device_registry(conn, state, host, now_iso)
            uid_by_host[host] = uid
            if args.verbose:
                print(f"registry: device_uid={uid} host={host}")
    else:
        print("collect info: running without DB persistence (--db-path not set)")

    print("collect info: raw EPC mode fixed (update() is not used)")

    next_refresh_at = (
        time.monotonic() + args.refresh_interval if args.refresh_interval > 0 else None
    )

    while True:
        if next_refresh_at is not None and time.monotonic() >= next_refresh_at:
            try:
                hosts = await discover_hosts_for_collect(client, args)
                if args.cidr:
                    hosts = filter_by_cidr(hosts, args.cidr)
                targets = await discover_targets_for_collect(client, hosts, eoj_filter, args)
                if args.verbose:
                    print("collect info: periodic profile refresh completed")
            except Exception as exc:  # noqa: BLE001
                print(f"collect warn: periodic profile refresh failed ({type(exc).__name__}: {exc})")
            next_refresh_at = time.monotonic() + args.refresh_interval

        for host, eoj in targets:
            eoj_gc, eoj_cc, eoj_ci = parse_eoj(eoj)
            try:
                payload = await fetch_current_raw_payload(
                    client,
                    host,
                    eoj_gc,
                    eoj_cc,
                    eoj_ci,
                    args.max_update_opc,
                )
                if not payload:
                    payload = {"value": "no data (empty get-map)"}
            except Exception as raw_exc:  # noqa: BLE001
                print(f"collect error: host={host} eoj={eoj} {type(raw_exc).__name__}: {raw_exc}")
                if args.rediscover_on_error:
                    try:
                        await refresh_device_profile(client, host, eoj_gc, eoj_cc, eoj_ci, args.timeout)
                        print(f"collect info: rediscover triggered host={host} eoj={eoj}")
                    except Exception as rediscover_exc:  # noqa: BLE001
                        print(
                            "collect warn: rediscover failed "
                            f"host={host} eoj={eoj} ({type(rediscover_exc).__name__}: {rediscover_exc})"
                        )
                continue

            try:
                now = datetime.now(timezone.utc).isoformat()
                print(f"host={host} eoj={eoj} at={now}")
                print(json.dumps(normalize_json(payload), ensure_ascii=False, sort_keys=True))
                if conn is not None:
                    uid = uid_by_host.get(host)
                    if uid is None:
                        state = getattr(client, "_state", {})
                        uid = upsert_device_registry(conn, state, host, now)
                        uid_by_host[host] = uid
                    rows = save_raw_samples(conn, now, uid, host, eoj, payload)
                    print(f"saved samples_raw rows={rows}")
            except Exception as exc:  # noqa: BLE001
                print(f"collect error: host={host} eoj={eoj} {type(exc).__name__}: {exc}")

        if args.once:
            break
        await asyncio.sleep(args.interval)
    if conn is not None:
        conn.close()
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
            print(f"  - eoj={eoj} desc={describe_eoj(eoj, mra)}")
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
    default_mra_dir = str((Path(__file__).resolve().parent.parent / "mra"))

    parser = argparse.ArgumentParser(
        description="Collect and display current ECHONET Lite values"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Fetch and display current values")
    p_collect.add_argument("--host", default="", help="Target device IPv4 address (optional; omit for auto-discovery)")
    p_collect.add_argument(
        "--eoj",
        default="",
        help="Target EOJ list, comma-separated (optional; omit to collect all discovered EOJs)",
    )
    p_collect.add_argument(
        "--cidr",
        default="",
        help="Optional IPv4 CIDR filter for discovered hosts (used when --host is omitted)",
    )
    p_collect.add_argument(
        "--db-path",
        default="hems_registry.sqlite3",
        help="SQLite DB path (default: hems_registry.sqlite3). Set empty to disable persistence.",
    )
    p_collect.add_argument("--listen-host", default="0.0.0.0", help="UDP bind host")
    p_collect.add_argument("--listen-port", type=int, default=3610, help="UDP bind port")
    p_collect.add_argument("--interval", type=float, default=30.0, help="Polling interval seconds")
    p_collect.add_argument(
        "--discovery-wait",
        type=float,
        default=2.0,
        help="Seconds to wait for multicast discovery responses when --host is omitted",
    )
    p_collect.add_argument(
        "--max-update-opc",
        type=int,
        default=24,
        help="Max OPC count per GET request in raw mode",
    )
    p_collect.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Timeout seconds for discover(host) / getAllPropertyMaps / rediscover",
    )
    p_collect.add_argument(
        "--refresh-interval",
        type=float,
        default=86400.0,
        help="Profile refresh interval seconds (default: 86400; <=0 to disable)",
    )
    p_collect.add_argument(
        "--rediscover-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Trigger rediscover+getAllPropertyMaps on collect errors",
    )
    p_collect.add_argument("--verbose", action="store_true", help="Show collector progress logs")
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
        default=default_mra_dir,
        help=f"Path to extracted MRA JSON directory for EPC name resolution (default: {default_mra_dir})",
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
