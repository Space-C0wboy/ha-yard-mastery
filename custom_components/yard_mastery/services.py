"""Yard Mastery actions. Writes are refused unless the entry's "Allow changes" option is on."""
from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import YmCoordinator
from .models import Window, WindowItem, lawn_square_feet, total_amount
from .writes import (
    area,
    build_journal_entry,
    ensure_writes_allowed,
    read,
    reminder_create,
    reminder_dismiss,
    reminder_update,
    resolve_items,
    resolve_product,
    resolve_window,
    run_write,
)

ACTIVITIES = ["mow", "edge", "aerate", "dethatch", "blow", "trim"]
STATUSES = {"done": "COMPLETED", "skipped": "SKIPPED", "not_done": "CREATED"}
PHOTO_ROOTS = ("/media",)  # plus the config directory

ENTRY = {vol.Optional("config_entry_id"): cv.string}
NAMES = vol.All(cv.ensure_list, [cv.string])
PRODUCT = vol.Schema({vol.Required("product"): cv.string, vol.Optional("amount"): vol.Coerce(float)})


def _items_alone(data: dict) -> dict:
    if data.get("items") and (data.get("products") or data.get("window")):
        raise vol.Invalid("Give either `items`, or `products` with an optional `window`")
    return data

SCHEMAS: dict[str, vol.Schema] = {
    "log_application": vol.Schema(
        {
            **ENTRY,
            vol.Optional("date"): cv.date,
            vol.Optional("products"): vol.All(cv.ensure_list, [PRODUCT], vol.Length(min=1)),
            vol.Optional("window"): cv.string,
            vol.Optional("areas"): NAMES,
            vol.Optional("notes"): cv.string,
            vol.Optional("soil_temp"): vol.Coerce(float),
        }
    ),
    "set_item_status": vol.Schema(
        vol.All(
            {
                **ENTRY,
                vol.Required("status"): vol.In(list(STATUSES)),
                vol.Optional("items"): vol.All(cv.ensure_list, [cv.positive_int]),
                vol.Optional("products"): NAMES,
                vol.Optional("window"): cv.string,
            },
            cv.has_at_least_one_key("items", "products"),
            _items_alone,
        )
    ),
    "log_activity": vol.Schema(
        {
            **ENTRY,
            vol.Optional("date"): cv.date,
            vol.Required("activities"): vol.All(cv.ensure_list, [vol.In(ACTIVITIES)], vol.Length(min=1)),
            vol.Optional("areas"): NAMES,
            vol.Optional("notes"): cv.string,
        }
    ),
    "add_photo": vol.Schema(
        vol.All(
            {
                **ENTRY,
                vol.Optional("date"): cv.date,
                vol.Exclusive("file", "source"): cv.string,
                vol.Exclusive("entity_id", "source"): cv.entity_id,
            },
            cv.has_at_least_one_key("file", "entity_id"),
        )
    ),
    "delete_journal_entry": vol.Schema(
        vol.All(
            {**ENTRY, vol.Exclusive("date", "which"): cv.date, vol.Exclusive("entry_id", "which"): cv.positive_int},
            cv.has_at_least_one_key("date", "entry_id"),
        )
    ),
    "create_reminder": vol.Schema({**ENTRY, vol.Required("date"): cv.date, vol.Required("title"): cv.string}),
    "update_reminder": vol.Schema(
        {
            **ENTRY,
            vol.Required("reminder_id"): cv.positive_int,
            vol.Optional("date"): cv.date,
            vol.Optional("title"): cv.string,
        }
    ),
    "dismiss_reminder": vol.Schema({**ENTRY, vol.Required("reminder_id"): cv.positive_int}),
    "refresh": vol.Schema(ENTRY),
    "import_history": vol.Schema(ENTRY),
}
READ_ONLY = {"refresh", "import_history"}


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> YmCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id := call.data.get("config_entry_id"):
        entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None:
            raise ServiceValidationError(f"No loaded Yard Mastery entry {entry_id}")
        return entry.runtime_data
    if len(entries) == 1:
        return entries[0].runtime_data
    if not entries:
        raise ServiceValidationError("Yard Mastery is not set up or not loaded")
    raise ServiceValidationError("Several Yard Mastery accounts are set up; choose one with config_entry_id")


