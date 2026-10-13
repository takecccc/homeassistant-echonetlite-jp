from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from custom_components.echonetlite_jp.echonet.classes import extension_for
from custom_components.echonetlite_jp.echonet.client import EchonetClient
from custom_components.echonetlite_jp.echonet.codec import decode_property
from custom_components.echonetlite_jp.echonet.mra import MraCatalog
from custom_components.echonetlite_jp.echonet.network import EchonetNetwork
from custom_components.echonetlite_jp.echonet.protocol import EOJ
from custom_components.echonetlite_jp.echonet.quirks import (
    DEFAULT_QUIRKS_DIR,
    DeviceKey,
    QuirkError,
    load_quirks,
    parse_quirk,
)

from ..fake_node import FAKE_HOST, FakeNode, FakeObject

PANASONIC = 0x00000B
PDB_KEY = DeviceKey(0x0287, PANASONIC, "MKN7350S1")

VENDOR_POWER = {
    "epc": "0xF0",
    "propertyName": {"ja": "独自電力", "en": "Vendor power"},
    "shortName": "vendorPower",
    "accessRule": {"get": "required", "set": "notApplicable", "inf": "optional"},
    "data": {"type": "number", "format": "uint16", "minimum": 0, "maximum": 65533, "unit": "W"},
}


def _write(directory: Path, name: str, data: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


def _catalog(tmp_path: Path, **files: dict[str, Any]) -> MraCatalog:
    for name, data in files.items():
        _write(tmp_path, f"{name}.json", data)
    return MraCatalog(quirks_dir=tmp_path)


def test_bundled_quirks_are_valid(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        load_quirks(DEFAULT_QUIRKS_DIR)
    assert not caplog.records


def test_without_quirks_the_mra_is_used(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert catalog.get_device_class(PDB_KEY) is catalog.get_class(0x0287)


def test_adds_vendor_property(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, a={"match": {"eoj": "0x0287"}, "elProperties": [VENDOR_POWER]})
    class_def = catalog.get_device_class(PDB_KEY)
    prop = class_def.get(0xF0)
    assert prop.name.ja == "独自電力"
    assert prop.get and not prop.set
    assert decode_property(class_def, 0xF0, {0xF0: b"\x01\x00"}) == 256
    assert class_def.quirks == ("a.json",)
    # the plain class is untouched
    assert catalog.get_class(0x0287).get(0xF0) is None


def test_matching_by_manufacturer_and_product(tmp_path: Path) -> None:
    def named(name: str) -> dict[str, Any]:
        return {**VENDOR_POWER, "propertyName": {"ja": name, "en": name}}

    catalog = _catalog(
        tmp_path,
        # listed out of order on purpose: application follows specificity
        c_product={
            "match": {"eoj": "0x0287", "manufacturer": "0x00000B", "productCode": "MKN7350S1"},
            "elProperties": [named("product")],
        },
        a_class={"match": {"eoj": "0x0287"}, "elProperties": [named("class")]},
        b_maker={
            "match": {"eoj": "0x0287", "manufacturer": "0x00000B"},
            "elProperties": [named("maker")],
        },
    )
    assert catalog.get_device_class(PDB_KEY).get(0xF0).name.en == "product"
    other_product = DeviceKey(0x0287, PANASONIC, "OTHER")
    assert catalog.get_device_class(other_product).get(0xF0).name.en == "maker"
    assert catalog.get_device_class(DeviceKey(0x0287, 0x000005, None)).get(0xF0).name.en == "class"
    assert catalog.get_device_class(DeviceKey(0x0130)).get(0xF0) is None
    assert catalog.get_device_class(PDB_KEY).quirks == (
        "a_class.json",
        "b_maker.json",
        "c_product.json",
    )


def test_partial_override_and_remove(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        a={
            "match": {"eoj": "0x0287"},
            "elProperties": [
                {"epc": "0xC0", "homeassistant": {"enabled": False, "category": "diagnostic"}},
                {"epc": "0xC6", "propertyName": {"ja": "主幹電力", "en": "Main power"}},
                {"epc": "0xC8", "remove": True},
            ],
        },
    )
    base = catalog.get_class(0x0287)
    class_def = catalog.get_device_class(PDB_KEY)
    c0 = class_def.get(0xC0)
    assert c0.schema == base.get(0xC0).schema
    assert c0.name == base.get(0xC0).name
    assert c0.hints == {"enabled": False, "category": "diagnostic"}
    assert class_def.get(0xC6).name.en == "Main power"
    assert class_def.get(0xC6).schema == base.get(0xC6).schema
    assert class_def.get(0xC8) is None


def test_user_defined_class(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        a={
            "match": {"eoj": "0x0F02", "manufacturer": "0x00000B"},
            "className": {"ja": "独自機器", "en": "Vendor device"},
            "shortName": "vendorDevice",
            "maxProperties": 4,
            "elProperties": [VENDOR_POWER],
        },
    )
    class_def = catalog.get_device_class(DeviceKey(0x0F02, PANASONIC, None))
    assert class_def.known
    assert class_def.name.ja == "独自機器"
    assert class_def.short_name == "vendorDevice"
    assert class_def.max_properties == 4
    assert class_def.get(0x80) is not None  # super class properties still apply


def test_extension_override(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, a={"match": {"eoj": "0x0287"}, "extension": None})
    assert extension_for(catalog.get_class(0x0287)) is not None
    assert extension_for(catalog.get_device_class(PDB_KEY)) is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "top level"),
        ({"elProperties": []}, "'match' is required"),
        ({"match": {"eoj": "0x0287", "maker": "0x0B"}}, "unknown key"),
        ({"match": {"eoj": "0x287X"}}, "not hex"),
        ({"match": {"eoj": "0x0287"}, "maxProperties": 0}, "maxProperties"),
        ({"match": {"eoj": "0x0287"}, "elProperties": [{"epc": "0x10"}]}, "invalid epc"),
        (
            {"match": {"eoj": "0x0287"}, "elProperties": [VENDOR_POWER, VENDOR_POWER]},
            "listed twice",
        ),
        (
            {"match": {"eoj": "0x0287"}, "elProperties": [{"epc": "0xC0", "remove": True, "x": 1}]},
            "cannot be combined",
        ),
        (
            {
                "match": {"eoj": "0x0287"},
                "elProperties": [{"epc": "0xC0", "homeassistant": {"category": "hidden"}}],
            },
            "category",
        ),
    ],
)
def test_invalid_quirks(data: Any, message: str) -> None:
    with pytest.raises(QuirkError, match=message):
        parse_quirk(data, "test.json")


