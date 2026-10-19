"""Shared test helpers."""
import json
import pathlib
import re
from datetime import UTC, datetime

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yard_mastery.const import BASE_URL, CONF_ALLOW_WRITES, DOMAIN

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    """Load tests/fixtures/<name>.json."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def url(path: str) -> re.Pattern:
    """Match BASE_URL + path with any query string."""
    return re.compile(re.escape(BASE_URL + path) + r"(\?.*)?$")


def make_entry(hass, *, allow_writes: bool = False, token: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-guid-1",
        data={
            "email": "lawn@example.com",
            "password": "pw",
            **({"token": "tok", "token_created": datetime.now(UTC).isoformat()} if token else {}),
        },
        options={CONF_ALLOW_WRITES: allow_writes},
    )
    entry.add_to_hass(hass)
    return entry


def requested(mock) -> list[str]:
    """Every request made so far, as 'METHOD /path?query' in call order."""
    calls = []
    for (method, request_url), reqs in mock.requests.items():
        calls.extend(f"{method} {request_url.path_qs.split('&appVersion')[0]}" for _ in reqs)
    return calls
