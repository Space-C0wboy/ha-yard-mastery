"""Typed models and pure parsers for Yard Mastery API payloads."""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowItem:
    id: int
    status: str
    completed: bool
    product_id: int
    product_name: str
    product_type: str | None
    material_type: str | None
    rate: float | None
    measure: str | None
    coverage_sq_ft: int | None
    nitrogen: float | None
    phosphorus: float | None
    potassium: float | None
    journal_date: date | None
    description: str | None
    display_order: int


@dataclass(frozen=True)
class Window:
    id: int
    name: str
    description: str | None
    start: date
    end: date
    items: tuple[WindowItem, ...]
    youtube_id: str | None
    image_url: str | None
    blackout_treatment: str | None
    has_details: bool
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Application:
    product_id: int
    product_name: str
    rate: float | None
    bundle_item_ids: tuple[int, ...]


@dataclass(frozen=True)
class JournalEntry:
    id: int | None
    day: date
    name: str | None
    body: str | None
    activities: tuple[str, ...]
    applications: tuple[Application, ...]
    segments: tuple[str, ...]
    soil_temp: float | None
    temp_unit: str | None
    photo_count: int
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Reminder:
    id: int
    due: date
    title: str
    dismissed: bool
    raw: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class Segment:
    id: int
    name: str
    square_feet: int | None
    color: str | None
    segment_type: str | None


@dataclass(frozen=True)
class Nutrient:
    key: str
    value: float | str
    unit: str | None
    optimal: bool | None
    low: float | None = None
    high: float | None = None
    status: str | None = None  # low / optimal / high, from the lab's colour code


@dataclass(frozen=True)
class SoilTest:
    barcode: str
    status: str
    created: date | None
    updated: date | None
    informs_program: bool
    nutrients: dict[str, Nutrient]
    recommended: tuple[str, ...]
    raw: dict = field(repr=False, compare=False)
    lab_date: date | None = None
    density: float | None = None


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _leaf(value: Any) -> bool:
    return isinstance(value, (int, float, str)) and not isinstance(value, bool)


def _parse_item(raw: dict) -> WindowItem:
    product = raw.get("product") or {}
    ptype = raw.get("productType") or product.get("productType") or {}
    status = raw.get("status") or ("COMPLETED" if raw.get("completed") else "CREATED")
    return WindowItem(
        id=raw["id"],
        status=status,
        completed=bool(raw.get("completed", status == "COMPLETED")),
        product_id=product["id"],
        product_name=product.get("name") or "",
        product_type=ptype.get("name"),
        material_type=product.get("materialType"),
        rate=_num(product.get("applicationRate")),
        measure=product.get("measure"),
        coverage_sq_ft=product.get("coverageSqFt"),
        nitrogen=_num(product.get("nitrogen")),
        phosphorus=_num(product.get("phosphorus")),
        potassium=_num(product.get("potassium")),
        journal_date=_date(raw.get("journalEntryDate")),
        description=raw.get("description"),
        display_order=raw.get("displayOrder") or 0,
    )


def parse_window(raw: dict) -> Window:
    """Parse a calendar bundle or a /details body. Raises ValueError if unusable."""
    start, end = _date(raw.get("startDate")), _date(raw.get("endDate"))
    if start is None or end is None or raw.get("id") is None:
        raise ValueError("bundle without id/dates")
    items = []
    for item in _list(raw.get("bundleItems")):
        try:
            items.append(_parse_item(item))
        except (KeyError, TypeError, AttributeError):
            _LOGGER.debug("Skipping malformed bundle item")
    items.sort(key=lambda i: i.display_order)
    return Window(
        id=raw["id"],
        name=raw.get("name") or "",
        description=raw.get("description"),
        start=start,
        end=end,
        items=tuple(items),
        youtube_id=raw.get("youtubeId"),
        image_url=raw.get("imageUrl"),
        blackout_treatment=raw.get("blackoutTreatment"),
        has_details=any(
            isinstance(b, dict)
            and (b.get("product") or {}).get("applicationRate") is not None
            for b in _list(raw.get("bundleItems"))
        ),
        raw=raw,
    )


def merge_window(base: Window, details: Window) -> Window:
    """Overlay a /details window onto a calendar bundle.

    Details supply rates and product data; status, completed and journal_date come from the calendar item
    when it has one, because cached details can be older than the calendar.
    """
    by_id = {i.id: i for i in details.items}
    items = tuple(
        replace(d, status=i.status, completed=i.completed, journal_date=i.journal_date) if (d := by_id.pop(i.id, None)) else i
        for i in base.items
    ) + tuple(by_id.values())
    return replace(
        base,
        items=items,
        description=details.description or base.description,
        youtube_id=details.youtube_id or base.youtube_id,
        image_url=details.image_url or base.image_url,
        blackout_treatment=details.blackout_treatment or base.blackout_treatment,
        has_details=base.has_details or details.has_details,
        raw=details.raw or base.raw,
    )


def _parse_application(raw: dict) -> Application:
    product = raw.get("product") or {}
    return Application(
        product_id=product["id"],
        product_name=product.get("name") or "",
        rate=_num(raw.get("appliedRate")),
        bundle_item_ids=tuple(raw.get("bundleItemIds") or ()),
    )


