"""Tests for the raw store and change log."""
import copy
from dataclasses import replace
from datetime import date

from custom_components.yard_mastery.models import SoilTest, parse_calendar
from custom_components.yard_mastery.store import YmSnapshot, YmStore

from .common import fixture


def _snapshot(raw: dict | None = None) -> YmSnapshot:
    windows, journal, reminders = parse_calendar(raw or fixture("calendar_2026"))
    return YmSnapshot(windows, journal, reminders, {})


async def test_first_snapshot_records_nothing(hass):
    store = YmStore(hass, "e1")
    assert store.record_changes(None, _snapshot()) == []
    assert store.history == []


async def test_item_status_change_recorded(hass):
    raw = fixture("calendar_2026")
    new_raw = copy.deepcopy(raw)
    changed = 0
    for day in new_raw["years"][0]["days"]:
        for item in (day.get("bundle") or {}).get("bundleItems", []):
            if item["product"]["name"] == "Dimension" and item.get("status") == "CREATED":
                item["status"] = "COMPLETED"
                item["completed"] = True
                changed += 1
    assert changed
    store = YmStore(hass, "e1")
    items = store.record_changes(_snapshot(raw), _snapshot(new_raw))
    statuses = [i for i in items if i["kind"] == "item_status"]
    assert len(statuses) == changed
    assert statuses[0]["old"] == "CREATED"
    assert statuses[0]["new"] == "COMPLETED"
    assert statuses[0]["source"] == "yard_mastery"
    assert store.history == items


async def test_journal_added_changed_deleted(hass):
    base = _snapshot()
    day = date(2026, 10, 19)
    store = YmStore(hass, "e1")

    removed = replace(base, journal={d: e for d, e in base.journal.items() if d != day})
    items = store.record_changes(base, removed)
    assert [(i["kind"], i["key"]) for i in items] == [("journal_deleted", "journal:2026-10-19")]

    items = store.record_changes(removed, base)
    assert [(i["kind"], i["key"]) for i in items] == [("journal_added", "journal:2026-10-19")]

    entry = base.journal[day]
    volatile = replace(entry, raw={**entry.raw, "bundleDetails": {"x": 1}})
    assert store.record_changes(base, replace(base, journal={**base.journal, day: volatile})) == []

    edited = replace(entry, raw={**entry.raw, "body": "edited"})
    items = store.record_changes(base, replace(base, journal={**base.journal, day: edited}))
    assert [(i["kind"], i["key"]) for i in items] == [("journal_changed", "journal:2026-10-19")]


async def test_soil_test_added_and_status(hass):
    base = _snapshot()
    store = YmStore(hass, "e1")
    test = SoilTest("B1", "Received", None, None, False, {}, (), {})
    with_test = replace(base, soil_tests={"B1": test})
    assert store.record_changes(base, with_test)[0]["kind"] == "soil_test_added"
    done = replace(with_test, soil_tests={"B1": replace(test, status="Complete")})
    item = store.record_changes(with_test, done)[0]
    assert (item["kind"], item["old"], item["new"]) == ("soil_test_status", "Received", "Complete")


async def test_record_write_tagged_home_assistant(hass):
    store = YmStore(hass, "e1")
    store.record_write("journal", {"date": "2026-10-19"})
    assert store.history[0]["source"] == "home_assistant"
    assert store.history[0]["kind"] == "write_journal"
    assert store.history[0]["detail"] == {"date": "2026-10-19"}


async def test_store_round_trip(hass):
    store = YmStore(hass, "e1")
    store.set_latest("calendar:2026", fixture("calendar_2026"))
    store.record_write("journal", {"date": "2026-10-19"})
    await store.save_now()

    again = YmStore(hass, "e1")
    await again.load()
    assert again.latest == store.latest
    assert again.history == store.history
