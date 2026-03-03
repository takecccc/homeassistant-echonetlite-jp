from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.echonetlite_jp.client import HemsEchonetClient


@pytest.fixture
def client() -> HemsEchonetClient:
    c = HemsEchonetClient(
        host="",
        eoj="",
        cidr="",
        listen_host="0.0.0.0",
        listen_port=3610,
        discovery_wait=1.0,
        timeout=1.0,
        refresh_interval=60.0,
        max_opc=8,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )
    c._client = SimpleNamespace()  # required by _augment_0287_channels assert path
    return c


@pytest.mark.asyncio
async def test_augment_0287_channels_simplex(client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xB3:
            return {33: "00000064", 34: "00000065", 35: "00000066"}
        if list_epc == 0xB5:
            return {33: "000A000B", 34: "000C000D", 35: "000E000F"}
        return {}

    async def fake_duplex(*args, **kwargs):
        return {}

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)

    payload = {"0xB1": "23"}  # 35 channels
    extra = await client._augment_0287_channels(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        get_map=[0xB3, 0xB5],
        payload=payload,
    )

    assert extra == ["v0287_ch33", "v0287_ch34", "v0287_ch35"]
    assert payload["v0287_ch33"] == "00000064000A000B"
    assert payload["v0287_ch34"] == "00000065000C000D"
    assert payload["v0287_ch35"] == "00000066000E000F"


@pytest.mark.asyncio
async def test_augment_0287_channels_duplex_fallback(client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xBC:
            return {33: "00100020", 34: "00300040"}
        return {}

    async def fake_duplex(*args, **kwargs):
        return {33: "000000AA", 34: "000000BB"}

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)

    payload = {"0xB1": "22"}  # 34 channels
    extra = await client._augment_0287_channels(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        get_map=[0xBA, 0xBC],
        payload=payload,
    )

    assert extra == ["v0287_ch33", "v0287_ch34"]
    assert payload["v0287_ch33"] == "000000AA00100020"
    assert payload["v0287_ch34"] == "000000BB00300040"


def test_resolve_metadata_for_virtual_channel(client: HemsEchonetClient) -> None:
    meta = client.resolve_epc_metadata_by_eoj("028701", "v0287_ch33")
    assert isinstance(meta, dict)
    assert meta["name"] == "計測チャンネル33"
    assert meta["short_name"] == "measurementChannel33"
