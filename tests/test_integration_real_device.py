from __future__ import annotations

import os
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
        counts = [c for c in (count_simplex, count_duplex) if isinstance(c, int)]
        if not counts:
            pytest.skip("B1/B8 channel count is not available")

        count = max(counts)
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
