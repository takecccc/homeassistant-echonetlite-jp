"""Base entity for ECHONET Lite objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EchonetConfigEntry, EchonetCoordinator
from .echonet.device import EchonetDevice
from .properties import PropertySpec, device_specs, read_value, snake_case


class EchonetEntity(CoordinatorEntity[EchonetCoordinator]):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EchonetCoordinator, device: EchonetDevice, spec: PropertySpec
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self.spec = spec
        self._attr_unique_id = f"{device.id}-{spec.key}"
        self._attr_name = spec.name
        self._attr_entity_category = spec.entity_category
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._attr_device_info = device_info(device, coordinator.language)
        # Suggest a stable English entity_id; Home Assistant would otherwise
        # romanize the (possibly Japanese) names.
        suffix = "" if spec.name is None else f"_{spec.object_id}"
        self.entity_id = f"{spec.platform}.{device_slug(device)}{suffix}"

    @property
    def device(self) -> EchonetDevice:
        return self.coordinator.network.devices[self._device_id]

    @property
    def available(self) -> bool:
        return super().available and self.device.available

    @property
    def current_value(self) -> Any:
        return read_value(self.device, self.spec, self.coordinator.language)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.spec.epc is None:
            return None
        return {"epc": f"0x{self.spec.epc:02X}"}


def device_slug(device: EchonetDevice) -> str:
    slug = snake_case(device.class_def.short_name) or f"echonet_{device.eoj.class_code:04x}"
    return f"{slug}_{device.eoj.instance}" if device.eoj.instance > 1 else slug


def device_info(device: EchonetDevice, language: str) -> DeviceInfo:
    class_name = device.class_def.name.get(language)
    name = f"{class_name} {device.eoj.instance}" if device.eoj.instance > 1 else class_name
    return DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        name=name,
        manufacturer=device.info.manufacturer,
        model=class_name,
        model_id=device.info.product_code,
        serial_number=device.info.serial_number,
    )


def async_setup_platform(
    entry: EchonetConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: Platform,
    factory: Callable[[EchonetCoordinator, EchonetDevice, PropertySpec], Entity],
) -> None:
    """Add entities for ``platform`` now and whenever new devices or values appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new: list[Entity] = []
        for device in coordinator.network.devices.values():
            for spec in device_specs(device, coordinator.language):
                uid = f"{device.id}-{spec.key}"
                if spec.platform is platform and uid not in known:
                    known.add(uid)
                    new.append(factory(coordinator, device, spec))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
