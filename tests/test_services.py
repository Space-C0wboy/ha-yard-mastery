"""Tests for the Yard Mastery actions: write gate, journal writes, item status, photos, reminders."""
import copy
from datetime import date

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.yard_mastery.const import DOMAIN, WRITES_OFF_MESSAGE
from custom_components.yard_mastery.writes import reminder_create, reminder_dismiss, reminder_update

from .common import fixture, requested, url

CURRENT = 12848538
SAVED = {"id": 1552252, "entryDate": "2026-10-19"}


def sent(mock, method: str, path: str) -> list:
    """JSON bodies sent with METHOD path, in order."""
    return [
        call.kwargs.get("json")
        for (m, request_url), calls in mock.requests.items()
        if m == method and request_url.path == path
        for call in calls
    ]


def _entry_10_19() -> dict:
    for year in fixture("calendar_2026")["years"]:
        for day in year["days"]:
            if day["date"] == "2026-10-19":
                return copy.deepcopy(day["journalEntry"])
    raise AssertionError


def _all_created() -> dict:
    """Calendar and details overrides with the current window's items not done."""
    calendar = fixture("calendar_2026")
    for year in calendar["years"]:
        for day in year["days"]:
            if (day.get("bundle") or {}).get("id") == CURRENT:
                for item in day["bundle"]["bundleItems"]:
                    item.update(status="CREATED", journalEntryDate=None)
    details = fixture("bundle_current")
    for item in details["bundleItems"]:
        item.update(status="CREATED", completed=False, journalEntryDate=None)
    return {"calendars": {2026: calendar}, "overrides": {f"/v1/dlux/bundle/{CURRENT}/details": details}}


async def _call(hass, action: str, data: dict | None = None):
    return await hass.services.async_call(DOMAIN, action, data or {}, blocking=True, return_response=True)


async def test_writes_gate_off_then_on(hass, mock_ym, load_entry):
    entry = await load_entry()
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED, repeat=True)
    with pytest.raises(ServiceValidationError, match=WRITES_OFF_MESSAGE):
        await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    for action, data in (
        ("set_item_status", {"status": "done", "products": ["Flagship"]}),
        ("log_activity", {"activities": ["mow"]}),
        ("delete_journal_entry", {"entry_id": 1}),
        ("create_reminder", {"date": "2026-10-25", "title": "Mow"}),
        ("update_reminder", {"reminder_id": 1, "title": "Mow"}),
        ("dismiss_reminder", {"reminder_id": 1}),
        ("add_photo", {"entity_id": "camera.yard"}),
    ):
        with pytest.raises(ServiceValidationError, match=WRITES_OFF_MESSAGE):
            await _call(hass, action, data)
    assert not [c for c in requested(mock_ym) if not c.startswith("GET")]

    hass.config_entries.async_update_entry(entry, options={**entry.options, "allow_writes": True})
    await hass.async_block_till_done()
    response = await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    assert response == SAVED
    assert len(sent(mock_ym, "POST", "/v2/journal/entry")) == 1


async def test_log_application_defaults_to_current_window(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True, **_all_created())
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application")
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    apps = {a["bundleItemIds"][0]: a for a in body["productApplications"]}
    assert apps.keys() == {30869937, 30869938, 30869936}
    # appliedRate is the total over the entry's 893 sq ft: rate x 893 / 1000, to 0.01
    assert apps[30869937]["appliedRate"] == 2.68
    assert apps[30869938]["appliedRate"] == 2.68
    assert apps[30869936]["appliedRate"] == 8.04
    assert apps[30869937]["product"]["id"] == 147
    assert all(a["product"]["measure"] is None for a in apps.values())
    assert body["tempValue"] == 62
    assert body["entryDate"] == "2026-10-19"
    assert body["bundleDetails"] is None
    assert body["lawnSqFt"] == 893


