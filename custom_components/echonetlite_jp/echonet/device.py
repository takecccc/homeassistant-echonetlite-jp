"""State of one ECHONET object (device) on the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .client import IDENTIFICATION_EPCS, PROPERTY_MAP_EPCS, ObjectInfo
from .codec import CodecError, decode_property
from .mra import ClassDef, Text
from .protocol import EOJ

if TYPE_CHECKING:
    from .client import EchonetClient


@dataclass(frozen=True)
class DerivedSensor:
    """A value a class extension computes beyond plain EPCs."""

    key: str
    name: Text
    unit: str | None
    total_increasing: bool = False


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
            return decode_property(self.class_def, epc, self.values)
        except CodecError:
            return None

    def poll_epcs(self) -> list[int]:
        """Readable EPCs worth polling for state."""
        skip = IDENTIFICATION_EPCS | PROPERTY_MAP_EPCS | self.class_def.atomic_epcs
        if self.extension is not None:
            skip |= self.extension.handled_epcs(self)
        return sorted(self.info.maps.get - skip)