def _respond(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return {} if value is None else {"result": value}


def _day(call: ServiceCall) -> date:
    return call.data.get("date") or dt_util.now().date()


def _item_product(window: Window, item: WindowItem) -> dict:
    for raw in window.raw.get("bundleItems") or []:
        if isinstance(raw, dict) and raw.get("id") == item.id and isinstance(raw.get("product"), dict):
            return dict(raw["product"])
    return {"id": item.product_id, "name": item.product_name}


def _segments(coordinator: YmCoordinator, names: list[str] | None) -> list[dict]:
    """Lawn segment objects (as /v1/lawn-segment returns them) by name."""
    raw = coordinator.store.latest.get("lawn_segments")
    known = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
    found = []
    for name in names or []:
        match = next((s for s in known if str(s.get("name") or "").casefold() == name.strip().casefold()), None)
        if match is None:
            raise ServiceValidationError(f"Yard Mastery has no lawn area \"{name}\"")
        found.append(match)
    return found


def _activities(coordinator: YmCoordinator, names: list[str]) -> list[dict]:
    """Activity objects from /v1/journal/entry/activity, else id + name from the calendar filter options."""
    raw = coordinator.store.latest.get("journal_activities")
    known = [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []
    by_name = {name.casefold(): (name, aid) for name, aid in coordinator.data.activities.items()}
    found = []
    for name in names:
        match = next((a for a in known if str(a.get("name") or "").casefold() == name), None)
        if match is None and name in by_name:
            match = {"id": by_name[name][1], "name": by_name[name][0]}
        if match is None:
            raise ServiceValidationError(f"Yard Mastery has no activity \"{name}\"")
        found.append(match)
    return found


async def _save_journal(
    coordinator: YmCoordinator,
    kind: str,
    detail: dict,
    day: date,
    applications: Callable[[float | None], list[dict]] | None = None,
    default_soil_temp: float | None = None,
    **changes: Any,
) -> Any:
    """GET the day's entry, merge the changes in and POST it back.

    `applications` is built from the entry's lawn size (the existing entry's lawnSqFt, else the yard's), since
    appliedRate is the total amount spread over that area. `default_soil_temp` is used when no soil_temp is
    given and the day has no saved entry with a temperature of its own.
    """
    lawn = lawn_square_feet(coordinator.data.yard)

    async def save() -> Any:
        raw = await coordinator.client.journal_entry(day)
        existing = raw if isinstance(raw, dict) else None
        entry_lawn = area((existing or {}).get("lawnSqFt")) or lawn
        soil_temp = changes.get("soil_temp")
        saved_temp = existing is not None and existing.get("id") is not None and existing.get("tempValue") is not None
        if soil_temp is None and not saved_temp:
            soil_temp = default_soil_temp
        body = build_journal_entry(
            existing,
            day,
            applications=applications(entry_lawn) if applications else None,
            lawn_sq_ft=lawn,
            **{**changes, "soil_temp": soil_temp},
        )
        return await coordinator.client.save_journal_entry(body)

    return await run_write(coordinator, kind, detail, save)


# ---- handlers --------------------------------------------------------------


async def _log_application(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    data = coordinator.data
    day = _day(call)
    # (product, explicit total amount or None, recommended rate, coverage, linked item ids)
    planned: list[tuple[dict, float | None, Any, Any, list[int] | None]] = []
    if products := call.data.get("products"):
        for wanted in products:
            product = await resolve_product(coordinator, wanted["product"])
            planned.append(
                (product, wanted.get("amount"), product.get("applicationRate"), product.get("coverageSqFt"), None)
            )
    else:
        window = resolve_window(data, call.data.get("window"))
        items = [i for i in window.items if i.status == "CREATED"]
        if not items:
            raise ServiceValidationError(
                f"Nothing is left to log in the Yard Mastery window {window.name}; name the products instead"
            )
        planned = [(_item_product(window, i), None, i.rate, i.coverage_sq_ft, [i.id]) for i in items]

    def applications(lawn: float | None) -> list[dict]:
        built = []
        for product, amount, rate, coverage, ids in planned:
            if amount is None:
                amount = total_amount(rate, coverage, lawn)
            if amount is None:
                raise ServiceValidationError(
                    f"Yard Mastery has no application rate for {product.get('name')} or the lawn size is unknown; "
                    "give the amount applied"
                )
            built.append({"appliedRate": amount, "bundleItemIds": ids, "product": product})
        return built

    today = day == dt_util.now().date()
    segments = _segments(coordinator, call.data.get("areas"))
    detail = {"date": day.isoformat(), "products": [p[0].get("name") for p in planned]}
    return await _save_journal(
        coordinator,
        "log_application",
        detail,
        day,
        applications=applications,
        default_soil_temp=data.soil_temp.get("soilTemp") if today else None,
        segments=segments,
        notes=call.data.get("notes"),
        soil_temp=call.data.get("soil_temp"),
        temp_unit=data.soil_temp.get("soilTempUnit") or "F",
    )


async def _set_item_status(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    items = resolve_items(
        coordinator.data,
        item_ids=call.data.get("items"),
        products=call.data.get("products"),
        window=call.data.get("window"),
    )
    status = STATUSES[call.data["status"]]
    ids = [i.id for i in items]
    detail = {"status": status, "items": ids, "products": [i.product_name for i in items]}
    return await run_write(
        coordinator, "set_item_status", detail, lambda: coordinator.client.set_item_status(ids, status)
    )


async def _log_activity(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    day = _day(call)
    activities = _activities(coordinator, call.data["activities"])
    segments = _segments(coordinator, call.data.get("areas"))
    detail = {"date": day.isoformat(), "activities": [a.get("name") for a in activities]}
    return await _save_journal(
        coordinator, "log_activity", detail, day, activities=activities, segments=segments, notes=call.data.get("notes")
    )


def _read_photo(hass: HomeAssistant, file: str) -> tuple[bytes, str]:
    """Read a photo under the config directory or /media that HA allows access to (runs in the executor)."""
    path = pathlib.Path(file).resolve()
    roots = [pathlib.Path(hass.config.path()).resolve(), *(pathlib.Path(r) for r in PHOTO_ROOTS)]
    if not hass.config.is_allowed_path(str(path)) or not any(path.is_relative_to(root) for root in roots):
        raise ServiceValidationError(f"{file} is not an allowed file under /config or /media")
    if not path.is_file():
        raise ServiceValidationError(f"{file} does not exist")
    return path.read_bytes(), path.name


async def _add_photo(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    hass = coordinator.hass
    day = _day(call)
    if file := call.data.get("file"):
        image, filename = await hass.async_add_executor_job(_read_photo, hass, file)
        source = filename
    else:
        entity_id = call.data["entity_id"]
        domain, object_id = entity_id.split(".", 1)
        if domain == "camera":
            from homeassistant.components.camera import async_get_image
        elif domain == "image":
            from homeassistant.components.image import async_get_image
        else:
            raise ServiceValidationError(f"{entity_id} is not a camera or image entity")
        image = (await async_get_image(hass, entity_id)).content
        filename, source = f"{object_id}.jpg", entity_id
    detail = {"date": day.isoformat(), "source": source}
    return await run_write(
        coordinator, "add_photo", detail, lambda: coordinator.client.add_journal_photo(day, image, filename)
    )


async def _delete_journal_entry(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    entry_id = call.data.get("entry_id")
    day = None if entry_id else call.data["date"]
    detail: dict[str, Any] = {"date": day.isoformat() if day else None, "entry_id": entry_id}

    async def delete() -> Any:
        target = entry_id
        if target is None:
            existing = await coordinator.client.journal_entry(day)
            target = existing.get("id") if isinstance(existing, dict) else None
            if target is None:
                raise ServiceValidationError(f"There is no Yard Mastery journal entry on {day}")
            detail["entry_id"] = target
        result = await coordinator.client.delete_journal_entry(target)
        gone = day or next((d for d, e in coordinator.data.snapshot.journal.items() if e.id == target), None)
        if gone is not None:
            coordinator.store.drop(f"journal:{gone.isoformat()}")
        return result

    return await run_write(coordinator, "delete_journal_entry", detail, delete)


async def _create_reminder(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    return await reminder_create(coordinator, call.data["date"], call.data["title"])


async def _update_reminder(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    return await reminder_update(
        coordinator, call.data["reminder_id"], day=call.data.get("date"), title=call.data.get("title")
    )


async def _dismiss_reminder(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    return await reminder_dismiss(coordinator, call.data["reminder_id"])


async def _refresh(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    await coordinator.request_refresh_full()


async def _import_history(coordinator: YmCoordinator, call: ServiceCall) -> Any:
    years = await read(coordinator, coordinator.import_history)
    await coordinator.async_request_refresh()
    return {"years": years}


HANDLERS: dict[str, Callable[[YmCoordinator, ServiceCall], Awaitable[Any]]] = {
    "log_application": _log_application,
    "set_item_status": _set_item_status,
    "log_activity": _log_activity,
    "add_photo": _add_photo,
    "delete_journal_entry": _delete_journal_entry,
    "create_reminder": _create_reminder,
    "update_reminder": _update_reminder,
    "dismiss_reminder": _dismiss_reminder,
    "refresh": _refresh,
    "import_history": _import_history,
}


def _handler(
    name: str, fn: Callable[[YmCoordinator, ServiceCall], Awaitable[Any]]
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    async def handle(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(call.hass, call)
        if name not in READ_ONLY:
            ensure_writes_allowed(coordinator.config_entry)
        result = await fn(coordinator, call)
        return _respond(result) if call.return_response else None

    return handle


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, fn in HANDLERS.items():
        hass.services.async_register(
            DOMAIN, name, _handler(name, fn), schema=SCHEMAS[name], supports_response=SupportsResponse.OPTIONAL
        )