def _activity_name(raw: Any) -> str:
    """Activities are {id, name} objects; a plain name is accepted too."""
    if isinstance(raw, dict):
        return str(raw.get("name") or "")
    return "" if raw is None else str(raw)


def parse_journal_entry(raw: dict) -> JournalEntry:
    """Parse a journal entry. Raises ValueError when the date is unusable."""
    day = _date(raw.get("entryDate"))
    if day is None:
        raise ValueError("journal entry without date")
    apps = []
    for app in _list(raw.get("productApplications")):
        try:
            apps.append(_parse_application(app))
        except (KeyError, TypeError, AttributeError):
            _LOGGER.debug("Skipping malformed product application")
    segments = []
    for seg in _list(raw.get("lawnSegments")):
        if isinstance(seg, dict):
            segments.append(str(seg.get("name") or seg.get("id") or ""))
        elif seg is not None:
            segments.append(str(seg))
    return JournalEntry(
        id=raw.get("id"),
        day=day,
        name=raw.get("name"),
        body=raw.get("body"),
        activities=tuple(a for a in map(_activity_name, _list(raw.get("activities"))) if a),
        applications=tuple(apps),
        segments=tuple(s for s in segments if s),
        soil_temp=_num(raw.get("tempValue")),
        temp_unit=raw.get("tempUnit"),
        photo_count=len(_list(raw.get("attachments"))),
        raw=raw,
    )


def _parse_reminder(raw: dict, fallback_day: date | None) -> Reminder:
    # App model fields: id, createDate, detail (HTML), dismissDate, dueDate
    due = _date(raw.get("dueDate")) or fallback_day
    if due is None or raw.get("id") is None:
        raise ValueError("reminder without id/date")
    title = raw.get("name") or raw.get("title")
    if not title and isinstance(raw.get("detail"), str):
        title = " ".join(re.sub(r"<[^>]+>", " ", raw["detail"]).split())
    return Reminder(
        id=raw["id"],
        due=due,
        title=title or "Reminder",
        dismissed=bool(raw.get("dismissDate")),
        raw=raw,
    )


def parse_calendar(
    raw: dict,
) -> tuple[dict[int, Window], dict[date, JournalEntry], dict[int, Reminder]]:
    """Parse a calendar/search payload into windows, journal entries, reminders."""
    windows: dict[int, Window] = {}
    journal: dict[date, JournalEntry] = {}
    reminders: dict[int, Reminder] = {}
    for year in _list(raw.get("years")):
        for day in _list(year.get("days")):
            day_date = _date(day.get("date"))
            if isinstance(day.get("bundle"), dict):
                try:
                    win = parse_window(day["bundle"])
                    windows[win.id] = win
                except (KeyError, TypeError, ValueError):
                    _LOGGER.debug("Skipping unparseable bundle on %s", day_date)
            if isinstance(day.get("journalEntry"), dict):
                try:
                    entry = parse_journal_entry(day["journalEntry"])
                    journal[entry.day] = entry
                except (KeyError, TypeError, ValueError):
                    _LOGGER.debug("Skipping unparseable journal entry on %s", day_date)
            for rem in _list(day.get("reminders")):
                try:
                    parsed = _parse_reminder(rem, day_date)
                    reminders[parsed.id] = parsed
                except (KeyError, TypeError, ValueError, AttributeError):
                    _LOGGER.debug("Skipping unparseable reminder on %s", day_date)
    return windows, journal, reminders


def parse_segments(raw: list | None) -> list[Segment]:
    segments = []
    for seg in _list(raw):
        try:
            segments.append(
                Segment(
                    id=seg["id"],
                    name=seg.get("name") or "",
                    square_feet=seg.get("squareFeet"),
                    color=seg.get("color"),
                    segment_type=seg.get("segmentType"),
                )
            )
        except (KeyError, TypeError, AttributeError):
            _LOGGER.debug("Skipping malformed lawn segment")
    return segments


_COLOR_STATUS = {"red": "low", "green": "optimal", "blue": "high"}


def _flatten_sample(
    group: str, sample: dict, out: dict[str, Nutrient], ranges: dict[str, tuple] | None = None
) -> None:
    for name, value in sample.items():
        if name == "recommendation":
            continue
        if name == "sample_nutrients" and isinstance(value, list):
            # per-nutrient list: {nutrient_abbr, value, ration, color_code}
            for nut in value:
                if not isinstance(nut, dict) or not nut.get("nutrient_abbr"):
                    continue
                if not _leaf(nut.get("value")):
                    continue
                key = f"{group}_{nut['nutrient_abbr']}".lower()
                color = nut.get("color_code")
                low, high = (ranges or {}).get(nut["nutrient_abbr"].lower(), (None, None))
                out[key] = Nutrient(
                    key,
                    nut["value"],
                    group,
                    None if color is None else color == "green",
                    low,
                    high,
                    _COLOR_STATUS.get(color),
                )
        elif isinstance(value, dict):
            for sub, subval in value.items():
                if _leaf(subval):
                    key = f"{group}_{name}_{sub}".lower()
                    out[key] = Nutrient(key, subval, None, None)
        elif _leaf(value):
            key = f"{group}_{name}".lower()
            out[key] = Nutrient(key, value, None, None)


