"""Gated writes: the option gate, item/product resolution, journal merge, logbook + change log + refresh."""
from __future__ import annotations

import copy
import html
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import YmAuthError, YmError
from .const import CONF_ALLOW_WRITES, DOMAIN, WRITES_OFF_MESSAGE
from .models import Window, WindowItem

if TYPE_CHECKING:
    from .coordinator import YmCoordinator, YmData

# Logbook wording per write kind; formatted with the write's detail.
_MESSAGES: dict[str, str | Callable[[dict], str]] = {
    "log_application": "logged {products} for {date}",
    "set_item_status": "set {products} to {status}",
    "log_activity": "logged {activities} for {date}",
    "add_photo": "added a photo to {date}",
    "delete_journal_entry": lambda d: (
        f"deleted the journal entry for {d['date']}" if d.get("date") else f"deleted journal entry {d.get('entry_id')}"
    ),
    "create_reminder": "created reminder \"{title}\" for {date}",
    "update_reminder": "updated reminder {reminder_id}",
    "dismiss_reminder": "dismissed reminder {reminder_id}",
}
_TAGS = re.compile(r"<[^>]*>")


def ensure_writes_allowed(entry: ConfigEntry) -> None:
    if not entry.options.get(CONF_ALLOW_WRITES, False):
        raise ServiceValidationError(WRITES_OFF_MESSAGE)


def _message(kind: str, detail: dict) -> str:
    values = {k: ", ".join(map(str, v)) if isinstance(v, list) else v for k, v in detail.items()}
    template = _MESSAGES.get(kind)
    try:
        text = template(values) if callable(template) else template.format(**values)
    except (KeyError, IndexError, AttributeError):
        text = kind.replace("_", " ")
    return f"Home Assistant {text}"


def _reauth(coordinator: YmCoordinator, err: YmAuthError) -> HomeAssistantError:
    """Start reauth (an action must not raise ConfigEntryAuthFailed) and return the error to raise."""
    coordinator.config_entry.async_start_reauth(coordinator.hass)
    return HomeAssistantError(f"Yard Mastery needs you to sign in again: {err}")


async def run_write(coordinator: YmCoordinator, kind: str, detail: dict, call: Callable[[], Awaitable[Any]]) -> Any:
    """Run one write: gate, call, error mapping, logbook, change log, refresh. Returns the API response."""
    ensure_writes_allowed(coordinator.config_entry)
    try:
        result = await call()
    except YmAuthError as err:
        raise _reauth(coordinator, err) from err
    except YmError as err:
        raise HomeAssistantError(f"Yard Mastery rejected the change: {err}") from err
    coordinator.hass.bus.async_fire(
        "logbook_entry", {"name": "Yard Mastery", "message": _message(kind, detail), "domain": DOMAIN}
    )
    coordinator.store.record_write(kind, detail)
    await coordinator.request_refresh_full()
    return result


async def read(coordinator: YmCoordinator, call: Callable[[], Awaitable[Any]]) -> Any:
    """A read made on behalf of an action, with the same error mapping as writes."""
    try:
        return await call()
    except YmAuthError as err:
        raise _reauth(coordinator, err) from err
    except YmError as err:
        raise HomeAssistantError(f"Could not read from Yard Mastery: {err}") from err


# ---- resolution ------------------------------------------------------------


def _all_windows(data: YmData) -> dict[int, Window]:
    windows = dict(data.snapshot.windows)
    for window in (data.current_window, data.next_window):
        if window is not None:
            windows[window.id] = window
    return windows


def window_of(data: YmData, item_id: int) -> Window | None:
    """The window holding program item `item_id`."""
    return next((w for w in _all_windows(data).values() if any(i.id == item_id for i in w.items)), None)


def resolve_window(data: YmData, window: str | None) -> Window:
    """`current` (default), `next`, a window name or a window start date."""
    key = (window or "current").strip()
    if key.lower() in ("current", "next"):
        found = data.current_window if key.lower() == "current" else data.next_window
        if found is None:
            raise ServiceValidationError(f"Yard Mastery has no {key.lower()} window")
        return found
    windows = _all_windows(data).values()
    try:
        start = date.fromisoformat(key)
    except ValueError:
        matches = [w for w in windows if w.name.casefold() == key.casefold()]
    else:
        matches = [w for w in windows if w.start == start]
    if len(matches) != 1:
        problem = "No" if not matches else "More than one"
        raise ServiceValidationError(f"{problem} Yard Mastery window matches \"{key}\"")
    return matches[0]


def resolve_items(
    data: YmData, *, item_ids: list[int] | None, products: list[str] | None, window: str | None
) -> list[WindowItem]:
    """Program items by id (any window), or by product name in a window (default: all of that window's items)."""
    if item_ids:
        by_id = {i.id: i for w in _all_windows(data).values() for i in w.items}
        unknown = [str(i) for i in item_ids if i not in by_id]
        if unknown:
            raise ServiceValidationError(f"Unknown Yard Mastery program item: {', '.join(unknown)}")
        return [by_id[i] for i in item_ids]
    found = resolve_window(data, window)
    if not products:
        return list(found.items)
    items = []
    for name in products:
        matches = [i for i in found.items if i.product_name.casefold() == name.strip().casefold()]
        if not matches:
            raise ServiceValidationError(f"No product \"{name}\" in the Yard Mastery window {found.name}")
        items.extend(matches)
    return items


def _raw_products(data: YmData) -> Iterable[dict]:
    """Raw product objects known locally: window items first, then journal history (newest first)."""
    for window in _all_windows(data).values():
        for item in window.raw.get("bundleItems") or []:
            if isinstance(item, dict) and isinstance(item.get("product"), dict):
                yield item["product"]
    for _, entry in sorted(data.snapshot.journal.items(), reverse=True):
        for app in entry.raw.get("productApplications") or []:
            if isinstance(app, dict) and isinstance(app.get("product"), dict):
                yield app["product"]


async def resolve_product(coordinator: YmCoordinator, name_or_id: str | int) -> dict:
    """A product object by id or name: window items, then journal history, then the product search."""
    text = str(name_or_id).strip()
    by_id = int(text) if text.isdigit() else None

    def matches(product: dict) -> bool:
        if by_id is not None:
            return product.get("id") == by_id
        return str(product.get("name") or "").casefold() == text.casefold()

    for product in _raw_products(coordinator.data):
        if matches(product):
            return copy.deepcopy(product)
    if by_id is not None:
        raise ServiceValidationError(f"Unknown Yard Mastery product id {by_id}")
    raw = await read(coordinator, lambda: coordinator.client.product_search(term=text))
    if isinstance(raw, dict):
        raw = raw.get("content") or raw.get("products") or []
    results = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    exact = [p for p in results if matches(p)]
    if len(exact) == 1 or (not exact and len(results) == 1):
        return copy.deepcopy((exact or results)[0])
    if not results:
        raise ServiceValidationError(f"Yard Mastery has no product \"{text}\"")
    names = ", ".join(str(p.get("name")) for p in results[:5])
    raise ServiceValidationError(f"More than one Yard Mastery product matches \"{text}\": {names}")


# ---- journal ---------------------------------------------------------------


