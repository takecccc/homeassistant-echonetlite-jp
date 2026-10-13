"""Shared Home Assistant test setup running the integration against FakeNode."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from unittest.mock import patch

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.echonetlite_jp.const import CONF_HOSTS, CONF_INTERFACE, DOMAIN
from custom_components.echonetlite_jp.echonet.client import EchonetClient
from custom_components.echonetlite_jp.echonet.mra import MraCatalog
from custom_components.echonetlite_jp.echonet.protocol import EOJ
from custom_components.echonetlite_jp.echonet.transport import EchonetTransport

from .fake_node import FAKE_HOST, FakeNode, FakeObject

AIRCON = EOJ(0x01, 0x30, 0x01)
PDB = EOJ(0x02, 0x87, 0x01)


def aircon() -> FakeObject:
    return FakeObject(
        {
            0x80: b"\x30",
            0x81: b"\x09",  # living room 1
            0x88: b"\x42",
            0x8A: bytes.fromhex("00000B"),
            0x8C: b"CS-X400D".ljust(12, b" "),
            0xA0: b"\x33",  # level 3
            0xB0: b"\x42",  # cooling
            0xB3: b"\x1a",
            0xBB: b"\x19",
            0xF5: b"\x01\x02",  # vendor specific, not in the MRA
        },
        settable={0x80, 0x81, 0xA0, 0xB0, 0xB3},
    )


def board() -> FakeObject:
    def channel_list(scale: int) -> bytes:
        return bytes([1, 2]) + scale.to_bytes(4, "big") + (2 * scale).to_bytes(4, "big")

    return FakeObject(
        {
            0x80: b"\x30",
            0xB1: b"\x02",
            0xC0: (1234).to_bytes(4, "big"),
            0xC2: b"\x01",
            0xC6: (500).to_bytes(4, "big"),
        },
        getters={0xB3: lambda: channel_list(100), 0xB7: lambda: channel_list(10)},
    )


async def setup_integration(
    hass: HomeAssistant,
    fake_node: FakeNode,
    *,
    language: str = "en",
    quirks_dir: Path | None = None,
) -> MockConfigEntry:
    hass.config.language = language
    port = await fake_node.start()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        options={CONF_HOSTS: [FAKE_HOST], CONF_INTERFACE: "", CONF_SCAN_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.echonetlite_jp.EchonetTransport",
            partial(EchonetTransport, bind_host="127.0.0.1", port=port, multicast=False),
        ),
        patch(
            "custom_components.echonetlite_jp.EchonetClient",
            partial(EchonetClient, timeout=0.2, retries=0),
        ),
        patch(
            "custom_components.echonetlite_jp.MraCatalog",
            partial(MraCatalog, quirks_dir=quirks_dir),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry
