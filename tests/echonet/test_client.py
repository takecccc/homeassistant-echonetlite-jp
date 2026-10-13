from __future__ import annotations

import pytest

from custom_components.echonetlite_jp.echonet.client import (
    EchonetClient,
    EchonetRequestRejected,
)
from custom_components.echonetlite_jp.echonet.protocol import EOJ
from custom_components.echonetlite_jp.echonet.transport import EchonetTimeoutError

from ..fake_node import FAKE_HOST, FAKE_UID, FakeNode, FakeObject

AIRCON = EOJ(0x01, 0x30, 0x01)


@pytest.fixture
def fake_node() -> FakeNode:
    return FakeNode(
        {
            AIRCON: FakeObject(
                {
                    0x80: b"\x30",
                    0x8A: bytes.fromhex("000005"),
                    0x8C: b"CS-X  \x00\x00\x00\x00\x00\x00",
                    0xB3: b"\x1a",
                    0xBB: b"\x19",
                },
                settable={0x80, 0xB3},
            )
        }
    )


async def test_get_node_and_object(echonet: EchonetClient) -> None:
    node = await echonet.get_node(FAKE_HOST)
    assert node.uid == FAKE_UID.hex().upper()
    assert node.manufacturer == "Panasonic"
    assert node.instances == (AIRCON,)

    info = await echonet.get_object(FAKE_HOST, AIRCON)
    assert info.maps.get >= {0x80, 0xB3, 0xBB, 0x9F}
    assert info.maps.set == {0x80, 0xB3}
    assert info.manufacturer == "Sharp"
    assert info.product_code == "CS-X"
    assert info.serial_number is None


async def test_get_reports_missing_properties(echonet: EchonetClient) -> None:
    result = await echonet.get(FAKE_HOST, AIRCON, [0x80, 0xBB, 0xC0])
    assert result.values == {0x80: b"\x30", 0xBB: b"\x19"}
    assert result.missing == {0xC0}


async def test_get_falls_back_to_single_requests(
    echonet: EchonetClient, fake_node: FakeNode
) -> None:
    fake_node.max_properties = 1  # node silently drops multi-property requests
    result = await echonet.get(FAKE_HOST, AIRCON, [0x80, 0xBB])
    assert result.values == {0x80: b"\x30", 0xBB: b"\x19"}


async def test_get_retries_when_whole_request_is_rejected(
    echonet: EchonetClient, fake_node: FakeNode
) -> None:
    fake_node.reject_multi = True
    result = await echonet.get(FAKE_HOST, AIRCON, [0x80, 0xBB])
    assert result.values == {0x80: b"\x30", 0xBB: b"\x19"}


async def test_get_does_not_retry_partial_sna(echonet: EchonetClient, fake_node: FakeNode) -> None:
    result = await echonet.get(FAKE_HOST, AIRCON, [0x80, 0xC0, 0xC1])
    assert result.missing == {0xC0, 0xC1}
    assert len(fake_node.requests) == 1


async def test_get_raises_when_node_is_silent(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.silent = True
    with pytest.raises(EchonetTimeoutError):
        await echonet.get(FAKE_HOST, AIRCON, [0x80, 0xBB])
    # gives up after the first unanswered single request instead of trying each EPC
    assert len(fake_node.requests) == 2


async def test_set(echonet: EchonetClient, fake_node: FakeNode) -> None:
    await echonet.set(FAKE_HOST, AIRCON, {0xB3: b"\x1b"})
    assert fake_node.objects[AIRCON].values[0xB3] == b"\x1b"
    with pytest.raises(EchonetRequestRejected) as err:
        await echonet.set(FAKE_HOST, AIRCON, {0xBB: b"\x00"})
    assert err.value.rejected == {0xBB}


async def test_chunking(echonet: EchonetClient, fake_node: FakeNode) -> None:
    small = EchonetClient(echonet.transport, timeout=0.3, retries=0, max_properties=2)
    result = await small.get(FAKE_HOST, AIRCON, [0x80, 0xB3, 0xBB])
    assert set(result.values) == {0x80, 0xB3, 0xBB}
    assert [len(f.properties) for f in fake_node.requests] == [2, 1]