def area(value: Any) -> float | None:
    """A usable lawn size: a positive number, else None (strings and junk count as missing)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return value


def _new_entry(day: date, lawn_sq_ft: float | None) -> dict:
    return {
        "id": None,
        "entryDate": day.isoformat(),
        "lawnSqFt": lawn_sq_ft,
        "name": None,
        "body": None,
        "activities": [],
        "productApplications": [],
        "lawnSegments": None,
        "tempValue": None,
        "tempUnit": "F",
        "attachments": [],
    }


def build_journal_entry(
    existing: dict | None,
    day: date,
    *,
    applications: list[dict] | None = None,
    activities: list[dict] | None = None,
    segments: list[dict] | None = None,
    notes: str | None = None,
    soil_temp: float | None = None,
    temp_unit: str | None = None,
    lawn_sq_ft: float | None = None,
) -> dict:
    """The journal entry to POST: `existing` (or a new day) merged with the additions. Pure.

    applications are `{appliedRate, bundleItemIds, product}`, appliedRate being the total amount spread over the
    entry's `lawnSqFt` (kept from `existing`; a new entry gets `lawn_sq_ft`); activities and segments are the app's objects
    (`/v1/journal/entry/activity` and `/v1/lawn-segment` items), unioned by name and id as the app does.
    """
    entry = copy.deepcopy(existing) if existing else _new_entry(day, lawn_sq_ft)
    entry["entryDate"] = day.isoformat()
    if area(entry.get("lawnSqFt")) is None and lawn_sq_ft:
        entry["lawnSqFt"] = lawn_sq_ft
    for key in ("activities", "productApplications", "attachments"):
        if not isinstance(entry.get(key), list):
            entry[key] = []

    apps: list[dict] = entry["productApplications"]
    for new in copy.deepcopy(applications or []):
        product_id = (new.get("product") or {}).get("id")
        current = next((a for a in apps if isinstance(a, dict) and (a.get("product") or {}).get("id") == product_id), None)
        if current is None:
            apps.append(new)
            continue
        if new.get("appliedRate") is not None:
            current["appliedRate"] = new["appliedRate"]
        ids = [*(current.get("bundleItemIds") or []), *(new.get("bundleItemIds") or [])]
        if ids:
            current["bundleItemIds"] = list(dict.fromkeys(ids))
    for app in apps:
        if isinstance(app, dict) and isinstance(app.get("product"), dict):
            app["product"]["measure"] = None

    names = {str(a.get("name") or "").casefold() for a in entry["activities"] if isinstance(a, dict)}
    for activity in activities or []:
        if str(activity.get("name") or "").casefold() not in names:
            entry["activities"].append(copy.deepcopy(activity))
            names.add(str(activity.get("name") or "").casefold())

    if segments:
        if not isinstance(entry.get("lawnSegments"), list):
            entry["lawnSegments"] = []
        ids = {s.get("id") for s in entry["lawnSegments"] if isinstance(s, dict)}
        for segment in segments:
            if segment.get("id") not in ids:
                entry["lawnSegments"].append(copy.deepcopy(segment))
                ids.add(segment.get("id"))

    if notes:
        text = html.escape(notes)
        entry["body"] = f"{entry['body']}<br>{text}" if entry.get("body") else text
    if soil_temp is not None:
        entry["tempValue"] = soil_temp
        entry["tempUnit"] = temp_unit or entry.get("tempUnit") or "F"
    entry["bundleDetails"] = None
    return entry


def unlink_bundle_item(existing: dict, item_id: int) -> tuple[dict, bool]:
    """`existing` with program item `item_id` unlinked, and whether any application linked it. Pure.

    As the app's unapply: the id leaves each application's bundleItemIds, and an application left with no ids
    is removed. Everything else in the entry is kept.
    """
    entry = copy.deepcopy(existing)
    apps = entry.get("productApplications") if isinstance(entry.get("productApplications"), list) else []
    kept, found = [], False
    for app in apps:
        ids = app.get("bundleItemIds") if isinstance(app, dict) else None
        if isinstance(ids, list) and item_id in ids:
            found = True
            rest = [i for i in ids if i != item_id]
            if not rest:
                continue
            app["bundleItemIds"] = rest
        kept.append(app)
    entry["productApplications"] = kept
    return entry, found


def entry_is_empty(entry: dict) -> bool:
    """The app's JournalEntry.isEmpty: no applications, activities, photos, areas, title or note text."""
    text = html.unescape(_TAGS.sub("", str(entry.get("body") or ""))).strip()
    keys = ("productApplications", "activities", "attachments", "lawnSegments", "name")
    return not text and not any(entry.get(key) for key in keys)


# ---- reminders -------------------------------------------------------------


def _reminder_detail(title: str) -> str:
    return f"<p>{html.escape(title)}</p>"


async def reminder_create(coordinator: YmCoordinator, day: date, title: str) -> dict:
    created = dt_util.utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {"createDate": created, "detail": _reminder_detail(title), "dueDate": day.isoformat()}
    return await run_write(
        coordinator,
        "create_reminder",
        {"date": day.isoformat(), "title": title},
        lambda: coordinator.client.create_reminder(body),
    )


async def reminder_update(
    coordinator: YmCoordinator, reminder_id: int, *, day: date | None, title: str | None
) -> dict:
    ensure_writes_allowed(coordinator.config_entry)
    reminder = coordinator.data.snapshot.reminders.get(reminder_id)
    if reminder is None:
        raise ServiceValidationError(f"Unknown Yard Mastery reminder {reminder_id}")
    raw = reminder.raw
    # The app's Reminder.toReminder: an event title is folded into the detail.
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    detail = f"<p><b>{event['title']}</b></p>{event.get('detail') or ''}" if event.get("title") else raw.get("detail")
    body = {
        "id": raw.get("id"),
        "createDate": raw.get("createDate"),
        "detail": _reminder_detail(title) if title is not None else detail,
        "dismissDate": raw.get("dismissDate"),
        "dueDate": (day or reminder.due).isoformat(),
    }
    return await run_write(
        coordinator,
        "update_reminder",
        {"reminder_id": reminder_id, "date": body["dueDate"], "title": title},
        lambda: coordinator.client.update_reminder(body),
    )


async def reminder_dismiss(coordinator: YmCoordinator, reminder_id: int) -> None:
    await run_write(
        coordinator,
        "dismiss_reminder",
        {"reminder_id": reminder_id},
        lambda: coordinator.client.dismiss_reminder(reminder_id),
    )