async def test_log_application_flagship_total_regression(hass, mock_ym, load_entry):
    # The app's 10-08 entry: Flagship 3.0 lb / 1000 sq ft on 893 sq ft was stored as appliedRate 2.68.
    await load_entry(allow_writes=True, overrides={"/v3/journal/entry": {"id": None, "lawnSqFt": 893}})
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert body["productApplications"][0]["appliedRate"] == 2.68


async def test_log_application_uses_existing_lawn_size(hass, mock_ym, load_entry):
    # A new entry gets the yard's measured size; an existing entry's own lawnSqFt wins.
    await load_entry(allow_writes=True, overrides={"/v3/journal/entry": {"id": 5, "lawnSqFt": 2000}})
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert body["lawnSqFt"] == 2000
    assert body["productApplications"][0]["appliedRate"] == 6.0


async def test_log_application_merges_existing_entry(hass, mock_ym, load_entry):
    existing = _entry_10_19()
    existing["attachments"] = [{"id": 1, "hostedUrl": "https://example.invalid/a.jpg", "reference": "a.jpg"}]
    await load_entry(allow_writes=True, overrides={"/v3/journal/entry": existing})
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship", "amount": 3.0}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert body["id"] == 1552252
    assert body["attachments"] == existing["attachments"]
    rates = {a["product"]["name"]: a["appliedRate"] for a in body["productApplications"]}
    assert rates == {"Compaction Cure": 9.0, "Prodiamine": 2.9, "Flagship": 3.0}
    flagship = next(a for a in body["productApplications"] if a["product"]["name"] == "Flagship")
    assert flagship["bundleItemIds"] == [30869937]
    assert any(c.startswith("GET /v3/journal/entry?") and "date=2026-10-19" in c for c in requested(mock_ym))


