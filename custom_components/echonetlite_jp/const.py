"""Constants for the ECHONET Lite JP integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "echonetlite_jp"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_HOSTS = "hosts"
CONF_INTERFACE = "interface"
# {device id: {group kind: [name of 1, name of 2, ...]}}; "" keeps the default name.
CONF_GROUP_NAMES = "group_names"

DEFAULT_SCAN_INTERVAL = 30
DISCOVERY_WAIT = 3.0
REDISCOVERY_INTERVAL = timedelta(minutes=30)
# A second look soon after startup catches nodes that were busy or missed the
# first discovery, instead of leaving them out for a full interval.
STARTUP_REDISCOVERY = timedelta(minutes=2)
# Minimum gap between rediscoveries triggered by devices (lost device, new instance).
REDISCOVERY_COOLDOWN = timedelta(minutes=5)
