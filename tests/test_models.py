"""Tests for models and parsers."""
import copy
from dataclasses import replace
from datetime import date

from custom_components.yard_mastery.models import (
    lawn_square_feet,
    merge_window,
    parse_calendar,
    parse_journal_entry,
    parse_segments,
    parse_soil_test,
    parse_window,
    season_totals,
    soil_history,
    whole_lawn_amount,
)

from .common import fixture


def test_parse_calendar_2026():
    windows, journal, reminders = parse_calendar(fixture("calendar_2026"))
    assert len(windows) == 8
    assert set(journal) == {date(2026, 10, 8), date(2026, 10, 19)}
    assert reminders == {}
    win = next(w for w in windows.values() if w.start == date(2026, 11, 19))
    assert [(i.product_name, i.status) for i in win.items] == [("Dimension", "CREATED")]
    assert win.has_details is False


def test_parse_calendar_empty():
    assert parse_calendar(fixture("calendar_empty")) == ({}, {}, {})


def test_parse_bundle_details_rates():
    win = parse_window(fixture("bundle_current"))
    flagship = next(i for i in win.items if i.product_name == "Flagship")
    assert flagship.rate == 3.0
    assert flagship.measure == "POUNDS"
    assert flagship.coverage_sq_ft == 1000
    assert (flagship.nitrogen, flagship.phosphorus, flagship.potassium) == (24, 0, 6)
    assert flagship.status == "COMPLETED"
    assert flagship.completed is True
    assert flagship.journal_date == date(2026, 10, 19)
    assert win.has_details is True
    assert win.start == date(2026, 10, 17)


def test_merge_window_adds_details():
    windows, _, _ = parse_calendar(fixture("calendar_2026"))
    base = windows[12848538]
    assert base.has_details is False
    merged = merge_window(base, parse_window(fixture("bundle_current")))
    assert merged.has_details is True
    assert {i.id: i.rate for i in merged.items}[30869937] == 3.0
    assert merged.id == base.id


def test_windows_deduped_across_years():
    raw = fixture("calendar_2026")
    other = copy.deepcopy(raw)
    bundle = other["years"][0]["days"][-1]["bundle"]
    bundle["startDate"], bundle["endDate"] = "2026-12-19", "2027-01-30"
    other["years"][0]["year"] = 2027
    w1, _, _ = parse_calendar(raw)
    w2, _, _ = parse_calendar(other)
    merged = {**w1, **w2}
    assert len([w for w in merged.values() if w.id == bundle["id"]]) == 1
    assert len(merged) == 8


def test_journal_entry_nulls_and_activity_only():
    entry = parse_journal_entry(
        {
            "id": 1,
            "entryDate": "2026-07-04",
            "activities": ["Mow"],
            "attachments": [],
            "lawnSegments": None,
            "tempValue": None,
        }
    )
    assert entry.applications == ()
    assert entry.segments == ()
    assert entry.activities == ("Mow",)
    assert entry.photo_count == 0
    assert entry.soil_temp is None


def test_journal_10_19_applications():
    _, journal, _ = parse_calendar(fixture("calendar_2026"))
    entry = journal[date(2026, 10, 19)]
    assert [(a.product_name, a.rate) for a in entry.applications] == [
        ("Compaction Cure", 9.0),
        ("Prodiamine", 2.9),
        ("Flagship", 2.8),
    ]
    assert entry.soil_temp == 62.0
    assert entry.temp_unit == "F"
    assert journal[date(2026, 10, 8)].photo_count == 1


def test_whole_lawn_amount():
    win = parse_window(fixture("bundle_current"))
    flagship = next(i for i in win.items if i.product_name == "Flagship")
    assert whole_lawn_amount(flagship, 893) == 2.68  # the app's 10-08 Flagship amount
    no_rate = parse_calendar(fixture("calendar_2026"))[0]
    item = next(iter(no_rate.values())).items[0]
    assert whole_lawn_amount(item, 893) is None


def test_lawn_square_feet_prefers_measured():
    assert lawn_square_feet({"squareFt": 3672, "squareFtMeasured": 893}) == 893
    assert lawn_square_feet({"squareFt": 3672, "squareFtMeasured": 0}) == 3672
    assert lawn_square_feet({}) is None


def test_parse_segments():
    segs = parse_segments(fixture("lawn_segments"))
    assert any(s.name == "Side Yard" and s.square_feet == 226 for s in segs)
    assert parse_segments(None) == []


