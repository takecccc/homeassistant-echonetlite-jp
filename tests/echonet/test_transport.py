from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from custom_components.echonetlite_jp.echonet.client import EchonetClient
from custom_components.echonetlite_jp.echonet.errors import EchonetConnectionClosed
from custom_components.echonetlite_jp.echonet.protocol import EOJ, ESV, Frame, Property

from ..fake_node import FAKE_HOST, FakeNode, FakeObject

AIRCON = EOJ(0x01, 0x30, 0x01)
TRANSPORT = "custom_components.echonetlite_jp.echonet.transport"


@pytest.fixture
def fake_node() -> FakeNode:
    return FakeNode({AIRCON: FakeObject({0x80: b"\x30"})})


async def test_discover_collects_responses(echonet: EchonetClient) -> None:
    # Loopback cannot carry multicast; aim the "group" at the fake node instead.
    with patch(f"{TRANSPORT}.MULTICAST_ADDRESS", FAKE_HOST):
        assert await echonet.discover(wait=0.2) == [FAKE_HOST]


async def test_infc_is_acknowledged_and_forwarded(
    echonet: EchonetClient, fake_node: FakeNode
) -> None:
    received: list[tuple[str, Frame]] = []
    remove = echonet.transport.add_listener(lambda host, frame: received.append((host, frame)))
    frame = Frame(7, AIRCON, EOJ(5, 0xFF, 1), ESV.INFC, (Property(0x80, b"\x31"),))
    assert fake_node._transport is not None
    fake_node._transport.sendto(frame.encode(), ("127.0.0.1", fake_node.port))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if received:
            break
    assert received == [(FAKE_HOST, frame)]
    # the node gets an INFC response with the same TID
    ack = [f for f in fake_node.requests if f.esv == ESV.INFC_RES]
    assert [(f.tid, f.properties) for f in ack] == [(7, (Property(0x80),))]
    remove()


async def test_listener_errors_are_contained(
    echonet: EchonetClient, fake_node: FakeNode, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(host: str, frame: Frame) -> None:
        raise RuntimeError("boom")

    echonet.transport.add_listener(broken)
    with caplog.at_level(logging.ERROR):
        fake_node.notify(AIRCON, 0x80, b"\x31", ("127.0.0.1", fake_node.port))
        await asyncio.sleep(0.05)
    assert "error in notification listener" in caplog.text
    # the transport keeps working
    assert await echonet.get_one(FAKE_HOST, AIRCON, 0x80) == b"\x30"


async def test_garbage_is_ignored(echonet: EchonetClient, fake_node: FakeNode) -> None:
    assert fake_node._transport is not None
    fake_node._transport.sendto(b"not echonet", ("127.0.0.1", fake_node.port))
    await asyncio.sleep(0.02)
    assert await echonet.get_one(FAKE_HOST, AIRCON, 0x80) == b"\x30"


async def test_close_wakes_waiters(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.silent = True
    request = asyncio.create_task(echonet.get_one(FAKE_HOST, AIRCON, 0x80))
    await asyncio.sleep(0.02)
    echonet.transport.close()
    with pytest.raises(EchonetConnectionClosed):
        await request
    with pytest.raises(EchonetConnectionClosed):
        await echonet.get_one(FAKE_HOST, AIRCON, 0x80)
