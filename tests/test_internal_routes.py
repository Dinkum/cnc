from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.models.entities import Backend
from app.routes import internal


def _request(
    *,
    expected_token: str = "runtime-secret",
    presented_token: str = "runtime-secret",
    forwarded: bool = False,
) -> Request:
    headers = [
        (b"host", b"127.0.0.1:9090"),
        (b"x-cnc-internal-token", presented_token.encode()),
        (b"x-cnc-ssh-server", b"100.64.0.2"),
    ]
    if forwarded:
        headers.append((b"x-forwarded-for", b"100.64.0.12"))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/internal/backend-ssh/llm-help/worker",
            "raw_path": b"/internal/backend-ssh/llm-help/worker",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 9090),
            "app": SimpleNamespace(
                state=SimpleNamespace(backend_ssh_internal_token=expected_token)
            ),
        }
    )


def _backend() -> Backend:
    backend = Backend(
        name="worker",
        kind="app",
        enabled=True,
        handoff_port=12004,
        sandbox_profile="ubuntu-24.04-systemd",
        volumes_json="[]",
    )
    backend.__dict__["inputs"] = []
    return backend


@pytest.mark.asyncio
async def test_internal_llm_help_uses_cached_cnc_state_without_guest_probe(
    monkeypatch,
) -> None:
    async def fake_load_backend(_session, backend_name: str) -> Backend:
        assert backend_name == "worker"
        return _backend()

    monkeypatch.setattr(internal, "_load_backend", fake_load_backend)
    monkeypatch.setattr(
        internal,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-worker",
                    "data": {
                        "ActiveState": "active",
                        "SubState": "running",
                        "RuntimeDiagnosis": "healthy",
                        "RuntimeIssues": "-",
                    },
                }
            ]
        },
    )

    response = await internal.backend_ssh_llm_help(
        _request(),
        "worker",
        False,
        object(),
        Settings(admin_port=9090),
    )

    body = response.body.decode()
    assert response.media_type == "text/plain"
    assert "# CNC Backend Context: worker" in body
    assert "status: active" in body
    assert "ssh root@100.64.0.2" in body


@pytest.mark.asyncio
async def test_internal_llm_help_rejects_proxied_or_bad_token(monkeypatch) -> None:
    async def fail_load_backend(_session, _backend_name: str):
        raise AssertionError("unauthorized request must not query the database")

    monkeypatch.setattr(internal, "_load_backend", fail_load_backend)

    for request in (
        _request(presented_token="wrong"),
        _request(forwarded=True),
    ):
        with pytest.raises(HTTPException) as raised:
            await internal.backend_ssh_llm_help(
                request,
                "worker",
                False,
                object(),
                Settings(admin_port=9090),
            )
        assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_internal_llm_help_has_server_side_deadline(monkeypatch) -> None:
    async def slow_load_backend(_session, _backend_name: str):
        await asyncio.sleep(0.05)
        return _backend()

    monkeypatch.setattr(internal, "_HELP_TIMEOUT_SEC", 0.001)
    monkeypatch.setattr(internal, "_load_backend", slow_load_backend)

    with pytest.raises(HTTPException) as raised:
        await internal.backend_ssh_llm_help(
            _request(),
            "worker",
            False,
            object(),
            Settings(admin_port=9090),
        )

    assert raised.value.status_code == 503
