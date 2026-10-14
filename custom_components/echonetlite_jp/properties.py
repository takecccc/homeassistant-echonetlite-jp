"""Map MRA property definitions onto Home Assistant entities."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from homeassistant.components.binary_sensor import (  # type: ignore[attr-defined]
    BinarySensorDeviceClass,
)
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
from .echonet.location import decode_location
from .echonet.mra import PropertyDef, Schema, Text

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
        location = decode_location(device.values.get(spec.epc, b""))
        return location.get(language) if location else None
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

_CONTROLS = frozenset({Platform.NUMBER, Platform.SELECT, Platform.SWITCH})
_TEMPORAL_CLASSES = {
    "date": SensorDeviceClass.DATE,
    "date-time": SensorDeviceClass.TIMESTAMP,
    "time": None,  # shown as "HH:MM" text
}


@dataclass(frozen=True)
class _Property:
    """One MRA property of one device, as input for the builders below."""

    device: EchonetDevice
    prop: PropertyDef
    language: str

    @property
    def epc(self) -> int:
        return self.prop.epc

    @property
    def schema(self) -> Schema:
        return primary(self.prop.schema)

    @property
    def settable(self) -> bool:
        return self.prop.set and self.epc in self.device.info.maps.set

    @property
    def management(self) -> bool:
        """Super class properties about managing the device, not its function."""
        return 0x80 <= self.epc <= 0x9F and self.epc not in _PRIMARY_SUPER_EPCS

    @property
    def object_id(self) -> str:
        return snake_case(self.prop.short_name or self.prop.name.en)

    def spec(self, platform: Platform, **attrs: Any) -> PropertySpec:
        """A spec for the whole property; ``attrs`` override the defaults."""
        defaults: dict[str, Any] = {
            "key": f"{self.epc:02x}",
            "object_id": self.object_id,
            "name": self.prop.name.get(self.language),
            "epc": self.epc,
            "schema": self.schema,
            "entity_category": _category(self.management, platform),
        }
        return PropertySpec(platform=platform, **(defaults | attrs))


def _category(management: bool, platform: Platform) -> EntityCategory | None:
    if not management:
        return None
    return EntityCategory.CONFIG if platform in _CONTROLS else EntityCategory.DIAGNOSTIC


def _property_specs(device: EchonetDevice, epc: int, language: str) -> list[PropertySpec]:
    prop = device.class_def.get(epc)
    if prop is None:
        return [_unknown_spec(epc)]
    p = _Property(device, prop, language)
    if epc == EPC_INSTALLATION_LOCATION:  # raw in the MRA; decoded by location.py
        return [p.spec(Platform.SENSOR)]
    builder = _BUILDERS.get(str(p.schema.get("type")))
    specs = builder(p) if builder is not None else []
    # Anything we cannot present is still available as hex, disabled by default.
    return specs or [
        p.spec(
            Platform.SENSOR,
            raw=True,
            entity_category=EntityCategory.DIAGNOSTIC,
            enabled_default=False,
        )
    ]


def _unknown_spec(epc: int) -> PropertySpec:
    """An EPC the MRA does not define (vendor-specific): disabled hex sensor."""
    return PropertySpec(
        platform=Platform.SENSOR,
        key=f"{epc:02x}",
        object_id=f"epc_{epc:02x}",
        name=f"EPC 0x{epc:02X}",
        epc=epc,
        raw=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
    )


def _state_specs(p: _Property) -> list[PropertySpec]:
    if is_binary_state(p.schema):
        if not p.settable:
            device_class = BinarySensorDeviceClass.PROBLEM if p.epc == EPC_FAULT_STATUS else None
            return [p.spec(Platform.BINARY_SENSOR, device_class=device_class)]
        if p.epc == EPC_OPERATION_STATUS:
            # The device's main switch carries the device name.
            return [p.spec(Platform.SWITCH, name=None)]
        return [p.spec(Platform.SWITCH)]
    options = tuple(e.label.get(p.language) for e in enum_values(p.schema))
    if p.settable:
        return [p.spec(Platform.SELECT, options=options)]
    return [p.spec(Platform.SENSOR, options=options, device_class=SensorDeviceClass.ENUM)]


def _numeric_specs(p: _Property) -> list[PropertySpec]:
    if p.settable:
        return [p.spec(Platform.NUMBER, **_numeric_attrs(p, p.schema, number=True))]
    return [p.spec(Platform.SENSOR, **_numeric_attrs(p, p.schema))]


def _numeric_value_specs(p: _Property) -> list[PropertySpec]:
    # Multipliers such as "unit for cumulative energy": only of diagnostic interest.
    return [p.spec(Platform.SENSOR, entity_category=EntityCategory.DIAGNOSTIC)]


def _temporal_specs(p: _Property) -> list[PropertySpec]:
    return [p.spec(Platform.SENSOR, device_class=_TEMPORAL_CLASSES[str(p.schema["type"])])]


def _object_specs(p: _Property) -> list[PropertySpec]:
    """One sensor per element of a composite value (e.g. current per phase)."""
    specs: list[PropertySpec] = []
    for element in p.schema.get("properties") or []:
        item = str(element.get("shortName") or "")
        schema = primary(element.get("element") or {})
        kind = schema.get("type")
        if not item or kind not in ("number", "level", "state", "date", "date-time"):
            continue
        element_name = Text.from_json(element.get("elementName"), item).get(p.language)
        attrs: dict[str, Any] = {
            "key": f"{p.epc:02x}_{item}",
            "object_id": f"{p.object_id}_{snake_case(item)}",
            "name": f"{p.prop.name.get(p.language)} {element_name}",
            "item": item,
            "schema": schema,
        }
        if kind in ("number", "level"):
            attrs |= _numeric_attrs(p, schema, item=item)
        elif kind == "state":
            attrs["device_class"] = SensorDeviceClass.ENUM
            attrs["options"] = tuple(e.label.get(p.language) for e in enum_values(schema))
        else:
            attrs["device_class"] = _TEMPORAL_CLASSES[str(kind)]
        specs.append(p.spec(Platform.SENSOR, **attrs))
    return specs


_BUILDERS: dict[str, Callable[[_Property], list[PropertySpec]]] = {
    "state": _state_specs,
    "number": _numeric_specs,
    "level": _numeric_specs,
    "numericValue": _numeric_value_specs,
    "date": _temporal_specs,
    "date-time": _temporal_specs,
    "time": _temporal_specs,
    "object": _object_specs,
}


def _numeric_attrs(
    p: _Property, schema: Schema, *, item: str | None = None, number: bool = False
) -> dict[str, Any]:
    """Unit, device class, state class and precision for a numeric value."""
    unit, device_class = _UNITS.get(str(schema.get("unit")), (schema.get("unit"), None))
    hint = f"{p.prop.short_name} {item or ''}".lower()
    class_code = p.device.eoj.class_code
    if unit == UnitOfVolume.CUBIC_METERS and class_code == _CLASS_GAS_METER:
        device_class = SensorDeviceClass.GAS
    elif unit in (UnitOfVolume.CUBIC_METERS, UnitOfVolume.LITERS) and (
        class_code == _CLASS_WATER_METER
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

    cumulative = "cumulative" in hint or "積算" in p.prop.name.ja
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
