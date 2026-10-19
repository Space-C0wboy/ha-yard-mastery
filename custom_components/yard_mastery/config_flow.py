"""Config flow: sign in, reauth, options."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import YardMasteryClient, YmAuthError, YmError
from .const import (
    CONF_ALLOW_WRITES,
    CONF_APP_VERSION,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_TOKEN_CREATED,
    DEFAULT_APP_VERSION,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    MIN_SCAN_MINUTES,
)

MAX_SCAN_MINUTES = 1440


class YmConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> YmOptionsFlow:
        return YmOptionsFlow()

    async def _sign_in(self, email: str, password: str) -> tuple[dict[str, Any], str]:
        """Log in and return (entry data, user guid). Raises YmError."""
        client = YardMasteryClient(async_get_clientsession(self.hass), email, password)
        await client.login()
        status = await client.user_status()
        guid = status["user"]["guid"]
        if not guid:
            raise YmError("Yard Mastery returned no user id")
        data = {
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_TOKEN: client.token,
            CONF_TOKEN_CREATED: client.token_created.isoformat(),
        }
        return data, guid

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, guid = await self._sign_in(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            except YmAuthError:
                errors["base"] = "invalid_auth"
            except (YmError, KeyError, TypeError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(guid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Yard Mastery", data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=(user_input or {}).get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, guid = await self._sign_in(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            except YmAuthError:
                errors["base"] = "invalid_auth"
            except (YmError, KeyError, TypeError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(guid)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class YmOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ALLOW_WRITES, default=current.get(CONF_ALLOW_WRITES, False)): bool,
                    vol.Optional(CONF_APP_VERSION, default=current.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)): vol.All(
                        str, vol.Length(min=1)
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_MINUTES)
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_MINUTES, max=MAX_SCAN_MINUTES)),
                }
            ),
        )
