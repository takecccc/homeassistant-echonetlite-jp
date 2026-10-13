from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pytest_socket import socket_allow_hosts

from custom_components.echonetlite_jp.echonet.client import EchonetClient
from custom_components.echonetlite_jp.echonet.transport import EchonetTransport

from .fake_node import FAKE_HOST, FakeNode


@pytest.fixture
def loopback_sockets(socket_enabled: None) -> None:
    socket_allow_hosts(["127.0.0.1", FAKE_HOST, "127.0.0.3"])


@pytest.fixture
def fake_node() -> FakeNode:
    """Populate ``fake_node.objects`` in the test, then use ``echonet``."""
    return FakeNode({})


@pytest.fixture
async def echonet(loopback_sockets: None, fake_node: FakeNode) -> AsyncIterator[EchonetClient]:
    port = await fake_node.start()
    transport = EchonetTransport(bind_host="127.0.0.1", port=port, multicast=False)
    await transport.open()
    try:
        yield EchonetClient(transport, timeout=0.3, retries=0)
    finally:
        transport.close()
        fake_node.stop()
