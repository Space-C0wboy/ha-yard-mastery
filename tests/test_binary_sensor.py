"""Tests for the Yard Mastery binary sensors."""
from .common import fixture


async def test_treatment_due_off_when_all_done(hass, load_entry):
    await load_entry()
    state = hass.states.get("binary_sensor.yard_mastery_treatment_due")
    assert state.state == "off"
    assert state.attributes["due_items"] == []
    assert state.attributes["window_end"] == "2026-11-18"


async def test_treatment_due_on_when_an_item_is_created(hass, load_entry):
    calendar = fixture("calendar_2026")
    bundle_id = fixture("home_bundle")["bundleId"]
    for year in calendar["years"]:
        for day in year["days"]:
            if day.get("bundle") and day["bundle"]["id"] == bundle_id:
                day["bundle"]["bundleItems"][0]["status"] = "CREATED"
    await load_entry(calendars={2026: calendar})
    state = hass.states.get("binary_sensor.yard_mastery_treatment_due")
    assert state.state == "on"
    assert state.attributes["due_items"] == ["Flagship"]


async def test_app_version_valid(hass, load_entry):
    await load_entry()
    state = hass.states.get("binary_sensor.yard_mastery_app_version_valid")
    assert state.state == "on"
    assert state.attributes["version"] == "4.00.05"
