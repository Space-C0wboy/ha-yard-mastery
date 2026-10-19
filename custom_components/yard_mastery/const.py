"""Constants for Yard Mastery."""
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "yard_mastery"

BASE_URL = "https://prod-api.yardmastery.com"
DEFAULT_APP_VERSION = "4.00.05"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Pixel) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/140.0 Mobile Safari/537.36"
)

CONF_TOKEN = "token"
CONF_TOKEN_CREATED = "token_created"
CONF_ALLOW_WRITES = "allow_writes"
CONF_APP_VERSION = "app_version"
CONF_SCAN_INTERVAL = "scan_interval"  # minutes

DEFAULT_SCAN_MINUTES = 30
MIN_SCAN_MINUTES = 15
SLOW_INTERVAL = timedelta(hours=6)
REFERENCE_INTERVAL = timedelta(hours=24)

TOKEN_MAX_AGE = timedelta(days=27)
RELOGIN_COOLDOWN = timedelta(minutes=15)
MAX_RELOGINS = 3

# Task 8 appends the calendar platform.
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CALENDAR]

WRITES_OFF_MESSAGE = "Changes to Yard Mastery are turned off in the integration's options"
