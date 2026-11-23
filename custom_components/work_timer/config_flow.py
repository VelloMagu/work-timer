"""Config flow for Work Timer."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN

DEFAULT_URL = "http://51318157-work-timer:8097"


def _schema(url: str = DEFAULT_URL) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("url", default=url): str,
            vol.Required("token"): str,
        }
    )


async def _users(hass, url: str, token: str) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    async with session.get(
        url.rstrip("/") + "/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as response:
        if response.status == 401:
            raise PermissionError
        if response.status != 200:
            raise ConnectionError
        payload = await response.json()
    if not isinstance(payload, dict):
        raise ConnectionError
    return payload


class WorkTimerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = str(user_input["url"]).rstrip("/")
            token = str(user_input["token"]).strip()
            try:
                await _users(self.hass, url, token)
            except PermissionError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, ConnectionError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Work Timer", data={"url": url, "token": token})
        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WorkTimerOptionsFlow:
        return WorkTimerOptionsFlow()


class WorkTimerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        assert entry is not None
        url = str(entry.data["url"])
        token = str(entry.data["token"])
        try:
            info = await self._people(url, token)
        except (aiohttp.ClientError, ConnectionError, TimeoutError, PermissionError):
            return self.async_abort(reason="cannot_connect")
        users = [str(item) for item in info.get("users") or []]
        people = info.get("ha_people") if isinstance(info.get("ha_people"), list) else []
        current = info.get("people") if isinstance(info.get("people"), dict) else {}
        choices = {"": "No default", **{name: name for name in users}}
        if user_input is not None:
            for person in people:
                entity = str(person.get("entity_id") or "")
                if not entity:
                    continue
                await self._set_person(url, token, entity, str(user_input.get(entity) or ""))
            return self.async_create_entry(data={})
        schema = {
            vol.Required(str(person["entity_id"]), default=str(current.get(person["entity_id"]) or "")): vol.In(choices)
            for person in people
            if person.get("entity_id")
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

    async def _people(self, url: str, token: str) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        async with session.get(
            url.rstrip("/") + "/v1/people",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 401:
                raise PermissionError
            if response.status != 200:
                raise ConnectionError
            payload = await response.json()
        if not isinstance(payload, dict):
            raise ConnectionError
        return payload

    async def _set_person(self, url: str, token: str, person: str, user: str) -> None:
        session = async_get_clientsession(self.hass)
        async with session.post(
            url.rstrip("/") + "/v1/people",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"person": person, "user": user},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status >= 400:
                raise ConnectionError
