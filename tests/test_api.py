"""Tests for the Yard Mastery API client."""
import asyncio
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aioresponses import CallbackResult

from custom_components.yard_mastery.api import YardMasteryClient, YmApiError, YmAuthError, YmConnectionError
from custom_components.yard_mastery.const import MAX_RELOGINS, RELOGIN_COOLDOWN, TOKEN_MAX_AGE

from .common import fixture, url

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, delta: timedelta) -> None:
        self.t += delta


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def make_client(session, clock=None, **kwargs) -> YardMasteryClient:
    kwargs.setdefault("token", "tok")
    kwargs.setdefault("token_created", T0)
    return YardMasteryClient(session, "lawn@example.com", "pw", now=clock or Clock(), **kwargs)


def calls(mock, method: str, path: str):
    """All recorded requests for method + path (any query)."""
    out = []
    for (m, u), reqs in mock.requests.items():
        if m == method and u.path == path:
            out.extend(reqs)
    return out


async def test_headers_raw_token_and_app_version(mock_ym, session):
    mock_ym.get(url("/v1/user/status"), payload={"ok": True})
    await make_client(session).user_status()
    (req,) = calls(mock_ym, "GET", "/v1/user/status")
    assert req.kwargs["headers"]["Authorization"] == "tok"
    assert req.kwargs["headers"]["appVersion"] == "4.00.05"
    assert req.kwargs["headers"]["Origin"] == "https://localhost"
    assert req.kwargs["params"]["appVersion"] == "4.00.05"


async def test_login_posts_credentials_and_reports_token(mock_ym, session):
    mock_ym.post(url("/login"), payload=fixture("login"))
    client = make_client(session, token=None, token_created=None)
    seen = []
    client.on_token = lambda token, created: seen.append((token, created))
    body = await client.login()
    assert body["accessToken"] == "tok-fixture"
    (req,) = calls(mock_ym, "POST", "/login")
    assert req.kwargs["json"] == {"email": "lawn@example.com", "password": "pw"}
    assert seen == [("tok-fixture", T0)]
    assert client.token == "tok-fixture"


async def test_expired_token_logs_in_first(mock_ym, session):
    mock_ym.post(url("/login"), payload=fixture("login"))
    mock_ym.get(url("/v2/yard"), payload={"ok": True})
    client = make_client(session, token_created=T0 - TOKEN_MAX_AGE - timedelta(days=0, seconds=1))
    await client.yard()
    assert len(calls(mock_ym, "POST", "/login")) == 1
    (req,) = calls(mock_ym, "GET", "/v2/yard")
    assert req.kwargs["headers"]["Authorization"] == "tok-fixture"
    assert [m for (m, _u) in mock_ym.requests] == ["POST", "GET"]


async def test_401_relogs_once_and_retries(mock_ym, session):
    mock_ym.get(url("/v2/yard"), status=401)
    mock_ym.post(url("/login"), payload=fixture("login"))
    mock_ym.get(url("/v2/yard"), payload={"ok": True})
    client = make_client(session)
    assert await client.yard() == {"ok": True}
    assert len(calls(mock_ym, "POST", "/login")) == 1
    assert client.request_count == 3


async def test_relogin_cooldown_and_cap(mock_ym, session):
    clock = Clock()
    client = make_client(session, clock)
    # Relogin works once, then success: a second 401 inside the cooldown must not log in again.
    mock_ym.get(url("/v2/yard"), status=401)
    mock_ym.post(url("/login"), payload=fixture("login"))
    mock_ym.get(url("/v2/yard"), payload={"ok": True})
    mock_ym.get(url("/v2/yard"), status=403)
    assert await client.yard() == {"ok": True}
    clock.advance(timedelta(minutes=1))
    with pytest.raises(YmAuthError):
        await client.yard()
    assert len(calls(mock_ym, "POST", "/login")) == 1

    # After the cooldown, three cycles of 401-even-after-login hit the cap.
    mock_ym.clear()
    logins_before = len(calls(mock_ym, "POST", "/login"))
    mock_ym.get(url("/v2/yard"), status=401, repeat=True)
    mock_ym.post(url("/login"), payload=fixture("login"), repeat=True)
    for _ in range(MAX_RELOGINS):
        clock.advance(RELOGIN_COOLDOWN + timedelta(minutes=1))
        with pytest.raises(YmAuthError):
            await client.yard()
    assert len(calls(mock_ym, "POST", "/login")) - logins_before == MAX_RELOGINS
    # Capped: no further login even after the cooldown.
    clock.advance(RELOGIN_COOLDOWN + timedelta(minutes=1))
    with pytest.raises(YmAuthError):
        await client.yard()
    assert len(calls(mock_ym, "POST", "/login")) - logins_before == MAX_RELOGINS


