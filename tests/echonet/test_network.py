from __future__ import annotations

from custom_components.echonetlite_jp.echonet.client import EchonetClient
from custom_components.echonetlite_jp.echonet.mra import MraCatalog
from custom_components.echonetlite_jp.echonet.network import EchonetNetwork
from custom_components.echonetlite_jp.echonet.protocol import EOJ, ESV, Frame, Property

from ..fake_node import FAKE_HOST, FAKE_UID, FakeNode, FakeObject

PDB = EOJ(0x02, 0x87, 0x01)
CATALOG = MraCatalog()


def _pdb(channels: int, *, settable: bool = True) -> FakeObject:
    ranges = {0xB2: [1, channels], 0xB6: [1, channels]}

    def channel_list(range_epc: int, value: int, signed: bool) -> bytes:
        start, count = ranges[range_epc]
        items = b"".join(
            (value * ch).to_bytes(4, "big", signed=signed) for ch in range(start, start + count)
        )
        return bytes([start, count]) + items

    def on_set(epc: int, edt: bytes) -> None:
        ranges[epc] = [edt[0], edt[1]]

    return FakeObject(
        {
            0x80: b"\x30",
            0xB1: bytes([channels]),
            0xC0: (5).to_bytes(4, "big"),
            0xC2: b"\x01",
            0xD0: bytes(8),
        },
        settable={0xB2, 0xB6} if settable else set(),
        on_set=on_set,
        getters={
            0xB2: lambda: bytes(ranges[0xB2]),
            0xB6: lambda: bytes(ranges[0xB6]),
            0xB3: lambda: channel_list(0xB2, 100, False),
            0xB7: lambda: channel_list(0xB6, 10, True),
        },
    )


async def test_discover_and_poll(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.objects[PDB] = _pdb(3)
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])

    new = await network.async_discover()
    assert [d.id for d in new] == [f"{FAKE_UID.hex().upper()}-028701"]
    device = new[0]
    assert device.class_def.name.ja == "分電盤メータリング"
    # list properties are handled by the extension; D0-EF and atomic pairs are not polled
    assert 0xD0 not in device.poll_epcs()
    assert 0xB3 not in device.poll_epcs()

    await network.async_poll()
    assert device.available
    assert device.decode(0xC0) == 0.5
    assert device.derived == {
        "ch1_energy": 10.0,
        "ch2_energy": 20.0,
        "ch3_energy": 30.0,
        "ch1_power": 10,
        "ch2_power": 20,
        "ch3_power": 30,
    }
    names = {s.key: s.name.ja for s in device.extension.sensors(device)}
    assert names["ch2_power"] == "計測チャンネル2 瞬時電力"

    # rediscovery keeps the same device object
    assert await network.async_discover() == []
    assert network.devices[device.id] is device


async def test_poll_pages_through_long_lists(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.objects[PDB] = _pdb(70)
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])
    [device] = await network.async_discover()
    await network.async_poll()
    assert device.derived["ch70_energy"] == 700.0
    assert device.derived["ch70_power"] == 700
    assert sum(1 for k in device.derived if k.endswith("_energy")) == 70


async def test_poll_without_range_setting(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.objects[PDB] = _pdb(2, settable=False)
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])
    [device] = await network.async_discover()
    await network.async_poll()
    assert device.derived["ch2_energy"] == 20.0
    assert not any(f.esv == 0x61 for f in fake_node.requests)


async def test_poll_marks_unavailable(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.objects[PDB] = _pdb(1)
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])
    [device] = await network.async_discover()
    fake_node.silent = True
    await network.async_poll()
    assert not device.available
    fake_node.silent = False
    await network.async_poll()
    assert device.available


async def test_notification_updates_values(echonet: EchonetClient, fake_node: FakeNode) -> None:
    fake_node.objects[PDB] = _pdb(1)
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])
    [device] = await network.async_discover()
    frame = Frame(1, PDB, EOJ(5, 0xFF, 1), ESV.INF, (Property(0x80, b"\x31"),))
    assert network.handle_notification(FAKE_HOST, frame) is device
    assert device.decode(0x80).is_on is False

    profile = Frame(2, EOJ(0x0E, 0xF0, 1), EOJ(5, 0xFF, 1), ESV.INF, (Property(0xD5, b"\x00"),))
    assert network.handle_notification(FAKE_HOST, profile) is None
    assert network.rediscovery_requested


async def test_user_defined_objects_are_skipped(
    echonet: EchonetClient, fake_node: FakeNode
) -> None:
    fake_node.objects[PDB] = _pdb(1)
    fake_node.objects[EOJ(0x0F, 0x02, 0x01)] = FakeObject({0x80: b"\x30"})
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST])
    assert [str(d.eoj) for d in await network.async_discover()] == ["028701"]
    network = EchonetNetwork(echonet, CATALOG, hosts=[FAKE_HOST], include_user_defined=True)
    assert len(await network.async_discover()) == 2
