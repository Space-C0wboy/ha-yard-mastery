"""Yard Mastery calendar: program windows, journal entries and reminders as all-day events."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ALLOW_WRITES
from .coordinator import YmConfigEntry, YmCoordinator
from .entity import YmEntity
from .models import JournalEntry, Window, lawn_square_feet, whole_lawn_amount
from .writes import reminder_create, reminder_dismiss, reminder_update

PARALLEL_UPDATES = 0

READ_ONLY_MESSAGE = "Use the yard_mastery actions to change program windows and journal entries"
_UNITS = {"POUNDS": "lb", "OUNCES": "oz", "FLUID_OUNCES": "fl oz", "GALLONS": "gal", "QUARTS": "qt"}


def _unit(measure: str | None) -> str:
    return _UNITS.get(measure or "", (measure or "").lower().replace("_", " "))


def _number(value: float) -> str:
    return f"{value:g}"


def _window_event(window: Window, lawn_sq_ft: float | None) -> CalendarEvent:
    lines = []
    for item in window.items:
        status = item.status.lower().replace("_", " ")
        if item.rate is None:
            lines.append(f"{item.product_name} — {status}")
            continue
        unit = _unit(item.measure)
        text = f"{item.product_name}: {_number(item.rate)} {unit}/1,000 sq ft"
        total = whole_lawn_amount(item, lawn_sq_ft) if lawn_sq_ft else None
        if total is not None:
            text += f" ({_number(total)} {unit} for the lawn)"
        lines.append(f"{text} — {status}")
    return CalendarEvent(
        start=window.start,
        end=window.end + timedelta(days=1),
        summary=window.name,
        description="\n".join(lines) or window.description,
        uid=f"window-{window.id}",
    )


def _journal_event(entry: JournalEntry, units: dict[int, str]) -> CalendarEvent:
    products = [a.product_name for a in entry.applications]
    parts = []
    if products:
        parts.append("Applied " + ", ".join(products))
    if entry.activities:
        parts.append(", ".join(entry.activities))
    lines = []
    for app in entry.applications:
        if app.rate is None:
            lines.append(app.product_name)
        else:
            unit = units.get(app.product_id)
            lines.append(f"{app.product_name}: {_number(app.rate)}" + (f" {unit}" if unit else ""))
    if entry.segments:
        lines.append("Areas: " + ", ".join(entry.segments))
    if entry.soil_temp is not None:
        lines.append(f"Soil temperature: {_number(entry.soil_temp)}°{entry.temp_unit or ''}")
    if entry.body:
        lines.append(entry.body)
    if entry.photo_count:
        lines.append(f"Photos: {entry.photo_count}")
    return CalendarEvent(
        start=entry.day,
        end=entry.day + timedelta(days=1),
        summary="; ".join(parts) or entry.name or "Lawn journal",
        description="\n".join(lines) or None,
        uid=f"journal-{entry.id if entry.id is not None else entry.day.isoformat()}",
    )


class YmCalendar(YmEntity, CalendarEntity):
    _attr_name = None

    def __init__(self, coordinator: YmCoordinator) -> None:
        super().__init__(coordinator, "calendar")

    @property
    def supported_features(self) -> CalendarEntityFeature:
        if not self.coordinator.config_entry.options.get(CONF_ALLOW_WRITES, False):
            return CalendarEntityFeature(0)
        return CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT

    def _window_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        lawn = lawn_square_feet(data.yard)
        return [_window_event(w, lawn) for w in sorted(data.snapshot.windows.values(), key=lambda w: w.start)]

    def _all_events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        units = {
            i.product_id: _unit(i.measure)
            for w in data.snapshot.windows.values()
            for i in w.items
            if i.measure
        }
        events = self._window_events()
        events += [_journal_event(e, units) for e in data.snapshot.journal.values()]
        events += [
            CalendarEvent(
                start=r.due, end=r.due + timedelta(days=1), summary=r.title, uid=f"reminder-{r.id}"
            )
            for r in data.snapshot.reminders.values()
            if not r.dismissed
        ]
        return events

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        upcoming = [e for e in self._window_events() if e.end > today]
        return min(upcoming, key=lambda e: e.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        # All-day events span local midnights; compare those with the (possibly mid-day) range ends.
        midnight = dt_util.start_of_local_day
        return sorted(
            (e for e in self._all_events() if midnight(e.start) < end_date and midnight(e.end) > start_date),
            key=lambda e: (e.start, e.uid),
        )

    @staticmethod
    def _reminder_id(uid: str) -> int:
        if uid.startswith("reminder-") and uid[9:].isdigit():
            return int(uid[9:])
        raise HomeAssistantError(READ_ONLY_MESSAGE)

    @staticmethod
    def _day(value: Any) -> date | None:
        if isinstance(value, datetime):
            return dt_util.as_local(value).date()
        return value if isinstance(value, date) else None

    async def async_create_event(self, **kwargs: Any) -> None:
        day = self._day(kwargs.get("dtstart"))
        if day is None:
            raise HomeAssistantError("A start date is required")
        await reminder_create(self.coordinator, day, kwargs["summary"])

    async def async_update_event(
        self, uid: str, event: dict[str, Any], recurrence_id: str | None = None, recurrence_range: str | None = None
    ) -> None:
        reminder_id = self._reminder_id(uid)
        await reminder_update(
            self.coordinator, reminder_id, day=self._day(event.get("dtstart")), title=event.get("summary")
        )

    async def async_delete_event(
        self, uid: str, recurrence_id: str | None = None, recurrence_range: str | None = None
    ) -> None:
        await reminder_dismiss(self.coordinator, self._reminder_id(uid))


async def async_setup_entry(
    hass: HomeAssistant, entry: YmConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([YmCalendar(entry.runtime_data)])
