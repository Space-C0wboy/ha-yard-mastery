"""Tests for the coordinator: polling tiers, history import, version issue, reauth."""
import copy
import re
from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.yard_mastery.api import YardMasteryClient, YmAuthError, YmConnectionError
from custom_components.yard_mastery.const import DOMAIN

from .common import fixture, requested, url

FAST = (
    "/v4/home/soil-temp",
    "/v4/home/bundle",
    "startDate=2026-01-01",
    "/v1/dlux/bundle/12848538/details",
    "/v1/dlux/bundle/12848539/details",
    "/v1/user/status",
    "/v2/yard",
)
SLOW = (
    "/v1/lawn-segment",
    "/v1/soil-test/dashboard",
    "barcode=YM00001",
    "/v1/soil-test/stilp/products/YM00001",
    "/v1/lawn-program/status",
    "startDate=2027-01-01",
)
REFERENCE = (
    "/v1/calendar/filter-options",
    "/v1/journal/entry/activity",
    "/v1/lawn-types",
    "/v1/settings/lawn-program/values",
    "/v1/system/version/validate",
)


def _count(calls: list[str], needle: str) -> int:
    return sum(needle in call for call in calls)


async def test_coordinator_fast_tier_requests(hass, mock_ym, load_entry, freezer):
    # Seeded calendar: no history import, so the first poll is the plain three tiers.
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    coordinator = entry.runtime_data
    calls = requested(mock_ym)
    for needle in (*FAST, *SLOW, *REFERENCE):
        assert _count(calls, needle) == 1, needle
    assert coordinator.data.request_count == len(calls)
    assert coordinator.data.version_valid is True
    assert coordinator.data.activities["Mow"] == 1
    assert coordinator.data.current_window.id == 12848538
    assert coordinator.data.next_window.id == 12848539
    assert [s.name for s in coordinator.data.segments]

    mock_ym.requests.clear()
    freezer.tick(timedelta(minutes=30))
    await coordinator.async_refresh()
    calls = requested(mock_ym)
    assert len(calls) <= 10
    for needle in FAST:
        assert _count(calls, needle) == 1, needle
    for needle in (*SLOW, *REFERENCE):
        assert _count(calls, needle) == 0, needle


async def test_import_history_walks_back_until_two_empty_years(mock_ym, load_entry):
    entry = await load_entry()
    calls = requested(mock_ym)
    for year in (2026, 2025, 2024, 2027):
        assert _count(calls, f"startDate={year}-01-01") >= 1
    assert _count(calls, "startDate=2023-01-01") == 0
    assert _count(calls, "date=2026-10-08") == 1
    assert _count(calls, "date=2026-10-19") == 1
    # Every window's details were fetched once by the import; current/next again by the fast tier.
    assert entry.runtime_data.data.snapshot.windows
    for window in entry.runtime_data.data.snapshot.windows.values():
        expected = 2 if window.id in (12848538, 12848539) else 1
        assert _count(calls, f"/v1/dlux/bundle/{window.id}/details") == expected
    assert await entry.runtime_data.import_history() == 1


async def test_windows_from_several_years_are_merged_by_id(load_entry):
    second_year = copy.deepcopy(fixture("calendar_2026"))
    days = [d for y in second_year["years"] for d in y["days"]]
    shared = next(d["bundle"] for d in days if (d.get("bundle") or {}).get("id") == 12848539)
    second_year["years"] = [{"year": 2027, "days": [{"date": "2027-01-02", "bundle": shared}]}]
    entry = await load_entry(calendars={2027: second_year})
    windows = entry.runtime_data.data.snapshot.windows
    assert len(windows) == 8
    assert 12848539 in windows


