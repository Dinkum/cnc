from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from inspect import signature
import os
from pathlib import Path
import shutil
import stat
from typing import Any

from sqlalchemy import delete, extract, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, BackendResourceSample, HostResourceSample
from app.schemas.apply import ApplyResponse
from app.services.app_containers import app_sandbox_dir
from app.services.apply_service import invalidate_apply_convergence, run_apply
from app.services.cgroup_diagnostics import collect_container_cgroup_diagnostics
from app.services.container_runtime import inspect_container, read_container_stats
from app.services.control_events import emit_control_event
from app.services.operations import OperationHandle
from app.services.resource_profile import (
    backend_resource_profile,
    build_resource_profile,
)
from app.services.renderers import container_name
from app.services.status_service import (
    _cpu_limit_percent_of_host,
    _derive_cpu_percent_of_entitlement,
    _parse_bytes,
    _parse_podman_stats_metrics,
    _read_linux_cpu_totals,
    _read_linux_memory,
    _read_linux_network_bytes,
)
from app.services.validators import (
    ValidationError,
    parse_volume_binding,
    parse_volumes_json,
)


AUTO_SIZE_LOOKBACK_HOURS = 24 * 7
AUTO_SIZE_SAMPLE_RETENTION_HOURS = 24 * 30
AUTO_SIZE_SAMPLE_BUCKET_MINUTES = 1
AUTO_SIZE_MIN_SAMPLES = 24
AUTO_SIZE_WARMUP_HOURS = 24
AUTO_SIZE_WARMUP_MIN_SAMPLES = 6
AUTO_SIZE_COOLDOWN_HOURS = 24 * 7
AUTO_SIZE_PROMOTE_CPU_PERCENT = 85
AUTO_SIZE_PROMOTE_MEMORY_PERCENT = 80
AUTO_SIZE_DEMOTE_CPU_PERCENT = 20
AUTO_SIZE_DEMOTE_MEMORY_PERCENT = 25
AUTO_SIZE_DISK_WALK_ENTRY_LIMIT = 50_000
AUTO_SIZE_ORDER = ("small", "medium", "large")

logger = get_logger("auto_size")


@dataclass(frozen=True)
class AutoSizeDecision:
    backend: str
    previous_size: str
    next_size: str
    decision: str
    reason: str
    cpu_p95: float | None
    memory_p95: float | None
    sample_count: int
    backend_id: int | None = None
    memory_peak_bytes: int | None = None
    trigger: str | None = None
    trigger_count: int | None = None
    previous_counter: int | None = None
    current_counter: int | None = None
    memory_high_bytes: int | None = None
    memory_max_bytes: int | None = None

    @property
    def changed(self) -> bool:
        return self.previous_size != self.next_size


@dataclass(frozen=True)
class DiskUsageMeasurement:
    total_bytes: int | None
    complete: bool
    skipped_paths: int = 0


@dataclass(frozen=True)
class MemoryEventTrigger:
    backend_id: int
    trigger: str
    trigger_count: int
    memory_peak_bytes: int | None
    previous_counter: int | None = None
    current_counter: int | None = None
    memory_high_bytes: int | None = None
    memory_max_bytes: int | None = None


@dataclass(frozen=True)
class MetricSampleWriteResult:
    written: int
    memory_event_triggers: tuple[MemoryEventTrigger, ...]


