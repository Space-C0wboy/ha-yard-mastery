"""Tests for the user, reauth and options flows."""
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.yard_mastery.const import DOMAIN

from .common import fixture, make_entry, url


@pytest.fixture
def no_setup():
    with patch("custom_components.yard_mastery.async_setup_entry", return_value=True):
        yield


def _mock_account(mock, *, login_status=200):
    if login_status == 200:
        mock.post(url("/login"), payload=fixture("login"))
        mock.get(url("/v1/user/status"), payload=fixture("user_status"))
    else:
        mock.post(url("/login"), status=login_status, payload={"message": "no"})


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_user_flow_creates_entry(hass, mock_ym, no_setup):
    _mock_account(mock_ym)
    result = await hass.config_entries.flow.async_configure(
        (await _start(hass))["flow_id"], {"email": "lawn@example.com", "password": "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Yard Mastery"
    assert result["data"]["email"] == "lawn@example.com"
    assert result["data"]["password"] == "pw"
    assert result["data"]["token"] == "tok-fixture"
    assert result["data"]["token_created"].startswith("2026-10-19")
    assert result["result"].unique_id == "user-guid-1"


async def test_user_flow_invalid_auth(hass, mock_ym, no_setup):
    _mock_account(mock_ym, login_status=401)
    result = await hass.config_entries.flow.async_configure(
        (await _start(hass))["flow_id"], {"email": "lawn@example.com", "password": "bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass, mock_ym, no_setup):
    mock_ym.post(url("/login"), status=503)
    result = await hass.config_entries.flow.async_configure(
        (await _start(hass))["flow_id"], {"email": "lawn@example.com", "password": "pw"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_duplicate_aborts(hass, mock_ym, no_setup):
    make_entry(hass)
    _mock_account(mock_ym)
    result = await hass.config_entries.flow.async_configure(
        (await _start(hass))["flow_id"], {"email": "lawn@example.com", "password": "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass, mock_ym, no_setup):
    entry = make_entry(hass)
    _mock_account(mock_ym)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new-pw"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-pw"
    assert entry.data["token"] == "tok-fixture"


async def test_reauth_wrong_account_aborts(hass, mock_ym, no_setup):
    entry = make_entry(hass)
    mock_ym.post(url("/login"), payload=fixture("login"))
    mock_ym.get(url("/v1/user/status"), payload={**fixture("user_status"), "user": {"guid": "someone-else"}})
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "pw"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["password"] == "pw"


async def test_reauth_invalid_password(hass, mock_ym, no_setup):
    entry = make_entry(hass)
    _mock_account(mock_ym, login_status=401)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "bad"})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_defaults_and_min_interval(hass, no_setup):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, options={})
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["type"] is FlowResultType.FORM
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(form["flow_id"], {"scan_interval": 14})
    result = await hass.config_entries.options.async_configure(form["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"allow_writes": False, "app_version": "4.00.05", "scan_interval": 30}

    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(form["flow_id"], {"scan_interval": 15})
    assert result["data"]["scan_interval"] == 15


async def test_user_flow_without_user_id_cannot_connect(hass, mock_ym, no_setup):
    mock_ym.post(url("/login"), payload=fixture("login"))
    mock_ym.get(url("/v1/user/status"), payload={"user": {"guid": ""}})
    result = await hass.config_entries.flow.async_configure(
        (await _start(hass))["flow_id"], {"email": "lawn@example.com", "password": "pw"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_reject_empty_app_version(hass, no_setup):
    entry = make_entry(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(form["flow_id"], {"app_version": ""})
