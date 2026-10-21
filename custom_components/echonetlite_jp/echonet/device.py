"""State of one ECHONET object (device) on the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .client import IDENTIFICATION_EPCS, PROPERTY_MAP_EPCS, ObjectInfo
from .codec import CodecError, CoefficientLookup, coefficient_lookup, decode_property
from .mra import ClassDef, Text
from .protocol import EOJ

if TYPE_CHECKING:
    from .client import EchonetClient


@dataclass(frozen=True)
class SensorGroup:
    """Sensors that belong to one numbered thing the user may name,
    such as one circuit of a distribution board."""

    kind: str  # e.g. "ch"; the same kind is numbered from 1
    index: int
    label: Text  # the kind without number, e.g. "計測チャンネル"


@dataclass(frozen=True)
class DerivedSensor:
    """A value a class extension computes beyond plain EPCs."""

    key: str
    name: Text
    unit: str | None
    total_increasing: bool = False
    group: SensorGroup | None = None
    measure: Text | None = None  # the name without the group, e.g. "積算電力量"


class ClassExtension(Protocol):
    """Class-specific behaviour that the generic MRA handling cannot express."""

    class_code: int

    def handled_epcs(self, device: EchonetDevice) -> frozenset[int]:
        """EPCs the extension takes over (not polled or exposed generically)."""

    async def async_poll(self, client: EchonetClient, device: EchonetDevice) -> dict[str, Any]:
        """Fetch derived values, keyed like :meth:`sensors`."""

    def sensors(self, device: EchonetDevice) -> list[DerivedSensor]:
        """Derived sensors for the device, based on its last poll."""


@dataclass
class EchonetDevice:
    node_uid: str
    info: ObjectInfo
    class_def: ClassDef
    values: dict[int, bytes] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    extension: ClassExtension | None = None

    @property
    def id(self) -> str:
        return f"{self.node_uid}-{self.eoj}"

    @property
    def eoj(self) -> EOJ:
        return self.info.eoj

    @property
    def host(self) -> str:
        return self.info.host

    def decode(self, epc: int) -> Any:
        """Decoded value of ``epc`` or None when missing/undecodable."""
        try:
            return decode_property(self.class_def, epc, self.values, self.info.maps.get)
        except CodecError:
            return None

    def coefficients(self) -> CoefficientLookup:
        """Coefficient lookup for decoding values of this device by hand."""
        return coefficient_lookup(self.class_def, self.values, self.info.maps.get)

    def sensor_groups(self) -> dict[str, tuple[Text, int]]:
        """Nameable groups of the extension's sensors: kind -> (label, count)."""
        groups: dict[str, tuple[Text, int]] = {}
        if self.extension is None:
            return groups
        for sensor in self.extension.sensors(self):
            if (group := sensor.group) is not None:
                count = max(group.index, groups.get(group.kind, (group.label, 0))[1])
                groups[group.kind] = (group.label, count)
        return groups

    def poll_epcs(self) -> list[int]:
        """Readable EPCs worth polling for state."""
        skip = IDENTIFICATION_EPCS | PROPERTY_MAP_EPCS | self.class_def.atomic_epcs
        if self.extension is not None:
            skip |= self.extension.handled_epcs(self)
        return sorted(self.info.maps.get - skip)
