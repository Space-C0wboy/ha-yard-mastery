"""The Yard Mastery integration."""
from __future__ import annotations

from datetime import datetime

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import YardMasteryClient
from .const import CONF_APP_VERSION, CONF_TOKEN, CONF_TOKEN_CREATED, DEFAULT_APP_VERSION, DOMAIN, PLATFORMS
from .coordinator import YmConfigEntry, YmCoordinator
from .services import async_setup_services
from .store import YmStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Yard Mastery actions once, for every entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: YmConfigEntry) -> bool:
    client = YardMasteryClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
        token_created=dt_util.parse_datetime(entry.data.get(CONF_TOKEN_CREATED) or ""),
        app_version=entry.options.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
    )

    def _persist_token(token: str, created: datetime) -> None:
        # Data-only update: the update listener below reloads on option changes only.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token, CONF_TOKEN_CREATED: created.isoformat()}
        )

    client.on_token = _persist_token

    store = YmStore(hass, entry.entry_id)
    await store.load()
    coordinator = YmCoordinator(hass, entry, client, store)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(store.save_now)
    options = dict(entry.options)

    async def _reload_on_options(hass: HomeAssistant, entry: YmConfigEntry) -> None:
        if dict(entry.options) != options:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_reload_on_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: YmConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: YmConfigEntry) -> None:
    """Delete the entry's stored payloads and change log."""
    await YmStore(hass, entry.entry_id).remove()
