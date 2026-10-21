"""Device inventory and polling, independent of Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import replace

from .classes import extension_for
from .client import EPC_INSTANCE_LIST_NOTIFICATION, EchonetClient
from .device import EchonetDevice
from .errors import EchonetError, EchonetTimeoutError
from .mra import MraCatalog
from .protocol import Frame
from .quirks import DeviceKey

_LOGGER = logging.getLogger(__name__)


class EchonetNetwork:
    """Keeps track of ECHONET objects and their latest property values.

    Devices are identified by node UID + EOJ so that they survive DHCP
    address changes.
    """

    def __init__(
        self,
        client: EchonetClient,
        catalog: MraCatalog,
        *,
        hosts: list[str] | None = None,
        discovery_wait: float = 2.0,
        include_user_defined: bool = False,
    ) -> None:
        self.client = client
        self.catalog = catalog
        self.devices: dict[str, EchonetDevice] = {}
        # Fixed addresses; empty means multicast discovery.
        self.hosts = list(hosts or [])
        self._discovery_wait = discovery_wait
        # Class group 0x0F is vendor-defined; only objects described by a quirk
        # are kept unless explicitly requested.
        self._include_user_defined = include_user_defined
        self.rediscovery_requested = False

    async def async_discover(self) -> list[EchonetDevice]:
        """Find nodes and their objects. Returns devices seen for the first time."""
        hosts = self.hosts or await self.client.discover(self._discovery_wait)
        found = await asyncio.gather(*(self._discover_host(host) for host in hosts))
        new: list[EchonetDevice] = []
        for devices in found:
            for device in devices:
                existing = self.devices.get(device.id)
                if existing is None:
                    self.devices[device.id] = device
                    new.append(device)
                else:
                    existing.info = device.info
        self.rediscovery_requested = False
        return new

    async def async_poll(self) -> None:
        by_host: dict[str, list[EchonetDevice]] = defaultdict(list)
        for device in self.devices.values():
            by_host[device.host].append(device)
        await asyncio.gather(*(self._poll_host(devices) for devices in by_host.values()))

    async def async_poll_device(self, device: EchonetDevice) -> None:
        try:
            result = await self.client.get(
                device.host,
                device.eoj,
                device.poll_epcs(),
                max_properties=device.class_def.max_properties,
            )
        except EchonetTimeoutError:
            if device.available:
                _LOGGER.info("%s (%s) stopped responding", device.id, device.host)
                # It may have a new address (DHCP); look for it on the network.
                if not self.hosts:
                    self.rediscovery_requested = True
            device.available = False
            return
        # Coefficients (units, multipliers) do not change: keep the last value
        # rather than scaling energy readings wrongly after one lost answer.
        for epc in result.missing - device.class_def.coefficient_epcs:
            device.values.pop(epc, None)
        device.values.update(result.values)
        if not device.available:
            _LOGGER.info("%s (%s) is responding again", device.id, device.host)
        device.available = True
        if device.extension is not None:
            try:
                # Values the extension could not read this time keep their
                # previous state instead of flickering to unknown.
                device.derived.update(await device.extension.async_poll(self.client, device))
            except EchonetError as err:
                _LOGGER.debug("%s: extension poll failed: %s", device.id, err)

    def handle_notification(self, host: str, frame: Frame) -> EchonetDevice | None:
        """Apply an INF/INFC frame. Returns the updated device, if any."""
        if frame.seoj.is_node_profile:
            if any(p.epc == EPC_INSTANCE_LIST_NOTIFICATION for p in frame.properties):
                self.rediscovery_requested = True
            return None
        for device in self.devices.values():
            if device.host == host and device.eoj == frame.seoj:
                device.values.update({p.epc: p.edt for p in frame.properties if p.edt})
                device.available = True
                return device
        if not self.hosts:
            self.rediscovery_requested = True
        return None

    async def _poll_host(self, devices: list[EchonetDevice]) -> None:
        # One request at a time per node; many nodes cannot handle more.
        for index, device in enumerate(devices):
            await self.async_poll_device(device)
            if not device.available:
                # The node is down; don't wait for a timeout per object.
                for other in devices[index + 1 :]:
                    other.available = False
                return

    async def _discover_host(self, host: str) -> list[EchonetDevice]:
        try:
            node = await self.client.get_node(host)
        except EchonetError as err:
            _LOGGER.warning("cannot read node profile of %s: %s", host, err)
            return []
        devices: list[EchonetDevice] = []
        for eoj in node.instances:
            if eoj.is_node_profile:
                continue
            try:
                info = await self.client.get_object(host, eoj)
            except EchonetError as err:
                _LOGGER.warning("cannot read %s on %s: %s", eoj, host, err)
                continue
            uid = node.uid or info.uid
            if uid is None:
                _LOGGER.warning("%s does not report a UID; identifying it by address", host)
                uid = f"ip-{host}"
            if info.manufacturer is None and node.manufacturer is not None:
                info = replace(info, manufacturer=node.manufacturer)
            if info.manufacturer_code is None and node.manufacturer_code is not None:
                info = replace(info, manufacturer_code=node.manufacturer_code)
            key = DeviceKey(eoj.class_code, info.manufacturer_code, info.product_code)
            class_def = await asyncio.to_thread(self.catalog.get_device_class, key)
            if eoj.group == 0x0F and not class_def.quirks and not self._include_user_defined:
                continue
            if class_def.quirks:
                _LOGGER.debug("%s %s: applying %s", host, eoj, ", ".join(class_def.quirks))
            devices.append(
                EchonetDevice(
                    node_uid=uid,
                    info=info,
                    class_def=class_def,
                    extension=extension_for(class_def),
                )
            )
        return devices
