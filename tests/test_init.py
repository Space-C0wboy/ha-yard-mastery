"""Tests for setup, unload and option-driven reload."""
from homeassistant.config_entries import ConfigEntryState

from custom_components.yard_mastery.const import DOMAIN
from custom_components.yard_mastery.entity import YmEntity


async def test_setup_and_unload(hass, load_entry):
    entry = await load_entry()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.user_status["user"]["guid"] == "user-guid-1"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_options_change_reloads_but_token_update_does_not(hass, load_entry):
    entry = await load_entry()
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(entry, data={**entry.data, "token": "newer"})
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    hass.config_entries.async_update_entry(entry, options={**entry.options, "scan_interval": 45})
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator
    assert entry.runtime_data.update_interval.total_seconds() == 45 * 60


async def test_entity_base_device_and_unique_id(load_entry):
    entry = await load_entry()
    entity = YmEntity(entry.runtime_data, "thing")
    assert entity.unique_id == "user-guid-1_thing"
    assert entity.has_entity_name
    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, "user-guid-1")}
    assert info["name"] == "Yard Mastery"
    assert info["manufacturer"] == "Yard Mastery (unofficial)"
    assert info["model"] == "STILP"


async def test_entity_base_tolerates_odd_program(load_entry):
    entry = await load_entry()
    entry.runtime_data.data.user_status["program"] = "STILP"
    assert YmEntity(entry.runtime_data, "x").device_info["model"] is None


async def test_removing_the_entry_removes_its_store(hass, load_entry, hass_storage, freezer):
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = await load_entry()
    key = f"yard_mastery.{entry.entry_id}"
    entry.runtime_data.store.set_latest("x", 1)  # a pending delayed save must not bring it back
    await entry.runtime_data.store.save_now()
    assert key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert key not in hass_storage


def test_every_action_has_an_icon():
    import json
    import pathlib

    import yaml

    folder = pathlib.Path(__file__).parent.parent / "custom_components" / "yard_mastery"
    icons = json.loads((folder / "icons.json").read_text(encoding="utf-8"))["services"]
    actions = yaml.safe_load((folder / "services.yaml").read_text(encoding="utf-8"))
    assert set(icons) == set(actions)
    assert all(v["service"].startswith("mdi:") for v in icons.values())
