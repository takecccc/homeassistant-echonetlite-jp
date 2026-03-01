from __future__ import annotations

DOMAIN = "echonetlite_jp"
PLATFORMS = ["sensor", "switch", "number", "select"]

CONF_EOJ = "eoj"
CONF_CIDR = "cidr"
CONF_LISTEN_HOST = "listen_host"
CONF_LISTEN_PORT = "listen_port"
CONF_DISCOVERY_WAIT = "discovery_wait"
CONF_TIMEOUT = "timeout"
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_MAX_OPC = "max_opc"
CONF_REDISCOVER_ON_ERROR = "rediscover_on_error"
CONF_MRA_DIR = "mra_dir"

DEFAULT_EOJ = ""
DEFAULT_CIDR = ""
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 3610
DEFAULT_DISCOVERY_WAIT = 2.0
DEFAULT_TIMEOUT = 3.0
DEFAULT_REFRESH_INTERVAL = 86400.0
DEFAULT_MAX_OPC = 24
DEFAULT_REDISCOVER_ON_ERROR = True
DEFAULT_MRA_DIR = ""
DEFAULT_SCAN_INTERVAL = 30
