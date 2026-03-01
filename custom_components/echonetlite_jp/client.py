from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from pychonet import ECHONETAPIClient
from pychonet.lib.const import ENL_MULTICAST_ADDRESS
from pychonet.lib.const import GET
from pychonet.lib.const import SETC
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
                get_map, set_map = self._get_property_maps(target.host, gc, cc, ci)
                payload = await self._fetch_raw_payload(target.host, gc, cc, ci, get_map)
                out[key] = {
                    "host": target.host,
                    "eoj": target.eoj,
                    "uid": target.uid,
                    "manufacturer": target.manufacturer,
                    "device_name": target.device_name,
                    "product_code": target.product_code,
                    "eoj_desc": target.eoj_desc,
                    "payload": payload,
                    "get_map": [self._epc_to_key(epc) for epc in get_map],
                    "set_map": [self._epc_to_key(epc) for epc in set_map],
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
                    "get_map": [],
                    "set_map": [],
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

    async def async_get_epc(self, target_key: str, epc_key: str) -> Any:
        assert self._client is not None
        target = self._target_by_key(target_key)
        if target is None:
            raise KeyError(f"target not found: {target_key}")
        epc = self._epc_from_key(epc_key)
        gc, cc, ci = self._parse_eoj(target.eoj)
        get_map, _set_map = self._get_property_maps(target.host, gc, cc, ci)
        if epc not in get_map:
            raise ValueError(f"{self._epc_to_key(epc)} is not in get-map")
        ok = await self._client.echonetMessage(target.host, gc, cc, ci, GET, [{"EPC": epc}])
        if not ok:
            raise TimeoutError(f"GET timeout: {target.host} {target.eoj} {self._epc_to_key(epc)}")
        state = getattr(self._client, "_state", {})
        instance = self._get_instance_state(state, target.host, gc, cc, ci)
        return self._normalize_epc_value(instance.get(epc))

    async def async_set_epc(self, target_key: str, epc_key: str, edt_hex: str) -> Any:
        edt = self._parse_edt_hex(edt_hex)
        return await self._async_set_epc_bytes(target_key, epc_key, edt)

    async def async_set_epc_value(self, target_key: str, epc_key: str, value: Any) -> Any:
        meta = self.resolve_epc_metadata(target_key, epc_key) or {}
        edt = self._encode_value_to_edt(value, meta)
        return await self._async_set_epc_bytes(target_key, epc_key, edt)

    async def _async_set_epc_bytes(self, target_key: str, epc_key: str, edt: bytes) -> Any:
        assert self._client is not None
        target = self._target_by_key(target_key)
        if target is None:
            raise KeyError(f"target not found: {target_key}")
        epc = self._epc_from_key(epc_key)
        gc, cc, ci = self._parse_eoj(target.eoj)
        _get_map, set_map = self._get_property_maps(target.host, gc, cc, ci)
        if epc not in set_map:
            raise ValueError(f"{self._epc_to_key(epc)} is not in set-map")
        opc = [{"EPC": epc, "PDC": len(edt), "EDT": int.from_bytes(edt, "big")}]
        ok = await self._client.echonetMessage(target.host, gc, cc, ci, SETC, opc)
        if not ok:
            raise TimeoutError(f"SET timeout: {target.host} {target.eoj} {self._epc_to_key(epc)}")
        state = getattr(self._client, "_state", {})
        instance = self._get_instance_state(state, target.host, gc, cc, ci)
        if epc in instance:
            return self._normalize_epc_value(instance.get(epc))
        return edt.hex().upper()

    def resolve_epc_metadata(self, target_key: str, epc_key: str) -> dict[str, Any] | None:
        target = self._target_by_key(target_key)
        if target is None:
            return None
        return self.resolve_epc_metadata_by_eoj(target.eoj, epc_key)

    def resolve_epc_metadata_by_eoj(self, eoj: str, epc_key: str) -> dict[str, Any] | None:
        try:
            epc = self._epc_from_key(epc_key)
        except ValueError:
            return None
        return self._mra.resolve_property(eoj, epc)

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

    async def _fetch_raw_payload(
        self, host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int, get_map: list[int]
    ) -> dict[str, Any]:
        assert self._client is not None
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
                    key = self._epc_to_key(epc)
                    payload[key] = self._normalize_epc_value(instance.get(epc))
            except Exception:
                for epc in chunk:
                    key = self._epc_to_key(epc)
                    try:
                        ok = await self._client.echonetMessage(
                            host, eoj_gc, eoj_cc, eoj_ci, GET, [{"EPC": epc}]
                        )
                        if not ok:
                            failures.append(f"{key}:timeout")
                            continue
                        state = getattr(self._client, "_state", {})
                        instance = self._get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
                        payload[key] = self._normalize_epc_value(instance.get(epc))
                    except Exception as exc:
                        failures.append(f"{key}:{type(exc).__name__}")

        if failures:
            payload["_errors"] = failures
        return payload

    def _get_property_maps(
        self, host: str, eoj_gc: int, eoj_cc: int, eoj_ci: int
    ) -> tuple[list[int], list[int]]:
        state = getattr(self._client, "_state", {})
        instance = self._get_instance_state(state, host, eoj_gc, eoj_cc, eoj_ci)
        get_map = sorted(set(instance.get(0x9F, []) or []))
        set_map = sorted(set(instance.get(0x9E, []) or []))
        return get_map, set_map

    def _target_by_key(self, target_key: str) -> Target | None:
        for target in self._targets:
            if target.key == target_key:
                return target
        return None

    @staticmethod
    def _parse_eoj(eoj: str) -> tuple[int, int, int]:
        raw = eoj.strip().lower().removeprefix("0x")
        if len(raw) != 6:
            raise ValueError("EOJ must be 3-byte hex")
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)

    @staticmethod
    def _epc_from_key(epc_key: str) -> int:
        raw = epc_key.strip().upper()
        if not raw.startswith("0X"):
            raise ValueError(f"invalid EPC key: {epc_key}")
        if len(raw) != 4:
            raise ValueError(f"invalid EPC key: {epc_key}")
        return int(raw, 16)

    @staticmethod
    def _epc_to_key(epc: int) -> str:
        return f"0x{int(epc):02X}"

    @staticmethod
    def _normalize_epc_value(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return value.hex().upper()
        return value

    @staticmethod
    def _parse_edt_hex(raw: str) -> bytes:
        value = raw.strip().replace(" ", "").replace("0x", "").replace("0X", "")
        if len(value) == 0:
            raise ValueError("EDT must not be empty")
        if len(value) % 2 != 0:
            raise ValueError("EDT hex length must be even")
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"invalid EDT hex: {raw}") from exc

    @classmethod
    def _encode_value_to_edt(cls, value: Any, meta: dict[str, Any]) -> bytes:
        value_type = str(meta.get("type") or "").strip().lower()
        if value_type == "state":
            return cls._encode_state(value, meta)
        if value_type in {"number", "level"}:
            return cls._encode_number(value, meta)
        if isinstance(value, str):
            return cls._parse_edt_hex(value)
        raise ValueError("value type is not supported for this EPC; pass EDT hex string")

    @classmethod
    def _encode_state(cls, value: Any, meta: dict[str, Any]) -> bytes:
        enum_map = meta.get("enum", {})
        if not isinstance(enum_map, dict):
            if isinstance(value, str):
                return cls._parse_edt_hex(value)
            raise ValueError("state EPC has no enum definition; pass EDT hex string")

        if isinstance(value, bool):
            token = cls._pick_token_by_bool(enum_map, value)
            if token:
                return bytes.fromhex(token)

        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("value must not be empty")
            if cls._is_hex_token(text):
                return cls._parse_edt_hex(text)
            norm = text.lower()
            for token, label in enum_map.items():
                if isinstance(label, str) and label.strip().lower() == norm:
                    return bytes.fromhex(token)
            if norm in {"on", "true", "1"}:
                token = cls._pick_token_by_bool(enum_map, True)
                if token:
                    return bytes.fromhex(token)
            if norm in {"off", "false", "0"}:
                token = cls._pick_token_by_bool(enum_map, False)
                if token:
                    return bytes.fromhex(token)

        raise ValueError("cannot map value to state enum; use EDT hex (e.g. 30/31)")

    @classmethod
    def _encode_number(cls, value: Any, meta: dict[str, Any]) -> bytes:
        fmt = str(meta.get("format") or "").strip().lower()
        if fmt not in {"uint8", "int8", "uint16", "int16", "uint32", "int32"}:
            raise ValueError("unsupported numeric format for SET")
        if isinstance(value, str):
            text = value.strip()
            if cls._is_hex_token(text):
                return cls._parse_edt_hex(text)
            try:
                numeric = float(text)
            except ValueError as exc:
                raise ValueError(f"invalid numeric value: {value}") from exc
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
        else:
            raise ValueError("numeric value is required for this EPC")

        multiple = meta.get("multiple")
        if isinstance(multiple, (int, float)) and multiple not in {0, 0.0}:
            numeric = numeric / float(multiple)

        if not math.isfinite(numeric):
            raise ValueError("numeric value must be finite")
        ivalue = int(round(numeric))
        if abs(numeric - ivalue) > 1e-6:
            raise ValueError("value cannot be represented exactly for this EPC")

        size_map = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "uint32": 4, "int32": 4}
        size = size_map[fmt]
        signed = fmt.startswith("int")
        lower = -(1 << (8 * size - 1)) if signed else 0
        upper = (1 << (8 * size - 1)) - 1 if signed else (1 << (8 * size)) - 1
        if ivalue < lower or ivalue > upper:
            raise ValueError(f"value out of range for {fmt}: {ivalue}")
        return int(ivalue).to_bytes(size, byteorder="big", signed=signed)

    @staticmethod
    def _pick_token_by_bool(enum_map: dict[str, Any], on: bool) -> str | None:
        wants = {"on", "true", "1"} if on else {"off", "false", "0"}
        for token, label in enum_map.items():
            if not isinstance(label, str):
                continue
            norm = label.strip().lower()
            if norm in wants:
                return token
        # Common ECHONET ON/OFF representations.
        for token in ("30", "31", "41", "42", "01", "00"):
            if token in enum_map:
                if on and token in {"30", "41", "01"}:
                    return token
                if not on and token in {"31", "42", "00"}:
                    return token
        return None

    @staticmethod
    def _is_hex_token(value: str) -> bool:
        token = value.strip()
        if token.startswith(("0x", "0X")):
            token = token[2:]
        if len(token) == 0 or len(token) % 2 != 0:
            return False
        return all(ch in "0123456789abcdefABCDEF" for ch in token)

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
