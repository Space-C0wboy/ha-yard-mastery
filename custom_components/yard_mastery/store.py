"""Raw latest-payload store and append-only change log."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .models import JournalEntry, Reminder, SoilTest, Window

STORAGE_VERSION = 1
SAVE_DELAY = 10
_VOLATILE_JOURNAL_KEYS = ("bundleDetails",)


@dataclass
class YmSnapshot:
    windows: dict[int, Window]
    journal: dict[date, JournalEntry]
    reminders: dict[int, Reminder]
    soil_tests: dict[str, SoilTest]


def _stable(entry: JournalEntry) -> dict:
    return {k: v for k, v in entry.raw.items() if k not in _VOLATILE_JOURNAL_KEYS}


class YmStore:
    """Persist the latest raw payload per key plus a change history."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"yard_mastery.{entry_id}")
        self.latest: dict[str, Any] = {}
        self.history: list[dict] = []

    async def load(self) -> None:
        data = await self._store.async_load() or {}
        self.latest = data.get("latest", {})
        self.history = data.get("history", [])

    def _data(self) -> dict:
        return {"latest": self.latest, "history": self.history}

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

    async def save_now(self) -> None:
        await self._store.async_save(self._data())

    async def remove(self) -> None:
        await self._store.async_remove()

    def set_latest(self, key: str, payload: Any) -> None:
        self.latest[key] = payload
        self._schedule_save()

    def drop(self, key: str) -> None:
        if self.latest.pop(key, None) is not None:
            self._schedule_save()

    def record_write(self, kind: str, detail: dict) -> None:
        self.history.append(
            {
                "at": dt_util.utcnow().isoformat(),
                "source": "home_assistant",
                "kind": f"write_{kind}",
                "detail": detail,
            }
        )
        self._schedule_save()

    def record_changes(
        self,
        old: YmSnapshot | None,
        new: YmSnapshot,
        *,
        source: str = "yard_mastery",
    ) -> list[dict]:
        """Diff two snapshots, append the changes to history and return them."""
        if old is None:
            return []
        at = dt_util.utcnow().isoformat()
        changes: list[dict] = []

        def add(kind: str, key: str, old_val: Any, new_val: Any) -> None:
            changes.append(
                {"at": at, "source": source, "kind": kind, "key": key, "old": old_val, "new": new_val}
            )

        for wid, win in new.windows.items():
            prev = old.windows.get(wid)
            if prev is None:
                add("window_added", f"window:{wid}", None, [win.start.isoformat(), win.end.isoformat()])
                continue
            if (prev.start, prev.end) != (win.start, win.end):
                add(
                    "window_dates",
                    f"window:{wid}",
                    [prev.start.isoformat(), prev.end.isoformat()],
                    [win.start.isoformat(), win.end.isoformat()],
                )
            prev_items = {i.id: i for i in prev.items}
            for item in win.items:
                before = prev_items.get(item.id)
                if before is not None and before.status != item.status:
                    add("item_status", f"item:{item.id}", before.status, item.status)

        for day, entry in new.journal.items():
            key = f"journal:{day.isoformat()}"
            prev_entry = old.journal.get(day)
            if prev_entry is None:
                add("journal_added", key, None, entry.name)
            elif _stable(prev_entry) != _stable(entry):
                add("journal_changed", key, prev_entry.name, entry.name)
        for day, prev_entry in old.journal.items():
            if day not in new.journal:
                add("journal_deleted", f"journal:{day.isoformat()}", prev_entry.name, None)

        for rid, rem in new.reminders.items():
            key = f"reminder:{rid}"
            prev_rem = old.reminders.get(rid)
            if prev_rem is None:
                add("reminder_added", key, None, rem.title)
            elif not prev_rem.dismissed and rem.dismissed:
                add("reminder_dismissed", key, False, True)
            elif prev_rem != rem:
                add(
                    "reminder_changed",
                    key,
                    [prev_rem.title, prev_rem.due.isoformat()],
                    [rem.title, rem.due.isoformat()],
                )

        for barcode, test in new.soil_tests.items():
            key = f"soil_test:{barcode}"
            prev_test: SoilTest | None = old.soil_tests.get(barcode)
            if prev_test is None:
                add("soil_test_added", key, None, test.status)
            elif prev_test.status != test.status:
                add("soil_test_status", key, prev_test.status, test.status)

        if changes:
            self.history.extend(changes)
            self._schedule_save()
        return changes
