"""Shared test fixtures."""
import re
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import ClientResponse
from aioresponses import aioresponses
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .common import fixture, make_entry, url


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/yard_mastery in every test."""
    yield


@pytest.fixture(autouse=True)
async def chicago(hass, freezer):
    """Pin the clock and local time zone: the fixtures are a 2026-10-19 snapshot."""
    freezer.move_to("2026-10-19 17:00:00+00:00")
    await hass.config.async_set_time_zone("America/Chicago")


class _Response(ClientResponse):
    """aioresponses 0.7.9 predates aiohttp 3.14's required `stream_writer` argument."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)


@pytest.fixture
def mock_ym() -> Generator[aioresponses]:
    # Localhost passes through so HA's test HTTP and websocket clients keep working.
    with patch("aioresponses.core.ClientResponse", _Response), aioresponses(passthrough=["http://127.0.0.1"]) as mock:
        yield mock


def _bundle_details(url_, **kwargs):
    """Any bundle id: the current bundle's details with the requested id."""
    from aioresponses import CallbackResult

    bundle_id = int(str(url_).split("/bundle/")[1].split("/")[0])
    return CallbackResult(payload={**fixture("bundle_current"), "id": bundle_id})


def mock_reads(mock: aioresponses, *, overrides: dict | None = None, calendars: dict | None = None) -> None:
    """Mock every read endpoint from the fixtures (repeatable).

    overrides: {path: payload} replacing a fixture. calendars: {year: payload}; other years are empty.
    """
    gets = {
        "/v1/system/version/validate": fixture("version_validate"),
        "/v1/user/status": fixture("user_status"),
        "/v2/yard": fixture("yard"),
        "/v1/lawn-segment": fixture("lawn_segments"),
        "/v4/home/soil-temp": fixture("soil_temp"),
        "/v4/home/bundle": fixture("home_bundle"),
        "/v1/calendar/filter-options": fixture("filter_options"),
        "/v1/soil-test/dashboard": fixture("soil_dashboard"),
        "/v1/soil-test/report": fixture("soil_report"),
        "/v1/soil-test/stilp/products/YM00001": fixture("stilp_products"),
        "/v1/lawn-program/status": fixture("lawn_program_status"),
        "/v1/lawn-types": fixture("lawn_types"),
        "/v1/settings/lawn-program/values": fixture("program_values"),
        "/v3/journal/entry": fixture("journal_2026_10_17"),
        "/v1/journal/entry/activity": [{"id": 1, "name": "Mow"}],
        "/v4/home/soil-test": [],
        **(overrides or {}),
    }
    for path, payload in gets.items():
        mock.get(url(path), payload=payload, repeat=True)
    home = fixture("home_bundle")
    mock.get(url(f"/v1/dlux/bundle/{home['bundleId']}/details"), payload=fixture("bundle_current"), repeat=True)
    mock.get(url(f"/v1/dlux/bundle/{home['nextBundleId']}/details"), payload=fixture("bundle_next"), repeat=True)
    mock.get(re.compile(r".*/v1/dlux/bundle/\d+/details.*"), callback=_bundle_details, repeat=True)
    # Calendar: the current year has data, every other year is empty.
    for year, payload in {2026: fixture("calendar_2026"), **(calendars or {})}.items():
        mock.get(re.compile(rf".*/v1/calendar/search\?.*startDate={year}-01-01.*"), payload=payload, repeat=True)
    mock.get(url("/v1/calendar/search"), payload=fixture("calendar_empty"), repeat=True)


@pytest.fixture
def load_entry(hass, mock_ym, hass_storage):
    """Factory: create an entry and set it up against mocked reads."""

    async def _load(*, overrides=None, calendars=None, seed=None, setup=True, **options) -> MockConfigEntry:
        entry = make_entry(hass, **options)
        mock_reads(mock_ym, overrides=overrides, calendars=calendars)
        mock_ym.post(url("/login"), payload=fixture("login"), repeat=True)
        if seed:
            hass_storage[f"yard_mastery.{entry.entry_id}"] = {
                "version": 1,
                "minor_version": 1,
                "key": f"yard_mastery.{entry.entry_id}",
                # A seeded store counts as already imported unless the test says otherwise.
                "data": {"latest": {"history_walked": True, "history_imported": True, **seed}, "history": []},
            }
        if setup:
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return entry

    return _load
