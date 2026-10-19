"""Yard Mastery REST client: auth, headers and the relogin policy."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    BASE_URL,
    DEFAULT_APP_VERSION,
    MAX_RELOGINS,
    RELOGIN_COOLDOWN,
    TOKEN_MAX_AGE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_AUTH_STATUSES = (401, 403)
# Soil-test barcodes sit in some paths and queries; error messages and logs carry the endpoint without them.
_BARCODE_PARTS = re.compile(r"(/stilp/products/|barcode=)[^/?&\s]+")


def mask(text: str) -> str:
    """`text` with any soil-test barcode replaced by ***."""
    return _BARCODE_PARTS.sub(r"\1***", text)


class YmError(Exception):
    """Base error."""


class YmAuthError(YmError):
    """Bad credentials, or the relogin cap was hit."""


class YmConnectionError(YmError):
    """Network failure, timeout or 5xx."""


class YmApiError(YmError):
    """Any other 4xx."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"Yard Mastery returned HTTP {status} {message}".strip())
        self.status = status


class YardMasteryClient:
    """Async client for the private Yard Mastery API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        token: str | None = None,
        token_created: datetime | None = None,
        app_version: str = DEFAULT_APP_VERSION,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._app_version = app_version
        self._now = now
        self.token = token
        self.token_created = token_created
        self.request_count = 0
        self.on_token: Callable[[str, datetime], None] | None = None
        self._last_login: datetime | None = None
        self._failed_relogins = 0
        self._relogin_lock = asyncio.Lock()

    # ---- transport -------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        data: Any = None,
        authed: bool = True,
    ) -> tuple[int, Any]:
        """One HTTP call. Returns (status, parsed body); 5xx and network errors raise."""
        headers = {
            "appVersion": self._app_version,
            "Accept": "application/json",
            "Origin": "https://localhost",
            "User-Agent": USER_AGENT,
        }
        if authed and self.token:
            headers["Authorization"] = self.token
        query = {**(params or {}), "appVersion": self._app_version}
        self.request_count += 1
        try:
            async with self._session.request(
                method,
                BASE_URL + path,
                params=query,
                headers=headers,
                json=json_body,
                # A callable builds a fresh single-use body (multipart) per attempt.
                data=data() if callable(data) else data,
                timeout=_TIMEOUT,
            ) as resp:
                status = resp.status
                try:
                    text = await resp.text()
                except UnicodeDecodeError as err:
                    raise YmApiError(status, mask(f"undecodable body on {method} {path}")) from err
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise YmConnectionError(mask(f"{method} {path}: {err}")) from err
        if status >= 500:
            raise YmConnectionError(mask(f"{method} {path}: HTTP {status}"))
        body: Any = None
        if text:
            try:
                body = json.loads(text)
            except ValueError:
                body = text
        return status, body

    async def login(self) -> dict:
        """Log in and remember the token. 400/401/403 mean bad credentials."""
        status, body = await self._send(
            "POST",
            "/login",
            json_body={"email": self._email, "password": self._password},
            authed=False,
        )
        if status in (400, 401, 403):
            raise YmAuthError("Yard Mastery rejected the email or password")
        if status >= 400 or not isinstance(body, dict) or not body.get("accessToken"):
            raise YmApiError(status, "on login")
        now = self._now()
        self.token = body["accessToken"]
        self.token_created = now
        self._last_login = now
        if self.on_token:
            self.on_token(self.token, now)
        return body

    def _token_expired(self) -> bool:
        return not self.token or self.token_created is None or self._now() - self.token_created >= TOKEN_MAX_AGE

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json_body: Any = None, data: Any = None
    ) -> Any:
        logged_in_here = False
        if self._token_expired():
            await self.login()
            logged_in_here = True
        used_token = self.token
        status, body = await self._send(method, path, params=params, json_body=json_body, data=data)
        if status in _AUTH_STATUSES:
            if not logged_in_here:
                async with self._relogin_lock:
                    # Another request may have re-logged in while this one waited: just retry with its token.
                    if self.token == used_token:
                        if self._failed_relogins >= MAX_RELOGINS:
                            raise YmAuthError("Yard Mastery keeps rejecting the login; reauthentication needed")
                        if self._last_login is not None and self._now() - self._last_login < RELOGIN_COOLDOWN:
                            raise YmAuthError("Yard Mastery rejected the token shortly after a login")
                        _LOGGER.debug("Token rejected on %s; logging in again", mask(f"{method} {path}"))
                        await self.login()
                    status, body = await self._send(method, path, params=params, json_body=json_body, data=data)
            if status in _AUTH_STATUSES:
                self._failed_relogins += 1
                raise YmAuthError("Yard Mastery rejected a fresh token")
        if status >= 400:
            raise YmApiError(status, mask(f"on {method} {path}"))
        self._failed_relogins = 0
        return body

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: Any = None, data: Any = None) -> Any:
        return await self._request("POST", path, json_body=json, data=data)

    async def put(self, path: str, json: Any = None) -> Any:
        return await self._request("PUT", path, json_body=json)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ---- reads -----------------------------------------------------------

    async def user_status(self) -> Any:
        return await self.get("/v1/user/status")

    async def version_validate(self) -> Any:
        return await self.get("/v1/system/version/validate")

    async def yard(self) -> Any:
        return await self.get("/v2/yard")

    async def lawn_segments(self) -> Any:
        return await self.get("/v1/lawn-segment")

    async def soil_temp(self) -> Any:
        return await self.get("/v4/home/soil-temp")

    async def home_bundle(self) -> Any:
        return await self.get("/v4/home/bundle")

    async def bundle_details(self, bundle_id: int) -> Any:
        return await self.get(f"/v1/dlux/bundle/{bundle_id}/details")

    async def calendar_year(self, year: int) -> Any:
        return await self.get(
            "/v1/calendar/search",
            {
                "viewType": "YEAR",
                "includeBundles": "true",
                "includeJournalEntries": "true",
                "includeReminders": "true",
                "startDate": f"{year}-01-01",
                "endDate": f"{year}-12-31",
            },
        )

    async def filter_options(self) -> Any:
        return await self.get("/v1/calendar/filter-options")

    async def journal_entry(self, day: date) -> Any:
        return await self.get("/v3/journal/entry", {"date": day.isoformat()})

    async def journal_activities(self) -> Any:
        return await self.get("/v1/journal/entry/activity")

    async def soil_tests(self) -> Any:
        return await self.get("/v4/home/soil-test")

    async def soil_dashboard(self) -> Any:
        return await self.get("/v1/soil-test/dashboard")

    async def soil_report(self, barcode: str) -> Any:
        return await self.get("/v1/soil-test/report", {"barcode": barcode})

    async def stilp_products(self, barcode: str) -> Any:
        return await self.get(f"/v1/soil-test/stilp/products/{barcode}")

    async def lawn_program_status(self) -> Any:
        return await self.get("/v1/lawn-program/status")

    async def lawn_types(self) -> Any:
        return await self.get("/v1/lawn-types")

    async def program_values(self) -> Any:
        return await self.get("/v1/settings/lawn-program/values")

    async def product_search(self, term: str, product_type: str = "") -> Any:
        return await self.get("/v2/product", {"type": product_type, "term": term})

    # ---- writes ----------------------------------------------------------

    async def set_item_status(self, item_ids: list[int], status: str) -> Any:
        return await self.put("/v1/dlux/user-bundle-item/change-status", {"bundleItemIds": item_ids, "status": status})

    async def save_journal_entry(self, entry: dict) -> dict:
        return await self.post("/v2/journal/entry", json=entry)

    async def delete_journal_entry(self, entry_id: int) -> Any:
        return await self.delete(f"/v1/journal/entry/{entry_id}")

    async def add_journal_photo(self, day: date, image: bytes, filename: str) -> dict:
        def form() -> aiohttp.FormData:
            fd = aiohttp.FormData()
            fd.add_field("file", image, filename=filename, content_type="image/jpeg")
            return fd

        return await self._request("POST", "/v1/journal/entry/image", params={"date": day.isoformat()}, data=form)

    async def create_reminder(self, reminder: dict) -> dict:
        return await self.post("/v1/reminder", json=reminder)

    async def update_reminder(self, reminder: dict) -> dict:
        return await self.put("/v1/reminder", json=reminder)

    async def dismiss_reminder(self, reminder_id: int) -> Any:
        return await self.put(f"/v1/reminder/{reminder_id}/dismiss")
