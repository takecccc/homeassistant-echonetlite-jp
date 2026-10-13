"""Class-specific extensions, keyed by two-byte class code."""

from __future__ import annotations

from ..device import ClassExtension
from .power_distribution_board import PowerDistributionBoard

EXTENSIONS: dict[int, ClassExtension] = {ext.class_code: ext for ext in (PowerDistributionBoard(),)}
