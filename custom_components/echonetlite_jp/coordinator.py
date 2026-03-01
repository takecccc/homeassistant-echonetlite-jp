from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed

from .client import HemsEchonetClient
from .const import CONF_CIDR
from .const import CONF_DISCOVERY_WAIT
from .const import CONF_EOJ
from .const import CONF_LISTEN_HOST
from .const import CONF_LISTEN_PORT
from .const import CONF_MAX_OPC
from .const import CONF_MRA_DIR
from .const import CONF_REDISCOVER_ON_ERROR
from .const import CONF_REFRESH_INTERVAL
from .const import CONF_TIMEOUT
from .const import DEFAULT_SCAN_INTERVAL
from .const import DOMAIN


class HemsEchonetCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        scan_interval = int(entry_data.get("scan_interval", DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            logger=hass.data[DOMAIN]["logger"],
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = HemsEchonetClient(
            host=entry_data.get("host", ""),
            eoj=entry_data.get(CONF_EOJ, ""),
            cidr=entry_data.get(CONF_CIDR, ""),
            listen_host=entry_data.get(CONF_LISTEN_HOST, "0.0.0.0"),
            listen_port=int(entry_data.get(CONF_LISTEN_PORT, 3610)),
            discovery_wait=float(entry_data.get(CONF_DISCOVERY_WAIT, 2.0)),
            timeout=float(entry_data.get(CONF_TIMEOUT, 3.0)),
            refresh_interval=float(entry_data.get(CONF_REFRESH_INTERVAL, 86400.0)),
            max_opc=int(entry_data.get(CONF_MAX_OPC, 24)),
            rediscover_on_error=bool(entry_data.get(CONF_REDISCOVER_ON_ERROR, True)),
            mra_dir=str(entry_data.get(CONF_MRA_DIR, "")),
            debug=bool(entry_data.get("debug", False)),
        )

    async def async_config_entry_first_refresh(self) -> None:  # type: ignore[override]
        try:
            await self.client.async_initialize()
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailed(f"failed to initialize pychonet client: {exc}") from exc
        await super().async_config_entry_first_refresh()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self.client.async_fetch()
        except Exception as exc:  # noqa: BLE001
            raise UpdateFailed(f"failed to fetch echonet data: {exc}") from exc
