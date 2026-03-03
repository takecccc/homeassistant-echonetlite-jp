from __future__ import annotations

import os
import json
from typing import Any

import pytest
from pytest_socket import socket_allow_hosts

from custom_components.echonetlite_jp.client import HemsEchonetClient

pytestmark = [pytest.mark.integration, pytest.mark.enable_socket]


@pytest.fixture(autouse=True)
def _allow_real_device_hosts(socket_enabled: None) -> None:
    """Allow multicast and target host even when pytest-socket defaults to localhost only."""
    allowed = ["127.0.0.1", "localhost", "224.0.23.0"]
    host = os.getenv("ECHONET_TEST_HOST", "").strip()
    if host:
        allowed.append(host)
    socket_allow_hosts(allowed)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"set {name} to run real-device integration test")
    return value


def _decode_uint8(value: Any) -> int | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    if token.startswith("0X"):
        token = token[2:]
    if len(token) == 0:
        return None
    if len(token) % 2 != 0:
        token = f"0{token}"
    try:
        raw = bytes.fromhex(token)
    except ValueError:
        return None
    if len(raw) != 1:
        return None
    out = raw[0]
    if out == 0xFD:  # undefined in MRA
        return None
    return out


def _total_channel_count(simplex: int | None, duplex: int | None) -> int | None:
    total = 0
    if isinstance(simplex, int):
        total += simplex
    if isinstance(duplex, int):
        total += duplex
    if total <= 0:
        return None
    return total


def _normalize_epc_key(value: str) -> str | None:
    token = str(value).strip()
    if not token.startswith("0x") and not token.startswith("0X"):
        return None
    try:
        epc = int(token, 16)
    except ValueError:
        return None
    return f"0x{epc:02X}"


