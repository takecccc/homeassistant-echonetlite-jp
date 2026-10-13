"""Map MRA property definitions onto Home Assistant entities."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.util import dt as dt_util

from .echonet.codec import EnumValue, enum_values, is_binary_state, primary
from .echonet.device import DerivedSensor, EchonetDevice
from .echonet.mra import PropertyDef, Schema

EPC_OPERATION_STATUS = 0x80
EPC_INSTALLATION_LOCATION = 0x81
EPC_FAULT_STATUS = 0x88

# Super class properties (0x80-0x9F) that are about the device's management
# rather than what it measures or controls.
_PRIMARY_SUPER_EPCS = frozenset({0x80, 0x84, 0x85, 0x88, 0x8F})
# The device's own clock (current time/date) changes every minute and is rarely
# useful; registered disabled unless a quirk says otherwise.
_CLOCK_EPCS = frozenset({0x97, 0x98})
_CLASS_WATER_METER = 0x0281
_CLASS_GAS_METER = 0x0282

_CATEGORIES = {
    None: None,
    "diagnostic": EntityCategory.DIAGNOSTIC,
    "config": EntityCategory.CONFIG,
}

# MRA unit -> (HA unit, device class)
_UNITS: dict[str, tuple[str, SensorDeviceClass | None]] = {
    "W": (UnitOfPower.WATT, SensorDeviceClass.POWER),
    "kW": (UnitOfPower.KILO_WATT, SensorDeviceClass.POWER),
    "Wh": (UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY),
    "kWh": (UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
    "MJ": (UnitOfEnergy.MEGA_JOULE, SensorDeviceClass.ENERGY),
    "A": (UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
    "mA": (UnitOfElectricCurrent.MILLIAMPERE, SensorDeviceClass.CURRENT),
    "V": (UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    "Celsius": (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE),
    "ms": (UnitOfTime.MILLISECONDS, SensorDeviceClass.DURATION),
    "second": (UnitOfTime.SECONDS, SensorDeviceClass.DURATION),
    "minute": (UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "minutes": (UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "days": (UnitOfTime.DAYS, SensorDeviceClass.DURATION),
    "L": (UnitOfVolume.LITERS, None),
    "m3": (UnitOfVolume.CUBIC_METERS, None),
    "m3/h": (UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR, SensorDeviceClass.VOLUME_FLOW_RATE),
    "%": (PERCENTAGE, None),
    "ppm": ("ppm", None),
    "lux": ("lx", SensorDeviceClass.ILLUMINANCE),
    "degree": ("°", None),
    "r/min": ("rpm", None),
}
_ACCUMULATING_CLASSES = {
    SensorDeviceClass.ENERGY,
    SensorDeviceClass.GAS,
    SensorDeviceClass.WATER,
}

# 0x81 installation location: bits 7-3 location code, bits 2-0 location number.
_LOCATIONS = {
    0x01: ("リビング", "Living room"),
    0x02: ("ダイニング", "Dining room"),
    0x03: ("キッチン", "Kitchen"),
    0x04: ("浴室", "Bathroom"),
    0x05: ("トイレ", "Lavatory"),
    0x06: ("洗面所", "Washroom"),
    0x07: ("廊下", "Passageway"),
    0x08: ("部屋", "Room"),
    0x09: ("階段", "Stairway"),
    0x0A: ("玄関", "Front door"),
    0x0B: ("納戸", "Storeroom"),
    0x0C: ("庭", "Garden"),
    0x0D: ("車庫", "Garage"),
    0x0E: ("ベランダ", "Veranda"),
    0x0F: ("その他", "Other"),
}


@dataclass(frozen=True, kw_only=True)
class PropertySpec:
    """What one entity shows and how to read it from an EchonetDevice."""

    platform: Platform
    key: str
    object_id: str
    name: str | None
    epc: int | None = None
    item: str | None = None  # element shortName inside an object property
    derived_key: str | None = None
    schema: Schema = field(default_factory=dict)
    raw: bool = False
    unit: str | None = None
    device_class: str | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True
    precision: int | None = None
    options: tuple[str, ...] | None = None


def device_specs(device: EchonetDevice, language: str) -> list[PropertySpec]:
    specs: list[PropertySpec] = []
    for epc in device.poll_epcs():
        prop = device.class_def.get(epc)
        hints = prop.hints if prop is not None else {}
        if epc in _CLOCK_EPCS:
            hints = {"enabled": False, **hints}
        specs.extend(_apply_hints(spec, hints) for spec in _property_specs(device, epc, language))
    if device.extension is not None:
        specs.extend(_derived_spec(sensor, language) for sensor in device.extension.sensors(device))
    return specs


def _apply_hints(spec: PropertySpec, hints: dict[str, Any]) -> PropertySpec:
    """Apply presentation overrides from quirks."""
    changes: dict[str, Any] = {}
    if "enabled" in hints:
        changes["enabled_default"] = hints["enabled"]
    if "category" in hints:
        changes["entity_category"] = _CATEGORIES[hints["category"]]
    return replace(spec, **changes) if changes else spec


def read_value(device: EchonetDevice, spec: PropertySpec, language: str) -> Any:
    """Current value converted for Home Assistant (None when unknown)."""
    if spec.derived_key is not None:
        return device.derived.get(spec.derived_key)
    assert spec.epc is not None
    if spec.raw:
        edt = device.values.get(spec.epc)
        return edt.hex().upper() if edt is not None else None
    if spec.epc == EPC_INSTALLATION_LOCATION:
        return _location(device.values.get(spec.epc), language)
    value = device.decode(spec.epc)
    if spec.item is not None:
        value = value.get(spec.item) if isinstance(value, dict) else None
    if isinstance(value, EnumValue):
        if spec.platform in (Platform.BINARY_SENSOR, Platform.SWITCH):
            return value.is_on
        if spec.schema.get("type") == "state":
            return value.label.get(language)
        return None  # special code such as "no data" in place of a number
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    if isinstance(value, dt.time):
        return value.isoformat(timespec="minutes")
    if isinstance(value, (bytes, dict, list)):
        return None
    return value


def find_option(spec: PropertySpec, label: str, language: str) -> EnumValue | None:
    for item in enum_values(spec.schema):
        if item.label.get(language) == label:
            return item
    return None


# ---------------------------------------------------------------- internals


def _property_specs(device: EchonetDevice, epc: int, language: str) -> list[PropertySpec]:
    prop = device.class_def.get(epc)
    base = snake_case(f"epc_{epc:02x}" if prop is None else prop.short_name or prop.name.en)
    if prop is None:
        return [
            PropertySpec(
                platform=Platform.SENSOR,
                key=f"{epc:02x}",
                object_id=base,
                name=f"EPC 0x{epc:02X}",
                epc=epc,
                raw=True,
                entity_category=EntityCategory.DIAGNOSTIC,
                enabled_default=False,
            )
        ]

    name = prop.name.get(language)
    diagnostic = 0x80 <= epc <= 0x9F and epc not in _PRIMARY_SUPER_EPCS
    category = EntityCategory.DIAGNOSTIC if diagnostic else None
    common: dict[str, Any] = {"key": f"{epc:02x}", "object_id": base, "name": name, "epc": epc}

    if epc == EPC_INSTALLATION_LOCATION:
        return [PropertySpec(platform=Platform.SENSOR, entity_category=category, **common)]

    schema = primary(prop.schema)
    kind = schema.get("type")
    settable = epc in device.info.maps.set and prop.set

    if kind == "state":
        if is_binary_state(schema):
            platform = Platform.SWITCH if settable else Platform.BINARY_SENSOR
            device_class = BinarySensorDeviceClass.PROBLEM if epc == EPC_FAULT_STATUS else None
            if epc == EPC_OPERATION_STATUS and settable:
                common["name"] = None  # the device's main switch carries the device name
            return [
                PropertySpec(
                    platform=platform,
                    schema=schema,
                    device_class=device_class,
                    entity_category=category,
                    **common,
                )
            ]
        options = tuple(e.label.get(language) for e in enum_values(schema))
        if settable:
            return [
                PropertySpec(
                    platform=Platform.SELECT,
                    schema=schema,
                    options=options,
                    entity_category=EntityCategory.CONFIG if diagnostic else None,
                    **common,
                )
            ]
        return [
            PropertySpec(
                platform=Platform.SENSOR,
                schema=schema,
                options=options,
                device_class=SensorDeviceClass.ENUM,
                entity_category=category,
                **common,
            )
        ]

    if kind in ("number", "level"):
        if settable:
            return [
                PropertySpec(
                    platform=Platform.NUMBER,
                    schema=schema,
                    entity_category=EntityCategory.CONFIG if diagnostic else None,
                    **_numeric_attrs(device, prop, schema, number=True),
                    **common,
                )
            ]
        return [
            PropertySpec(
                platform=Platform.SENSOR,
                schema=schema,
                entity_category=category,
                **_numeric_attrs(device, prop, schema),
                **common,
            )
        ]

    if kind == "numericValue":
        return [
            PropertySpec(
                platform=Platform.SENSOR,
                schema=schema,
                entity_category=EntityCategory.DIAGNOSTIC,
                **common,
            )
        ]

    if kind in ("date", "date-time", "time"):
        return [
            PropertySpec(
                platform=Platform.SENSOR,
                schema=schema,
                device_class={
                    "date": SensorDeviceClass.DATE,
                    "date-time": SensorDeviceClass.TIMESTAMP,
                }.get(kind),
                entity_category=category,
                **common,
            )
        ]

    if kind == "object":
        specs = [
            spec
            for element in schema.get("properties") or []
            if (
                spec := _element_spec(
                    device, prop, element, base=base, category=category, language=language
                )
            )
        ]
        if specs:
            return specs

    return [
        PropertySpec(
            platform=Platform.SENSOR,
            raw=True,
            entity_category=EntityCategory.DIAGNOSTIC,
            enabled_default=False,
            **common,
        )
    ]


def _element_spec(
    device: EchonetDevice,
    prop: PropertyDef,
    element: dict[str, Any],
    *,
    base: str,
    category: EntityCategory | None,
    language: str,
) -> PropertySpec | None:
    item = str(element.get("shortName") or "")
    schema = primary(element.get("element") or {})
    kind = schema.get("type")
    if not item or kind not in ("number", "level", "state", "date-time", "date"):
        return None
    element_names = element.get("elementName") or {}
    element_name = element_names.get("ja" if language.startswith("ja") else "en") or item
    common: dict[str, Any] = {
        "platform": Platform.SENSOR,
        "key": f"{prop.epc:02x}_{item}",
        "object_id": f"{base}_{snake_case(item)}",
        "name": f"{prop.name.get(language)} {element_name}",
        "epc": prop.epc,
        "item": item,
        "schema": schema,
        "entity_category": category,
    }
    if kind in ("number", "level"):
        return PropertySpec(**common, **_numeric_attrs(device, prop, schema, item=item))
    if kind == "state":
        return PropertySpec(
            **common,
            device_class=SensorDeviceClass.ENUM,
            options=tuple(e.label.get(language) for e in enum_values(schema)),
        )
    return PropertySpec(
        **common,
        device_class=SensorDeviceClass.DATE if kind == "date" else SensorDeviceClass.TIMESTAMP,
    )


def _numeric_attrs(
    device: EchonetDevice,
    prop: PropertyDef,
    schema: Schema,
    *,
    item: str | None = None,
    number: bool = False,
) -> dict[str, Any]:
    unit, device_class = _UNITS.get(str(schema.get("unit")), (schema.get("unit"), None))
    hint = f"{prop.short_name} {item or ''}".lower()
    if unit == UnitOfVolume.CUBIC_METERS and device.eoj.class_code == _CLASS_GAS_METER:
        device_class = SensorDeviceClass.GAS
    elif unit in (UnitOfVolume.CUBIC_METERS, UnitOfVolume.LITERS) and (
        device.eoj.class_code == _CLASS_WATER_METER
    ):
        device_class = SensorDeviceClass.WATER
    elif unit == PERCENTAGE and "humidity" in hint:
        device_class = SensorDeviceClass.HUMIDITY
    elif unit == "ppm" and "co2" in hint:
        device_class = SensorDeviceClass.CO2

    attrs: dict[str, Any] = {"unit": unit, "device_class": device_class}
    multiple = schema.get("multiple")
    if isinstance(multiple, (int, float)) and multiple < 1:
        attrs["precision"] = max(0, -int(f"{multiple:e}".split("e")[1]))
    if number:
        return attrs

    cumulative = "cumulative" in hint or "積算" in prop.name.ja
    if device_class in _ACCUMULATING_CLASSES or unit in (UnitOfVolume.CUBIC_METERS, "kvarh"):
        attrs["state_class"] = SensorStateClass.TOTAL_INCREASING if cumulative else None
    elif unit is not None:
        attrs["state_class"] = SensorStateClass.MEASUREMENT
    return attrs


def _derived_spec(sensor: DerivedSensor, language: str) -> PropertySpec:
    unit, device_class = _UNITS.get(sensor.unit or "", (sensor.unit, None))
    return PropertySpec(
        platform=Platform.SENSOR,
        key=sensor.key,
        object_id=sensor.key,
        name=sensor.name.get(language),
        derived_key=sensor.key,
        unit=unit,
        device_class=device_class,
        state_class=(
            SensorStateClass.TOTAL_INCREASING
            if sensor.total_increasing
            else SensorStateClass.MEASUREMENT
        ),
    )


def snake_case(name: str) -> str:
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _location(edt: bytes | None, language: str) -> str | None:
    if not edt:
        return None
    ja = language.startswith("ja")
    code = edt[0]
    if len(edt) != 1 or code == 0x01:
        return "位置情報" if ja else "Position information"
    if code == 0x00:
        return "未設定" if ja else "Not set"
    if code == 0xFF:
        return "不定" if ja else "Indefinite"
    names = _LOCATIONS.get(code >> 3)
    if names is None:
        return f"0x{code:02X}"
    label = names[0] if ja else names[1]
    number = code & 0x07
    return f"{label} {number}" if number else label
