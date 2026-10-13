"""Class-specific extensions.

An extension is chosen by class code by default; a quirk can select another
one by name (``"extension": "<name>"``) or disable it (``"extension": null``).
"""

from __future__ import annotations

import logging

from ..device import ClassExtension
from ..mra import ClassDef
from ..quirks import UNCHANGED
from .power_distribution_board import PowerDistributionBoard

_LOGGER = logging.getLogger(__name__)

EXTENSIONS: dict[str, ClassExtension] = {
    "power_distribution_board": PowerDistributionBoard(),
}
DEFAULT_EXTENSIONS: dict[int, str] = {
    0x0287: "power_distribution_board",
}


def extension_for(class_def: ClassDef) -> ClassExtension | None:
    name = class_def.extension
    if name == UNCHANGED:
        name = DEFAULT_EXTENSIONS.get(class_def.class_code)
    if name is None:
        return None
    extension = EXTENSIONS.get(name)
    if extension is None:
        _LOGGER.error("unknown extension %r requested by %s", name, ", ".join(class_def.quirks))
    return extension
