import sys

import pytest
import pytest_asyncio
import sqlalchemy.ext.asyncio as sqlalchemy_async
from sqlalchemy.ext.asyncio import AsyncEngine

from app import database
from app.services.status_service import cancel_status_cache_prefill


_ORIGINAL_CREATE_ASYNC_ENGINE = sqlalchemy_async.create_async_engine
_TRACKED_ASYNC_ENGINES: set[AsyncEngine] = set()


def _tracking_create_async_engine(*args, **kwargs) -> AsyncEngine:
    active_engine = _ORIGINAL_CREATE_ASYNC_ENGINE(*args, **kwargs)
    _TRACKED_ASYNC_ENGINES.add(active_engine)
    return active_engine


sqlalchemy_async.create_async_engine = _tracking_create_async_engine
database.create_async_engine = _tracking_create_async_engine


@pytest.fixture(autouse=True)
def _default_test_host_paths(monkeypatch, tmp_path):
    lock_path = tmp_path / "host-mutation.lock"
    monkeypatch.setenv("APPLY_LOCK_PATH", str(lock_path))
    main_module = sys.modules.get("app.main")
    if main_module is not None:
        monkeypatch.setattr(
            main_module,
            "boot_settings",
            main_module.boot_settings.model_copy(update={"apply_lock_path": lock_path}),
        )

    def is_free(_port: int) -> bool:
        return True

    monkeypatch.setattr("app.services.port_preflight.is_loopback_port_free", is_free)
    monkeypatch.setattr("app.services.backend_commands.is_loopback_port_free", is_free)
    monkeypatch.setattr("app.services.port_allocator.is_loopback_port_free", is_free)
    monkeypatch.setattr("app.ui.routes.shared.is_loopback_port_free", is_free)


@pytest_asyncio.fixture(autouse=True)
async def _cancel_status_cache_prefill_after_test():
    yield
    await cancel_status_cache_prefill()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engines_after_test():
    yield
    await database.dispose_engine()
    engines = list(_TRACKED_ASYNC_ENGINES)
    _TRACKED_ASYNC_ENGINES.clear()
    for active_engine in engines:
        await active_engine.dispose()
