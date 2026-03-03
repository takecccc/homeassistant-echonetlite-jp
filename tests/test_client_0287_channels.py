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
async def test_augment_0287_channels_returns_empty_when_only_unknown_f0_data(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_simplex(*args, **kwargs):
        return {}

    async def fake_duplex(*args, **kwargs):
        return {}

    async def fake_single(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)
    monkeypatch.setattr(client, "_get_single_epc_value", fake_single)

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

    assert extra == []
    assert "v0287_ch33" not in payload


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


@pytest.mark.asyncio
async def test_augment_0287_channels_extends_max_from_detected_duplex_data(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xB3:
            # simplex ch33..39
            return {
                33: "00000011",
                34: "00000012",
                35: "00000013",
                36: "00000014",
                37: "00000015",
                38: "00000016",
                39: "00000017",
            }
        if list_epc == 0xB5:
            return {
                33: "00110012",
                34: "00130014",
                35: "00150016",
                36: "00170018",
                37: "0019001A",
                38: "001B001C",
                39: "001D001E",
            }
        if list_epc == 0xBC:
            # Duplex local channel 1
            return {1: "00210022"}
        return {}

    async def fake_duplex(*args, **kwargs):
        # Duplex local channel 1
        return {1: "00000021"}

    async def fake_single(*args, **kwargs):
        return None

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)
    monkeypatch.setattr(client, "_get_single_epc_value", fake_single)

    payload = {
        "0xB1": "27",  # simplex=39
        # B8 is unavailable on this device
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

    assert "v0287_ch40" in extra
    assert payload["v0287_ch40"] == "0000002100210022"


def test_resolve_metadata_for_virtual_channel(client: HemsEchonetClient) -> None:
    meta = client.resolve_epc_metadata_by_eoj("028701", "v0287_ch33")
    assert isinstance(meta, dict)
    assert meta["name"] == "計測チャンネル33"
    assert meta["short_name"] == "measurementChannel33"


def test_build_fetch_map_excludes_d0_to_ef_for_0287(client: HemsEchonetClient) -> None:
    fetch_map = client._build_fetch_map("028701", [0x80, 0xD0, 0xD1, 0xEF, 0xB1])
    assert fetch_map == [0x80, 0xB1]


def test_parse_0287_list_value_from_structured_dict(client: HemsEchonetClient) -> None:
    value = {"startChannel": 1, "range": 2, "values": [0x11, 0x12]}
    out = client._parse_0287_list_value(value, item_size=4, ignore_reported_start=True)
    assert out == {1: "00000011", 2: "00000012"}


@pytest.mark.asyncio
async def test_augment_0287_channels_prefers_list_for_channel_1_to_32(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xB3:
            return {1: "000000AA"}
        if list_epc == 0xB5:
            return {1: "00010002"}
        if list_epc == 0xBC:
            return {}
        return {}

    async def fake_duplex(*args, **kwargs):
        return {}

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)

    payload = {
        "0xB1": "01",
        "0xD0": "FFFFFFFFFFFFFFFF",  # should be overwritten by list-derived data
    }
    extra = await client._augment_0287_channels(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        get_map=[0xB3, 0xB5, 0xD0],
        set_map=[],
        payload=payload,
    )

    assert extra == []
    assert payload["0xD0"] == "000000AA00010002"


@pytest.mark.asyncio
async def test_fetch_0287_simplex_list_accepts_headerless_payload(client: HemsEchonetClient) -> None:
    class DummyClient:
        def __init__(self) -> None:
            self._state = {
                "127.0.0.1": {
                    "instances": {0x02: {0x87: {0x01: {0xB3: "0000001100000012"}}}}
                }
            }

        async def echonetMessage(self, *args, **kwargs):  # noqa: N802
            return True

    client._client = DummyClient()
    out = await client._fetch_0287_simplex_list(
        host="127.0.0.1",
        eoj_gc=0x02,
        eoj_cc=0x87,
        eoj_ci=0x01,
        range_epc=0xB2,
        list_epc=0xB3,
        start_channel=1,
        fetch_range=2,
        item_size=4,
        can_set_range=False,
        ignore_reported_start=True,
    )
    assert out == {1: "00000011", 2: "00000012"}


@pytest.mark.asyncio
async def test_augment_0287_channels_respects_b1_b8_item_counts(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_simplex(*args, **kwargs):
        list_epc = kwargs["list_epc"]
        if list_epc == 0xB3:
            # more items than B1
            return {1: "00000011", 2: "00000012", 3: "00000013"}
        if list_epc == 0xB5:
            return {1: "00110012", 2: "00130014", 3: "00150016"}
        if list_epc == 0xBC:
            # more items than B8
            return {1: "00210022", 2: "00230024"}
        return {}

    async def fake_duplex(*args, **kwargs):
        return {1: "00000021", 2: "00000022"}

    monkeypatch.setattr(client, "_fetch_0287_simplex_list", fake_simplex)
    monkeypatch.setattr(client, "_fetch_0287_duplex_energy_list", fake_duplex)

    payload = {
        "0xB1": "02",  # simplex=2
        "0xB8": "01",  # duplex=1
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

    # strict total count = 3, so no 4th channel should be produced
    assert "v0287_ch33" not in extra
    assert "v0287_ch34" not in extra
    assert payload["0xD0"] == "0000001100110012"
    assert payload["0xD1"] == "0000001200130014"
    assert payload["0xD2"] == "0000002100210022"
