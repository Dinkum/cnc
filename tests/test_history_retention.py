from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.entities import ApplyRun, ControlEvent, HostApplyState, Operation
from app.services.history_retention import (
    HistoryRetentionPolicy,
    prune_control_plane_history,
)


@pytest.fixture
async def session(tmp_path) -> AsyncSession:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as value:
        yield value
    await engine.dispose()


def _apply_run(
    *, status: str, created_at: datetime, operation_id: int | None = None
) -> ApplyRun:
    return ApplyRun(
        status=status,
        message=status,
        operation_id=operation_id,
        desired_state_json='{"state":true}',
        generated_nginx_files_json="{}",
        runtime_graph_json="{}",
        route_contracts_json="{}",
        backend_contracts_json="{}",
        resource_profile_json="{}",
        details_json="{}",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_apply_snapshot_retention_keeps_recovery_rows_and_recent_outcomes(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    old = now - timedelta(days=120)
    active_operation = Operation(
        kind="apply_host", status="running", started_at=old, details_json="{}"
    )
    session.add(active_operation)
    await session.flush()
    protected = _apply_run(status="success", created_at=old)
    active = _apply_run(
        status="error", created_at=old, operation_id=active_operation.id
    )
    stripped_success = _apply_run(status="success", created_at=now)
    kept_success = _apply_run(status="success", created_at=now)
    stripped_failure = _apply_run(status="error", created_at=now)
    kept_failure = _apply_run(status="error", created_at=now)
    session.add_all(
        [
            protected,
            active,
            stripped_success,
            kept_success,
            stripped_failure,
            kept_failure,
        ]
    )
    await session.flush()
    session.add(
        HostApplyState(
            id=1,
            last_successful_apply_run_id=protected.id,
            last_successful_operation_id=active_operation.id,
        )
    )
    await session.flush()

    await prune_control_plane_history(
        session,
        now=now,
        policy=HistoryRetentionPolicy(
            apply_full_successes=1,
            apply_full_failures=1,
            apply_summary_days=365,
            apply_summary_rows=100,
            operation_days=365,
            operation_rows=100,
            control_event_days=365,
            control_event_rows=100,
        ),
    )
    await session.commit()
    session.expire_all()

    rows = {
        row.id: row for row in (await session.execute(select(ApplyRun))).scalars().all()
    }
    assert rows[protected.id].desired_state_json is not None
    assert rows[active.id].desired_state_json is not None
    assert rows[kept_success.id].desired_state_json is not None
    assert rows[kept_failure.id].desired_state_json is not None
    assert rows[stripped_success.id].desired_state_json is None
    assert rows[stripped_failure.id].desired_state_json is None


@pytest.mark.asyncio
async def test_history_summaries_are_age_and_count_bounded(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    old = now - timedelta(days=120)
    protected_operation = Operation(
        kind="apply_host", status="success", started_at=old, finished_at=old
    )
    active_operation = Operation(kind="apply_host", status="running", started_at=old)
    terminal_operations = [
        Operation(kind="apply_host", status="failed", started_at=old, finished_at=old)
        for _ in range(3)
    ]
    session.add_all([protected_operation, active_operation, *terminal_operations])
    await session.flush()
    protected_run = _apply_run(status="success", created_at=old)
    active_run = _apply_run(
        status="error", created_at=old, operation_id=active_operation.id
    )
    ordinary_runs = [_apply_run(status="error", created_at=old) for _ in range(3)]
    session.add_all([protected_run, active_run, *ordinary_runs])
    session.add_all(
        [
            ControlEvent(
                kind="test",
                source="test",
                summary="test",
                created_at=old,
            )
            for _ in range(2)
        ]
        + [
            ControlEvent(
                kind="test",
                source="test",
                summary="test",
                created_at=now,
            )
            for _ in range(4)
        ]
    )
    await session.flush()
    session.add(
        HostApplyState(
            id=1,
            last_successful_apply_run_id=protected_run.id,
            last_successful_operation_id=protected_operation.id,
        )
    )
    await session.flush()

    await prune_control_plane_history(
        session,
        now=now,
        policy=HistoryRetentionPolicy(
            apply_full_successes=1,
            apply_full_failures=1,
            apply_summary_days=30,
            apply_summary_rows=2,
            operation_days=30,
            operation_rows=2,
            control_event_days=30,
            control_event_rows=2,
        ),
    )
    await session.commit()

    apply_ids = set((await session.execute(select(ApplyRun.id))).scalars())
    operation_ids = set((await session.execute(select(Operation.id))).scalars())
    event_ids = set((await session.execute(select(ControlEvent.id))).scalars())
    assert apply_ids == {protected_run.id, active_run.id}
    assert operation_ids == {protected_operation.id, active_operation.id}
    assert len(event_ids) == 2
