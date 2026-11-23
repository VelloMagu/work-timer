"""Work Timer devices."""

from __future__ import annotations

from datetime import timedelta
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

DOMAIN = "work_timer"
PLATFORMS = ["button", "sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = WorkTimerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


class WorkTimerCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Work Timer",
            update_interval=timedelta(seconds=15),
            config_entry=entry,
        )
        self.entry = entry
        self.url = str(entry.data["url"]).rstrip("/")
        self.token = str(entry.data["token"])
        self.names: tuple[str, ...] | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _async_update_data(self) -> dict[str, dict]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{self.url}/v1/users",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 401:
                    raise UpdateFailed("The Work Timer token was rejected")
                if response.status != 200:
                    raise UpdateFailed(f"Work Timer returned {response.status}")
                payload = await response.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(str(err)) from err
        rows = payload.get("users") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise UpdateFailed("Work Timer returned no users")
        names = tuple(str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name"))
        if self.names is not None and names != self.names:
            self.names = names
            self.hass.async_create_task(self.hass.config_entries.async_reload(self.entry.entry_id))
        else:
            self.names = names
        return {str(row["name"]): row for row in rows if isinstance(row, dict) and row.get("name")}

    async def async_command(self, user: str, command: str) -> None:
        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self.url}/v1/timer",
            headers=self._headers(),
            json={"user": user, "command": command},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status >= 400:
                raise UpdateFailed(f"Work Timer returned {response.status}")
        await self.async_request_refresh()
