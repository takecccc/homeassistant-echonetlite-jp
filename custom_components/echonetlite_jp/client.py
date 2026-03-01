from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Any

from pychonet import ECHONETAPIClient
from pychonet.lib.const import ENL_MULTICAST_ADDRESS
from pychonet.lib.const import GET
from pychonet.lib.udpserver import UDPServer

from .mra import MRAClassResolver


@dataclass(frozen=True)
class Target:
    host: str
    eoj: str
    uid: str
    manufacturer: str | None
    device_name: str | None
    product_code: str | None
    eoj_desc: str

    @property
    def key(self) -> str:
        return f"{self.uid}-{self.eoj}"


class HemsEchonetClient:
    def __init__(
        self,
        *,
        host: str,
        eoj: str,
        cidr: str,
        listen_host: str,
        listen_port: int,
        discovery_wait: float,
        timeout: float,
        refresh_interval: float,
        max_opc: int,
        rediscover_on_error: bool,
        mra_dir: str,
        debug: bool,
    ) -> None:
        self._host = host.strip()
        self._eoj_filter = self._parse_eoj_candidates(eoj)
        self._cidr = cidr.strip()
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._discovery_wait = discovery_wait
        self._timeout = timeout
        self._refresh_interval = refresh_interval
        self._max_opc = max(1, max_opc)
        self._rediscover_on_error = rediscover_on_error
        self._debug = debug
        self._mra = MRAClassResolver(mra_dir)

        self._udp: UDPServer | None = None
        self._client: ECHONETAPIClient | None = None
        self._targets: list[Target] = []
        self._next_refresh_at: float | None = None

    @property
    def targets(self) -> list[Target]:
        return self._targets

    async def async_initialize(self) -> None:
        loop = asyncio.get_running_loop()
        self._udp = UDPServer()
        self._udp.run(self._listen_host, self._listen_port, loop=loop)
        self._client = ECHONETAPIClient(server=self._udp)
        await self.async_refresh_inventory()

    async def async_refresh_inventory(self) -> None:
        assert self._client is not None
        hosts = await self._discover_hosts()
        target_map: dict[str, Target] = {}
        for host in hosts:
            try:
                await asyncio.wait_for(self._client.discover(host), timeout=self._timeout)
            except Exception:
                continue
            state = getattr(self._client, "_state", {})
            uid = self._resolve_device_uid(state, host)
            manufacturer, product_code = self._resolve_node_metadata(state, host)
            eojs = self._list_eojs_for_host(state, host)
            if self._eoj_filter:
                eojs = [e for e in eojs if e in self._eoj_filter]
            for eoj in eojs:
                gc, cc, ci = self._parse_eoj(eoj)
                try:
                    await asyncio.wait_for(
                        self._client.getAllPropertyMaps(host, gc, cc, ci),
                        timeout=self._timeout,
                    )
                    eoj_desc = self._resolve_eoj_desc(eoj)
                    t = Target(
                        host=host,
                        eoj=eoj,
                        uid=uid,
                        manufacturer=manufacturer,
                        device_name=self._resolve_device_name(state, host, product_code),
                        product_code=product_code,
                        eoj_desc=eoj_desc,
                    )
                    target_map[t.key] = t
                except Exception:
                    continue
        self._targets = sorted(target_map.values(), key=lambda x: (x.uid, x.eoj))
        self._next_refresh_at = (
            time.monotonic() + self._refresh_interval if self._refresh_interval > 0 else None
        )

    async def async_fetch(self) -> dict[str, dict[str, Any]]:
        assert self._client is not None
        if self._next_refresh_at is not None and time.monotonic() >= self._next_refresh_at:
            await self.async_refresh_inventory()

        out: dict[str, dict[str, Any]] = {}
        for target in self._targets:
            gc, cc, ci = self._parse_eoj(target.eoj)
            key = target.key
            try:
                payload = await self._fetch_raw_payload(target.host, gc, cc, ci)
                out[key] = {
                    "host": target.host,
                    "eoj": target.eoj,
                    "uid": target.uid,
                    "manufacturer": target.manufacturer,
                    "device_name": target.device_name,
                    "product_code": target.product_code,
                    "eoj_desc": target.eoj_desc,
                    "payload": payload,
                    "errors": payload.get("_errors", []),
                }
            except Exception as exc:
                out[key] = {
                    "host": target.host,
                    "eoj": target.eoj,
                    "uid": target.uid,
                    "manufacturer": target.manufacturer,
                    "device_name": target.device_name,
                    "product_code": target.product_code,
                    "eoj_desc": target.eoj_desc,
                    "payload": {},
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
                if self._rediscover_on_error:
                    try:
                        await asyncio.wait_for(
                            self._client.discover(target.host), timeout=self._timeout
                        )
                        await asyncio.wait_for(
                            self._client.getAllPropertyMaps(target.host, gc, cc, ci),
                            timeout=self._timeout,
                        )
                    except Exception:
                        pass
        return out

    async def _discover_hosts(self) -> list[str]:
        assert self._client is not None
        if self._host:
            hosts = [self._host]
            if self._cidr:
                hosts = self._filter_by_cidr(hosts, self._cidr)
            return hosts

        discovered: set[str] = set()

        async def on_unknown_host(host: str) -> None:
            if host in {ENL_MULTICAST_ADDRESS, self._listen_host}:
                return
            discovered.add(host)

        self._client._discover_callback = on_unknown_host
        discover_task = asyncio.create_task(self._client.discover())
        await asyncio.sleep(self._discovery_wait)
        if not discover_task.done():
            discover_task.cancel()
            try:
                await discover_task
            except asyncio.CancelledError:
                pass

        hosts = sorted(discovered)
        if self._cidr:
            hosts = self._filter_by_cidr(hosts, self._cidr)
        return hosts

    async def _fetch_raw_payload(self, host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int) -> dict[str, Any]:
        assert self._client is not None
        state = getattr(self._client, "_state", {})
        instance = self._get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
        get_map = sorted(set(instance.get(0x9F, []) or []))
        if not get_map:
            return {"value": "no data (empty get-map)"}

        payload: dict[str, Any] = {}
        failures: list[str] = []
        for start in range(0, len(get_map), self._max_opc):
            chunk = get_map[start : start + self._max_opc]
            opc = [{"EPC": epc} for epc in chunk]
            try:
                ok = await self._client.echonetMessage(host, eoj_gc, eoj_cc, eoj_ci, GET, opc)
                if not ok:
                    raise TimeoutError("chunk timeout")
                state = getattr(self._client, "_state", {})
                instance = self._get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
                for epc in chunk:
                    key = f"0x{epc:02X}"
                    value = instance.get(epc)
                    if isinstance(value, (bytes, bytearray)):
                        payload[key] = value.hex().upper()
                    else:
                        payload[key] = value
            except Exception:
                for epc in chunk:
                    key = f"0x{epc:02X}"
                    try:
                        ok = await self._client.echonetMessage(
                            host, eoj_gc, eoj_cc, eoj_ci, GET, [{"EPC": epc}]
                        )
                        if not ok:
                            failures.append(f"{key}:timeout")
                            continue
                        state = getattr(self._client, "_state", {})
                        instance = self._get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
                        value = instance.get(epc)
                        if isinstance(value, (bytes, bytearray)):
                            payload[key] = value.hex().upper()
                        else:
                            payload[key] = value
                    except Exception as exc:
                        failures.append(f"{key}:{type(exc).__name__}")

        if failures:
            payload["_errors"] = failures
        return payload

    @staticmethod
    def _parse_eoj(eoj: str) -> tuple[int, int, int]:
        raw = eoj.strip().lower().removeprefix("0x")
        if len(raw) != 6:
            raise ValueError("EOJ must be 3-byte hex")
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)

    @classmethod
    def _parse_eoj_candidates(cls, raw: str) -> list[str]:
        if not raw.strip():
            return []
        out: list[str] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            gc, cc, ci = cls._parse_eoj(item)
            out.append(f"{gc:02X}{cc:02X}{ci:02X}")
        return sorted(set(out))

    @staticmethod
    def _filter_by_cidr(hosts: list[str], cidr: str) -> list[str]:
        network = ipaddress.ip_network(cidr, strict=False)
        if network.version != 4:
            return hosts
        out: list[str] = []
        for host in hosts:
            try:
                if ipaddress.ip_address(host) in network:
                    out.append(host)
            except ValueError:
                continue
        return out

    @staticmethod
    def _list_eojs_for_host(state: dict[str, Any], host: str) -> list[str]:
        instances = state.get(host, {}).get("instances", {})
        eojs: list[str] = []
        for eoj_gc, by_cc in instances.items():
            for eoj_cc, by_ci in by_cc.items():
                for eoj_ci in by_ci.keys():
                    eojs.append(f"{int(eoj_gc):02X}{int(eoj_cc):02X}{int(eoj_ci):02X}")
        eojs.sort()
        return eojs

    @staticmethod
    def _resolve_device_uid(state: dict[str, Any], host: str) -> str:
        uid = state.get(host, {}).get("uid")
        if uid:
            return str(uid)
        return f"host:{host}"

    @staticmethod
    def _resolve_node_metadata(state: dict[str, Any], host: str) -> tuple[str | None, str | None]:
        node = state.get(host, {})
        manufacturer = node.get("manufacturer")
        product_code = node.get("product_code")
        if isinstance(manufacturer, (dict, list)):
            manufacturer = json.dumps(manufacturer, ensure_ascii=False)
        if manufacturer is not None:
            manufacturer = str(manufacturer)
        if product_code is not None:
            product_code = str(product_code)
        return manufacturer, product_code

    @staticmethod
    def _resolve_device_name(state: dict[str, Any], host: str, product_code: str | None) -> str | None:
        node = state.get(host, {})
        for key in ("device_name", "name", "product_name"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return product_code

    def _resolve_eoj_desc(self, eoj: str) -> str:
        desc = self._mra.resolve_class_name(eoj)
        if desc:
            return desc
        # fallback to pychonet built-in dictionary
        try:
            from pychonet.lib.eojx import EOJX_CLASS
            gc, cc, _ci = self._parse_eoj(eoj)
            return EOJX_CLASS.get(gc, {}).get(cc, f"EOJ {eoj}")
        except Exception:
            return f"EOJ {eoj}"

    @staticmethod
    def _get_instance_state(
        state: dict[str, Any], host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int
    ) -> dict[int, Any]:
        return (
            state.get(host, {})
            .get("instances", {})
            .get(eoj_gc, {})
            .get(eoj_cc, {})
            .get(eoj_ci, {})
        )
