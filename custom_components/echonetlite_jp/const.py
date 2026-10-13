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

DEFAULT_SCAN_INTERVAL = 30
DISCOVERY_WAIT = 3.0
REDISCOVERY_INTERVAL = timedelta(minutes=30)
# Minimum gap between rediscoveries triggered by devices (lost device, new instance).
REDISCOVERY_COOLDOWN = timedelta(minutes=5)