@pytest.mark.asyncio
async def test_real_device_fetch_has_target(socket_enabled: None) -> None:
    host = _required_env("ECHONET_TEST_HOST")
    eoj = os.getenv("ECHONET_TEST_EOJ", "028701").strip() or "028701"

    client = HemsEchonetClient(
        host=host,
        eoj=eoj,
        cidr="",
        listen_host="0.0.0.0",
        listen_port=int(os.getenv("ECHONET_TEST_LISTEN_PORT", "3610")),
        discovery_wait=float(os.getenv("ECHONET_TEST_DISCOVERY_WAIT", "1.0")),
        timeout=float(os.getenv("ECHONET_TEST_TIMEOUT", "3.0")),
        refresh_interval=60.0,
        max_opc=16,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )

    try:
        await client.async_initialize()
        data = await client.async_fetch()

        assert data, "async_fetch returned no targets"
        assert any(str(v.get("eoj") or "").upper() == eoj.upper() for v in data.values())
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_real_device_0287_extended_channels_if_applicable(socket_enabled: None) -> None:
    host = _required_env("ECHONET_TEST_HOST")
    eoj = os.getenv("ECHONET_TEST_EOJ", "028701").strip() or "028701"
    if not eoj.upper().startswith("0287"):
        pytest.skip("this test is specific to EOJ 0287xx")

    client = HemsEchonetClient(
        host=host,
        eoj=eoj,
        cidr="",
        listen_host="0.0.0.0",
        listen_port=int(os.getenv("ECHONET_TEST_LISTEN_PORT", "3610")),
        discovery_wait=float(os.getenv("ECHONET_TEST_DISCOVERY_WAIT", "1.0")),
        timeout=float(os.getenv("ECHONET_TEST_TIMEOUT", "3.0")),
        refresh_interval=60.0,
        max_opc=16,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )

    try:
        await client.async_initialize()
        data = await client.async_fetch()

        target = None
        for item in data.values():
            if str(item.get("eoj") or "").upper() == eoj.upper():
                target = item
                break
        assert isinstance(target, dict), f"target eoj {eoj} not found"

        payload = target.get("payload", {})
        assert isinstance(payload, dict)

        count_simplex = _decode_uint8(payload.get("0xB1"))
        count_duplex = _decode_uint8(payload.get("0xB8"))
        count = _total_channel_count(count_simplex, count_duplex)
        if count is None:
            pytest.skip("B1/B8 channel count is not available")

        if count <= 32:
            pytest.skip(f"device reports up to {count} channels; no 33+ verification needed")

        max_channel = min(count, 41)
        expected_keys = [f"v0287_ch{ch}" for ch in range(33, max_channel + 1)]

        get_map = target.get("get_map", [])
        if not isinstance(get_map, list):
            get_map = []

        missing = [key for key in expected_keys if key not in payload or key not in get_map]
        assert not missing, f"missing virtual 33+ channel keys: {missing}"
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_real_device_0287_fetches_channel_lists(socket_enabled: None) -> None:
    host = _required_env("ECHONET_TEST_HOST")
    eoj = os.getenv("ECHONET_TEST_EOJ", "028701").strip() or "028701"
    if not eoj.upper().startswith("0287"):
        pytest.skip("this test is specific to EOJ 0287xx")

    client = HemsEchonetClient(
        host=host,
        eoj=eoj,
        cidr="",
        listen_host="0.0.0.0",
        listen_port=int(os.getenv("ECHONET_TEST_LISTEN_PORT", "3610")),
        discovery_wait=float(os.getenv("ECHONET_TEST_DISCOVERY_WAIT", "1.0")),
        timeout=float(os.getenv("ECHONET_TEST_TIMEOUT", "3.0")),
        refresh_interval=60.0,
        max_opc=16,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )

    try:
        await client.async_initialize()
        target = next((t for t in client.targets if t.eoj.upper() == eoj.upper()), None)
        assert target is not None, f"target eoj {eoj} not found"

        gc, cc, ci = client._parse_eoj(target.eoj)
        get_map, set_map = client._get_property_maps(target.host, gc, cc, ci)
        fetch_map = client._build_fetch_map(target.eoj, get_map)
        payload = await client._fetch_raw_payload(target.host, gc, cc, ci, fetch_map)

        count_simplex = _decode_uint8(payload.get("0xB1"))
        count_duplex = _decode_uint8(payload.get("0xB8"))
        total = _total_channel_count(count_simplex, count_duplex)
        if total is None:
            pytest.skip("B1/B8 channel count is not available")

        fetch_range = min(total, 41)
        simplex_energy = await client._fetch_0287_simplex_list(
            target.host,
            gc,
            cc,
            ci,
            range_epc=0xB2,
            list_epc=0xB3,
            start_channel=1,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xB2 in set_map),
            ignore_reported_start=True,
        )
        simplex_current = await client._fetch_0287_simplex_list(
            target.host,
            gc,
            cc,
            ci,
            range_epc=0xB6,
            list_epc=0xB7,
            start_channel=1,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xB6 in set_map),
            ignore_reported_start=True,
        )
        duplex_energy = await client._fetch_0287_duplex_energy_list(
            target.host,
            gc,
            cc,
            ci,
            range_epc=0xB9,
            list_epc=0xBA,
            start_channel=1,
            fetch_range=fetch_range,
            can_set_range=(0xB9 in set_map),
            ignore_reported_start=True,
        )
        duplex_current = await client._fetch_0287_simplex_list(
            target.host,
            gc,
            cc,
            ci,
            range_epc=0xBD,
            list_epc=0xBE,
            start_channel=1,
            fetch_range=fetch_range,
            item_size=4,
            can_set_range=(0xBD in set_map),
            ignore_reported_start=True,
        )
        # Some devices return list values only in multi-OPC fetch path.
        parsed_payload_lists = [
            client._decode_0287_list_payload(payload.get("0xB3"), item_size=4, ignore_reported_start=True),
            client._decode_0287_list_payload(payload.get("0xB7"), item_size=4, ignore_reported_start=True),
            client._decode_0287_list_payload(payload.get("0xBA"), item_size=8, ignore_reported_start=True),
            client._decode_0287_list_payload(payload.get("0xBE"), item_size=4, ignore_reported_start=True),
        ]

        if os.getenv("ECHONET_TEST_DEBUG_LISTS", "").strip() == "1":
            debug = {
                "raw_payload": {
                    "0xB1": payload.get("0xB1"),
                    "0xB8": payload.get("0xB8"),
                    "0xB3": payload.get("0xB3"),
                    "0xB7": payload.get("0xB7"),
                    "0xBA": payload.get("0xBA"),
                    "0xBE": payload.get("0xBE"),
                },
                "fetched_lists": {
                    "B3": simplex_energy,
                    "B7": simplex_current,
                    "BA": duplex_energy,
                    "BE": duplex_current,
                },
                "parsed_from_payload": {
                    "B3": parsed_payload_lists[0],
                    "B7": parsed_payload_lists[1],
                    "BA": parsed_payload_lists[2],
                    "BE": parsed_payload_lists[3],
                },
            }
            print("ECHONET 0287 LIST DEBUG:")
            print(json.dumps(debug, ensure_ascii=False, indent=2, sort_keys=True))

        assert any((simplex_energy, simplex_current, duplex_energy, duplex_current, *parsed_payload_lists)), (
            "no list data fetched from B3/B7/BA/BE"
        )
        for data in (simplex_energy, simplex_current, duplex_energy, duplex_current):
            for token in data.values():
                assert isinstance(token, str) and len(token) == 8
        for data in parsed_payload_lists:
            for token in data.values():
                assert isinstance(token, str)
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_real_device_all_get_entities_are_stored_in_payload(socket_enabled: None) -> None:
    host = _required_env("ECHONET_TEST_HOST")
    eoj = os.getenv("ECHONET_TEST_EOJ", "").strip()

    client = HemsEchonetClient(
        host=host,
        eoj=eoj,
        cidr="",
        listen_host="0.0.0.0",
        listen_port=int(os.getenv("ECHONET_TEST_LISTEN_PORT", "3610")),
        discovery_wait=float(os.getenv("ECHONET_TEST_DISCOVERY_WAIT", "1.0")),
        timeout=float(os.getenv("ECHONET_TEST_TIMEOUT", "3.0")),
        refresh_interval=60.0,
        max_opc=16,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )

    try:
        await client.async_initialize()

        merged: dict[str, dict[str, Any]] = {}
        attempts = max(1, int(os.getenv("ECHONET_TEST_FETCH_ATTEMPTS", "3")))
        for _ in range(attempts):
            data = await client.async_fetch()
            for target_key, item in data.items():
                prev = merged.get(target_key)
                if not isinstance(prev, dict):
                    merged[target_key] = item
                    continue
                prev_payload = prev.get("payload", {})
                new_payload = item.get("payload", {})
                if isinstance(prev_payload, dict) and isinstance(new_payload, dict):
                    prev_payload.update(new_payload)
                    prev["payload"] = prev_payload
                if (not prev.get("get_map")) and item.get("get_map"):
                    prev["get_map"] = item.get("get_map")
                if (not prev.get("set_map")) and item.get("set_map"):
                    prev["set_map"] = item.get("set_map")
                prev_errors = prev.get("errors", [])
                new_errors = item.get("errors", [])
                if isinstance(prev_errors, list) and isinstance(new_errors, list):
                    prev["errors"] = sorted(set([*prev_errors, *new_errors]))

        assert merged, "async_fetch returned no targets"

        excluded_get_keys = {"0x9D", "0x9E", "0x9F"}
        failures: list[str] = []
        for target_key, item in merged.items():
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            get_map = item.get("get_map", [])
            normalized_get: list[str] = []
            if isinstance(get_map, list):
                for key in get_map:
                    if not isinstance(key, str):
                        continue
                    if key.startswith("v"):
                        normalized_get.append(key)
                        continue
                    epc = _normalize_epc_key(key)
                    if epc:
                        normalized_get.append(epc)
            missing = [
                key
                for key in sorted(set(normalized_get))
                if key not in excluded_get_keys and key not in payload
            ]
            if missing:
                eoj_desc = str(item.get("eoj") or "unknown")
                failures.append(f"{target_key}({eoj_desc}): missing={missing[:20]}")

        assert not failures, "some GET entities are not stored in payload: " + "; ".join(failures)
    finally:
        await client.async_shutdown()
