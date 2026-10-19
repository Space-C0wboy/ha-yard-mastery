"""Coordinator: fast, slow and reference polling tiers, history import, version check."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import YardMasteryClient, YmApiError, YmAuthError, YmConnectionError
from .const import (
    CONF_APP_VERSION,
    CONF_SCAN_INTERVAL,
    DEFAULT_APP_VERSION,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    REFERENCE_INTERVAL,
    SLOW_INTERVAL,
)
from .models import (
    Segment,
    Window,
    merge_window,
    parse_calendar,
    parse_journal_entry,
    parse_segments,
    parse_soil_test,
    parse_window,
)
from .store import YmSnapshot, YmStore

_LOGGER = logging.getLogger(__name__)

ISSUE_APP_VERSION = "app_version_invalid"
MAX_HISTORY_YEARS = 10
RETRY_AFTER = timedelta(hours=1)
EXTRA_PER_POLL = 3  # import-resume and retry requests allowed on top of the fast tier
KEY_WALKED = "history_walked"
KEY_IMPORTED = "history_imported"
# Schema drift must never fail a poll: every parse call is wrapped with these.
_PARSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)

type YmConfigEntry = ConfigEntry[YmCoordinator]


@dataclass
class YmData:
    user_status: dict
    yard: dict
    soil_temp: dict
    home_bundle: dict
    current_window: Window | None
    next_window: Window | None
    snapshot: YmSnapshot
    segments: list[Segment]
    activities: dict[str, int]
    version_valid: bool | None
    last_sync: datetime
    request_count: int
    last_error: str | None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _log_key(key: str) -> str:
    """A store key fit for logs: per-test keys end in the soil-test barcode."""
    if key.startswith(("soil_report:", "stilp:")):
        return key.split(":", 1)[0] + ":***"
    return key


def _day_of(key: str) -> date | None:
    try:
        return date.fromisoformat(key.split(":", 1)[1])
    except ValueError:
        return None


def _safe_window(raw: Any) -> Window | None:
    if not isinstance(raw, dict):
        return None
    try:
        return parse_window(raw)
    except _PARSE_ERRORS:
        _LOGGER.debug("Skipping unparseable bundle")
        return None


class YmCoordinator(DataUpdateCoordinator[YmData]):
    """Polls Yard Mastery in three tiers and keeps the raw payloads in the store."""

    config_entry: YmConfigEntry

    def __init__(self, hass: HomeAssistant, entry: YmConfigEntry, client: YardMasteryClient, store: YmStore) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_MINUTES)),
        )
        self.client = client
        self.store = store
        self.force_slow = False
        self._last_slow: datetime | None = None
        self._last_reference: datetime | None = None
        self._snapshot: YmSnapshot | None = None
        self._extra = EXTRA_PER_POLL
        # Optional fetches that failed: key -> (when, call, args, message, final). Retried at most hourly,
        # a few per poll; a 4xx or auth error is final (not retried outside the slow/reference tiers).
        self._failed: dict[str, tuple[datetime, Any, tuple, str, bool]] = {}

    # ---- fetching --------------------------------------------------------

    async def _fetch(self, key: str, call, *args) -> Any:
        """Fetch and keep the raw payload; any error propagates."""
        raw = await call(*args)
        self.store.set_latest(key, raw)
        return raw

    async def _fetch_optional(self, key: str, call, *args) -> Any:
        """Optional data: a failure keeps the old payload, is reported and is retried at most hourly."""
        now = dt_util.utcnow()
        failed = self._failed.get(key)
        if failed is not None and now - failed[0] < RETRY_AFTER:
            return None
        try:
            raw = await self._fetch(key, call, *args)
        except (YmApiError, YmAuthError, YmConnectionError) as err:
            _LOGGER.debug("Could not refresh %s: %s", _log_key(key), err)
            # Auth on optional data is final for the key, not reauth: only the fast tier and writes reauth,
            # and the client already caps its re-logins.
            final = isinstance(err, YmAuthError) or (isinstance(err, YmApiError) and 400 <= err.status < 500)
            self._failed[key] = (now, call, args, str(err), final)
            return None
        self._failed.pop(key, None)
        return raw

    async def _extra_fetch(self, key: str, call, *args) -> bool:
        """A retry or import-resume fetch, limited per poll. True when the key needs no further work."""
        failed = self._failed.get(key)
        if failed is not None:
            if failed[4]:
                return True  # the service said no; given up until a tier asks again
            if dt_util.utcnow() - failed[0] < RETRY_AFTER:
                return False
        if self._extra <= 0:
            return False
        self._extra -= 1
        await self._fetch_optional(key, call, *args)
        return key not in self._failed

    def _wanted(self, key: str) -> bool:
        """Per-test payloads are only wanted while the test is on the dashboard."""
        if key.startswith(("soil_report:", "stilp:")):
            barcode = key.split(":", 1)[1]
            dashboard = _dict(self.store.latest.get("soil_dashboard"))
            return any(_dict(i).get("barcode") == barcode for i in dashboard.get("completed") or [])
        return True

    async def _async_update_data(self) -> YmData:
        try:
            return await self._poll()
        except YmAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (YmConnectionError, YmApiError) as err:
            raise UpdateFailed(str(err)) from err

    async def _poll(self) -> YmData:
        now = dt_util.utcnow()
        year = dt_util.now().year
        self._extra = EXTRA_PER_POLL
        if self._snapshot is None and any(key.startswith("calendar:") for key in self.store.latest):
            # After a restart the first diff is against what was stored, so offline changes are logged.
            self._snapshot = self._assemble()
        if not self.store.latest.get(KEY_IMPORTED):
            if self.store.latest.get(KEY_WALKED):
                await self._import_missing()
            else:
                await self.import_history()

        await self._fast_tier(year)
        for key, (when, call, args, _, final) in sorted(self._failed.items(), key=lambda kv: kv[1][0]):
            if not self._wanted(key):
                del self._failed[key]
            elif not final and now - when >= RETRY_AFTER and await self._extra_fetch(key, call, *args):
                if key == "version_validate":
                    self._apply_version(self.store.latest.get(key))
        if self.force_slow or self._last_slow is None or now - self._last_slow >= SLOW_INTERVAL:
            # The timestamp advances even when a fetch fails: failed keys are retried hourly instead.
            self._last_slow = now
            await self._slow_tier(year)
            self.force_slow = False
        if self._last_reference is None or now - self._last_reference >= REFERENCE_INTERVAL:
            self._last_reference = now
            await self._reference_tier()

        return self._build(now)

    async def _fast_tier(self, year: int) -> None:
        await self._fetch("soil_temp", self.client.soil_temp)
        home = _dict(await self._fetch("home_bundle", self.client.home_bundle))
        await self._fetch(f"calendar:{year}", self.client.calendar_year, year)
        for bundle_id in (home.get("bundleId"), home.get("nextBundleId")):
            if bundle_id is not None:
                await self._fetch(f"bundle:{bundle_id}", self.client.bundle_details, bundle_id)
        await self._fetch("user_status", self.client.user_status)
        await self._fetch("yard", self.client.yard)

    async def _slow_tier(self, year: int) -> None:
        await self._fetch_optional("lawn_segments", self.client.lawn_segments)
        dashboard = _dict(await self._fetch_optional("soil_dashboard", self.client.soil_dashboard))
        for item in dashboard.get("completed") or []:
            barcode = _dict(item).get("barcode")
            if barcode:
                await self._fetch_optional(f"soil_report:{barcode}", self.client.soil_report, barcode)
                await self._fetch_optional(f"stilp:{barcode}", self.client.stilp_products, barcode)
        await self._fetch_optional("lawn_program_status", self.client.lawn_program_status)
        await self._fetch_optional(f"calendar:{year + 1}", self.client.calendar_year, year + 1)
        for window in self._windows().values():
            if not window.has_details and f"bundle:{window.id}" not in self.store.latest:
                await self._fetch_optional(f"bundle:{window.id}", self.client.bundle_details, window.id)

    async def _reference_tier(self) -> None:
        await self._fetch_optional("filter_options", self.client.filter_options)
        await self._fetch_optional("journal_activities", self.client.journal_activities)
        await self._fetch_optional("lawn_types", self.client.lawn_types)
        await self._fetch_optional("program_values", self.client.program_values)
        self._apply_version(await self._fetch_optional("version_validate", self.client.version_validate))

    def _apply_version(self, result: Any) -> None:
        if isinstance(result, dict) and isinstance(result.get("isValid"), bool):
            self._update_version_issue(result)

    def _update_version_issue(self, result: dict) -> None:
        if result["isValid"]:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_APP_VERSION)
            return
        version = result.get("version") or self.config_entry.options.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_APP_VERSION,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_APP_VERSION,
            translation_placeholders={"version": str(version)},
        )

    # ---- history ---------------------------------------------------------

    @staticmethod
    def _has_data(raw: Any) -> bool:
        return isinstance(raw, dict) and bool(raw.get("years"))

    async def import_history(self) -> int:
        """Walk the calendar back until two empty years (or 10 years), plus next year, then fetch every
        journal day and every bundle not seen yet (resumable). Returns the number of years with data."""
        this_year = dt_util.now().year
        with_data = empty_run = 0
        for year in range(this_year, this_year - MAX_HISTORY_YEARS, -1):
            raw = await self._fetch(f"calendar:{year}", self.client.calendar_year, year)
            if self._has_data(raw):
                with_data += 1
                empty_run = 0
            else:
                empty_run += 1
                if empty_run >= 2:
                    break
        await self._fetch(f"calendar:{this_year + 1}", self.client.calendar_year, this_year + 1)
        self.store.set_latest(KEY_WALKED, True)
        self._extra = 10**6  # the first import pass is not capped
        await self._import_missing()
        self._extra = EXTRA_PER_POLL
        return with_data

    async def _import_missing(self) -> None:
        """Fetch journal days and bundles not stored yet (a few per poll once resuming).

        The import is marked done when every key is stored or given up on; failures stay in last_error."""
        done = True
        journal_days: set[date] = set()
        for _, found_journal, _ in self._calendars():
            journal_days.update(found_journal)
        for day in sorted(journal_days):
            key = f"journal:{day.isoformat()}"
            if key not in self.store.latest:
                done = await self._extra_fetch(key, self.client.journal_entry, day) and done
        for window_id in self._windows():
            key = f"bundle:{window_id}"
            if key not in self.store.latest:
                done = await self._extra_fetch(key, self.client.bundle_details, window_id) and done
        if done:
            self.store.set_latest(KEY_IMPORTED, True)

    async def request_refresh_full(self) -> None:
        """Refresh everything, slow tier included (used after writes)."""
        self.force_slow = True
        await self.async_request_refresh()

    # ---- assembling data -------------------------------------------------

    def _calendars(self, parsed_years: set[int] | None = None):
        """Parsed stored calendars; the years that parsed are added to `parsed_years`."""
        for key in sorted(self.store.latest):
            if key.startswith("calendar:"):
                try:
                    parsed = parse_calendar(self.store.latest[key])
                except _PARSE_ERRORS:
                    _LOGGER.warning("Skipping unparseable %s", key)
                    continue
                if parsed_years is not None and key[9:].isdigit():
                    parsed_years.add(int(key[9:]))
                yield parsed

    def _windows(self) -> dict[int, Window]:
        """Calendar windows from every stored year (one per id), overlaid with any stored bundle details."""
        latest = self.store.latest
        windows: dict[int, Window] = {}
        for found, _, _ in self._calendars():
            windows.update(found)
        detail_ids = {int(k[7:]) for k in latest if k.startswith("bundle:") and k[7:].isdigit()}
        for window_id in {*windows, *detail_ids}:
            details = _safe_window(latest.get(f"bundle:{window_id}"))
            base = windows.get(window_id)
            window = merge_window(base, details) if base and details else base or details
            if window is not None:
                windows[window_id] = window
        return windows

    def _assemble(self) -> YmSnapshot:
        """Windows, journal, reminders and soil tests as parsed from the stored payloads."""
        latest = self.store.latest
        windows = self._windows()
        journal: dict[date, Any] = {}
        reminders: dict = {}
        listed: set[date] = set()
        stored_years: set[int] = set()
        for _, found_journal, found_reminders in self._calendars(stored_years):
            journal.update({day: e for day, e in found_journal.items() if e.id is not None})
            listed.update(found_journal)
            reminders.update(found_reminders)
        # Journal days fetched during the history import fill days the calendar lists without a usable entry.
        # A day its year's calendar no longer lists was deleted in the app: its stored payload goes too.
        # An entry without an id is the app's unsaved template for an empty day, not a real entry.
        for key, raw in list(latest.items()):
            if key.startswith("journal:") and isinstance(raw, dict):
                day = _day_of(key)
                if day is not None and day not in listed:
                    if day.year in stored_years:
                        self.store.drop(key)
                    continue
                try:
                    entry = parse_journal_entry(raw)
                except _PARSE_ERRORS:
                    _LOGGER.debug("Skipping unparseable %s", key)
                    continue
                if entry.id is not None:
                    journal.setdefault(entry.day, entry)

        soil_tests = {}
        dashboard = _dict(latest.get("soil_dashboard"))
        for group in ("completed", "inProgress", "archived"):
            for item in dashboard.get(group) or []:
                barcode = _dict(item).get("barcode")
                if not barcode:
                    continue
                try:
                    soil_tests[barcode] = parse_soil_test(
                        item, latest.get(f"soil_report:{barcode}"), latest.get(f"stilp:{barcode}")
                    )
                except _PARSE_ERRORS:
                    _LOGGER.debug("Skipping unparseable soil test")

        return YmSnapshot(windows=windows, journal=journal, reminders=reminders, soil_tests=soil_tests)

    def _build(self, now: datetime) -> YmData:
        latest = self.store.latest
        snapshot = self._assemble()
        windows = snapshot.windows
        self.store.record_changes(self._snapshot, snapshot)
        self._snapshot = snapshot

        home = _dict(latest.get("home_bundle"))
        try:
            segments = parse_segments(latest.get("lawn_segments"))
        except _PARSE_ERRORS:
            segments = []
        activities = {
            a["name"]: a["id"]
            for a in _dict(latest.get("filter_options")).get("activities") or []
            if isinstance(a, dict) and "name" in a and "id" in a
        }
        valid = _dict(latest.get("version_validate")).get("isValid")
        return YmData(
            user_status=_dict(latest.get("user_status")),
            yard=_dict(latest.get("yard")),
            soil_temp=_dict(latest.get("soil_temp")),
            home_bundle=home,
            current_window=windows.get(home.get("bundleId")),
            next_window=windows.get(home.get("nextBundleId")),
            snapshot=snapshot,
            segments=segments,
            activities=activities,
            version_valid=valid if isinstance(valid, bool) else None,
            last_sync=now,
            request_count=self.client.request_count,
            last_error=max(self._failed.values(), key=lambda f: f[0])[3] if self._failed else None,
        )