def test_season_totals_counts_only_pounds():
    windows, journal, _ = parse_calendar(fixture("calendar_2026"))
    items = {i.product_id: i for w in windows.values() for i in w.items}
    # detailed bundle knows Flagship is POUNDS at 24 % N; calendar items carry no measure
    items.update({i.product_id: i for i in parse_window(fixture("bundle_current")).items})
    # appliedRate is the total over the lawn (calendar entries carry no lawnSqFt: the 893 fallback applies)
    totals = season_totals(journal.values(), items, 2026, 893)
    assert totals["n"] == round((2.68 + 2.8) / 0.893 * 0.24, 3)
    assert "Spoon Juice" in totals["not_counted"]
    assert totals["applications"] == 5
    assert totals["by_product"]["Flagship"]["amount"] == round(2.68 + 2.8, 3)
    assert season_totals(journal.values(), items, 2025, 893)["applications"] == 0
    # An entry's own lawnSqFt wins over the fallback; with neither, nothing is counted.
    one = next(e for e in journal.values() if e.day.day == 19)
    one = replace(one, raw={**one.raw, "lawnSqFt": 1000})
    assert season_totals([one], items, 2026, 893)["n"] == round(2.8 * 0.24, 3)
    assert season_totals(journal.values(), items, 2026)["n"] == 0


def test_parse_soil_test_flattens_numbers():
    dash = fixture("soil_dashboard")["completed"][0]
    soil = parse_soil_test(dash, fixture("soil_report"), fixture("stilp_products"))
    assert soil.status == "Completed"
    assert soil.created == date(2026, 9, 30)
    assert soil.updated == date(2026, 10, 6)
    assert soil.informs_program is True
    assert soil.nutrients
    for key, nut in soil.nutrients.items():
        assert "recommendation" not in key
        assert isinstance(nut.value, (int, float, str))
        assert key == key.lower()
    assert soil.nutrients["ppm_ca"].value == 654.36
    assert soil.nutrients["ppm_ca"].unit == "ppm"
    assert "Flagship" in soil.recommended


def test_parse_soil_test_without_report():
    dash = fixture("soil_dashboard")["completed"][0]
    soil = parse_soil_test(dash, None, None)
    assert soil.nutrients == {}
    assert soil.recommended == ()


def test_merge_window_keeps_calendar_status_over_details():
    windows, _, _ = parse_calendar(fixture("calendar_2026"))
    base = windows[12848538]
    details = parse_window(fixture("bundle_current"))
    stale = replace(details, items=tuple(replace(i, status="CREATED", completed=False, journal_date=None) for i in details.items))
    merged = merge_window(base, stale)
    by_id = {i.id: i for i in merged.items}
    for item in base.items:
        assert (by_id[item.id].status, by_id[item.id].completed, by_id[item.id].journal_date) == (
            item.status,
            item.completed,
            item.journal_date,
        )
        assert by_id[item.id].rate == {i.id: i.rate for i in details.items}[item.id]


def test_journal_activities_as_objects_or_names():
    # The app (and our writes) post {id, name} objects; older payloads may carry plain names.
    entry = parse_journal_entry(
        {"id": 1, "entryDate": "2026-07-04", "activities": [{"id": 1, "name": "Mow"}, "Edge", {"id": 9}]}
    )
    assert entry.activities == ("Mow", "Edge")


def _soil(report=None, **dash):
    base = {**fixture("soil_dashboard")["completed"][0], **dash}
    return parse_soil_test(base, fixture("soil_report") if report is None else report, None)


def test_parse_soil_test_ranges_and_status():
    soil = _soil()
    assert soil.lab_date == date(2026, 10, 6)
    assert soil.density == 54.0
    k = soil.nutrients["ppm_k"]
    assert (k.value, k.status, k.optimal) == (33.88, "low", False)
    assert soil.nutrients["ppm_ca"].status == "high"
    assert soil.nutrients["ppm_cu"].status == "optimal"
    b = soil.nutrients["ppm_b"]
    assert (b.low, b.high) == (0.22, 0.66)
    nh4 = soil.nutrients["ppm_nh4-n"]
    assert (nh4.low, nh4.high) == (None, None)


def test_soil_history_one_test():
    hist = soil_history([_soil()])
    assert len(hist) == 1
    h = hist[0]
    assert h["date"] == "2026-10-06"
    assert h["density"] == 54.0
    assert h["nutrients"]["ph"]["value"] == 6.39
    assert h["nutrients"]["k"] == {"value": 33.88, "low": h["nutrients"]["k"]["low"], "high": h["nutrients"]["k"]["high"], "status": "low"}
    assert h["nutrients"]["k"]["low"] is not None
    assert set(h["nutrients"]) >= {"ph", "n", "p", "k", "ca", "mg", "s", "fe", "mn", "zn", "b", "cu", "na", "nh4-n", "no3-n"}


def test_soil_history_orders_and_skips_incomplete():
    old_report = copy.deepcopy(fixture("soil_report"))
    old_report["report"]["lab_date"] = "2025-09-01T10:00:00"
    for nut in old_report["report"]["results"]["ppm"]["samples"][0]["sample_nutrients"]:
        if nut["nutrient_abbr"] == "ph":
            nut["value"] = 6.1
    new = _soil()
    old = _soil(old_report, barcode="YM00002")
    pending = _soil(barcode="YM00003", statusDisplayName="Processing")
    hist = soil_history([new, pending, old])
    assert [h["date"] for h in hist] == ["2025-09-01", "2026-10-06"]
    assert hist[0]["nutrients"]["ph"]["value"] == 6.1


def test_soil_history_has_no_barcode():
    import json
    assert "YM0000" not in json.dumps(soil_history([_soil()]))
