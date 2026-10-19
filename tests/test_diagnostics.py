"""Tests for the redacted diagnostics."""
import json

from custom_components.yard_mastery.diagnostics import async_get_config_entry_diagnostics

SECRETS = [
    "lawn@example.com",
    '"pw"',
    ": \"tok\"",
    "1 Test St",
    "00000",
    "user-guid-1",
    "YM00001",
    "TRACK0001",
    "example.invalid",
    "LAB0001",
    '"XX"',
]


async def test_diagnostics_redacts_personal_data(hass, load_entry):
    entry = await load_entry()
    coordinator = entry.runtime_data
    # Barcodes and the guid also show up in dict keys, change-log keys and error strings.
    coordinator.store.latest["stilp:YM00001"] = {"path": "s3://bucket/user-guid-1/file.pdf"}
    coordinator.store.history.append(
        {"kind": "soil_test_added", "key": "soil_test:YM00001", "old": None, "new": "SUBMITTED"}
    )
    coordinator.store.history.append({"kind": "x", "key": "soil_report:YM00001", "old": None, "new": None})
    coordinator.data.last_error = "GET /v1/soil-test/report?barcode=YM00001 failed (user-guid-1)"

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)

    for secret in SECRETS:
        assert secret not in text, secret
    assert "latest" in diag and "history" in diag
    assert diag["latest"]
    assert diag["coordinator"]["request_count"] > 0
    assert "**REDACTED**" in diag["coordinator"]["last_error"]
    assert "soil_test:**REDACTED**" in {h["key"] for h in diag["history"]}
    assert "stilp:**REDACTED**" in diag["latest"]
    assert diag["entry"]["options"] == {"allow_writes": False}
