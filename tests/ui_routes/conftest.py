import os

import pytest

from app.config import get_settings


_RUNTIME_SETTING_KEYS = (
    "ACCESS_KEY_HASH",
    "BETA_ROUTING",
    "BETA_HARDENING",
    "MULTI_NODE_ENABLED",
    "NETDATA_ENABLED",
    "PUSHOVER_APP_TOKEN",
    "PUSHOVER_USER_KEY",
    "SHIELD_ENABLED",
)
_MISSING = object()


@pytest.fixture(autouse=True)
def _restore_runtime_setting_environment():
    before = {key: os.environ.get(key, _MISSING) for key in _RUNTIME_SETTING_KEYS}
    for key in _RUNTIME_SETTING_KEYS:
        os.environ.pop(key, None)
    get_settings.cache_clear()
    yield
    for key, value in before.items():
        if value is _MISSING:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