async def test_schema_drift_does_not_fail_a_poll(load_entry):
    entry = await load_entry(
        seed={"calendar:2026": fixture("calendar_2026"), "calendar:2020": [], "bundle:7": {"id": 7}},
        overrides={"/v1/lawn-segment": {"unexpected": True}, "/v1/soil-test/dashboard": [1, 2]},
    )
    assert entry.runtime_data.last_update_success
    assert entry.runtime_data.data.segments == []
    assert entry.runtime_data.data.snapshot.soil_tests == {}


async def test_version_invalid_creates_issue(hass, load_entry):
    entry = await load_entry(overrides={"/v1/system/version/validate": {"isValid": False, "version": "4.00.05"}})
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "app_version_invalid")
    assert issue.translation_key == "app_version_invalid"
    assert issue.translation_placeholders == {"version": "4.00.05"}
    assert entry.runtime_data.data.version_valid is False


async def test_version_valid_clears_issue(hass, load_entry):
    ir.async_create_issue(
        hass,
        DOMAIN,
        "app_version_invalid",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="app_version_invalid",
        translation_placeholders={"version": "4.00.05"},
    )
    await load_entry()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "app_version_invalid") is None


async def test_auth_error_starts_reauth(hass, load_entry):
    with patch.object(YardMasteryClient, "soil_temp", side_effect=YmAuthError("nope")):
        entry = await load_entry()
    assert entry.state.name == "SETUP_ERROR"
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_connection_error_is_retried(load_entry):
    with patch.object(YardMasteryClient, "soil_temp", side_effect=YmConnectionError("down")):
        entry = await load_entry()
    assert entry.state.name == "SETUP_RETRY"


async def test_slow_tier_failure_keeps_poll_alive_and_retries(mock_ym, load_entry, freezer):
    mock_ym.get(url("/v1/lawn-segment"), status=503)  # one blip, then the normal mock answers
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert "503" in coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(hours=1))
    await coordinator.async_refresh()
    # The failed key is retried hourly, without waiting for the next slow tier.
    assert _count(requested(mock_ym), "/v1/lawn-segment") == 1
    assert coordinator.data.last_error is None


async def test_request_refresh_full_runs_slow_tier(hass, mock_ym, load_entry):
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    mock_ym.requests.clear()
    await entry.runtime_data.request_refresh_full()
    await hass.async_block_till_done()
    assert _count(requested(mock_ym), "/v1/lawn-segment") == 1


async def test_token_persisted_after_login(hass, load_entry):
    entry = await load_entry(token=False)
    assert entry.data["token"] == "tok-fixture"
    assert entry.data["token_created"].startswith("2026-10-19")


def _calendar_with_status(window_id: int, item_id: int, status: str) -> dict:
    raw = copy.deepcopy(fixture("calendar_2026"))
    for year in raw["years"]:
        for day in year["days"]:
            bundle = day.get("bundle") or {}
            if bundle.get("id") == window_id:
                for item in bundle["bundleItems"]:
                    if item["id"] == item_id:
                        item["status"] = status
    return raw


def _item_changes(entry) -> list[dict]:
    return [c for c in entry.runtime_data.store.history if c["kind"] == "item_status"]


async def test_change_made_while_offline_is_logged_after_restart(load_entry):
    entry = await load_entry(
        seed={"calendar:2026": fixture("calendar_2026")},
        calendars={2026: _calendar_with_status(12211687, 29353197, "COMPLETED")},
    )
    changes = _item_changes(entry)
    assert [(c["key"], c["old"], c["new"]) for c in changes] == [("item:29353197", "CREATED", "COMPLETED")]


async def test_fresh_calendar_status_beats_stale_cached_details(load_entry):
    stale = next(
        d["bundle"] for y in fixture("calendar_2026")["years"] for d in y["days"] if (d.get("bundle") or {}).get("id") == 12211687
    )
    stale = copy.deepcopy(stale)
    for item in stale["bundleItems"]:
        item["product"]["applicationRate"] = 3.0
    entry = await load_entry(
        seed={"calendar:2026": fixture("calendar_2026"), "bundle:12211687": stale},
        calendars={2026: _calendar_with_status(12211687, 29353197, "COMPLETED")},
    )
    item = next(i for i in entry.runtime_data.data.snapshot.windows[12211687].items if i.id == 29353197)
    assert (item.status, item.completed, item.rate) == ("COMPLETED", True, 3.0)
    assert [c["new"] for c in _item_changes(entry)] == ["COMPLETED"]


