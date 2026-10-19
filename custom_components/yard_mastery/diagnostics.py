"""Diagnostics: the store and coordinator state with personal data redacted."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import YmConfigEntry

REDACTED = "**REDACTED**"
TO_REDACT = {
    "email",
    "password",
    "token",
    "accessToken",
    "firstName",
    "lastName",
    "street1",
    "street2",
    "city",
    "postalCode",
    "zipCode",
    "lat",
    "lng",
    "latLngPoints",
    "guid",
    "userGuid",
    "barcode",
    "trackingNumber",
    "trackingUrl",
    "hostedUrl",
    "reference",
    "submitter",
    "grower",
    "farm",
    "field",
    "stateAbbreviation",
    "lab_number",
    "sample_set_id",
    "sample_set_system_id",
}


def _barcodes(data: Any) -> set[str]:
    """Every barcode value anywhere in the payloads (they also appear in keys and URLs)."""
    found: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "barcode" and isinstance(value, str) and value:
                found.add(value)
            found |= _barcodes(value)
    elif isinstance(data, list):
        for value in data:
            found |= _barcodes(value)
    return found


def _scrub(data: Any, secrets: list[str]) -> Any:
    """Replace secret substrings in string values and dict keys, recursively."""
    if isinstance(data, str):
        for secret in secrets:
            data = data.replace(secret, REDACTED)
        return data
    if isinstance(data, dict):
        return {_scrub(k, secrets): _scrub(v, secrets) for k, v in data.items()}
    if isinstance(data, list):
        return [_scrub(v, secrets) for v in data]
    return data


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: YmConfigEntry) -> dict[str, Any]:
    # runtime_data is missing while setup is retrying or failed, which is when diagnostics matter most.
    coordinator = getattr(entry, "runtime_data", None)
    store = coordinator.store if coordinator else None
    state = coordinator.data if coordinator else None
    latest = store.latest if store else {}
    history = store.history if store else []

    secrets = _barcodes(latest) | _barcodes(history)
    if entry.unique_id:
        secrets.add(entry.unique_id)
    if isinstance(entry.data.get("email"), str):
        secrets.add(entry.data["email"])
    # Longest first, so a secret that contains another is replaced whole.
    ordered = sorted((s for s in secrets if s), key=len, reverse=True)

    diag = {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "coordinator": {
            "last_sync": state.last_sync.isoformat() if state else None,
            "request_count": state.request_count if state else None,
            "last_error": state.last_error if state else None,
            "version_valid": state.version_valid if state else None,
        },
        "latest": latest,
        "history": history,
    }
    return _scrub(async_redact_data(diag, TO_REDACT), ordered)