async def test_set_item_status_maps_names(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    mock_ym.put(url("/v1/dlux/user-bundle-item/change-status"), status=200)
    await _call(hass, "set_item_status", {"status": "done", "products": ["Dimension"], "window": "next"})
    assert sent(mock_ym, "PUT", "/v1/dlux/user-bundle-item/change-status") == [
        {"bundleItemIds": [30869939], "status": "COMPLETED"}
    ]


async def test_unknown_product_is_validation_error(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    mock_ym.get(url("/v2/product"), payload=[], repeat=True)
    with pytest.raises(ServiceValidationError, match="Nope"):
        await _call(hass, "set_item_status", {"status": "skipped", "products": ["Nope"]})
    with pytest.raises(ServiceValidationError, match="Nope"):
        await _call(hass, "log_application", {"products": [{"product": "Nope"}]})
    assert not [c for c in requested(mock_ym) if c.startswith(("POST", "PUT"))]


async def test_log_application_searches_unknown_products(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    mock_ym.get(url("/v2/product"), payload=[{"id": 900, "name": "Hydretain", "applicationRate": 4.0, "coverageSqFt": 1000}])
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "hydretain"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    added = body["productApplications"][-1]
    assert added == {
        "appliedRate": 3.57,  # 4.0 per 1000 on 893 sq ft
        "bundleItemIds": None,
        "product": {"id": 900, "name": "Hydretain", "applicationRate": 4.0, "coverageSqFt": 1000, "measure": None},
    }


async def test_write_logs_logbook_and_history(hass, mock_ym, load_entry):
    entry = await load_entry(allow_writes=True)
    events = async_capture_events(hass, "logbook_entry")
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    polls = sum("/v4/home/bundle" in c for c in requested(mock_ym))
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["name"] == "Yard Mastery"
    assert events[0].data["domain"] == DOMAIN
    assert "Flagship" in events[0].data["message"]
    last = entry.runtime_data.store.history[-1]
    assert last["source"] == "home_assistant"
    assert last["kind"] == "write_log_application"
    assert last["detail"]["products"] == ["Flagship"]
    assert sum("/v4/home/bundle" in c for c in requested(mock_ym)) == polls + 1


async def test_log_activity_uses_activity_objects(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_activity", {"activities": ["mow", "edge"], "areas": ["front yard"], "notes": "Short"})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    # Mow from /v1/journal/entry/activity, Edge from the calendar filter options.
    assert body["activities"] == [{"id": 1, "name": "Mow"}, {"id": 3, "name": "Edge"}]
    assert [s["id"] for s in body["lawnSegments"]] == [637201]
    assert body["body"] == "Short"
    assert body["productApplications"] == []


async def test_add_photo_rejects_outside_paths(hass, mock_ym, load_entry, tmp_path):
    await load_entry(allow_writes=True)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8")
    hass.config.allowlist_external_dirs.add(str(tmp_path))  # allow-listed, but not under /config or /media
    for path in (str(outside), "/etc/passwd", hass.config.path("www", "..", "..", "etc", "passwd")):
        with pytest.raises(ServiceValidationError):
            await _call(hass, "add_photo", {"file": path})
    assert not [c for c in requested(mock_ym) if c.startswith("POST")]


async def test_add_photo_from_file(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    www = hass.config.path("www")
    hass.config.allowlist_external_dirs.add(www)
    path = await hass.async_add_executor_job(_write_photo, www)
    mock_ym.post(url("/v1/journal/entry/image"), payload=[{"id": 5}])
    response = await _call(hass, "add_photo", {"file": path, "date": "2026-10-18"})
    assert response == {"result": [{"id": 5}]}
    assert any(c.startswith("POST /v1/journal/entry/image?") and "date=2026-10-18" in c for c in requested(mock_ym))


def _write_photo(directory: str) -> str:
    import pathlib

    folder = pathlib.Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lawn.jpg").write_bytes(b"\xff\xd8\xff")
    return str(folder / "lawn.jpg")


async def test_delete_journal_entry_by_date(hass, mock_ym, load_entry):
    entry = await load_entry(allow_writes=True, overrides={"/v3/journal/entry": _entry_10_19()})
    assert "journal:2026-10-19" in entry.runtime_data.store.latest
    mock_ym.delete(url("/v1/journal/entry/1552252"), status=200)
    mock_ym.delete(url("/v1/journal/entry/42"), status=200)
    events = async_capture_events(hass, "logbook_entry")
    with pytest.raises(vol.Invalid):
        await _call(hass, "delete_journal_entry")  # no silent default to today
    await _call(hass, "delete_journal_entry", {"date": "2026-10-19"})
    assert any(c.startswith("DELETE /v1/journal/entry/1552252") for c in requested(mock_ym))
    assert "journal:2026-10-19" not in entry.runtime_data.store.latest  # the stored payload is not re-added
    await _call(hass, "delete_journal_entry", {"entry_id": 42})
    assert [e.data["message"] for e in events] == [
        "Home Assistant deleted the journal entry for 2026-10-19",
        "Home Assistant deleted journal entry 42",
    ]


async def test_set_item_status_items_not_combined(hass, load_entry):
    await load_entry(allow_writes=True)
    for extra in ({"products": ["Dimension"]}, {"window": "next"}):
        with pytest.raises(vol.Invalid):
            await _call(hass, "set_item_status", {"status": "done", "items": [30869939], **extra})


async def test_reminder_payloads(hass, mock_ym, load_entry):
    reminder = {"id": 77, "createDate": "2026-10-01T12:00:00.000Z", "detail": "<p>Old</p>", "dismissDate": None,
                "dueDate": "2026-10-25"}
    calendar = fixture("calendar_2026")
    calendar["years"][0]["days"].append({"date": "2026-10-25", "reminders": [reminder]})
    entry = await load_entry(allow_writes=True, calendars={2026: calendar})
    coordinator = entry.runtime_data
    mock_ym.post(url("/v1/reminder"), payload={**reminder, "id": 78})
    mock_ym.put(url("/v1/reminder"), payload=reminder)
    mock_ym.put(url("/v1/reminder/77/dismiss"), status=200)

    assert (await reminder_create(coordinator, date(2026, 10, 30), "Buy <seed>"))["id"] == 78
    await reminder_update(coordinator, 77, day=date(2026, 10, 26), title=None)
    assert await reminder_dismiss(coordinator, 77) is None
    assert sent(mock_ym, "POST", "/v1/reminder") == [
        {"createDate": "2026-10-19T17:00:00.000Z", "detail": "<p>Buy &lt;seed&gt;</p>", "dueDate": "2026-10-30"}
    ]
    assert sent(mock_ym, "PUT", "/v1/reminder") == [{**reminder, "dueDate": "2026-10-26"}]
    assert any(c.startswith("PUT /v1/reminder/77/dismiss") for c in requested(mock_ym))
    with pytest.raises(ServiceValidationError, match="12345"):
        await _call(hass, "update_reminder", {"reminder_id": 12345, "title": "x"})


async def test_refresh_and_import_history_work_with_writes_off(hass, mock_ym, load_entry):
    await load_entry()
    before = sum("/v4/home/bundle" in c for c in requested(mock_ym))
    await _call(hass, "refresh")
    await hass.async_block_till_done()
    assert sum("/v4/home/bundle" in c for c in requested(mock_ym)) == before + 1
    assert await _call(hass, "import_history") == {"years": 1}


async def test_log_application_without_rate_needs_an_amount(hass, mock_ym, load_entry):
    await load_entry(allow_writes=True)
    mock_ym.get(url("/v2/product"), payload=[{"id": 900, "name": "Hydretain"}], repeat=True)
    with pytest.raises(ServiceValidationError, match="amount"):
        await _call(hass, "log_application", {"products": [{"product": "Hydretain"}]})
    assert not sent(mock_ym, "POST", "/v2/journal/entry")
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Hydretain", "amount": 4.5}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert body["productApplications"][-1]["appliedRate"] == 4.5


@pytest.mark.parametrize("existing", [{"id": None}, {"id": None, "lawnSqFt": "893 sq ft"}])
async def test_log_application_new_entry_falls_back_to_yard_size(hass, mock_ym, load_entry, existing):
    # No usable lawnSqFt on the day's entry: the yard's measured 893 sq ft is used and stored.
    await load_entry(allow_writes=True, overrides={"/v3/journal/entry": existing})
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert body["lawnSqFt"] == 893
    assert body["productApplications"][0]["appliedRate"] == 2.68


async def test_log_application_keeps_existing_soil_temp(hass, mock_ym, load_entry):
    existing = _entry_10_19()
    await load_entry(
        allow_writes=True,
        overrides={"/v3/journal/entry": existing, "/v4/home/soil-temp": {"soilTemp": 55, "soilTempUnit": "F"}},
    )
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert (body["tempValue"], body["tempUnit"]) == (existing["tempValue"], existing["tempUnit"])


async def test_log_application_soil_temp_unit_from_payload(hass, mock_ym, load_entry):
    await load_entry(
        allow_writes=True,
        overrides={"/v3/journal/entry": {"id": None}, "/v4/home/soil-temp": {"soilTemp": 17, "soilTempUnit": "C"}},
    )
    mock_ym.post(url("/v2/journal/entry"), payload=SAVED)
    await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    (body,) = sent(mock_ym, "POST", "/v2/journal/entry")
    assert (body["tempValue"], body["tempUnit"]) == (17, "C")


async def test_write_auth_failure_is_an_error_and_starts_reauth(hass, mock_ym, load_entry):
    from unittest.mock import patch

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.yard_mastery.api import YardMasteryClient, YmAuthError

    await load_entry(allow_writes=True)
    with (
        patch.object(YardMasteryClient, "save_journal_entry", side_effect=YmAuthError("nope")),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _call(hass, "log_application", {"products": [{"product": "Flagship"}]})
    assert type(err.value) is HomeAssistantError
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
