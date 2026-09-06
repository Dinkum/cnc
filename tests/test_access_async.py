import asyncio
import threading
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import access
from app.config import Settings
from app.services import agent_channel
from app.ui.routes import pages


@pytest.fixture(autouse=True)
def isolated_verifier(monkeypatch):
    monkeypatch.setattr(access, "_ACCESS_VERIFY_SEMAPHORE", asyncio.BoundedSemaphore(1))
    monkeypatch.setattr(access, "_ATTEMPTS", {})


def login_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/access",
            "scheme": "http",
            "headers": [
                (b"host", b"127.0.0.1:9090"),
                (b"origin", b"http://127.0.0.1:9090"),
            ],
            "client": ("192.0.2.10", 1234),
            "server": ("127.0.0.1", 9090),
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_web_and_agent_login_share_worker_bound_without_blocking_loop(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    threads = []

    def verify(_settings, _token):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(access, "verify_access_key", verify)
    settings = Settings(_env_file=None, access_key_hash="configured")
    web = asyncio.create_task(
        pages.access_submit(
            login_request(),
            access_key="valid test access key",
            access_key_confirm="",
            next_path="/",
            settings=settings,
        )
    )
    agent = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert threads[0] != threading.get_ident()
        agent = asyncio.create_task(
            agent_channel.authenticate_agent_websocket(
                SimpleNamespace(
                    headers={"authorization": "Bearer valid test access key"},
                    client=SimpleNamespace(host="192.0.2.10"),
                ),
                settings,
            )
        )
        await asyncio.sleep(0)
        assert len(threads) == 1
        assert not agent.done()
        release.set()
        response, _ = await asyncio.gather(web, agent)
        assert response.status_code == 303
        assert "cnc_access=" in response.headers["set-cookie"]
        assert len(threads) == 2
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (web, agent) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_repeated_cancellation_holds_slot_until_worker_finishes(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def verify(_settings, token):
        calls.append(token)
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(access, "verify_access_key", verify)
    settings = Settings(_env_file=None)
    first = asyncio.create_task(
        access.verify_access_key_async(settings, "first", attempt_key="first")
    )
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(
            access.verify_access_key_async(settings, "second", attempt_key="second")
        )
        await asyncio.sleep(0)
        assert calls == ["first"]
        assert not first.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == (True, 0)
        assert calls == ["first", "second"]
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_web_login_records_failure_once_and_preserves_backoff(monkeypatch):
    calls = []
    monkeypatch.setattr(
        access, "verify_access_key", lambda *_: calls.append(True) or False
    )
    settings = Settings(_env_file=None, access_key_hash="configured")

    async def submit():
        return await pages.access_submit(
            login_request(),
            access_key="incorrect test key",
            access_key_confirm="",
            next_path="/",
            settings=settings,
        )

    assert (await submit()).status_code == 401
    assert (await submit()).status_code == 429
    assert len(calls) == 1
    assert access._ATTEMPTS["192.0.2.10"].failures == 1
    assert access.access_attempt_backoff_seconds("agent:192.0.2.10") == 0
