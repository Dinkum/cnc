from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.routes import join


class FakeRequest:
    async def json(self) -> dict[str, object]:
        return {"node_uid": "node-a"}


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _locked_error() -> OperationalError:
    return OperationalError(
        "UPDATE cluster_nodes SET latency_ms=?",
        {},
        sqlite3.OperationalError("database is locked"),
    )


@pytest.mark.asyncio
async def test_node_join_confirm_retries_sqlite_busy(monkeypatch) -> None:
    attempts = 0
    session = FakeSession()

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_confirm_node(_session, *, token: str, payload: dict[str, object]):
        nonlocal attempts
        attempts += 1
        assert token == "join-token"
        assert payload == {"node_uid": "node-a"}
        if attempts == 1:
            raise _locked_error()
        return SimpleNamespace(
            node_uid="node-a",
            name="node-a.example",
            role="follower",
            state="healthy",
            tailnet_ip="100.64.0.19",
            latency_ms=3.4,
        )

    monkeypatch.setattr(join, "confirm_node", fake_confirm_node)
    monkeypatch.setattr(join.asyncio, "sleep", fake_sleep)

    response = await join.node_join_confirm(
        FakeRequest(),
        x_cnc_join_token="join-token",
        settings=Settings(multi_node_enabled=True),
        session=session,
    )

    assert response.status_code == 200
    assert attempts == 2
    assert session.rollbacks == 1
    assert json.loads(response.body)["node"]["id"] == "node-a"


@pytest.mark.asyncio
async def test_node_join_confirm_returns_503_after_sqlite_busy_retries(
    monkeypatch,
) -> None:
    attempts = 0
    session = FakeSession()

    async def fake_sleep(_delay: float) -> None:
        return None

    async def locked_confirm_node(_session, *, token: str, payload: dict[str, object]):
        nonlocal attempts
        attempts += 1
        raise _locked_error()

    monkeypatch.setattr(join, "confirm_node", locked_confirm_node)
    monkeypatch.setattr(join.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(join, "JOIN_CONFIRM_DB_BUSY_ATTEMPTS", 2)

    response = await join.node_join_confirm(
        FakeRequest(),
        x_cnc_join_token="join-token",
        settings=Settings(multi_node_enabled=True),
        session=session,
    )

    assert response.status_code == 503
    assert attempts == 2
    assert session.rollbacks == 2
    assert json.loads(response.body)["detail"] == (
        "database is busy; retry join confirmation"
    )


@pytest.mark.asyncio
async def test_join_routes_return_404_when_multi_node_disabled() -> None:
    settings = Settings(multi_node_enabled=False)
    session = FakeSession()

    install = await join.node_join_install_script(settings=settings)
    register = await join.node_join_register(
        FakeRequest(),
        x_cnc_join_token="join-token",
        settings=settings,
        session=session,
    )
    confirm = await join.node_join_confirm(
        FakeRequest(),
        x_cnc_join_token="join-token",
        settings=settings,
        session=session,
    )

    assert install.status_code == 404
    assert register.status_code == 404
    assert confirm.status_code == 404
    assert session.rollbacks == 0
