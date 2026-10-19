"""Tests for the Yard Mastery sensors."""
import json

from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.yard_mastery.models import season_totals

from .common import fixture


async def _load(hass, load_entry, **kwargs):
    # Soil temperature is reported in °F; the test instance must display US units.
    hass.config.units = US_CUSTOMARY_SYSTEM
    return await load_entry(**kwargs)


def _by_uid(hass, entry):
    return {e.unique_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_soil_temperature(hass, load_entry):
    await _load(hass, load_entry)
    state = hass.states.get("sensor.yard_mastery_soil_temperature")
    assert state.state == "62"
    assert state.attributes["unit_of_measurement"] == "°F"
    assert state.attributes["device_class"] == "temperature"
    assert state.attributes["zip_status"] == "VALID"


async def test_current_window(hass, load_entry):
    await _load(hass, load_entry)
    state = hass.states.get("sensor.yard_mastery_current_window")
    assert state.state == "STILP Application - Flagship Pack"
    assert state.attributes["start"] == "2026-10-17"
    assert state.attributes["end"] == "2026-11-18"
    items = {i["product"]: i for i in state.attributes["items"]}
    assert items["Flagship"]["whole_lawn"] == 2.68  # 3.0 per 1000 on the measured 893 sq ft
    assert set(items["Flagship"]) == {
        "product", "type", "material", "rate", "unit", "whole_lawn", "n", "p", "k", "status", "journal_date",
    }
    assert items["Flagship"]["status"] == "COMPLETED"
    assert items["Flagship"]["journal_date"] == "2026-10-19"


async def test_progress(hass, load_entry):
    await _load(hass, load_entry)
    state = hass.states.get("sensor.yard_mastery_current_window_progress")
    assert state.state == "100"
    assert (state.attributes["steps"], state.attributes["completed"]) == (3, 3)
    assert (state.attributes["skipped"], state.attributes["remaining"]) == (0, 0)


async def test_next_window(hass, load_entry):
    await _load(hass, load_entry)
    assert hass.states.get("sensor.yard_mastery_next_window").state == "STILP Application Pack"
    assert hass.states.get("sensor.yard_mastery_next_window_start").state == "2026-11-19"
    assert hass.states.get("sensor.yard_mastery_days_until_next_window").state == "31"


async def test_last_application(hass, load_entry):
    await _load(hass, load_entry)
    state = hass.states.get("sensor.yard_mastery_last_application")
    assert state.state == "2026-10-19"
    assert len(state.attributes["products"]) == 3
    assert state.attributes["soil_temp"] == 62.0
    assert hass.states.get("sensor.yard_mastery_last_journal_entry").state == "2026-10-19"


async def test_nitrogen_this_year(hass, load_entry):
    entry = await _load(hass, load_entry)
    data = entry.runtime_data.data
    items = {i.product_id: i for w in data.snapshot.windows.values() for i in w.items}
    expected = season_totals(data.snapshot.journal.values(), items, 2026, 893)
    state = hass.states.get("sensor.yard_mastery_nitrogen_this_year")
    assert float(state.state) == expected["n"] > 0
    assert state.attributes["phosphorus"] == expected["p"]
    assert state.attributes["potassium"] == expected["k"]
    assert "Spoon Juice" in state.attributes["not_counted"]
    assert "Flagship" in state.attributes["by_product"]
    assert hass.states.get("sensor.yard_mastery_applications_this_year").state == str(expected["applications"])


async def test_soil_test_and_nutrients(hass, load_entry):
    entry = await _load(hass, load_entry)
    state = hass.states.get("sensor.yard_mastery_soil_test")
    assert state.state == "Completed"
    assert state.attributes["days_old"] == 19
    assert state.attributes["test_date"] == "2026-09-30"
    assert state.attributes["informs_program"] is True
    assert "Flagship" in state.attributes["recommended_products"]
    assert state.attributes["test_id"] == 70766
    assert state.attributes["description"] == "Overall Sample"
    assert state.attributes["test_type"] == "LAWN"
    assert state.attributes["can_inform_program"] is True
    by_uid = _by_uid(hass, entry)
    ph = by_uid["user-guid-1_soil_ppm_ph"]
    assert ph.entity_category.value == "diagnostic"
    assert ph.disabled_by is None
    assert hass.states.get(ph.entity_id).state == "6.39"
    for main in ("n", "p", "k"):
        assert by_uid[f"user-guid-1_soil_ppm_{main}"].disabled_by is None
    assert by_uid["user-guid-1_soil_ppm_ca"].disabled_by is not None
    assert hass.states.get(by_uid["user-guid-1_soil_ppm_ca"].entity_id) is None


async def test_soil_test_falls_back_to_latest_completed(hass, load_entry):
    await _load(hass, load_entry, overrides={"/v2/yard": {**fixture("yard"), "selectedTest": None}})
    assert hass.states.get("sensor.yard_mastery_soil_test").state == "Completed"


async def test_segments_and_diagnostics(hass, load_entry):
    entry = await _load(hass, load_entry)
    front = hass.states.get(_by_uid(hass, entry)["user-guid-1_segment_637201"].entity_id)
    assert front.state == "667"
    assert front.attributes["friendly_name"] == "Yard Mastery Front Yard"
    assert front.attributes["segment_type"] == "LAWN"
    dug = hass.states.get("sensor.yard_mastery_dug_ai_turns")
    assert (dug.state, dug.attributes["limit"]) == ("0", 40)
    sync = hass.states.get("sensor.yard_mastery_last_sync")
    assert sync.state.startswith("2026-10-19T17:00:00")
    assert sync.attributes["request_count"] > 0
    program = hass.states.get("sensor.yard_mastery_program")
    assert program.state == "STILP"
    assert program.attributes["square_feet"] == 3672
    assert program.attributes["complete_date"] == "2026-10-06"


async def test_attributes_under_size_limit(hass, load_entry):
    entry = await _load(hass, load_entry)
    checked = 0
    for ent in _by_uid(hass, entry).values():
        if (state := hass.states.get(ent.entity_id)) is not None:
            checked += 1
            assert len(json.dumps(state.attributes)) < 12288, ent.entity_id
    assert checked >= 15


async def test_no_personal_attributes(hass, load_entry):
    entry = await _load(hass, load_entry)
    for ent in _by_uid(hass, entry).values():
        if (state := hass.states.get(ent.entity_id)) is not None:
            blob = json.dumps(dict(state.attributes)) + state.state
            for secret in ("example.com", "Test St", "user-guid-1", "YM00001", "TRACK0001"):
                assert secret not in blob, (ent.entity_id, secret)


async def test_soil_temperature_unit_follows_api(hass, load_entry):
    hass.config.units = US_CUSTOMARY_SYSTEM
    await load_entry(overrides={"/v4/home/soil-temp": {**fixture("soil_temp"), "soilTempUnit": "C", "soilTemp": 17}})
    state = hass.states.get("sensor.yard_mastery_soil_temperature")
    assert state.attributes["unit_of_measurement"] == "°F"
    assert round(float(state.state)) == 63