async def test_bad_credentials_raise_auth_error(mock_ym, session):
    mock_ym.post(url("/login"), status=401)
    with pytest.raises(YmAuthError):
        await make_client(session, token=None, token_created=None).login()


async def test_server_error_is_connection_error(mock_ym, session):
    mock_ym.get(url("/v2/yard"), status=503)
    mock_ym.get(url("/v2/yard"), exception=aiohttp.ClientError("boom"))
    client = make_client(session)
    with pytest.raises(YmConnectionError):
        await client.yard()
    with pytest.raises(YmConnectionError):
        await client.yard()


async def test_calendar_year_query(mock_ym, session):
    mock_ym.get(url("/v1/calendar/search"), payload=fixture("calendar_empty"))
    await make_client(session).calendar_year(2026)
    (req,) = calls(mock_ym, "GET", "/v1/calendar/search")
    assert req.kwargs["params"] == {
        "viewType": "YEAR",
        "includeBundles": "true",
        "includeJournalEntries": "true",
        "includeReminders": "true",
        "startDate": "2026-01-01",
        "endDate": "2026-12-31",
        "appVersion": "4.00.05",
    }


async def test_set_item_status_body(mock_ym, session):
    mock_ym.put(url("/v1/dlux/user-bundle-item/change-status"), payload={})
    await make_client(session).set_item_status([1, 2], "COMPLETED")
    (req,) = calls(mock_ym, "PUT", "/v1/dlux/user-bundle-item/change-status")
    assert req.kwargs["json"] == {"bundleItemIds": [1, 2], "status": "COMPLETED"}


async def test_concurrent_401s_share_one_relogin(mock_ym, session):
    def yard(_url, **kwargs):
        if kwargs["headers"].get("Authorization") == "tok-fixture":
            return CallbackResult(status=200, payload={"ok": True})
        return CallbackResult(status=401)

    mock_ym.get(url("/v2/yard"), callback=yard, repeat=True)
    mock_ym.post(url("/login"), payload=fixture("login"))
    client = make_client(session)
    real_send = client._send

    async def yielding_send(*args, **kwargs):
        # Mocked I/O never suspends; yield so both requests are in flight on the stale token.
        await asyncio.sleep(0)
        return await real_send(*args, **kwargs)

    client._send = yielding_send
    assert await asyncio.gather(client.yard(), client.yard()) == [{"ok": True}, {"ok": True}]
    assert len(calls(mock_ym, "POST", "/login")) == 1


async def test_undecodable_body_is_api_error(mock_ym, session):
    mock_ym.get(url("/v2/yard"), body=b"\xff\xfe\xfa", content_type="application/json")
    with pytest.raises(YmApiError):
        await make_client(session).yard()


async def test_errors_do_not_carry_the_barcode(mock_ym, session):
    client = make_client(session)
    mock_ym.get(url("/v1/soil-test/stilp/products/YM00001"), status=404)
    mock_ym.get(url("/v1/soil-test/stilp/products/YM00001"), status=503)
    mock_ym.get(url("/v1/soil-test/report"), exception=aiohttp.ClientConnectionError("GET /x?barcode=YM00001 failed"))
    for call in (client.stilp_products, client.stilp_products, client.soil_report):
        with pytest.raises((YmApiError, YmConnectionError)) as err:
            await call("YM00001")
        assert "YM00001" not in str(err.value)
        assert "soil-test" in str(err.value)
        if call == client.stilp_products:
            assert "/stilp/products/***" in str(err.value)
    assert str(err.value).endswith("barcode=*** failed")
