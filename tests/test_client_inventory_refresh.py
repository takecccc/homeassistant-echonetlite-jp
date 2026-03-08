from __future__ import annotations

from types import MethodType
from types import SimpleNamespace

import pytest

from custom_components.echonetlite_jp.client import HemsEchonetClient
from custom_components.echonetlite_jp.client import Target


@pytest.fixture
def client() -> HemsEchonetClient:
    c = HemsEchonetClient(
        host="",
        eoj="",
        cidr="",
        listen_host="0.0.0.0",
        listen_port=3610,
        discovery_wait=0.1,
        timeout=0.1,
        refresh_interval=86400.0,
        max_opc=8,
        rediscover_on_error=False,
        mra_dir="custom_components/echonetlite_jp/mra_data",
        debug=False,
    )
    return c


@pytest.mark.asyncio
async def test_refresh_inventory_keeps_previous_targets_when_discovery_empty(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = Target(
        host="192.168.12.22",
        eoj="028701",
        uid="uid-old",
        manufacturer="X",
        device_name="dev",
        product_code="p",
        eoj_desc="desc",
    )
    client._targets = [previous]
    client._client = SimpleNamespace()

    async def fake_discover_hosts(self):  # noqa: ANN001
        return []

    monkeypatch.setattr(client, "_discover_hosts", MethodType(fake_discover_hosts, client))

    await client.async_refresh_inventory()

    assert client.targets == [previous]
    assert client._next_refresh_at is not None


@pytest.mark.asyncio
async def test_refresh_inventory_replaces_targets_when_discovery_succeeds(
    client: HemsEchonetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = Target(
        host="192.168.12.22",
        eoj="028701",
        uid="uid-old",
        manufacturer="X",
        device_name="dev",
        product_code="p",
        eoj_desc="desc",
    )
    client._targets = [previous]

    class DummyClient:
        def __init__(self) -> None:
            self._state = {
                "192.168.12.23": {
                    "uid": "uid-new",
                    "manufacturer": "Y",
                    "product_code": "prod",
                    "instances": {0x02: {0x87: {0x01: {0x9F: [0x80]}}}},
                }
            }

        async def discover(self, host=None):  # noqa: ANN001
            return True

        async def getAllPropertyMaps(self, host, gc, cc, ci):  # noqa: ANN001
            return True

    client._client = DummyClient()

    async def fake_discover_hosts(self):  # noqa: ANN001
        return ["192.168.12.23"]

    monkeypatch.setattr(client, "_discover_hosts", MethodType(fake_discover_hosts, client))
    monkeypatch.setattr(client, "_resolve_eoj_desc", lambda eoj: f"EOJ {eoj}")

    await client.async_refresh_inventory()

    assert len(client.targets) == 1
    assert client.targets[0].host == "192.168.12.23"
    assert client.targets[0].uid == "uid-new"
