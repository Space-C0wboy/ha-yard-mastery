"""Tests for the Yard Mastery calendar."""
from datetime import date, datetime

import pytest
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .common import fixture, url
from .test_services import sent

ENTITY = "calendar.yard_mastery"


async def _events(hass, start="2026-10-01", end="2026-12-31"):
    entity = hass.data["calendar"].get_entity(ENTITY)
    return await entity.async_get_events(
        hass, dt_util.as_local(datetime.fromisoformat(start)), dt_util.as_local(datetime.fromisoformat(end))
    )


async def test_events_in_range(hass, load_entry):
    await load_entry()
    events = {e.uid: e for e in await _events(hass)}
    flagship = next(e for e in events.values() if e.uid.startswith("window-") and e.start == date(2026, 10, 17))
    assert flagship.end == date(2026, 11, 19)
    assert "Flagship: 3 lb/1,000 sq ft (2.68 lb for the lawn) — completed" in flagship.description
    dimension = next(e for e in events.values() if e.start == date(2026, 11, 19))
    assert dimension.end == date(2026, 12, 20)
    assert "Dimension: 3 lb/1,000 sq ft" in dimension.description
    journal = [e for e in events.values() if e.uid.startswith("journal-")]
    assert sorted(e.start for e in journal) == [date(2026, 10, 8), date(2026, 10, 19)]
    assert not [e for e in events.values() if e.uid.startswith("reminder-")]


async def test_journal_summary_10_19(hass, load_entry):
    await load_entry()
    event = next(e for e in await _events(hass) if e.start == date(2026, 10, 19) and e.uid.startswith("journal-"))
    assert event.summary == "Applied Compaction Cure, Prodiamine, Flagship"
    assert "Flagship: 2.8 lb" in event.description
    assert "/1,000" not in event.description
    assert "Soil temperature: 62°F" in event.description


async def test_current_window_is_the_event(hass, load_entry):
    await load_entry()
    state = hass.states.get(ENTITY)
    assert state.attributes["start_time"].startswith("2026-10-17")


async def test_reminders_appear_unless_dismissed(hass, load_entry):
    reminder = {"id": 77, "createDate": "2026-10-01T12:00:00.000Z", "detail": "<p>Mow</p>", "dismissDate": None,
                "dueDate": "2026-10-25"}
    done = {**reminder, "id": 78, "dismissDate": "2026-10-02T00:00:00.000Z"}
    calendar = fixture("calendar_2026")
    calendar["years"][0]["days"].append({"date": "2026-10-25", "reminders": [reminder, done]})
    await load_entry(calendars={2026: calendar})
    events = [e for e in await _events(hass) if e.uid.startswith("reminder-")]
    assert [(e.uid, e.summary, e.start) for e in events] == [("reminder-77", "Mow", date(2026, 10, 25))]


async def test_calendar_features_follow_option(hass, mock_ym, load_entry):
    await load_entry()
    assert hass.states.get(ENTITY).attributes["supported_features"] == 0
    entry = hass.config_entries.async_entries("yard_mastery")[0]
    hass.config_entries.async_update_entry(entry, options={"allow_writes": True})
    await hass.async_block_till_done()
    features = hass.states.get(ENTITY).attributes["supported_features"]
    want = CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT
    assert features == want

    reminder = {"id": 77, "createDate": "2026-10-01T12:00:00.000Z", "detail": "<p>Old</p>", "dismissDate": None,
                "dueDate": "2026-10-25"}
    mock_ym.post(url("/v1/reminder"), payload={**reminder, "id": 78})
    mock_ym.put(url("/v1/reminder/77/dismiss"), status=200)
    entity = hass.data["calendar"].get_entity(ENTITY)
    await entity.async_create_event(dtstart=date(2026, 10, 30), dtend=date(2026, 10, 31), summary="Buy seed")
    assert sent(mock_ym, "POST", "/v1/reminder")[0]["dueDate"] == "2026-10-30"
    await entity.async_delete_event("reminder-77")


async def test_writes_off_refuses_reminder_create(hass, load_entry):
    await load_entry()
    entity = hass.data["calendar"].get_entity(ENTITY)
    with pytest.raises(ServiceValidationError):
        await entity.async_create_event(dtstart=date(2026, 10, 30), dtend=date(2026, 10, 31), summary="x")


async def test_window_event_edit_refused(hass, load_entry):
    await load_entry(allow_writes=True)
    entity = hass.data["calendar"].get_entity(ENTITY)
    for uid in ("window-12848538", "journal-1552252"):
        with pytest.raises(HomeAssistantError, match="Use the yard_mastery actions"):
            await entity.async_update_event(uid, {"summary": "x"})
        with pytest.raises(HomeAssistantError, match="Use the yard_mastery actions"):
            await entity.async_delete_event(uid)


async def test_update_reminder_event_puts_new_date_and_title(hass, mock_ym, load_entry):
    reminder = {"id": 77, "createDate": "2026-10-01T12:00:00.000Z", "detail": "<p>Old</p>", "dismissDate": None,
                "dueDate": "2026-10-25"}
    calendar = fixture("calendar_2026")
    calendar["years"][0]["days"].append({"date": "2026-10-25", "reminders": [reminder]})
    await load_entry(allow_writes=True, calendars={2026: calendar})
    mock_ym.put(url("/v1/reminder"), payload=reminder)
    entity = hass.data["calendar"].get_entity(ENTITY)
    await entity.async_update_event("reminder-77", {"dtstart": date(2026, 10, 27), "summary": "New"})
    assert sent(mock_ym, "PUT", "/v1/reminder") == [{**reminder, "detail": "<p>New</p>", "dueDate": "2026-10-27"}]


async def test_events_in_a_range_not_ending_at_midnight(hass, load_entry):
    # A trigger look-ahead crossing midnight still sees the window that starts the next day.
    await load_entry()
    events = await _events(hass, "2026-11-18T23:52:00", "2026-11-19T00:07:00")
    starts = sorted(e.start for e in events if e.uid.startswith("window-"))
    assert starts == [date(2026, 10, 17), date(2026, 11, 19)]
    assert not await _events(hass, "2026-11-19T00:00:00", "2026-11-19T00:00:00")