def test_invalid_files_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    _write(tmp_path, "bad.json", {"match": {}})
    _write(tmp_path, "good.json", {"match": {"eoj": "0x0287"}, "elProperties": [VENDOR_POWER]})
    with caplog.at_level(logging.ERROR):
        quirks = load_quirks(tmp_path)
    assert [q.source for q in quirks] == ["good.json"]
    assert len(caplog.records) == 2


def test_new_property_without_data_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = _catalog(tmp_path, a={"match": {"eoj": "0x0287"}, "elProperties": [{"epc": "0xF0"}]})
    with caplog.at_level(logging.ERROR):
        assert catalog.get_device_class(PDB_KEY).get(0xF0) is None
    assert "'data' is required" in caplog.text


async def test_network_uses_quirks(
    tmp_path: Path, echonet: EchonetClient, fake_node: FakeNode
) -> None:
    vendor = EOJ(0x0F, 0x02, 0x01)
    fake_node.objects[vendor] = FakeObject(
        {0x80: b"\x30", 0x8A: bytes.fromhex("00000B"), 0xF0: b"\x00\x64", 0xF1: b"\x01"}
    )
    fake_node.objects[EOJ(0x0F, 0x42, 0x01)] = FakeObject({0x80: b"\x30"})
    catalog = _catalog(
        tmp_path,
        a={
            "match": {"eoj": "0x0F02", "manufacturer": "0x00000B"},
            "className": {"ja": "独自機器", "en": "Vendor device"},
            "maxProperties": 1,
            "elProperties": [VENDOR_POWER],
        },
    )
    network = EchonetNetwork(echonet, catalog, hosts=[FAKE_HOST])
    # only the user-defined object with a quirk is kept
    [device] = await network.async_discover()
    assert device.eoj == vendor

    fake_node.requests.clear()
    await network.async_poll()
    assert device.decode(0xF0) == 100
    assert {len(frame.properties) for frame in fake_node.requests} == {1}
