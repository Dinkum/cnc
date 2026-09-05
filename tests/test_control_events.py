from pathlib import Path
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ControlEvent
from app.services import control_events
from app.services.control_events import (
    emit_control_event,
    list_recent_control_events,
    list_recent_output_events,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_list_recent_control_events_filters_for_backend(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{db_path}")

    await emit_control_event(
        settings,
        kind="apply_completed",
        source="apply",
        summary="Changes applied",
        severity="success",
        scope="host",
        affects_all=True,
    )
    await emit_control_event(
        settings,
        kind="backend_fix_recovered",
        source="fix",
        summary="Backend fix recovered",
        severity="success",
        scope="backend",
        backend_name="web",
    )
    await emit_control_event(
        settings,
        kind="resource_resized",
        source="auto_size",
        summary="Resource size changed",
        severity="info",
        scope="host",
        related_backends=["web"],
    )
    await emit_control_event(
        settings,
        kind="backup_created",
        source="backup",
        summary="Backup created",
        severity="success",
        scope="backend",
        backend_name="smoke",
    )

    async with maker() as session:
        rows = await list_recent_control_events(session, backend_name="web", limit=10)

    summaries = [item.summary for item in rows]
    assert "Changes applied" in summaries
    assert "Backend fix recovered" in summaries
    assert "Resource size changed" in summaries
    assert "Backup created" not in summaries


@pytest.mark.asyncio
async def test_list_recent_control_events_filters_before_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)

    async with maker() as session:
        session.add_all(
            [
                ControlEvent(
                    kind="backend_unique",
                    source="test",
                    summary="Direct web event",
                    severity="info",
                    scope="backend",
                    backend_name="web",
                ),
                ControlEvent(
                    kind="related_unique",
                    source="test",
                    summary="Related web event",
                    severity="info",
                    scope="host",
                    related_backends_json='["web"]',
                ),
                ControlEvent(
                    kind="host_unique",
                    source="test",
                    summary="Host-wide event",
                    severity="info",
                    scope="host",
                    affects_all=True,
                ),
                ControlEvent(
                    kind="related_other",
                    source="test",
                    summary="Different related backend",
                    severity="info",
                    scope="host",
                    related_backends_json='["web-plus"]',
                ),
                ControlEvent(
                    kind="malformed_related",
                    source="test",
                    summary="Malformed related targets",
                    severity="info",
                    scope="host",
                    related_backends_json="not-json",
                ),
                *[
                    ControlEvent(
                        kind="other_backend_event",
                        source="test",
                        summary=f"Other backend event {index}",
                        severity="info",
                        scope="backend",
                        backend_name="other",
                    )
                    for index in range(200)
                ],
            ]
        )
        await session.commit()

        rows = await list_recent_control_events(session, backend_name="web", limit=12)

    assert [item.summary for item in rows] == [
        "Host-wide event",
        "Related web event",
        "Direct web event",
    ]


@pytest.mark.asyncio
async def test_output_timeline_caps_selected_host_events_without_starving_output(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    async with maker() as session:
        session.add(
            ControlEvent(
                kind="backend_error",
                source="runtime",
                summary="Unique backend event",
                severity="error",
                scope="backend",
                backend_name="web",
            )
        )
        await session.flush()
        for index in range(20):
            session.add(
                ControlEvent(
                    kind="apply_completed",
                    source="apply",
                    summary=f"Host apply {index}",
                    severity="success",
                    scope="host",
                    affects_all=True,
                )
            )
        session.add_all(
            [
                ControlEvent(
                    kind="update_completed",
                    source="update",
                    summary="Control-plane update",
                    severity="success",
                    scope="host",
                    affects_all=True,
                ),
                ControlEvent(
                    kind="resource_resized",
                    source="auto_size",
                    summary="Web resized",
                    severity="success",
                    scope="host",
                    related_backends_json='["web"]',
                ),
            ]
        )
        await session.commit()

        rows = await list_recent_output_events(session, backend_name="web", limit=12)

    summaries = [row.summary for row in rows]
    assert "Unique backend event" in summaries
    assert "Web resized" in summaries
    assert "Control-plane update" not in summaries
    assert sum(row.affects_all for row in rows) == 4


@pytest.mark.asyncio
async def test_list_recent_control_events_can_filter_before_backend_creation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    created_at = datetime(2026, 5, 1, 14, 0, 0)

    async with maker() as session:
        session.add_all(
            [
                ControlEvent(
                    kind="update_completed",
                    source="update",
                    summary="Old host update completed",
                    severity="success",
                    scope="host",
                    affects_all=True,
                    created_at=created_at - timedelta(hours=2),
                ),
                ControlEvent(
                    kind="apply_completed",
                    source="apply",
                    summary="New host apply completed",
                    severity="success",
                    scope="host",
                    affects_all=True,
                    created_at=created_at + timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()

        rows = await list_recent_control_events(
            session, backend_name="web", since=created_at, limit=10
        )

    assert [item.summary for item in rows] == ["New host apply completed"]


@pytest.mark.asyncio
async def test_emit_control_event_uses_notification_policy(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    await _make_session(db_path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{db_path}")
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(control_events, "send_pushover_notification_async", fake_notify)

    skipped = await emit_control_event(
        settings,
        kind="apply_completed",
        source="apply",
        summary="Changes applied",
        severity="success",
        scope="host",
        affects_all=True,
    )
    notified = await emit_control_event(
        settings,
        kind="auto_size_resized",
        source="auto_size",
        summary="Auto-size resized outputs",
        severity="success",
        scope="host",
        related_backends=["web"],
        subevents=[
            {"label": "outputs", "value": "1"},
            {
                "label": "changes",
                "value": "web: small->medium (cpu p95 92%, memory p95 88%)",
            },
            {"label": "run", "value": "#9"},
        ],
        details={"run_id": 9},
    )

    assert skipped == {
        "sent": False,
        "event": "apply_completed",
        "reason": "policy_skip",
    }
    assert notified == {
        "sent": True,
        "event": "auto_size_resized",
        "reason": "policy_match",
    }
    assert len(sent) == 1
    assert sent[0]["title"] == "CNC auto-size resized outputs"
    assert sent[0]["event"] == "auto_size_resized"
    assert sent[0]["run_id"] == 9
    message = str(sent[0]["message"])
    assert "time: " in message
    assert "title: Auto-size resized outputs" in message
    assert "status: success" in message
    assert "values:" in message
    assert "  outputs: 1" in message
    assert "cpu p95 92%" in message
    assert 'details: {"affects_all":false' in message


@pytest.mark.asyncio
async def test_emit_control_event_can_suppress_policy_notification(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    await _make_session(db_path)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{db_path}")
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(control_events, "send_pushover_notification_async", fake_notify)

    result = await emit_control_event(
        settings,
        kind="apply_failed",
        source="apply",
        summary="Host apply failed",
        severity="error",
        scope="host",
        affects_all=True,
        notify=False,
    )

    assert result == {"sent": False, "event": "apply_failed", "reason": "policy_skip"}
    assert sent == []
