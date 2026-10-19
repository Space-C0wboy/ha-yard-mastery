"""Guard against personal data leaking into the fixtures (structural checks against the fakes)."""
import json

import pytest

from .common import FIXTURES

EXPECTED = {
    "postalCode": "00000",
    "city": "Testville",
    "street1": "1 Test St",
    "email": "lawn@example.com",
    "guid": "user-guid-1",
    "userGuid": "user-guid-1",
    "barcode": "YM00001",
    "latLngPoints": "[]",
    "stateAbbreviation": "XX",
    "lab_number": "LAB0001",
    "grower_id": 0,
    "submitter_id": 0,
    "farm_id": 0,
    "field_id": 0,
}


def _walk(obj):
    """Yield every (key, value) pair in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixtures_have_no_obvious_pii(path):
    for key, value in _walk(json.loads(path.read_text(encoding="utf-8"))):
        assert key not in ("lat", "lng"), f"{key} in {path.name}"
        if key in EXPECTED:
            assert value == EXPECTED[key], f"{key} in {path.name}"
        if key == "hostedUrl":
            assert value.startswith("https://example.invalid/"), f"hostedUrl in {path.name}"