async def test_partial_import_resumes_and_completes(hass, mock_ym, load_entry, freezer):
    mock_ym.get(re.compile(r".*/v3/journal/entry\?.*date=2026-10-08.*"), status=503)
    entry = await load_entry()
    coordinator = entry.runtime_data
    assert "journal:2026-10-08" not in coordinator.store.latest
    assert "history_imported" not in coordinator.store.latest
    assert "HTTP 503" in coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(hours=1))
    await coordinator.async_refresh()
    assert "journal:2026-10-08" in coordinator.store.latest
    assert coordinator.store.latest["history_imported"] is True
    assert coordinator.data.last_error is None
    calls = requested(mock_ym)
    assert _count(calls, "date=2026-10-08") == 1
    assert _count(calls, "date=2026-10-19") == 0  # already stored: only the missing piece is fetched
    assert _count(calls, "startDate=2025-01-01") == 0  # the calendar walk is not repeated


async def test_permanently_failing_fetch_does_not_defeat_the_tiers(mock_ym, load_entry, freezer):
    mock_ym.get(url("/v1/soil-test/stilp/products/YM00001"), status=400, repeat=True)
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    coordinator = entry.runtime_data
    assert "HTTP 400" in coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(minutes=30))
    await coordinator.async_refresh()
    calls = requested(mock_ym)
    assert len(calls) <= 10
    assert _count(calls, "/stilp/") == 0
    assert "HTTP 400" in coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(minutes=30))
    await coordinator.async_refresh()
    calls = requested(mock_ym)
    assert _count(calls, "/stilp/") == 0  # a 4xx is final between slow tiers
    assert len(calls) <= 10
    mock_ym.requests.clear()
    freezer.tick(timedelta(hours=6))
    await coordinator.async_refresh()
    assert _count(requested(mock_ym), "/stilp/") == 1  # the next slow tier asks again


async def test_import_resume_is_capped_and_gives_up_on_a_4xx(mock_ym, load_entry, freezer):
    mock_ym.get(re.compile(r".*/v3/journal/entry.*"), status=400, repeat=True)
    entry = await load_entry()
    coordinator = entry.runtime_data
    assert "history_imported" not in coordinator.store.latest
    polls = 0
    while "history_imported" not in coordinator.store.latest and polls < 10:
        mock_ym.requests.clear()
        freezer.tick(timedelta(minutes=30))
        await coordinator.async_refresh()
        polls += 1
        assert len(requested(mock_ym)) <= 10
    assert coordinator.store.latest["history_imported"] is True
    assert "HTTP 400" in coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(hours=2))
    await coordinator.async_refresh()
    assert _count(requested(mock_ym), "date=") == 0  # given-up days are not asked again


async def test_retries_are_capped_per_poll(mock_ym, load_entry, freezer):
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    coordinator = entry.runtime_data
    for n in range(6):
        coordinator._failed[f"bundle:{n}"] = (
            dt_util.utcnow() - timedelta(hours=2), coordinator.client.bundle_details, (n,), "boom", False,
        )
    mock_ym.requests.clear()
    freezer.tick(timedelta(minutes=30))
    await coordinator.async_refresh()
    assert len(requested(mock_ym)) <= 10


async def test_failed_key_no_longer_wanted_is_dropped(mock_ym, load_entry, freezer):
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    coordinator = entry.runtime_data
    coordinator._failed["stilp:GONE"] = (
        dt_util.utcnow() - timedelta(hours=2), coordinator.client.stilp_products, ("GONE",), "x", False,
    )
    mock_ym.requests.clear()
    freezer.tick(timedelta(minutes=30))
    await coordinator.async_refresh()
    assert "stilp:GONE" not in coordinator._failed
    assert _count(requested(mock_ym), "GONE") == 0


