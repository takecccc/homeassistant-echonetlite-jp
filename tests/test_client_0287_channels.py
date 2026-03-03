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
        set_map=[0xB2, 0xB4],
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
        set_map=[0xB9, 0xBB],
        payload=payload,
    )

    assert extra == ["v0287_ch33", "v0287_ch34"]
    assert payload["v0287_ch33"] == "000000AA00100020"
    assert payload["v0287_ch34"] == "000000BB00300040"


@pytest.mark.asyncio
async def test_augment_0287_channels_prefers_direct_f0_to_f8(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_simplex(*args, **kwargs):
        raise AssertionError("range list path must not be used when 0xF0.. is available")

    async def fail_duplex(*args, **kwargs):
        raise AssertionError("range list path must not be used when 0xF0.. is available")

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fail_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fail_duplex)

    payload = {
        "0xF0": "00000064000A000B",
        "0xF1": "00000065000C000D",
    }
    extra = await client._augment_0287_channels(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        get_map=[0xF0, 0xF1],
        set_map=[],
        payload=payload,
    )

    assert extra == ["v0287_ch33", "v0287_ch34"]
    assert payload["v0287_ch33"] == "00000064000A000B"
    assert payload["v0287_ch34"] == "00000065000C000D"


@pytest.mark.asyncio
async def test_augment_0287_channels_merges_simplex_and_duplex_when_set_not_supported(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xB3:
            return {33: "000000A1", 34: "000000A2", 35: "000000A3", 36: "000000A4"}
        if list_epc == 0xB5:
            return {33: "00110012", 34: "00130014", 35: "00150016", 36: "00170018"}
        if list_epc == 0xBC:
            # Duplex local channel 1
            return {1: "00210022"}
        return {}

    async def fake_duplex(*args, **kwargs):
        # Duplex local channel 1
        return {1: "000000B1"}

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)

    payload = {
        "0xB1": "24",  # simplex=36
        "0xB8": "01",  # duplex=1 -> total=37
    }
    extra = await client._augment_0287_channels(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        get_map=[0xB3, 0xB5, 0xBA, 0xBC],
        set_map=[],
        payload=payload,
    )

    assert extra == ["v0287_ch33", "v0287_ch34", "v0287_ch35", "v0287_ch36", "v0287_ch37"]
    assert payload["v0287_ch33"] == "000000A100110012"
    assert payload["v0287_ch34"] == "000000A200130014"
    assert payload["v0287_ch35"] == "000000A300150016"
    assert payload["v0287_ch36"] == "000000A400170018"
    # ch37 from duplex local channel 1 shifted by simplex count(36)
    assert payload["v0287_ch37"] == "000000B100210022"


def test_resolve_metadata_for_virtual_channel(client: HemsEchonetClient) -> None:
    meta = client.resolve_epc_metadata_by_eoj("028701", "v0287_ch33")
    assert isinstance(meta, dict)
    assert meta["name"] == "計測チャンネル33"
    assert meta["short_name"] == "measurementChannel33"