async def run_auto_size_tick(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    sample_now = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    async with logger.operation(
        "auto_size.tick", bucket_start=_bucket_start(sample_now).isoformat()
    ):
        backends = (
            (await session.execute(select(Backend).order_by(Backend.id.asc())))
            .scalars()
            .all()
        )
        enabled_app_backends = [
            backend for backend in backends if backend.kind == "app" and backend.enabled
        ]
        resource_profile = build_resource_profile(settings, enabled_app_backends)
        metric_rows = await _collect_lightweight_backend_metrics(
            enabled_app_backends,
            settings,
            sample_now=sample_now,
        )
        metric_write = await _write_metric_samples(
            session,
            enabled_app_backends,
            metric_rows,
            sample_now,
        )
        samples_written = metric_write.written
        host_sample_written = await _write_host_metric_sample(
            session, resource_profile, sample_now
        )
        pruned_samples = await _prune_old_samples(session, sample_now)
        result: dict[str, Any] = {
            "bucket_start": _bucket_start(sample_now).isoformat(),
            "sampled_backends": len(enabled_app_backends),
            "samples_written": samples_written,
            "host_sample_written": host_sample_written,
            "samples_pruned": pruned_samples,
            "evaluated": False,
            "changed_backends": [],
            "apply": None,
            "notification": None,
        }
        logger.info(
            "auto_size.tick.samples_recorded",
            backend_count=len(enabled_app_backends),
            samples_written=samples_written,
            host_sample_written=host_sample_written,
            samples_pruned=pruned_samples,
        )

        immediate_decisions = _memory_event_promotion_decisions(
            enabled_app_backends,
            metric_write.memory_event_triggers,
        )
        emergency_alerts = _memory_event_resource_alerts(
            enabled_app_backends,
            metric_write.memory_event_triggers,
        )
        evaluation_due = _evaluation_due(
            sample_now, settings.auto_size_nightly_hour_utc
        )
        if not evaluation_due and not immediate_decisions and not emergency_alerts:
            logger.info(
                "auto_size.tick.skipped_evaluation",
                current_hour_utc=sample_now.hour,
                current_minute_utc=sample_now.minute,
                nightly_hour_utc=settings.auto_size_nightly_hour_utc,
            )
            if not await _commit_metric_sample_writes(
                session, result, bucket_start=_bucket_start(sample_now)
            ):
                return result
            return result

        samples_committed = await _commit_metric_sample_writes(
            session, result, bucket_start=_bucket_start(sample_now)
        )
        if not samples_committed:
            logger.warning(
                "auto_size.tick.continuing_evaluation_after_sample_collision",
                bucket_start=_bucket_start(sample_now).isoformat(),
            )
            for decision in immediate_decisions:
                logger.warning(
                    "auto_size.memory_event_promotion.deferred",
                    backend_id=decision.backend_id,
                    backend=decision.backend,
                    previous_size=decision.previous_size,
                    next_size=decision.next_size,
                    memory_peak_bytes=decision.memory_peak_bytes,
                    trigger=decision.trigger,
                    trigger_count=decision.trigger_count,
                    reason="counter_checkpoint_not_committed",
                )
            immediate_decisions = []
            emergency_alerts = []
        for backend, trigger, reason in emergency_alerts:
            await _emit_emergency_resource_alert(
                settings,
                backend=backend,
                trigger=trigger,
                reason=reason,
            )
        if immediate_decisions:
            backend_by_id = {backend.id: backend for backend in enabled_app_backends}
            for decision in immediate_decisions:
                backend = backend_by_id.get(decision.backend_id)
                if backend is None:
                    continue
                backend.resource_size = decision.next_size
                backend.resource_size_updated_at = sample_now
                logger.warning(
                    "auto_size.emergency_promotion.requested",
                    **_emergency_promotion_fields(decision),
                )

        decisions = list(immediate_decisions)
        if evaluation_due:
            if immediate_decisions:
                nightly_decisions = await _evaluate_auto_sizes(
                    session,
                    sample_now,
                    excluded_backend_ids={
                        decision.backend_id
                        for decision in immediate_decisions
                        if decision.backend_id is not None
                    },
                )
            else:
                nightly_decisions = await _evaluate_auto_sizes(session, sample_now)
            decisions.extend(nightly_decisions)
        result["evaluated"] = True
        result["decisions"] = [_decision_payload(item) for item in decisions]
        changed = [item for item in decisions if item.changed]
        if not changed:
            logger.info(
                "auto_size.tick.no_size_changes", evaluated_backends=len(decisions)
            )
            await session.commit()
            return result

        changed_names = [item.backend for item in changed]
        result["changed_backends"] = changed_names
        logger.info(
            "auto_size.tick.size_changes_ready",
            changed_backends=changed_names,
            change_count=len(changed),
        )
        apply_parameters = signature(run_apply).parameters
        apply_kwargs: dict[str, Any] = {}
        if "operation_kind" in apply_parameters:
            apply_kwargs["operation_kind"] = "auto_size"
        if "actor" in apply_parameters:
            apply_kwargs["actor"] = "auto_size"
        if "commit_on_success" in apply_parameters:
            apply_kwargs["commit_on_success"] = False
        if "emit_success_event" in apply_parameters:
            apply_kwargs["emit_success_event"] = False
        with session.no_autoflush:
            apply_response = await run_apply(session, settings, **apply_kwargs)
        result["apply"] = {
            "status": apply_response.status,
            "message": apply_response.message,
            "run_id": apply_response.run_id,
        }
        if apply_response.status == "success":
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                retry_prepared = await _prepare_emergency_retry(
                    session,
                    immediate_decisions,
                    bucket_start=_bucket_start(sample_now),
                    invalidate_convergence=True,
                )
                result["apply"] = {
                    "status": "error",
                    "message": "apply completed but auto-size commit failed",
                    "run_id": None,
                    "uncommitted_run_id": apply_response.run_id,
                    "phase": "database_commit",
                    "error": str(exc),
                    "emergency_retry_prepared": retry_prepared,
                    "convergence_invalidated": True,
                }
                await _record_auto_size_commit_failure(settings, apply_response, exc)
                logger.exception(
                    "auto_size.tick.commit_failed_after_apply",
                    run_id=apply_response.run_id,
                    changed_backends=changed_names,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                for decision in immediate_decisions:
                    logger.error(
                        "auto_size.emergency_promotion_failed",
                        **_emergency_promotion_fields(
                            decision, apply_run_id=apply_response.run_id
                        ),
                        apply_status="database_commit_failed",
                        retry_prepared=retry_prepared,
                    )
                return result
            await _complete_deferred_apply_operation(settings, apply_response)
            await _emit_deferred_apply_completed_event(settings, apply_response)
            for decision in changed:
                if decision.trigger is None:
                    continue
                logger.info(
                    "auto_size.emergency_promoted",
                    **_emergency_promotion_fields(
                        decision, apply_run_id=apply_response.run_id
                    ),
                )
            result["notification"] = await _notify_auto_size_changes(
                settings,
                changed,
                run_id=apply_response.run_id,
            )
        else:
            await session.rollback()
            retry_prepared = await _prepare_emergency_retry(
                session,
                immediate_decisions,
                bucket_start=_bucket_start(sample_now),
                invalidate_convergence=False,
            )
            result["apply"]["emergency_retry_prepared"] = retry_prepared
            for decision in changed:
                if decision.trigger is None:
                    continue
                logger.error(
                    "auto_size.emergency_promotion_failed",
                    **_emergency_promotion_fields(
                        decision, apply_run_id=apply_response.run_id
                    ),
                    apply_status=apply_response.status,
                    retry_prepared=retry_prepared,
                )
                await _emit_emergency_promotion_failed_event(
                    settings,
                    decision,
                    apply_response=apply_response,
                )
        logger.info(
            "auto_size.tick.apply_completed",
            status=apply_response.status,
            run_id=apply_response.run_id,
            changed_backends=changed_names,
        )
        return result


async def _prepare_emergency_retry(
    session: AsyncSession,
    decisions: list[AutoSizeDecision],
    *,
    bucket_start: datetime,
    invalidate_convergence: bool,
) -> bool:
    backend_ids = sorted(
        {
            decision.backend_id
            for decision in decisions
            if isinstance(decision.backend_id, int)
        }
    )
    if not backend_ids and not invalidate_convergence:
        return False
    try:
        if invalidate_convergence:
            await invalidate_apply_convergence(session)
        if backend_ids:
            await session.execute(
                delete(BackendResourceSample).where(
                    BackendResourceSample.backend_id.in_(backend_ids),
                    BackendResourceSample.bucket_start == bucket_start,
                )
            )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "auto_size.emergency_retry.prepare_failed",
            backend_ids=backend_ids,
            bucket_start=bucket_start.isoformat(),
            convergence_invalidation_requested=invalidate_convergence,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    logger.warning(
        "auto_size.emergency_retry.prepared",
        backend_ids=backend_ids,
        bucket_start=bucket_start.isoformat(),
        convergence_invalidated=invalidate_convergence,
    )
    return True


async def _commit_metric_sample_writes(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    bucket_start: datetime,
) -> bool:
    try:
        await session.commit()
        return True
    except IntegrityError as exc:
        await session.rollback()
        result["sample_write_status"] = "collision"
        result["sample_write_error"] = str(exc)
        logger.warning(
            "auto_size.tick.sample_commit_collision",
            bucket_start=bucket_start.isoformat(),
            error=str(exc),
        )
        return False


def _deferred_apply_operation(
    settings: Settings, apply_response: ApplyResponse
) -> OperationHandle | None:
    operation_id = apply_response.details.get("deferred_operation_id")
    if not isinstance(operation_id, int):
        return None
    operation_kind = str(
        apply_response.details.get("deferred_operation_kind") or "auto_size"
    )
    return OperationHandle(id=operation_id, kind=operation_kind, settings=settings)


async def _complete_deferred_apply_operation(
    settings: Settings, apply_response: ApplyResponse
) -> None:
    operation = _deferred_apply_operation(settings, apply_response)
    if operation is None:
        return
    await operation.complete(
        "success",
        phase="completed",
        details={
            "apply_run_id": apply_response.run_id,
            "status": "success",
            "app_healthcheck_status": apply_response.details.get(
                "app_healthcheck_status"
            ),
        },
    )


async def _record_auto_size_commit_failure(
    settings: Settings,
    apply_response: ApplyResponse,
    exc: Exception,
) -> None:
    details = {
        "phase": "database_commit",
        "error": str(exc),
        "failure_mode": "partial",
        "manual_review_required": True,
        "uncommitted_apply_run_id": apply_response.run_id,
        "message": "Auto-size apply finished, but the resource-size commit failed.",
    }
    operation = _deferred_apply_operation(settings, apply_response)
    if operation is not None:
        await operation.complete(
            "partial",
            phase="database_commit",
            error=str(exc),
            details=details,
        )
    await emit_control_event(
        settings,
        kind="apply_failed",
        source="auto_size",
        summary="Auto-size apply completed but database commit failed",
        severity="error",
        scope="host",
        affects_all=True,
        subevents=[
            {"label": "phase", "value": "database_commit"},
            {"label": "status", "value": "partial"},
            {"label": "run", "value": f"uncommitted #{apply_response.run_id}"},
        ],
        details=details,
    )


async def _emit_deferred_apply_completed_event(
    settings: Settings, apply_response: ApplyResponse
) -> None:
    degraded_health = apply_response.details.get("app_healthcheck_status") == "degraded"
    await emit_control_event(
        settings,
        kind="apply_completed",
        source="apply",
        summary="Auto-size changes applied with app health warnings"
        if degraded_health
        else "Auto-size changes applied",
        severity="warn" if degraded_health else "success",
        scope="host",
        affects_all=True,
        subevents=[
            {"label": "run", "value": f"#{apply_response.run_id}"},
            {"label": "status", "value": "degraded" if degraded_health else "success"},
        ],
        details={
            "run_id": apply_response.run_id,
            "status": "success",
            "message": apply_response.message,
            "app_healthcheck_status": apply_response.details.get(
                "app_healthcheck_status"
            ),
            "app_healthcheck_failures": apply_response.details.get(
                "app_healthcheck_failures"
            )
            or [],
        },
    )


async def _notify_auto_size_changes(
    settings: Settings,
    decisions: list[AutoSizeDecision],
    *,
    run_id: int | None,
) -> dict[str, Any]:
    changes = [_change_summary(decision) for decision in decisions]
    backend_names = [decision.backend for decision in decisions]
    return await emit_control_event(
        settings,
        kind="auto_size_resized",
        source="auto_size",
        summary="Auto-size resized outputs",
        severity="success",
        scope="host",
        related_backends=backend_names,
        subevents=[
            {"label": "outputs", "value": str(len(decisions))},
            {"label": "changes", "value": _summarize_changes(changes)},
            {"label": "run", "value": f"#{run_id}" if run_id is not None else ""},
        ],
        details={
            "run_id": run_id,
            "changes": [_decision_payload(decision) for decision in decisions],
        },
        notify=True,
    )


async def _collect_lightweight_backend_metrics(
    backends: list[Backend],
    settings: Settings,
    *,
    sample_now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    base_profile = build_resource_profile(settings, backends)
    sample_disk_usage = _disk_sample_due(sample_now or datetime.now(UTC))
    metrics_by_backend: dict[str, dict[str, Any]] = {}
    for backend in backends:
        resource_profile = backend_resource_profile(backend, base_profile)
        memory_max_bytes = _parse_bytes(resource_profile.memory_max)
        stats_result = await read_container_stats(
            container_name(backend.name),
            timeout_sec=settings.command_timeout_status_sec,
        )
        metrics: dict[str, Any] = {}
        if not stats_result.ok:
            logger.info(
                "auto_size.sample.skipped",
                backend=backend.name,
                reason="container_stats_unavailable",
                returncode=stats_result.returncode,
            )
        else:
            metrics.update(
                _parse_podman_stats_metrics(
                    stats_result.stdout,
                    fallback_memory_max_bytes=memory_max_bytes,
                )
            )
            metrics["cpu_percent"] = _derive_cpu_percent_of_entitlement(
                metrics.get("cpu_percent_of_host"),
                resource_profile.cpu_entitlement_percent_of_host,
            )
            metrics["cpu_entitlement_percent_of_host"] = (
                resource_profile.cpu_entitlement_percent_of_host
            )
            metrics["cpu_limit_percent_of_host"] = _cpu_limit_percent_of_host(
                resource_profile.cpu_quota,
                resource_profile.host_cpu_count,
            )

        _inspect_result, inspect_payload = await asyncio.to_thread(
            inspect_container,
            container_name(backend.name),
            settings.command_timeout_status_sec,
        )
        cgroup = await asyncio.to_thread(
            collect_container_cgroup_diagnostics,
            inspect_payload,
            backend_name=backend.name,
            timeout_sec=settings.command_timeout_status_sec,
        )
        memory = cgroup.get("memory") if isinstance(cgroup, dict) else None
        if isinstance(memory, dict):
            peak = memory.get("peak")
            events = memory.get("events")
            metrics["memory_peak_bytes"] = peak if isinstance(peak, int) else None
            metrics["memory_events"] = events if isinstance(events, dict) else {}
            high = memory.get("high")
            maximum = memory.get("max")
            metrics["memory_high_bytes"] = (
                high
                if isinstance(high, int)
                else _parse_bytes(resource_profile.memory_high)
            )
            if isinstance(maximum, int):
                metrics["memory_max_bytes"] = maximum
            current = memory.get("current")
            if isinstance(current, int):
                metrics["memory_current_bytes"] = current
            if (
                isinstance(metrics.get("memory_current_bytes"), int)
                and isinstance(metrics.get("memory_max_bytes"), int)
                and metrics["memory_max_bytes"] > 0
            ):
                metrics["memory_percent"] = round(
                    metrics["memory_current_bytes"] / metrics["memory_max_bytes"] * 100,
                    1,
                )
        if sample_disk_usage:
            disk_usage = _coerce_disk_usage_measurement(
                await asyncio.to_thread(_backend_disk_usage_bytes, backend, settings)
            )
            metrics["disk_usage_bytes"] = disk_usage.total_bytes
            metrics["disk_usage_complete"] = disk_usage.complete
            metrics["disk_usage_skipped_paths"] = disk_usage.skipped_paths
        if metrics:
            metrics_by_backend[backend.name] = metrics
    return metrics_by_backend


async def _write_metric_samples(
    session: AsyncSession,
    backends: list[Backend],
    metric_rows: dict[str, dict[str, Any]],
    now: datetime,
) -> MetricSampleWriteResult:
    bucket_start = _bucket_start(now)
    sampled_backend_ids = [
        backend.id
        for backend in backends
        if isinstance(metric_rows.get(backend.name), dict)
    ]
    samples_by_backend_id = await _load_samples_for_bucket(
        session, sampled_backend_ids, bucket_start
    )
    previous_samples_by_backend_id = await _load_previous_samples(
        session, sampled_backend_ids, bucket_start
    )
    written = 0
    memory_event_triggers: list[MemoryEventTrigger] = []
    for backend in backends:
        metrics = metric_rows.get(backend.name)
        if not isinstance(metrics, dict):
            continue
        previous_sample = previous_samples_by_backend_id.get(backend.id)
        sample = samples_by_backend_id.get(backend.id)
        if sample is None:
            sample = BackendResourceSample(
                backend_id=backend.id, bucket_start=bucket_start
            )
            session.add(sample)
        baseline_sample = sample if sample.id is not None else previous_sample
        memory_events = metrics.get("memory_events")
        memory_events = memory_events if isinstance(memory_events, dict) else {}
        baseline_max_events = getattr(baseline_sample, "memory_events_max", None)
        baseline_oom_kills = getattr(baseline_sample, "memory_events_oom_kill", None)
        observed_max_events = _nonnegative_int(memory_events.get("max"))
        observed_oom_kills = _nonnegative_int(memory_events.get("oom_kill"))
        current_max_events = (
            observed_max_events
            if observed_max_events is not None
            else baseline_max_events
        )
        current_oom_kills = (
            observed_oom_kills if observed_oom_kills is not None else baseline_oom_kills
        )
        max_delta = _counter_delta(observed_max_events, baseline_max_events)
        oom_kill_delta = _counter_delta(observed_oom_kills, baseline_oom_kills)
        memory_peak = _nonnegative_int(metrics.get("memory_peak_bytes"))
        if oom_kill_delta > 0 or max_delta > 0:
            oom_triggered = oom_kill_delta > 0
            previous_counter = (
                _nonnegative_int(baseline_oom_kills)
                if oom_triggered
                else _nonnegative_int(baseline_max_events)
            )
            current_counter = (
                observed_oom_kills if oom_triggered else observed_max_events
            )
            assert current_counter is not None
            memory_event_triggers.append(
                MemoryEventTrigger(
                    backend_id=backend.id,
                    trigger="oom_kill" if oom_triggered else "memory.max",
                    trigger_count=oom_kill_delta if oom_triggered else max_delta,
                    memory_peak_bytes=memory_peak,
                    previous_counter=previous_counter,
                    current_counter=current_counter,
                    memory_high_bytes=_nonnegative_int(
                        metrics.get("memory_high_bytes")
                    ),
                    memory_max_bytes=_nonnegative_int(metrics.get("memory_max_bytes")),
                )
            )
        sample.sampled_at = now
        _record_rollup_value(
            sample,
            "cpu_percent_of_host",
            _float_value(metrics.get("cpu_percent_of_host")),
        )
        _record_rollup_value(
            sample,
            "cpu_percent_of_entitlement",
            _float_value(metrics.get("cpu_percent")),
        )
        _record_rollup_value(
            sample, "memory_percent", _float_value(metrics.get("memory_percent"))
        )
        memory_current = metrics.get("memory_current_bytes")
        memory_current_value = (
            float(memory_current) if isinstance(memory_current, int) else None
        )
        _record_rollup_value(sample, "memory_current_bytes", memory_current_value)
        disk_usage = _float_value(metrics.get("disk_usage_bytes"))
        _record_rollup_value(sample, "disk_usage_bytes", disk_usage)
        network_rates = _network_rates_from_sample_delta(previous_sample, metrics, now)
        _record_rollup_value(sample, "network_rx_bps", network_rates["network_rx_bps"])
        _record_rollup_value(sample, "network_tx_bps", network_rates["network_tx_bps"])
        _record_rollup_value(
            sample, "network_total_bps", network_rates["network_total_bps"]
        )
        sample.cpu_percent_of_host = _round_int(
            getattr(sample, "cpu_percent_of_host_last")
        )
        sample.cpu_percent_of_entitlement = _round_int(
            getattr(sample, "cpu_percent_of_entitlement_last")
        )
        sample.cpu_entitlement_percent_of_host = _float_value(
            metrics.get("cpu_entitlement_percent_of_host")
        )
        sample.cpu_limit_percent_of_host = _float_value(
            metrics.get("cpu_limit_percent_of_host")
        )
        sample.memory_percent = _round_int(getattr(sample, "memory_percent_last"))
        if disk_usage is not None:
            sample.disk_usage_bytes = _round_int(
                getattr(sample, "disk_usage_bytes_last")
            )
            sample.disk_usage_complete = bool(metrics.get("disk_usage_complete"))
            sample.disk_usage_skipped_paths = int(
                metrics.get("disk_usage_skipped_paths") or 0
            )
        sample.network_rx_bps = _round_int(getattr(sample, "network_rx_bps_last"))
        sample.network_tx_bps = _round_int(getattr(sample, "network_tx_bps_last"))
        sample.network_total_bps = _round_int(getattr(sample, "network_total_bps_last"))
        sample.memory_current_bytes = (
            int(memory_current) if isinstance(memory_current, int) else None
        )
        memory_max = metrics.get("memory_max_bytes")
        sample.memory_max_bytes = (
            int(memory_max) if isinstance(memory_max, int) else None
        )
        sample.memory_peak_bytes = memory_peak
        sample.memory_events_max = current_max_events
        sample.memory_events_oom_kill = current_oom_kills
        network_rx = metrics.get("network_rx_bytes")
        sample.network_rx_bytes = (
            int(network_rx) if isinstance(network_rx, int) else sample.network_rx_bytes
        )
        network_tx = metrics.get("network_tx_bytes")
        sample.network_tx_bytes = (
            int(network_tx) if isinstance(network_tx, int) else sample.network_tx_bytes
        )
        written += 1
        logger.debug(
            "auto_size.sample.recorded",
            backend=backend.name,
            bucket_start=bucket_start.isoformat(),
            cpu_percent_of_host=sample.cpu_percent_of_host,
            cpu_percent_of_entitlement=sample.cpu_percent_of_entitlement,
            cpu_entitlement_percent_of_host=sample.cpu_entitlement_percent_of_host,
            memory_percent=sample.memory_percent,
            disk_usage_bytes=sample.disk_usage_bytes,
        )
    return MetricSampleWriteResult(
        written=written,
        memory_event_triggers=tuple(memory_event_triggers),
    )


async def _load_samples_for_bucket(
    session: AsyncSession,
    backend_ids: list[int],
    bucket_start: datetime,
) -> dict[int, BackendResourceSample]:
    if not backend_ids:
        return {}
    samples = (
        (
            await session.execute(
                select(BackendResourceSample).where(
                    BackendResourceSample.backend_id.in_(backend_ids),
                    BackendResourceSample.bucket_start == bucket_start,
                )
            )
        )
        .scalars()
        .all()
    )
    return {sample.backend_id: sample for sample in samples}


async def _load_previous_samples(
    session: AsyncSession,
    backend_ids: list[int],
    bucket_start: datetime,
) -> dict[int, BackendResourceSample]:
    if not backend_ids:
        return {}
    latest_bucket_rows = (
        await session.execute(
            select(
                BackendResourceSample.backend_id,
                func.max(BackendResourceSample.bucket_start).label("bucket_start"),
            )
            .where(
                BackendResourceSample.backend_id.in_(backend_ids),
                BackendResourceSample.bucket_start < bucket_start,
            )
            .group_by(BackendResourceSample.backend_id)
        )
    ).all()
    latest_bucket_by_backend_id = {
        int(backend_id): bucket
        for backend_id, bucket in latest_bucket_rows
        if backend_id is not None and bucket is not None
    }
    if not latest_bucket_by_backend_id:
        return {}
    candidate_buckets = sorted(set(latest_bucket_by_backend_id.values()))
    samples = (
        (
            await session.execute(
                select(BackendResourceSample).where(
                    BackendResourceSample.backend_id.in_(latest_bucket_by_backend_id),
                    BackendResourceSample.bucket_start.in_(candidate_buckets),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        sample.backend_id: sample
        for sample in samples
        if latest_bucket_by_backend_id.get(sample.backend_id) == sample.bucket_start
    }


def _allocated_size(stat_result: os.stat_result) -> int:
    blocks = getattr(stat_result, "st_blocks", None)
    if isinstance(blocks, int) and blocks > 0:
        return blocks * 512
    return int(getattr(stat_result, "st_size", 0) or 0)


def _path_disk_usage_bytes(path: Path) -> int | None:
    try:
        root_stat = path.lstat()
    except OSError:
        return None

    total = _allocated_size(root_stat)
    if not stat.S_ISDIR(root_stat.st_mode):
        return total

    stack = [path]
    visited_entries = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited_entries += 1
                    if visited_entries > AUTO_SIZE_DISK_WALK_ENTRY_LIMIT:
                        return None
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += _allocated_size(entry_stat)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return total


def _backend_disk_usage_paths(backend: Backend, settings: Settings) -> list[Path]:
    paths = [app_sandbox_dir(settings, backend.name)]
    try:
        volume_entries = parse_volumes_json(backend.volumes_json)
    except ValidationError:
        volume_entries = []
    for volume in volume_entries:
        try:
            binding = parse_volume_binding(volume)
        except ValidationError:
            continue
        if binding.source.startswith("/"):
            paths.append(Path(binding.source))

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser().resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def _coerce_disk_usage_measurement(value: object) -> DiskUsageMeasurement:
    if isinstance(value, DiskUsageMeasurement):
        return value
    if isinstance(value, int):
        return DiskUsageMeasurement(total_bytes=value, complete=True)
    return DiskUsageMeasurement(total_bytes=None, complete=False)


def _backend_disk_usage_bytes(
    backend: Backend, settings: Settings
) -> DiskUsageMeasurement:
    total = 0
    found = False
    skipped_paths = 0
    for path in _backend_disk_usage_paths(backend, settings):
        usage = _path_disk_usage_bytes(path)
        if usage is None:
            skipped_paths += 1
            continue
        found = True
        total += usage
    return DiskUsageMeasurement(
        total_bytes=total if found else None,
        complete=found and skipped_paths == 0,
        skipped_paths=skipped_paths,
    )


def _disk_sample_due(now: datetime) -> bool:
    return _bucket_start(now).minute == 0


async def _write_host_metric_sample(
    session: AsyncSession,
    resource_profile: Any,
    now: datetime,
) -> bool:
    bucket_start = _bucket_start(now)
    previous_sample = await _load_previous_host_sample(session, bucket_start)
    sample = await _load_host_sample(session, bucket_start)
    if sample is None:
        sample = HostResourceSample(bucket_start=bucket_start)
        session.add(sample)
    sample.sampled_at = now

    cpu_totals = _read_linux_cpu_totals()
    cpu_percent = _host_cpu_percent_from_totals(previous_sample, cpu_totals)
    if cpu_percent is None:
        cpu_percent = _host_cpu_percent_from_load(resource_profile)
    memory_percent = _host_memory_percent(resource_profile)
    disk_percent = _host_disk_percent()
    network = _host_network_rates(previous_sample, _read_linux_network_bytes(), now)

    _record_rollup_value(sample, "cpu_percent", cpu_percent)
    _record_rollup_value(sample, "memory_percent", memory_percent)
    _record_rollup_value(sample, "disk_percent", disk_percent)
    _record_rollup_value(sample, "network_rx_bps", network["network_rx_bps"])
    _record_rollup_value(sample, "network_tx_bps", network["network_tx_bps"])
    _record_rollup_value(sample, "network_total_bps", network["network_total_bps"])
    sample.cpu_percent = _round_int(getattr(sample, "cpu_percent_last"))
    sample.memory_percent = _round_int(getattr(sample, "memory_percent_last"))
    sample.disk_percent = _round_int(getattr(sample, "disk_percent_last"))
    sample.network_rx_bytes = network["network_rx_bytes"]
    sample.network_tx_bytes = network["network_tx_bytes"]
    sample.network_rx_bps = _round_int(getattr(sample, "network_rx_bps_last"))
    sample.network_tx_bps = _round_int(getattr(sample, "network_tx_bps_last"))
    sample.network_total_bps = _round_int(getattr(sample, "network_total_bps_last"))
    if cpu_totals is not None:
        sample.cpu_total_jiffies = int(cpu_totals[0])
        sample.cpu_idle_jiffies = int(cpu_totals[1])
    logger.debug(
        "auto_size.host_sample.recorded",
        bucket_start=bucket_start.isoformat(),
        cpu_percent=sample.cpu_percent,
        memory_percent=sample.memory_percent,
        disk_percent=sample.disk_percent,
        network_total_bps=sample.network_total_bps,
    )
    return True


def _host_cpu_percent_from_totals(
    previous_sample: HostResourceSample | None,
    cpu_totals: tuple[int, int] | None,
) -> float | None:
    if cpu_totals is None or previous_sample is None:
        return None
    total, idle = cpu_totals
    if (
        previous_sample.cpu_total_jiffies is None
        or previous_sample.cpu_idle_jiffies is None
    ):
        return None
    elapsed_total = total - previous_sample.cpu_total_jiffies
    elapsed_idle = idle - previous_sample.cpu_idle_jiffies
    if elapsed_total <= 0:
        return None
    return max(
        0.0, min(100.0, ((elapsed_total - elapsed_idle) / elapsed_total) * 100.0)
    )


def _host_cpu_percent_from_load(resource_profile: Any) -> float | None:
    try:
        loadavg_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    cpu_count = getattr(resource_profile, "host_cpu_count", None)
    if not isinstance(cpu_count, int) or cpu_count <= 0:
        return None
    return max(0.0, min(100.0, (loadavg_1m / cpu_count) * 100.0))


def _host_memory_percent(resource_profile: Any) -> float | None:
    memory_total_bytes = getattr(resource_profile, "host_memory_bytes", None)
    memory_used_bytes: int | None = None
    memory = _read_linux_memory()
    if memory is not None:
        memory_total_bytes, memory_used_bytes = memory
    if not isinstance(memory_total_bytes, int) or memory_total_bytes <= 0:
        return None
    if not isinstance(memory_used_bytes, int):
        return None
    return max(0.0, min(100.0, (memory_used_bytes / memory_total_bytes) * 100.0))


def _host_disk_percent() -> float | None:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return max(0.0, min(100.0, (usage.used / usage.total) * 100.0))


def _host_network_rates(
    previous_sample: HostResourceSample | None,
    network_bytes: tuple[int, int] | None,
    sampled_at: datetime,
) -> dict[str, int | float | None]:
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None
    if network_bytes is not None:
        rx_bytes, tx_bytes = network_bytes
    if (
        previous_sample is not None
        and rx_bytes is not None
        and tx_bytes is not None
        and previous_sample.network_rx_bytes is not None
        and previous_sample.network_tx_bytes is not None
    ):
        previous_sampled_at = _sampled_at(previous_sample)
        elapsed = (sampled_at - previous_sampled_at).total_seconds()
        rx_delta = rx_bytes - previous_sample.network_rx_bytes
        tx_delta = tx_bytes - previous_sample.network_tx_bytes
        if elapsed > 0 and rx_delta >= 0 and tx_delta >= 0:
            rx_bps = rx_delta / elapsed
            tx_bps = tx_delta / elapsed
    return {
        "network_rx_bytes": rx_bytes,
        "network_tx_bytes": tx_bytes,
        "network_rx_bps": rx_bps,
        "network_tx_bps": tx_bps,
        "network_total_bps": (rx_bps + tx_bps)
        if rx_bps is not None and tx_bps is not None
        else None,
    }


def _sampled_at(sample: BackendResourceSample | HostResourceSample) -> datetime:
    sampled_at = getattr(sample, "sampled_at", None) or sample.bucket_start
    return (
        sampled_at.astimezone(UTC)
        if sampled_at.tzinfo
        else sampled_at.replace(tzinfo=UTC)
    )


def _network_rates_from_sample_delta(
    previous_sample: BackendResourceSample | None,
    metrics: dict[str, Any],
    sampled_at: datetime,
) -> dict[str, float | None]:
    rx_bytes = metrics.get("network_rx_bytes")
    tx_bytes = metrics.get("network_tx_bytes")
    fallback_rx = _float_value(metrics.get("network_rx_bps"))
    fallback_tx = _float_value(metrics.get("network_tx_bps"))
    fallback_total = _float_value(metrics.get("network_total_bps"))
    if fallback_total is None and fallback_rx is not None and fallback_tx is not None:
        fallback_total = fallback_rx + fallback_tx
    fallback = {
        "network_rx_bps": fallback_rx,
        "network_tx_bps": fallback_tx,
        "network_total_bps": fallback_total,
    }
    if (
        not isinstance(rx_bytes, int)
        or not isinstance(tx_bytes, int)
        or previous_sample is None
    ):
        return fallback
    if (
        previous_sample.network_rx_bytes is None
        or previous_sample.network_tx_bytes is None
    ):
        return fallback
    previous_sampled_at = _sampled_at(previous_sample)
    elapsed = (sampled_at - previous_sampled_at).total_seconds()
    if elapsed <= 0:
        return fallback
    rx_delta = rx_bytes - previous_sample.network_rx_bytes
    tx_delta = tx_bytes - previous_sample.network_tx_bytes
    if rx_delta < 0 or tx_delta < 0:
        return fallback
    rx_bps = rx_delta / elapsed
    tx_bps = tx_delta / elapsed
    return {
        "network_rx_bps": rx_bps,
        "network_tx_bps": tx_bps,
        "network_total_bps": rx_bps + tx_bps,
    }


async def _load_sample(
    session: AsyncSession,
    backend_id: int,
    bucket_start: datetime,
) -> BackendResourceSample | None:
    return (
        await session.execute(
            select(BackendResourceSample).where(
                BackendResourceSample.backend_id == backend_id,
                BackendResourceSample.bucket_start == bucket_start,
            )
        )
    ).scalar_one_or_none()


async def _load_previous_sample(
    session: AsyncSession,
    backend_id: int,
    bucket_start: datetime,
) -> BackendResourceSample | None:
    return (
        await session.execute(
            select(BackendResourceSample)
            .where(
                BackendResourceSample.backend_id == backend_id,
                BackendResourceSample.bucket_start < bucket_start,
            )
            .order_by(BackendResourceSample.bucket_start.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load_host_sample(
    session: AsyncSession,
    bucket_start: datetime,
) -> HostResourceSample | None:
    return (
        await session.execute(
            select(HostResourceSample).where(
                HostResourceSample.bucket_start == bucket_start
            )
        )
    ).scalar_one_or_none()


async def _load_previous_host_sample(
    session: AsyncSession,
    bucket_start: datetime,
) -> HostResourceSample | None:
    return (
        await session.execute(
            select(HostResourceSample)
            .where(HostResourceSample.bucket_start < bucket_start)
            .order_by(HostResourceSample.bucket_start.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _prune_old_samples(session: AsyncSession, now: datetime) -> int:
    cutoff = _bucket_start(now) - timedelta(hours=AUTO_SIZE_SAMPLE_RETENTION_HOURS)
    backend_result = await session.execute(
        delete(BackendResourceSample).where(BackendResourceSample.bucket_start < cutoff)
    )
    host_result = await session.execute(
        delete(HostResourceSample).where(HostResourceSample.bucket_start < cutoff)
    )
    return int(backend_result.rowcount or 0) + int(host_result.rowcount or 0)


def _memory_event_promotion_decisions(
    backends: list[Backend],
    triggers: tuple[MemoryEventTrigger, ...],
) -> list[AutoSizeDecision]:
    trigger_by_backend_id = {trigger.backend_id: trigger for trigger in triggers}
    decisions: list[AutoSizeDecision] = []
    for backend in backends:
        trigger = trigger_by_backend_id.get(backend.id)
        current_size = str(backend.resource_size or "small")
        if (
            trigger is None
            or not _backend_is_auto_managed(backend)
            or current_size == AUTO_SIZE_ORDER[-1]
        ):
            continue
        next_size = _step_size(current_size, direction="up")
        decisions.append(
            AutoSizeDecision(
                backend=backend.name,
                previous_size=current_size,
                next_size=next_size,
                decision="promote",
                reason=f"memory_event_{trigger.trigger}",
                cpu_p95=None,
                memory_p95=None,
                sample_count=0,
                backend_id=backend.id,
                memory_peak_bytes=trigger.memory_peak_bytes,
                trigger=trigger.trigger,
                trigger_count=trigger.trigger_count,
                previous_counter=trigger.previous_counter,
                current_counter=trigger.current_counter,
                memory_high_bytes=trigger.memory_high_bytes,
                memory_max_bytes=trigger.memory_max_bytes,
            )
        )
    return decisions


def _backend_is_auto_managed(backend: Backend) -> bool:
    if backend.resource_mode != "auto":
        return False
    return not any(
        str(getattr(backend, field, "") or "").strip()
        for field in (
            "memory_high_override",
            "memory_max_override",
            "cpu_quota_override",
        )
    )


def _memory_event_resource_alerts(
    backends: list[Backend],
    triggers: tuple[MemoryEventTrigger, ...],
) -> list[tuple[Backend, MemoryEventTrigger, str]]:
    trigger_by_backend_id = {trigger.backend_id: trigger for trigger in triggers}
    alerts: list[tuple[Backend, MemoryEventTrigger, str]] = []
    for backend in backends:
        trigger = trigger_by_backend_id.get(backend.id)
        if trigger is None:
            continue
        if not _backend_is_auto_managed(backend):
            alerts.append((backend, trigger, "manual_or_custom_policy"))
        elif str(backend.resource_size or "small") == AUTO_SIZE_ORDER[-1]:
            alerts.append((backend, trigger, "maximum_tier_reached"))
    return alerts


def _trigger_fields(trigger: MemoryEventTrigger) -> dict[str, Any]:
    return {
        "backend_id": trigger.backend_id,
        "trigger": trigger.trigger,
        "previous_counter": trigger.previous_counter,
        "current_counter": trigger.current_counter,
        "counter_delta": trigger.trigger_count,
        "memory_peak_bytes": trigger.memory_peak_bytes,
        "memory_high_bytes": trigger.memory_high_bytes,
        "memory_max_bytes": trigger.memory_max_bytes,
    }


def _emergency_promotion_fields(
    decision: AutoSizeDecision, *, apply_run_id: int | None = None
) -> dict[str, Any]:
    return {
        "backend_id": decision.backend_id,
        "backend": decision.backend,
        "old_size": decision.previous_size,
        "new_size": decision.next_size,
        "trigger": decision.trigger,
        "previous_counter": decision.previous_counter,
        "current_counter": decision.current_counter,
        "counter_delta": decision.trigger_count,
        "memory_peak_bytes": decision.memory_peak_bytes,
        "memory_high_bytes": decision.memory_high_bytes,
        "memory_max_bytes": decision.memory_max_bytes,
        "apply_run_id": apply_run_id,
    }


async def _emit_emergency_resource_alert(
    settings: Settings,
    *,
    backend: Backend,
    trigger: MemoryEventTrigger,
    reason: str,
) -> None:
    fields = {"backend": backend.name, "reason": reason, **_trigger_fields(trigger)}
    logger.error("auto_size.emergency_resource_alert", **fields)
    await emit_control_event(
        settings,
        kind="auto_size_resource_alert",
        source="auto_size",
        summary=f"Memory containment triggered for {backend.name}",
        severity="error",
        scope="backend",
        backend_name=backend.name,
        related_backends=[backend.name],
        subevents=[
            {"label": "trigger", "value": trigger.trigger},
            {"label": "reason", "value": reason},
            {"label": "peak", "value": str(trigger.memory_peak_bytes or "unknown")},
        ],
        details=fields,
        notify=True,
    )


async def _emit_emergency_promotion_failed_event(
    settings: Settings,
    decision: AutoSizeDecision,
    *,
    apply_response: ApplyResponse,
) -> None:
    fields = _emergency_promotion_fields(
        decision, apply_run_id=apply_response.run_id
    ) | {"apply_status": apply_response.status}
    await emit_control_event(
        settings,
        kind="auto_size_resource_alert",
        source="auto_size",
        summary=f"Emergency memory promotion failed for {decision.backend}",
        severity="error",
        scope="backend",
        backend_name=decision.backend,
        related_backends=[decision.backend],
        subevents=[
            {"label": "trigger", "value": str(decision.trigger or "unknown")},
            {
                "label": "size",
                "value": f"{decision.previous_size}->{decision.next_size}",
            },
            {"label": "apply", "value": apply_response.status},
        ],
        details=fields,
        notify=True,
    )


async def _evaluate_auto_sizes(
    session: AsyncSession,
    now: datetime,
    *,
    excluded_backend_ids: set[int] | None = None,
) -> list[AutoSizeDecision]:
    query = select(Backend).where(
        Backend.kind == "app",
        Backend.enabled.is_(True),
        Backend.resource_mode == "auto",
    )
    if excluded_backend_ids:
        query = query.where(Backend.id.notin_(excluded_backend_ids))
    auto_backends = (
        (await session.execute(query.order_by(Backend.id.asc()))).scalars().all()
    )
    decisions: list[AutoSizeDecision] = []
    lookback_cutoff = _bucket_start(now) - timedelta(hours=AUTO_SIZE_LOOKBACK_HOURS)
    samples_by_backend_id = await _load_auto_size_checkpoint_samples(
        session,
        [backend.id for backend in auto_backends],
        lookback_cutoff,
    )
    for backend in auto_backends:
        samples = samples_by_backend_id.get(backend.id, [])
        decision = _decide_backend_size(backend, samples, now)
        decisions.append(decision)
        if decision.changed:
            backend.resource_size = decision.next_size
            backend.resource_size_updated_at = now
        logger.debug(
            "auto_size.backend_evaluated",
            backend=backend.name,
            previous_size=decision.previous_size,
            next_size=decision.next_size,
            decision=decision.decision,
            reason=decision.reason,
            sample_count=decision.sample_count,
            cpu_p95=decision.cpu_p95,
            memory_p95=decision.memory_p95,
        )
    return decisions


async def _load_auto_size_checkpoint_samples(
    session: AsyncSession,
    backend_ids: list[int],
    lookback_cutoff: datetime,
) -> dict[int, list[BackendResourceSample]]:
    if not backend_ids:
        return {}
    samples = (
        (
            await session.execute(
                select(BackendResourceSample)
                .where(
                    BackendResourceSample.backend_id.in_(backend_ids),
                    BackendResourceSample.bucket_start >= lookback_cutoff,
                    extract("minute", BackendResourceSample.bucket_start) == 0,
                )
                .order_by(
                    BackendResourceSample.backend_id.asc(),
                    BackendResourceSample.bucket_start.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[BackendResourceSample]] = {}
    for sample in samples:
        if not _is_hourly_checkpoint(sample.bucket_start):
            continue
        grouped.setdefault(sample.backend_id, []).append(sample)
    return grouped


def _decide_backend_size(
    backend: Backend,
    samples: list[BackendResourceSample],
    now: datetime,
) -> AutoSizeDecision:
    now = _as_utc(now)
    current_size = str(backend.resource_size or "small")
    cpu_values = [
        float(item.cpu_percent_of_entitlement)
        for item in samples
        if item.cpu_percent_of_entitlement is not None
    ]
    memory_values = [
        float(item.memory_percent)
        for item in samples
        if item.memory_percent is not None
    ]
    cpu_p95 = _percentile(cpu_values, 95)
    memory_p95 = _percentile(memory_values, 95)
    sample_count = len(samples)

    if sample_count == 0:
        return AutoSizeDecision(
            backend=backend.name,
            previous_size=current_size,
            next_size=current_size,
            decision="keep",
            reason="no_samples",
            cpu_p95=cpu_p95,
            memory_p95=memory_p95,
            sample_count=sample_count,
        )

    if backend.resource_size_updated_at is not None:
        cooldown_cutoff = now - timedelta(hours=AUTO_SIZE_COOLDOWN_HOURS)
        if _as_utc(backend.resource_size_updated_at) >= cooldown_cutoff:
            return AutoSizeDecision(
                backend=backend.name,
                previous_size=current_size,
                next_size=current_size,
                decision="keep",
                reason="cooldown_active",
                cpu_p95=cpu_p95,
                memory_p95=memory_p95,
                sample_count=sample_count,
            )

    if backend.created_at is not None and _as_utc(
        backend.created_at
    ) >= now - timedelta(hours=AUTO_SIZE_WARMUP_HOURS):
        if sample_count >= AUTO_SIZE_WARMUP_MIN_SAMPLES and _is_pressure(
            cpu_p95, memory_p95
        ):
            next_size = _step_size(current_size, direction="up")
            return AutoSizeDecision(
                backend=backend.name,
                previous_size=current_size,
                next_size=next_size,
                decision="promote" if next_size != current_size else "keep",
                reason="warmup_pressure"
                if next_size != current_size
                else "already_at_max",
                cpu_p95=cpu_p95,
                memory_p95=memory_p95,
                sample_count=sample_count,
            )

    if sample_count < AUTO_SIZE_MIN_SAMPLES:
        return AutoSizeDecision(
            backend=backend.name,
            previous_size=current_size,
            next_size=current_size,
            decision="keep",
            reason="insufficient_samples",
            cpu_p95=cpu_p95,
            memory_p95=memory_p95,
            sample_count=sample_count,
        )

    if _is_pressure(cpu_p95, memory_p95):
        next_size = _step_size(current_size, direction="up")
        return AutoSizeDecision(
            backend=backend.name,
            previous_size=current_size,
            next_size=next_size,
            decision="promote" if next_size != current_size else "keep",
            reason="sustained_pressure"
            if next_size != current_size
            else "already_at_max",
            cpu_p95=cpu_p95,
            memory_p95=memory_p95,
            sample_count=sample_count,
        )

    if _is_idle(cpu_p95, memory_p95):
        next_size = _step_size(current_size, direction="down")
        return AutoSizeDecision(
            backend=backend.name,
            previous_size=current_size,
            next_size=next_size,
            decision="demote" if next_size != current_size else "keep",
            reason="sustained_idle" if next_size != current_size else "already_at_min",
            cpu_p95=cpu_p95,
            memory_p95=memory_p95,
            sample_count=sample_count,
        )

    return AutoSizeDecision(
        backend=backend.name,
        previous_size=current_size,
        next_size=current_size,
        decision="keep",
        reason="steady_state",
        cpu_p95=cpu_p95,
        memory_p95=memory_p95,
        sample_count=sample_count,
    )


def _is_pressure(cpu_p95: float | None, memory_p95: float | None) -> bool:
    return (
        isinstance(cpu_p95, (int, float)) and cpu_p95 >= AUTO_SIZE_PROMOTE_CPU_PERCENT
    ) or (
        isinstance(memory_p95, (int, float))
        and memory_p95 >= AUTO_SIZE_PROMOTE_MEMORY_PERCENT
    )


def _is_idle(cpu_p95: float | None, memory_p95: float | None) -> bool:
    return (
        isinstance(cpu_p95, (int, float))
        and isinstance(memory_p95, (int, float))
        and cpu_p95 <= AUTO_SIZE_DEMOTE_CPU_PERCENT
        and memory_p95 <= AUTO_SIZE_DEMOTE_MEMORY_PERCENT
    )


def _step_size(current_size: str, *, direction: str) -> str:
    normalized = current_size if current_size in AUTO_SIZE_ORDER else AUTO_SIZE_ORDER[0]
    index = AUTO_SIZE_ORDER.index(normalized)
    if direction == "up":
        return AUTO_SIZE_ORDER[min(len(AUTO_SIZE_ORDER) - 1, index + 1)]
    return AUTO_SIZE_ORDER[max(0, index - 1)]


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(
        0, min(len(ordered) - 1, int(round((percentile / 100) * (len(ordered) - 1))))
    )
    return round(ordered[position], 1)


def _bucket_start(now: datetime) -> datetime:
    normalized = now.astimezone(UTC).replace(second=0, microsecond=0)
    bucket_minute = (
        normalized.minute // AUTO_SIZE_SAMPLE_BUCKET_MINUTES
    ) * AUTO_SIZE_SAMPLE_BUCKET_MINUTES
    return normalized.replace(minute=bucket_minute)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _is_hourly_checkpoint(value: datetime) -> bool:
    return _as_utc(value).minute == 0


def _evaluation_due(now: datetime, nightly_hour_utc: int) -> bool:
    normalized = now.astimezone(UTC)
    return normalized.hour == nightly_hour_utc and _is_hourly_checkpoint(normalized)


def _record_rollup_value(
    sample: BackendResourceSample, prefix: str, value: float | None
) -> None:
    if value is None:
        return
    count_attr = f"{prefix}_count"
    sum_attr = f"{prefix}_sum"
    min_attr = f"{prefix}_min"
    max_attr = f"{prefix}_max"
    first_attr = f"{prefix}_first"
    last_attr = f"{prefix}_last"

    current_count = int(getattr(sample, count_attr) or 0)
    current_sum = getattr(sample, sum_attr)
    current_min = getattr(sample, min_attr)
    current_max = getattr(sample, max_attr)

    setattr(sample, count_attr, current_count + 1)
    setattr(sample, sum_attr, float(current_sum or 0.0) + value)
    setattr(
        sample,
        min_attr,
        value if current_min is None else min(float(current_min), value),
    )
    setattr(
        sample,
        max_attr,
        value if current_max is None else max(float(current_max), value),
    )
    if current_count == 0 or getattr(sample, first_attr) is None:
        setattr(sample, first_attr, value)
    setattr(sample, last_attr, value)


def _float_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _counter_delta(current: int | None, previous: Any) -> int:
    if current is None:
        return 0
    previous_value = _nonnegative_int(previous)
    if previous_value is None:
        # A missing predecessor is a baseline, not evidence that the current
        # cgroup generation produced every historical event this minute.
        return 0
    if current >= previous_value:
        return current - previous_value
    # A recreated cgroup resets cumulative counters; any non-zero value belongs
    # to the new payload and must not be hidden by the previous generation.
    return current


def _round_int(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return int(round(float(value)))


def _change_summary(decision: AutoSizeDecision) -> str:
    metrics: list[str] = []
    if decision.cpu_p95 is not None:
        metrics.append(f"cpu p95 {decision.cpu_p95:g}%")
    if decision.memory_p95 is not None:
        metrics.append(f"memory p95 {decision.memory_p95:g}%")
    metric_summary = f", {', '.join(metrics)}" if metrics else ""
    return f"{decision.backend}: {decision.previous_size}->{decision.next_size} ({decision.reason}{metric_summary})"


def _summarize_changes(changes: list[str], *, limit: int = 3) -> str:
    shown = [item for item in changes if item]
    if len(shown) <= limit:
        return ", ".join(shown)
    return f"{', '.join(shown[:limit])} (+{len(shown) - limit} more)"


def _decision_payload(decision: AutoSizeDecision) -> dict[str, Any]:
    return {
        "backend_id": decision.backend_id,
        "backend": decision.backend,
        "previous_size": decision.previous_size,
        "next_size": decision.next_size,
        "decision": decision.decision,
        "reason": decision.reason,
        "cpu_p95": decision.cpu_p95,
        "memory_p95": decision.memory_p95,
        "sample_count": decision.sample_count,
        "memory_peak_bytes": decision.memory_peak_bytes,
        "trigger": decision.trigger,
        "trigger_count": decision.trigger_count,
        "previous_counter": decision.previous_counter,
        "current_counter": decision.current_counter,
        "memory_high_bytes": decision.memory_high_bytes,
        "memory_max_bytes": decision.memory_max_bytes,
    }
