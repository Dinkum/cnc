from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ApplyRun, ControlEvent, HostApplyState, Operation


ACTIVE_OPERATION_STATUSES = ("queued", "running")
APPLY_SNAPSHOT_COLUMNS = (
    ApplyRun.desired_state_json,
    ApplyRun.generated_nginx_files_json,
    ApplyRun.runtime_graph_json,
    ApplyRun.route_contracts_json,
    ApplyRun.backend_contracts_json,
    ApplyRun.resource_profile_json,
)


@dataclass(frozen=True)
class HistoryRetentionPolicy:
    apply_full_successes: int = 20
    apply_full_failures: int = 20
    apply_summary_days: int = 90
    apply_summary_rows: int = 500
    operation_days: int = 90
    operation_rows: int = 1_000
    control_event_days: int = 90
    control_event_rows: int = 5_000


DEFAULT_HISTORY_RETENTION = HistoryRetentionPolicy()


async def prune_control_plane_history(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    policy: HistoryRetentionPolicy = DEFAULT_HISTORY_RETENTION,
) -> None:
    """Bound durable history while preserving live recovery references."""
    timestamp = now or datetime.now(UTC)
    apply_cutoff = timestamp - timedelta(days=policy.apply_summary_days)
    operation_cutoff = timestamp - timedelta(days=policy.operation_days)
    event_cutoff = timestamp - timedelta(days=policy.control_event_days)

    host_state = await session.get(HostApplyState, 1)
    protected_apply_id = (
        host_state.last_successful_apply_run_id if host_state is not None else None
    )
    protected_operation_id = (
        host_state.last_successful_operation_id if host_state is not None else None
    )

    active_operation_ids = select(Operation.id).where(
        Operation.status.in_(ACTIVE_OPERATION_STATUSES)
    )
    protected_apply = ApplyRun.operation_id.is_not(None) & ApplyRun.operation_id.in_(
        active_operation_ids
    )
    if protected_apply_id is not None:
        protected_apply = or_(ApplyRun.id == protected_apply_id, protected_apply)

    full_success_ids = (
        select(ApplyRun.id)
        .where(ApplyRun.status == "success")
        .order_by(ApplyRun.id.desc())
        .limit(max(0, policy.apply_full_successes))
    )
    full_failure_ids = (
        select(ApplyRun.id)
        .where(ApplyRun.status != "success")
        .order_by(ApplyRun.id.desc())
        .limit(max(0, policy.apply_full_failures))
    )
    keep_full_snapshot = or_(
        protected_apply,
        ApplyRun.id.in_(full_success_ids),
        ApplyRun.id.in_(full_failure_ids),
    )
    await session.execute(
        update(ApplyRun)
        .where(~keep_full_snapshot)
        .where(or_(*(column.is_not(None) for column in APPLY_SNAPSHOT_COLUMNS)))
        .values({column.key: None for column in APPLY_SNAPSHOT_COLUMNS})
    )

    recent_apply_ids = (
        select(ApplyRun.id)
        .order_by(ApplyRun.id.desc())
        .limit(max(0, policy.apply_summary_rows))
    )
    await session.execute(
        delete(ApplyRun)
        .where(~protected_apply)
        .where(
            or_(
                ApplyRun.created_at < apply_cutoff,
                ~ApplyRun.id.in_(recent_apply_ids),
            )
        )
    )

    recent_operation_ids = (
        select(Operation.id)
        .order_by(Operation.id.desc())
        .limit(max(0, policy.operation_rows))
    )
    protected_operation = Operation.status.in_(ACTIVE_OPERATION_STATUSES)
    if protected_operation_id is not None:
        protected_operation = or_(
            Operation.id == protected_operation_id, protected_operation
        )
    await session.execute(
        delete(Operation)
        .where(~protected_operation)
        .where(
            or_(
                Operation.finished_at < operation_cutoff,
                ~Operation.id.in_(recent_operation_ids),
            )
        )
    )

    recent_event_ids = (
        select(ControlEvent.id)
        .order_by(ControlEvent.id.desc())
        .limit(max(0, policy.control_event_rows))
    )
    await session.execute(
        delete(ControlEvent).where(
            or_(
                ControlEvent.created_at < event_cutoff,
                ~ControlEvent.id.in_(recent_event_ids),
            )
        )
    )