async def test_version_check_retry_updates_the_issue(hass, mock_ym, load_entry, freezer):
    mock_ym.get(url("/v1/system/version/validate"), status=503)
    entry = await load_entry(overrides={"/v1/system/version/validate": {"isValid": False, "version": "4.00.05"}})
    assert ir.async_get(hass).async_get_issue(DOMAIN, "app_version_invalid") is None
    freezer.tick(timedelta(hours=1))
    await entry.runtime_data.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "app_version_invalid") is not None


async def test_journal_template_without_id_is_not_an_entry(load_entry):
    # The mocked /v3/journal/entry returns the app's unsaved template (id null) for 2026-10-17.
    old = fixture("calendar_2026")
    for day in old["years"][0]["days"]:
        if day["date"] == "2026-10-19":
            day.pop("journalEntry")
    entry = await load_entry(seed={"calendar:2026": old, "journal:2026-10-17": fixture("journal_2026_10_17")})
    journal = entry.runtime_data.data.snapshot.journal
    assert sorted(day.isoformat() for day in journal) == ["2026-10-08", "2026-10-19"]
    added = [c["key"] for c in entry.runtime_data.store.history if c.get("kind") == "journal_added"]
    assert added == ["journal:2026-10-19"]


def _calendar_without(day: str) -> dict:
    raw = copy.deepcopy(fixture("calendar_2026"))
    for year in raw["years"]:
        for d in year["days"]:
            if d["date"] == day:
                d.pop("journalEntry", None)
    return raw


def _journal_entry(day: str) -> dict:
    for year in fixture("calendar_2026")["years"]:
        for d in year["days"]:
            if d["date"] == day:
                return copy.deepcopy(d["journalEntry"])
    raise AssertionError(day)


async def test_journal_entry_deleted_in_the_app_leaves_ha(load_entry):
    # The import stored journal:2026-10-08; the app then deleted that entry, so the calendar drops it.
    entry = await load_entry(
        seed={"calendar:2026": fixture("calendar_2026"), "journal:2026-10-08": _journal_entry("2026-10-08")},
        calendars={2026: _calendar_without("2026-10-08")},
    )
    coordinator = entry.runtime_data
    assert date(2026, 10, 8) not in coordinator.data.snapshot.journal
    assert "journal:2026-10-08" not in coordinator.store.latest
    deleted = [c["key"] for c in coordinator.store.history if c["kind"] == "journal_deleted"]
    assert deleted == ["journal:2026-10-08"]


async def test_auth_error_on_an_optional_fetch_is_not_reauth(hass, mock_ym, load_entry, freezer):
    mock_ym.get(url("/v1/journal/entry/activity"), status=403, repeat=True)
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    assert entry.state.name == "LOADED"
    assert not [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    coordinator = entry.runtime_data
    assert coordinator.data.last_error
    mock_ym.requests.clear()
    freezer.tick(timedelta(hours=1))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert _count(requested(mock_ym), "/v1/journal/entry/activity") == 0  # final until the next reference tier


async def test_failed_soil_test_fetch_does_not_leak_the_barcode(mock_ym, load_entry, caplog):
    caplog.set_level("DEBUG", logger="custom_components.yard_mastery")
    mock_ym.get(url("/v1/soil-test/stilp/products/YM00001"), status=400, repeat=True)
    mock_ym.get(url("/v1/soil-test/report"), status=503, repeat=True)
    entry = await load_entry(seed={"calendar:2026": fixture("calendar_2026")})
    last_error = entry.runtime_data.data.last_error
    assert last_error and "YM00001" not in last_error
    assert "Could not refresh soil_report:***" in caplog.text
    assert "YM00001" not in caplog.text
