"""Tests for the pure write helpers: journal merge, item resolution."""
from datetime import date

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.yard_mastery.writes import build_journal_entry, resolve_items

from .common import fixture


def _entry_10_19() -> dict:
    for year in fixture("calendar_2026")["years"]:
        for day in year["days"]:
            if day["date"] == "2026-10-19":
                return day["journalEntry"]
    raise AssertionError("fixture has no 10-19 entry")


def test_build_journal_entry_new_day_shape():
    body = build_journal_entry(None, date(2026, 10, 19))
    assert body == {
        "id": None,
        "entryDate": "2026-10-19",
        "lawnSqFt": None,
        "name": None,
        "body": None,
        "activities": [],
        "productApplications": [],
        "lawnSegments": None,
        "tempValue": None,
        "tempUnit": "F",
        "attachments": [],
        "bundleDetails": None,
    }


def test_build_journal_entry_lawn_size():
    assert build_journal_entry(None, date(2026, 10, 19), lawn_sq_ft=893)["lawnSqFt"] == 893
    assert build_journal_entry({"lawnSqFt": 700}, date(2026, 10, 19), lawn_sq_ft=893)["lawnSqFt"] == 700
    assert build_journal_entry({"lawnSqFt": None}, date(2026, 10, 19), lawn_sq_ft=893)["lawnSqFt"] == 893


def test_build_journal_entry_merges_and_clears_volatile_fields():
    existing = {
        **_entry_10_19(),
        "bundleDetails": {"id": 1},
        "activities": [{"id": 1, "name": "Mow"}],
        "body": "<p>Watered in</p>",
    }
    existing["productApplications"][0]["product"]["measure"] = "OUNCES"
    flagship = {"id": 147, "name": "Flagship", "measure": "POUNDS"}
    spoon = {"id": 164, "name": "Spoon Juice", "measure": "OUNCES"}
    body = build_journal_entry(
        existing,
        date(2026, 10, 19),
        applications=[
            {"appliedRate": 3.0, "bundleItemIds": None, "product": flagship},
            {"appliedRate": 15.0, "bundleItemIds": [1], "product": spoon},
        ],
        activities=[{"id": 1, "name": "Mow"}, {"id": 3, "name": "Edge"}],
        segments=[{"id": 637201, "name": "Front Yard"}],
        notes="Hot & dry",
        soil_temp=61.5,
    )
    assert existing["bundleDetails"] == {"id": 1}  # input untouched
    assert body["id"] == 1552252
    assert body["bundleDetails"] is None
    apps = {a["product"]["id"]: a for a in body["productApplications"]}
    assert list(apps) == [575, 16, 147, 164]
    assert apps[147]["appliedRate"] == 3.0
    assert apps[147]["bundleItemIds"] == [30869937]
    assert apps[16]["appliedRate"] == 2.9
    assert apps[164]["bundleItemIds"] == [1]
    assert all(a["product"]["measure"] is None for a in body["productApplications"])
    assert [a["name"] for a in body["activities"]] == ["Mow", "Edge"]
    assert body["lawnSegments"] == [{"id": 637201, "name": "Front Yard"}]
    assert body["body"] == "<p>Watered in</p><br>Hot &amp; dry"
    assert body["tempValue"] == 61.5
    assert body["entryDate"] == "2026-10-19"


async def test_resolve_items(load_entry):
    entry = await load_entry()
    data = entry.runtime_data.data
    assert [i.id for i in resolve_items(data, item_ids=None, products=["dimension"], window="next")] == [30869939]
    assert [i.id for i in resolve_items(data, item_ids=None, products=None, window="2026-11-19")] == [30869939]
    assert [i.id for i in resolve_items(data, item_ids=[30869938], products=None, window=None)] == [30869938]
    assert len(resolve_items(data, item_ids=None, products=None, window=None)) == 3
    with pytest.raises(ServiceValidationError, match="Nope"):
        resolve_items(data, item_ids=None, products=["Nope"], window=None)
    with pytest.raises(ServiceValidationError, match="999"):
        resolve_items(data, item_ids=[999], products=None, window=None)
    with pytest.raises(ServiceValidationError, match="Someday"):
        resolve_items(data, item_ids=None, products=None, window="Someday")