def parse_soil_test(
    dashboard_item: dict, report: dict | None, stilp: dict | list | None
) -> SoilTest:
    nutrients: dict[str, Nutrient] = {}
    density: float | None = None
    results = ((report or {}).get("report") or {}).get("results") or {}
    for group, body in results.items():
        if not isinstance(body, dict):
            continue
        ranges = {
            str(r["nutrient_abbr"]).lower(): (_num(r.get("low")), _num(r.get("high")))
            for r in _list(body.get("available_ranges"))
            if isinstance(r, dict) and r.get("nutrient_abbr")
        }
        for sample in _list(body.get("samples")):
            if isinstance(sample, dict):
                _flatten_sample(group, sample, nutrients, ranges)
                if density is None:
                    density = _num(sample.get("density_score"))
    products = stilp.get("products") if isinstance(stilp, dict) else stilp
    recommended: list[str] = []
    for entry in _list(products):
        name = ((entry or {}).get("product") or {}).get("name")
        if name and name not in recommended:
            recommended.append(name)
    return SoilTest(
        barcode=dashboard_item.get("barcode") or "",
        status=dashboard_item.get("statusDisplayName") or "",
        created=_date(dashboard_item.get("createDate")),
        updated=_date(dashboard_item.get("updateDate")),
        informs_program=bool(dashboard_item.get("doesInformLawnProgram")),
        nutrients=nutrients,
        recommended=tuple(recommended),
        raw=report or dashboard_item,
        lab_date=_date(((report or {}).get("report") or {}).get("lab_date")),
        density=density,
    )


def soil_history(tests: Iterable[SoilTest]) -> list[dict]:
    """Every completed test, oldest first: lab date, density and each ppm sample nutrient. No barcodes."""
    out = []
    for test in tests:
        if test.status.lower() != "completed":
            continue
        day = test.lab_date or test.created
        nutrients = {
            key.partition("_")[2]: {"value": n.value, "low": n.low, "high": n.high, "status": n.status}
            for key, n in test.nutrients.items()
            if key.startswith("ppm_") and n.unit == "ppm" and isinstance(n.value, (int, float))
        }
        out.append({"date": day.isoformat() if day else None, "density": test.density, "nutrients": nutrients})
    return sorted(out, key=lambda h: h["date"] or "")


def lawn_square_feet(yard: dict) -> float | None:
    """The app's lawn size: the measured area when known, else the yard's square feet."""
    for key in ("squareFtMeasured", "squareFt"):
        value = _num(yard.get(key))
        if value is not None and value > 0:
            return value
    return None


def total_amount(rate: float | None, coverage_sq_ft: float | None, lawn_sq_ft: float | None) -> float | None:
    """Total product over the lawn (what the app stores as appliedRate): rate x area / coverage, to 0.01."""
    if rate is None or not coverage_sq_ft or not lawn_sq_ft:
        return None
    return round(rate * lawn_sq_ft / coverage_sq_ft, 2)


def whole_lawn_amount(item: WindowItem, square_feet: float) -> float | None:
    """Total product for the whole lawn: rate x area / coverage, to 0.01 (as the app rounds)."""
    return total_amount(item.rate, item.coverage_sq_ft, square_feet)


def season_totals(
    entries: Iterable[JournalEntry],
    items_by_product: dict[int, WindowItem],
    year: int,
    lawn_sq_ft: float | None = None,
) -> dict:
    """Pounds of N/P/K per 1000 sq ft applied in a year (POUNDS products only).

    appliedRate is the total spread over the entry's lawnSqFt (fallback: lawn_sq_ft), so per 1000 sq ft
    it is divided by lawnSqFt / 1000.
    """
    n = p = k = 0.0
    count = 0
    by_product: dict[str, dict[str, float]] = {}
    not_counted: list[str] = []
    for entry in entries:
        if entry.day.year != year:
            continue
        lawn = _num(entry.raw.get("lawnSqFt")) or lawn_sq_ft
        for app in entry.applications:
            count += 1
            item = items_by_product.get(app.product_id)
            if item is None or item.measure != "POUNDS" or app.rate is None or not lawn:
                if app.product_name not in not_counted:
                    not_counted.append(app.product_name)
                continue
            per_1000 = app.rate / (lawn / 1000)
            n_add = per_1000 * (item.nitrogen or 0) / 100
            n += n_add
            p += per_1000 * (item.phosphorus or 0) / 100
            k += per_1000 * (item.potassium or 0) / 100
            slot = by_product.setdefault(app.product_name, {"amount": 0.0, "n": 0.0})
            slot["amount"] = round(slot["amount"] + app.rate, 3)
            slot["n"] = round(slot["n"] + n_add, 3)
    return {
        "n": round(n, 3),
        "p": round(p, 3),
        "k": round(k, 3),
        "applications": count,
        "by_product": by_product,
        "not_counted": not_counted,
    }
