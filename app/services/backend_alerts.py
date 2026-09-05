from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.app_diagnostics import (
    collect_app_backend_diagnostics,
    collect_app_diagnostics_snapshot,
)
from app.services.app_fix import fix_app_backend
from app.services.control_events import emit_control_event
from app.services.host_state import (
    LockedStateSectionTransaction,
    host_state_lock_path,
)
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    cancel_pushover_emergency_async,
    pushover_is_configured,
    send_pushover_notification_async,
    summarize_list,
)
from app.services.renderers import container_name

logger_name = "backend.alerts"
logger = get_logger(logger_name)
HOST_STATE_SECTION = "backend_alerts"
BACKEND_ALERTS_RUN_LEASE_SEC = 900
AUTO_FIX_SAFE_DIAGNOSES = {
    "sandbox_missing",
    "sandbox_publish_broken",
}
AUTO_FIX_SAFE_ISSUES = {
    "bootstrap_failed",
    "bootstrap_stuck",
    "bridge_dns_enabled",
    "container_missing",
    "legacy_direct_podman_runtime_owner",
    "legacy_loopback_proxy_present",
    "loopback_publish_missing",
    "missing_saved_spec",
    "mounted_storage_orphan",
    "network_missing",
    "network_orphan",
    "private_ip_missing",
    "stale_name_registration",
    "storage_orphan",
}
AUTO_FIX_IGNORED_ISSUES = {
    "guest_system_unready",
}
EXEC_RECOVERY_SAFE_ISSUES = {
    "loopback_unreachable",
    "podman_exec_unavailable",
    "private_unreachable",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _parse_iso(value: object) -> datetime | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _backend_alerts_run_active(state: dict[str, Any], *, now: datetime) -> bool:
    running = state.get("running")
    if not isinstance(running, dict):
        return False
    started_at = _parse_iso(running.get("started_at"))
    if started_at is None:
        return False
    return (now - started_at).total_seconds() < BACKEND_ALERTS_RUN_LEASE_SEC


def _skipped_payload(
    *,
    now_iso: str,
    notifications_configured: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "checked_at": now_iso,
        "backend_count": 0,
        "healthy_count": 0,
        "unhealthy_count": 0,
        "auto_fix_attempt_count": 0,
        "auto_fix_success_count": 0,
        "auto_fix_failure_count": 0,
        "notifications_configured": notifications_configured,
        "notifications": [],
        "backends": [],
        "ok": True,
        "skipped": True,
        "reason": reason,
    }


def _clear_backend_alerts_running_marker(
    settings: Settings, *, started_at: str
) -> None:
    try:
        with _backend_alerts_state_transaction(settings) as transaction:
            state = dict(transaction.value)
            running = state.get("running")
            if (
                isinstance(running, dict)
                and str(running.get("started_at") or "") == started_at
            ):
                state.pop("running", None)
                transaction.replace(state)
    except OSError:
        logger.warning(
            "backend_alerts.check.running_marker_clear_failed",
            path=str(settings.host_state_path),
        )


def backend_alerts_lock_path(path: Path) -> Path:
    return host_state_lock_path(path)


def _log_backend_alerts_state_written(path: Path, payload: dict[str, Any]) -> None:
    logger.debug(
        "backend_alerts.state.written",
        path=str(path),
        backend_count=len(payload.get("backends") or {})
        if isinstance(payload.get("backends"), dict)
        else 0,
    )


def _backend_alerts_state_transaction(
    settings: Settings,
    *,
    blocking: bool = True,
) -> LockedStateSectionTransaction:
    return LockedStateSectionTransaction(
        settings.host_state_path,
        section=HOST_STATE_SECTION,
        blocking=blocking,
    )


def read_backend_alerts_state(settings: Settings) -> dict[str, Any]:
    with _backend_alerts_state_transaction(settings) as transaction:
        payload = dict(transaction.value)
        if not isinstance(payload.get("backends"), dict):
            logger.warning(
                "backend_alerts.state.invalid_backends",
                path=str(settings.host_state_path),
            )
            payload["backends"] = {}
            transaction.replace(payload)
        return payload


def write_backend_alerts_state(settings: Settings, payload: dict[str, Any]) -> None:
    with _backend_alerts_state_transaction(settings) as transaction:
        transaction.replace(payload)
        _log_backend_alerts_state_written(settings.host_state_path, payload)


def _enabled_hostnames(backend: Backend) -> list[str]:
    return sorted(
        str(item.hostname)
        for item in getattr(backend, "inputs", [])
        if bool(getattr(item, "enabled", False))
        and str(getattr(item, "hostname", "")).strip()
    )


def _issue_signature(diagnostics: dict[str, Any]) -> str:
    issues = diagnostics.get("issues")
    if not isinstance(issues, list):
        return ""
    return ",".join(sorted(str(item).strip() for item in issues if str(item).strip()))


def _issues_list(diagnostics: dict[str, Any]) -> list[str]:
    issues = diagnostics.get("issues")
    if not isinstance(issues, list):
        return []
    return [str(item).strip() for item in issues if str(item).strip()]


def _maintenance_issues_list(diagnostics: dict[str, Any]) -> list[str]:
    issues = diagnostics.get("maintenance_issues")
    if not isinstance(issues, list):
        return []
    return [str(item).strip() for item in issues if str(item).strip()]


def _compact_cgroup_evidence(diagnostics: dict[str, Any]) -> dict[str, Any]:
    cgroup = diagnostics.get("cgroup")
    cgroup = cgroup if isinstance(cgroup, dict) else {}
    memory = cgroup.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    pressure = memory.get("pressure")
    pressure = pressure if isinstance(pressure, dict) else {}
    pids = cgroup.get("pids")
    pids = pids if isinstance(pids, dict) else {}
    return {
        "memory_current": memory.get("current"),
        "memory_max": memory.get("max"),
        "memory_events": memory.get("events")
        if isinstance(memory.get("events"), dict)
        else {},
        "memory_pressure_avg10": {
            key: sample.get("avg10")
            for key, sample in pressure.items()
            if key in {"some", "full"} and isinstance(sample, dict)
        },
        "pids_current": pids.get("current"),
        "pids_max": pids.get("max"),
        "pids_events": pids.get("events")
        if isinstance(pids.get("events"), dict)
        else {},
    }


def _auto_fix_issue_signature(diagnostics: dict[str, Any]) -> str:
    issues = set(_issues_list(diagnostics))
    issues.update(_maintenance_issues_list(diagnostics))
    issues -= AUTO_FIX_IGNORED_ISSUES
    return ",".join(sorted(issues))


def _auto_fix_eligibility(diagnostics: dict[str, Any]) -> dict[str, Any]:
    diagnosis = str(diagnostics.get("diagnosis") or "").strip()
    issues = _issues_list(diagnostics)
    maintenance_issues = _maintenance_issues_list(diagnostics)
    considered_issues = sorted(
        (set(issues) | set(maintenance_issues)) - AUTO_FIX_IGNORED_ISSUES
    )
    payload = {
        "eligible": False,
        "reason": "",
        "diagnosis": diagnosis,
        "issues": issues,
        "maintenance_issues": maintenance_issues,
        "considered_issues": considered_issues,
        "issue_signature": _auto_fix_issue_signature(diagnostics),
    }
    if bool(diagnostics.get("ok")) and not considered_issues:
        payload["reason"] = "healthy"
        return payload
    if bool(diagnostics.get("ok")):
        if not set(considered_issues) <= AUTO_FIX_SAFE_ISSUES:
            payload["reason"] = "maintenance_issues_not_auto_fix_safe"
            return payload
        payload["eligible"] = True
        payload["reason"] = "eligible"
        return payload
    if diagnosis == "backend_exec_unavailable":
        if not bool(diagnostics.get("container_exists")):
            payload["reason"] = "exec_recovery_container_unavailable"
            return payload
        if str(diagnostics.get("backend_exec_status") or "") not in {
            "circuit_open",
            "failed",
            "timeout",
        }:
            payload["reason"] = "exec_recovery_status_not_safe"
            return payload
        considered_issue_set = set(considered_issues)
        if (
            "podman_exec_unavailable" not in considered_issue_set
            or not considered_issue_set <= EXEC_RECOVERY_SAFE_ISSUES
        ):
            payload["reason"] = "exec_recovery_issues_not_safe"
            return payload
        payload["eligible"] = True
        payload["reason"] = "eligible"
        return payload
    if diagnosis not in AUTO_FIX_SAFE_DIAGNOSES:
        payload["reason"] = f"diagnosis_{diagnosis or 'unknown'}_not_auto_fix_safe"
        return payload
    if not considered_issues:
        payload["reason"] = "no_auto_fixable_issues"
        return payload
    if not set(considered_issues) <= AUTO_FIX_SAFE_ISSUES:
        payload["reason"] = "issues_not_auto_fix_safe"
        return payload
    payload["eligible"] = True
    payload["reason"] = "eligible"
    return payload


def _exec_recovery_grace_remaining(
    previous: dict[str, Any],
    *,
    issue_signature: str,
    now: datetime,
    settings: Settings,
) -> int:
    grace_sec = max(0, settings.backend_alert_grace_period_sec)
    if grace_sec <= 0:
        return 0
    if str(previous.get("last_issue_signature") or "") != issue_signature:
        return grace_sec
    first_unhealthy_at = _parse_iso(previous.get("first_unhealthy_at"))
    if first_unhealthy_at is None:
        return grace_sec
    elapsed = max(0, int((now - first_unhealthy_at).total_seconds()))
    return max(0, grace_sec - elapsed)


def _auto_fix_cooldown_remaining(
    previous: dict[str, Any],
    *,
    issue_signature: str,
    now: datetime,
    settings: Settings,
) -> int:
    if settings.backend_auto_fix_cooldown_sec <= 0:
        return 0
    if (
        str(previous.get("last_auto_fix_issue_signature") or "").strip()
        != issue_signature
    ):
        return 0
    last_attempt_at = _parse_iso(previous.get("last_auto_fix_attempt_at"))
    if last_attempt_at is None:
        return 0
    elapsed = max(0, int((now - last_attempt_at).total_seconds()))
    return max(0, settings.backend_auto_fix_cooldown_sec - elapsed)


async def _maybe_auto_fix_backend(
    *,
    backend: Backend,
    diagnostics: dict[str, Any],
    previous: dict[str, Any],
    enabled_app_backends: list[Backend],
    settings: Settings,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    eligibility = _auto_fix_eligibility(diagnostics)
    payload: dict[str, Any] = {
        "enabled": bool(settings.backend_auto_fix_enabled),
        "eligible": bool(eligibility.get("eligible")),
        "attempted": False,
        "reason": str(eligibility.get("reason") or ""),
        "diagnosis": str(eligibility.get("diagnosis") or ""),
        "issues": list(eligibility.get("issues") or []),
        "maintenance_issues": list(eligibility.get("maintenance_issues") or []),
        "considered_issues": list(eligibility.get("considered_issues") or []),
        "issue_signature": str(eligibility.get("issue_signature") or ""),
        "cooldown_remaining_sec": 0,
        "outcome": "skipped",
        "changed": False,
        "failure": None,
    }
    if not settings.backend_auto_fix_enabled:
        payload["reason"] = "auto_fix_disabled"
        return payload, diagnostics
    if not payload["eligible"]:
        return payload, diagnostics

    if payload["diagnosis"] == "backend_exec_unavailable":
        recovery_grace_remaining_sec = _exec_recovery_grace_remaining(
            previous,
            issue_signature=payload["issue_signature"],
            now=now,
            settings=settings,
        )
        payload["recovery_grace_remaining_sec"] = recovery_grace_remaining_sec
        if recovery_grace_remaining_sec > 0:
            payload["reason"] = "exec_recovery_grace_active"
            return payload, diagnostics

    cooldown_remaining_sec = _auto_fix_cooldown_remaining(
        previous,
        issue_signature=payload["issue_signature"],
        now=now,
        settings=settings,
    )
    payload["cooldown_remaining_sec"] = cooldown_remaining_sec
    if cooldown_remaining_sec > 0:
        payload["reason"] = "cooldown_active"
        return payload, diagnostics

    payload["attempted"] = True
    logger.info(
        "backend_alerts.auto_fix.attempting",
        backend_id=backend.id,
        backend=backend.name,
        container=container_name(backend.name),
        diagnosis=payload["diagnosis"],
        issues=payload["considered_issues"],
        issue_signature=payload["issue_signature"],
    )
    try:
        fix_payload = await fix_app_backend(
            backend,
            settings,
            enabled_app_backends=enabled_app_backends,
            record_event=False,
        )
    except Exception as exc:
        payload["outcome"] = "failed"
        payload["reason"] = "auto_fix_exception"
        payload["failure"] = {"error": str(exc), "phase": "auto_fix"}
        logger.warning(
            "backend_alerts.auto_fix.failed",
            backend_id=backend.id,
            backend=backend.name,
            container=container_name(backend.name),
            diagnosis=payload["diagnosis"],
            issues=payload["considered_issues"],
            error=str(exc),
        )
        await send_pushover_notification_async(
            settings,
            title=f"CNC backend auto-fix failed: {backend.name}",
            message=build_operator_message(
                "Backend auto-fix raised an exception.",
                status="failure",
                facts=[
                    ("backend", backend.name),
                    ("diagnosis", payload["diagnosis"]),
                    ("issues", payload["considered_issues"]),
                    ("error", str(exc)),
                ],
                action=f"cnc-admin app doctor {backend.name}",
            ),
            priority=0,
            event="backend_auto_fix_failed",
            source=logger_name,
            backend=backend.name,
            url=admin_dashboard_url(settings, path=f"/outputs/{backend.id}"),
            url_title=f"Open {backend.name}",
        )
        return payload, diagnostics

    payload["changed"] = bool(fix_payload.get("changed"))
    payload["failure"] = (
        fix_payload.get("failure")
        if isinstance(fix_payload.get("failure"), dict)
        else None
    )
    payload["operation_id"] = fix_payload.get("operation_id")
    payload["repair_plan"] = (
        fix_payload.get("repair_plan")
        if isinstance(fix_payload.get("repair_plan"), list)
        else []
    )
    payload["repair_actions"] = (
        fix_payload.get("repair_actions")
        if isinstance(fix_payload.get("repair_actions"), list)
        else []
    )
    payload["pre_recovery_evidence"] = (
        fix_payload.get("pre_recovery_evidence")
        if isinstance(fix_payload.get("pre_recovery_evidence"), dict)
        else {}
    )
    after = (
        fix_payload.get("after")
        if isinstance(fix_payload.get("after"), dict)
        else diagnostics
    )
    payload["outcome"] = "recovered" if bool(fix_payload.get("ok")) else "failed"
    payload["reason"] = (
        "auto_fix_recovered" if bool(fix_payload.get("ok")) else "auto_fix_failed"
    )
    logger.info(
        "backend_alerts.auto_fix.completed",
        backend_id=backend.id,
        backend=backend.name,
        container=container_name(backend.name),
        diagnosis=payload["diagnosis"],
        issues=payload["considered_issues"],
        issue_signature=payload["issue_signature"],
        operation_id=payload["operation_id"],
        changed=payload["changed"],
        outcome=payload["outcome"],
        repair_plan=payload["repair_plan"],
        repair_actions=payload["repair_actions"],
        after_issues=_issues_list(after),
        failure=payload["failure"],
        pre_recovery_evidence=payload["pre_recovery_evidence"],
    )
    await emit_control_event(
        settings,
        kind="backend_auto_fix_recovered"
        if payload["outcome"] == "recovered"
        else "backend_auto_fix_failed",
        source=logger_name,
        summary="Backend auto-fix recovered"
        if payload["outcome"] == "recovered"
        else "Backend auto-fix failed",
        severity="success" if payload["outcome"] == "recovered" else "error",
        scope="backend",
        backend_name=backend.name,
        subevents=[
            {"label": "diagnosis", "value": payload["diagnosis"] or "unknown"},
            {
                "label": "issues",
                "value": (
                    ", ".join(str(item) for item in payload["considered_issues"])
                    if payload["considered_issues"]
                    else "none"
                ),
            },
            {"label": "changed", "value": "yes" if payload["changed"] else "no"},
        ],
        details={
            "backend": backend.name,
            "diagnosis": payload["diagnosis"],
            "issues": payload["issues"],
            "maintenance_issues": payload["maintenance_issues"],
            "considered_issues": payload["considered_issues"],
            "outcome": payload["outcome"],
            "failure": payload["failure"],
            "operation_id": payload["operation_id"],
            "repair_plan": payload["repair_plan"],
            "repair_actions": payload["repair_actions"],
            "pre_recovery_evidence": payload["pre_recovery_evidence"],
        },
    )
    if payload["outcome"] == "failed":
        failure = payload["failure"] if isinstance(payload["failure"], dict) else {}
        await send_pushover_notification_async(
            settings,
            title=f"CNC backend auto-fix failed: {backend.name}",
            message=build_operator_message(
                "Backend auto-fix completed but did not recover the output.",
                status="failure",
                facts=[
                    ("backend", backend.name),
                    ("diagnosis", payload["diagnosis"]),
                    ("issues", payload["considered_issues"]),
                    ("error", failure.get("error") or payload["reason"]),
                ],
                action=f"cnc-admin app doctor {backend.name}",
            ),
            priority=0,
            event="backend_auto_fix_failed",
            source=logger_name,
            backend=backend.name,
            url=admin_dashboard_url(settings, path=f"/outputs/{backend.id}"),
            url_title=f"Open {backend.name}",
        )
    return payload, after


def _format_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, remaining = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if remaining or not parts:
        parts.append(f"{remaining}s")
    return " ".join(parts[:3])


def _problem_summary(diagnostics: dict[str, Any]) -> str:
    issues = {str(item).strip() for item in diagnostics.get("issues") or []}
    bootstrap = diagnostics.get("bootstrap")
    bootstrap_status = (
        str(bootstrap.get("status") or "").strip()
        if isinstance(bootstrap, dict)
        else ""
    )
    if bootstrap_status == "failed":
        return "bootstrap failed"
    if bootstrap_status == "stuck":
        return "bootstrap is stuck"
    if "container_missing" in issues or "private_ip_missing" in issues:
        return "container is not published on the private path"
    if "loopback_unreachable" in issues:
        return "loopback healthcheck is failing"
    if "private_unreachable" in issues:
        return "private healthcheck is failing"
    if issues:
        return summarize_list(sorted(issues), limit=2)
    return ""


def _diagnostics_failure_payload(backend: Backend, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "issues": ["diagnostics_failed"],
        "maintenance_issues": [],
        "diagnosis": "diagnostics_failed",
        "bootstrap": {"status": "unknown"},
        "private_ip": "",
        "loopback_port": backend.port,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _down_message(
    *,
    backend: Backend,
    hostnames: list[str],
    diagnostics: dict[str, Any],
    down_for_seconds: int,
    reminder: bool,
) -> str:
    issues = diagnostics.get("issues")
    issue_list = [str(item) for item in issues] if isinstance(issues, list) else []
    bootstrap = diagnostics.get("bootstrap")
    bootstrap_status = ""
    if isinstance(bootstrap, dict):
        bootstrap_status = str(bootstrap.get("status") or "").strip()
    private_ip = str(diagnostics.get("private_ip") or "").strip()
    loopback_port = diagnostics.get("loopback_port")
    return build_operator_message(
        "Backend is still down." if reminder else "Backend is down.",
        status="failure",
        facts=[
            ("backend", backend.name),
            ("routes", summarize_list(hostnames, limit=3) if hostnames else ""),
            ("problem", _problem_summary(diagnostics)),
            ("issues", summarize_list(issue_list, limit=4) if issue_list else ""),
            (
                "bootstrap",
                bootstrap_status
                if bootstrap_status and bootstrap_status != "succeeded"
                else "",
            ),
            ("private_ip", private_ip),
            ("loopback_port", loopback_port if loopback_port is not None else ""),
            ("down_for", _format_duration(down_for_seconds)),
        ],
        action=f"cnc-admin app doctor {backend.name}",
    )


def _recovery_message(
    *,
    backend: Backend,
    hostnames: list[str],
    recovered_after_seconds: int,
) -> str:
    return build_operator_message(
        "Backend recovered.",
        status="success",
        facts=[
            ("backend", backend.name),
            ("routes", summarize_list(hostnames, limit=3) if hostnames else ""),
            ("recovered_after", _format_duration(recovered_after_seconds)),
        ],
        action=f"cnc-admin app doctor {backend.name}",
    )


async def _load_enabled_app_backends() -> list[Backend]:
    from app.database import SessionLocal

    async with SessionLocal() as session:
        result = await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.kind == "app", Backend.enabled.is_(True))
            .order_by(Backend.name.asc())
        )
        return list(result.scalars().all())


async def _load_backend_by_name(backend_name: str) -> Backend | None:
    from app.database import SessionLocal

    async with SessionLocal() as session:
        result = await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.name == backend_name)
            .limit(1)
        )
        return result.scalar_one_or_none()


async def run_backend_alerts_check(settings: Settings) -> dict[str, Any]:
    now = _utc_now()
    now_iso = now.isoformat(timespec="seconds")
    notifications_configured = pushover_is_configured(settings)
    try:
        with _backend_alerts_state_transaction(
            settings, blocking=False
        ) as state_transaction:
            state = dict(state_transaction.value)
            if _backend_alerts_run_active(state, now=now):
                logger.info("backend_alerts.check.skipped", reason="already_running")
                return _skipped_payload(
                    now_iso=now_iso,
                    notifications_configured=notifications_configured,
                    reason="already_running",
                )
            previous_backends = (
                state.get("backends") if isinstance(state.get("backends"), dict) else {}
            )
            state["running"] = {"started_at": now_iso}
            state_transaction.replace(state)
    except BlockingIOError:
        logger.info("backend_alerts.check.skipped", reason="already_running")
        return _skipped_payload(
            now_iso=now_iso,
            notifications_configured=notifications_configured,
            reason="already_running",
        )

    try:
        backends = await _load_enabled_app_backends()
        logger.info(
            "backend_alerts.check.started",
            backend_count=len(backends),
            notifications_configured=notifications_configured,
        )
        diagnostics_snapshot = None
        diagnostics_snapshot_error = ""
        if backends:
            try:
                diagnostics_snapshot = await asyncio.to_thread(
                    collect_app_diagnostics_snapshot, settings
                )
            except Exception as exc:
                diagnostics_snapshot_error = str(exc)
                logger.exception(
                    "backend_alerts.diagnostics_snapshot.failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        diagnostics_semaphore = asyncio.Semaphore(4)

        async def collect_bounded_diagnostics(backend: Backend) -> dict[str, Any]:
            async with diagnostics_semaphore:
                try:
                    parameters = signature(collect_app_backend_diagnostics).parameters
                    if "snapshot" not in parameters:
                        return await asyncio.to_thread(
                            collect_app_backend_diagnostics, backend, settings
                        )
                    return await asyncio.to_thread(
                        collect_app_backend_diagnostics,
                        backend,
                        settings,
                        snapshot=diagnostics_snapshot,
                    )
                except Exception as exc:
                    logger.exception(
                        "backend_alerts.diagnostics.failed",
                        backend=backend.name,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    return _diagnostics_failure_payload(backend, exc)

        diagnostics_payloads = await asyncio.gather(
            *[collect_bounded_diagnostics(backend) for backend in backends]
        )
        all_backends_down = bool(backends) and all(
            not diagnostics.get("observation_deferred")
            and not bool(diagnostics.get("ok"))
            for diagnostics in diagnostics_payloads
        )
        if not all_backends_down:
            await cancel_pushover_emergency_async(
                settings, emergency_key="all_backends_down"
            )

        notifications: list[dict[str, Any]] = []
        pending_notifications: list[dict[str, Any]] = []
        backend_payloads: list[dict[str, Any]] = []
        next_state: dict[str, Any] = {"checked_at": now_iso, "backends": {}}
        auto_fix_attempt_count = 0
        auto_fix_success_count = 0
        auto_fix_failure_count = 0

        for backend, diagnostics in zip(backends, diagnostics_payloads, strict=True):
            hostnames = _enabled_hostnames(backend)
            previous_entry = previous_backends.get(backend.name)
            previous = previous_entry if isinstance(previous_entry, dict) else {}
            previous_status = str(previous.get("status") or "").strip().lower()
            auto_fix, diagnostics = await _maybe_auto_fix_backend(
                backend=backend,
                diagnostics=diagnostics,
                previous=previous,
                enabled_app_backends=backends,
                settings=settings,
                now=now,
            )
            if auto_fix.get("attempted"):
                auto_fix_attempt_count += 1
                if auto_fix.get("outcome") == "recovered":
                    auto_fix_success_count += 1
                else:
                    auto_fix_failure_count += 1
            if diagnostics.get("observation_deferred"):
                observation_issues = (
                    diagnostics.get("observation_issues")
                    if isinstance(diagnostics.get("observation_issues"), list)
                    else []
                )
                preserved_status = str(previous.get("status") or "unknown").lower()
                if preserved_status not in {"up", "down"}:
                    preserved_status = "unknown"
                preserved_state = dict(previous)
                preserved_state.update(
                    {
                        "status": preserved_status,
                        "checked_at": now_iso,
                        "hostnames": hostnames,
                        "observation_deferred": True,
                        "observation_issues": observation_issues,
                    }
                )
                next_state["backends"][backend.name] = preserved_state
                backend_payloads.append(
                    {
                        "backend": backend.name,
                        "hostnames": hostnames,
                        "ok": None,
                        "issues": diagnostics.get("issues")
                        if isinstance(diagnostics.get("issues"), list)
                        else [],
                        "maintenance_issues": diagnostics.get("maintenance_issues")
                        if isinstance(diagnostics.get("maintenance_issues"), list)
                        else [],
                        "observation_issues": observation_issues,
                        "status": "unknown",
                        "down_for_seconds": 0,
                        "alert_grace_remaining_sec": 0,
                        "notified": False,
                        "notification_attempted": False,
                        "notification_kind": "",
                        "notification_reason": "observation_deferred",
                        "notification_configured": notifications_configured,
                        "previous_status": previous_status or "unknown",
                        "auto_fix": auto_fix,
                    }
                )
                if (
                    not previous.get("observation_deferred")
                    or previous.get("observation_issues") != observation_issues
                ):
                    log_observation = (
                        logger.warning
                        if diagnostics.get("backend_exec_status") == "guard_unavailable"
                        else logger.info
                    )
                    log_observation(
                        "backend_alerts.backend.observation_deferred",
                        backend_id=backend.id,
                        backend=backend.name,
                        container=container_name(backend.name),
                        diagnosis=diagnostics.get("diagnosis"),
                        backend_exec_status=diagnostics.get("backend_exec_status"),
                        previous_status=previous_status or "unknown",
                        observation_issues=observation_issues,
                        cgroup=_compact_cgroup_evidence(diagnostics),
                    )
                continue
            if previous.get("observation_deferred"):
                logger.info(
                    "backend_alerts.backend.observation_resumed",
                    backend_id=backend.id,
                    backend=backend.name,
                    container=container_name(backend.name),
                    diagnosis=diagnostics.get("diagnosis"),
                    backend_exec_status=diagnostics.get("backend_exec_status"),
                    previous_observation_issues=previous.get("observation_issues")
                    or [],
                )
            ok = bool(diagnostics.get("ok"))
            first_unhealthy_at = _parse_iso(previous.get("first_unhealthy_at"))
            last_notified_at = _parse_iso(previous.get("last_notified_at"))
            notified_down_at = _parse_iso(previous.get("notified_down_at"))
            last_notification_attempt_at = _parse_iso(
                previous.get("last_notification_attempt_at")
            )
            issue_signature = _issue_signature(diagnostics)
            down_for_seconds = 0
            grace_remaining_sec = 0
            notification_kind = ""
            notification_sent = False
            notification_attempted = False
            notification_reason = ""

            if ok:
                recovered_after_seconds = 0
                if first_unhealthy_at is not None:
                    recovered_after_seconds = max(
                        0, int((now - first_unhealthy_at).total_seconds())
                    )
                if previous_status == "down":
                    await emit_control_event(
                        settings,
                        kind="backend_recovered",
                        source=logger_name,
                        summary="Backend recovered",
                        severity="success",
                        scope="backend",
                        backend_name=backend.name,
                        subevents=[
                            {
                                "label": "recovered_after",
                                "value": _format_duration(recovered_after_seconds),
                            },
                            {
                                "label": "routes",
                                "value": summarize_list(hostnames, limit=3)
                                if hostnames
                                else "-",
                            },
                        ],
                        details={
                            "backend_id": backend.id,
                            "backend": backend.name,
                            "container": container_name(backend.name),
                            "recovered_after_seconds": recovered_after_seconds,
                        },
                    )
                if notified_down_at is not None:
                    notification_kind = "recovered"
                    notification_reason = "recovered_after_alert"
                    notification_attempted = notifications_configured
                    pending_notifications.append(
                        {
                            "backend": backend.name,
                            "hostnames": hostnames,
                            "kind": notification_kind,
                            "title": f"CNC backend recovered: {backend.name}",
                            "message": _recovery_message(
                                backend=backend,
                                hostnames=hostnames,
                                recovered_after_seconds=recovered_after_seconds,
                            ),
                            "priority": 0,
                            "event": "backend_recovered",
                            "url": admin_dashboard_url(
                                settings, path=f"/outputs/{backend.id}"
                            ),
                            "url_title": f"Open {backend.name}",
                        }
                    )
                else:
                    notification_reason = "healthy"
                next_state["backends"][backend.name] = {
                    "status": "up",
                    "checked_at": now_iso,
                    "hostnames": hostnames,
                    "issues": [],
                    "first_unhealthy_at": None,
                    "last_healthy_at": now_iso,
                    "last_issue_signature": "",
                    "last_notification_kind": notification_kind
                    or previous.get("last_notification_kind")
                    or "",
                    "last_notified_at": previous.get("last_notified_at") or "",
                    "last_notification_attempt_at": (
                        now_iso
                        if notification_attempted
                        else (previous.get("last_notification_attempt_at") or "")
                    ),
                    "notified_down_at": None,
                    "last_auto_fix_attempt_at": (
                        now_iso
                        if auto_fix.get("attempted")
                        else (previous.get("last_auto_fix_attempt_at") or "")
                    ),
                    "last_auto_fix_issue_signature": (
                        auto_fix.get("issue_signature")
                        or previous.get("last_auto_fix_issue_signature")
                        or ""
                    ),
                    "last_auto_fix_outcome": (
                        auto_fix.get("outcome")
                        or previous.get("last_auto_fix_outcome")
                        or ""
                    ),
                    "last_auto_fix_reason": (
                        auto_fix.get("reason")
                        or previous.get("last_auto_fix_reason")
                        or ""
                    ),
                }
            else:
                if first_unhealthy_at is None:
                    first_unhealthy_at = now
                down_for_seconds = max(
                    0, int((now - first_unhealthy_at).total_seconds())
                )
                should_send_down = (
                    down_for_seconds >= settings.backend_alert_grace_period_sec
                )
                grace_remaining_sec = max(
                    0, settings.backend_alert_grace_period_sec - down_for_seconds
                )
                if previous_status != "down":
                    await emit_control_event(
                        settings,
                        kind="backend_became_unhealthy",
                        source=logger_name,
                        summary="Backend became unhealthy",
                        severity="warn",
                        scope="backend",
                        backend_name=backend.name,
                        subevents=[
                            {
                                "label": "problem",
                                "value": _problem_summary(diagnostics)
                                or "runtime issue",
                            },
                            {
                                "label": "issues",
                                "value": summarize_list(
                                    _issues_list(diagnostics), limit=4
                                )
                                or "none",
                            },
                        ],
                        details={
                            "backend_id": backend.id,
                            "backend": backend.name,
                            "container": container_name(backend.name),
                            "diagnosis": diagnostics.get("diagnosis"),
                            "backend_exec_status": diagnostics.get(
                                "backend_exec_status"
                            ),
                            "issues": _issues_list(diagnostics),
                            "cgroup": _compact_cgroup_evidence(diagnostics),
                        },
                    )
                retry_delay_sec = max(0, settings.backend_alert_down_retry_sec)
                retry_due = (
                    last_notification_attempt_at is None
                    or retry_delay_sec <= 0
                    or (now - last_notification_attempt_at).total_seconds()
                    >= retry_delay_sec
                )
                if should_send_down and notified_down_at is None and retry_due:
                    notification_kind = "down"
                    notification_reason = "first_down_alert_due"
                elif should_send_down and notified_down_at is None:
                    notification_reason = "down_alert_retry_not_due"
                elif (
                    should_send_down
                    and notified_down_at is not None
                    and settings.backend_alert_repeat_sec > 0
                    and last_notified_at is not None
                    and (now - last_notified_at).total_seconds()
                    >= settings.backend_alert_repeat_sec
                ):
                    notification_kind = "reminder"
                    notification_reason = "reminder_due"
                elif not should_send_down:
                    notification_reason = "within_grace_period"
                elif notified_down_at is None:
                    notification_reason = "down_alert_waiting"
                elif settings.backend_alert_repeat_sec <= 0:
                    notification_reason = "reminders_disabled"
                else:
                    notification_reason = "reminder_not_due"

                if notification_kind:
                    notification_attempted = notifications_configured
                    pending_notifications.append(
                        {
                            "backend": backend.name,
                            "hostnames": hostnames,
                            "kind": notification_kind,
                            "title": (
                                f"CNC backend still down: {backend.name}"
                                if notification_kind == "reminder"
                                else f"CNC backend down: {backend.name}"
                            ),
                            "message": _down_message(
                                backend=backend,
                                hostnames=hostnames,
                                diagnostics=diagnostics,
                                down_for_seconds=down_for_seconds,
                                reminder=notification_kind == "reminder",
                            ),
                            "priority": 1,
                            "event": "backend_reminder"
                            if notification_kind == "reminder"
                            else "backend_down",
                            "url": admin_dashboard_url(
                                settings, path=f"/outputs/{backend.id}"
                            ),
                            "url_title": f"Open {backend.name}",
                        }
                    )
                next_state["backends"][backend.name] = {
                    "status": "down",
                    "checked_at": now_iso,
                    "hostnames": hostnames,
                    "issues": diagnostics.get("issues")
                    if isinstance(diagnostics.get("issues"), list)
                    else [],
                    "first_unhealthy_at": first_unhealthy_at.isoformat(
                        timespec="seconds"
                    ),
                    "last_healthy_at": previous.get("last_healthy_at") or "",
                    "last_issue_signature": issue_signature,
                    "last_notification_kind": notification_kind
                    or previous.get("last_notification_kind")
                    or "",
                    "last_notified_at": previous.get("last_notified_at") or "",
                    "last_notification_attempt_at": (
                        now_iso
                        if notification_attempted
                        else (previous.get("last_notification_attempt_at") or "")
                    ),
                    "notified_down_at": (previous.get("notified_down_at") or ""),
                    "last_auto_fix_attempt_at": (
                        now_iso
                        if auto_fix.get("attempted")
                        else (previous.get("last_auto_fix_attempt_at") or "")
                    ),
                    "last_auto_fix_issue_signature": (
                        auto_fix.get("issue_signature")
                        or previous.get("last_auto_fix_issue_signature")
                        or ""
                    ),
                    "last_auto_fix_outcome": (
                        auto_fix.get("outcome")
                        or previous.get("last_auto_fix_outcome")
                        or ""
                    ),
                    "last_auto_fix_reason": (
                        auto_fix.get("reason")
                        or previous.get("last_auto_fix_reason")
                        or ""
                    ),
                }

            backend_payloads.append(
                {
                    "backend": backend.name,
                    "hostnames": hostnames,
                    "ok": ok,
                    "issues": diagnostics.get("issues")
                    if isinstance(diagnostics.get("issues"), list)
                    else [],
                    "maintenance_issues": (
                        diagnostics.get("maintenance_issues")
                        if isinstance(diagnostics.get("maintenance_issues"), list)
                        else []
                    ),
                    "status": "up" if ok else "down",
                    "down_for_seconds": down_for_seconds if not ok else 0,
                    "alert_grace_remaining_sec": grace_remaining_sec if not ok else 0,
                    "notified": notification_sent,
                    "notification_attempted": notification_attempted,
                    "notification_kind": notification_kind,
                    "notification_reason": notification_reason,
                    "notification_configured": notifications_configured,
                    "previous_status": previous_status or "unknown",
                    "auto_fix": auto_fix,
                }
            )
            logger.debug(
                "backend_alerts.backend.observed",
                backend_id=backend.id,
                backend=backend.name,
                container=container_name(backend.name),
                previous_status=previous_status or "unknown",
                status="up" if ok else "down",
                hostnames=hostnames,
                issues=diagnostics.get("issues")
                if isinstance(diagnostics.get("issues"), list)
                else [],
                issue_signature=issue_signature,
                down_for_seconds=down_for_seconds if not ok else 0,
                grace_remaining_sec=grace_remaining_sec if not ok else 0,
                notification_kind=notification_kind,
                notification_reason=notification_reason,
                notified=notification_sent,
                auto_fix=auto_fix,
            )

        initial_down_notifications = [
            item for item in pending_notifications if item.get("kind") == "down"
        ]
        if len(backends) > 1 and all_backends_down and initial_down_notifications:
            affected_backends = sorted(
                str(item.get("backend") or "")
                for item in initial_down_notifications
                if str(item.get("backend") or "")
            )
            pending_notifications.append(
                {
                    "backend": "",
                    "hostnames": [],
                    "kind": "all_down",
                    "title": "CNC all outputs down",
                    "message": build_operator_message(
                        "Every enabled output is currently unhealthy.",
                        status="failure",
                        facts=[
                            ("outputs", len(affected_backends)),
                            ("affected", summarize_list(affected_backends, limit=5)),
                        ],
                        action="cnc-admin app doctor --all",
                    ),
                    "priority": 2,
                    "event": "all_backends_down",
                    "emergency_key": "all_backends_down",
                    "url": admin_dashboard_url(settings, tab="outputs"),
                    "url_title": "Open CNC outputs",
                }
            )

        with _backend_alerts_state_transaction(settings) as state_transaction:
            final_state = dict(next_state)
            state_transaction.replace(final_state)
        _log_backend_alerts_state_written(settings.host_state_path, next_state)

        sent_notification_updates: list[dict[str, Any]] = []
        for notification in pending_notifications:
            backend_name = str(notification["backend"])
            kind = str(notification["kind"])
            sent = await send_pushover_notification_async(
                settings,
                title=str(notification["title"]),
                message=str(notification["message"]),
                priority=int(notification["priority"]),
                event=str(notification["event"]),
                source=logger_name,
                backend=backend_name,
                url=str(notification["url"] or "") or None,
                url_title=str(notification["url_title"] or "") or None,
                emergency_key=str(notification.get("emergency_key") or ""),
            )
            notification_result = {
                "backend": backend_name,
                "hostnames": notification["hostnames"],
                "kind": kind,
                "sent": sent,
            }
            notifications.append(notification_result)
            for backend_payload in backend_payloads:
                if backend_payload["backend"] == backend_name:
                    backend_payload["notified"] = sent
                    break
            if sent:
                sent_notification_updates.append(notification_result)

        if sent_notification_updates:
            with _backend_alerts_state_transaction(settings) as state_transaction:
                persisted_state = dict(state_transaction.value)
                persisted_backends = persisted_state.get("backends")
                if not isinstance(persisted_backends, dict):
                    persisted_backends = {}
                    persisted_state["backends"] = persisted_backends
                for notification in sent_notification_updates:
                    backend_name = str(notification["backend"])
                    entry = persisted_backends.get(backend_name)
                    if not isinstance(entry, dict):
                        continue
                    kind = str(notification["kind"])
                    entry["last_notified_at"] = now_iso
                    entry["last_notification_kind"] = kind
                    if kind == "down":
                        entry["notified_down_at"] = now_iso
                    persisted_backends[backend_name] = entry
                state_transaction.replace(persisted_state)
            _log_backend_alerts_state_written(settings.host_state_path, persisted_state)
        healthy_count = sum(1 for item in backend_payloads if item["ok"] is True)
        unhealthy_count = sum(1 for item in backend_payloads if item["ok"] is False)
        unknown_count = sum(1 for item in backend_payloads if item["ok"] is None)
        logger.info(
            "backend_alerts.check.completed",
            backend_count=len(backend_payloads),
            unhealthy_count=unhealthy_count,
            notification_count=len(notifications),
            auto_fix_attempt_count=auto_fix_attempt_count,
            auto_fix_success_count=auto_fix_success_count,
            auto_fix_failure_count=auto_fix_failure_count,
        )
        return {
            "checked_at": now_iso,
            "backend_count": len(backend_payloads),
            "healthy_count": healthy_count,
            "unhealthy_count": unhealthy_count,
            "unknown_count": unknown_count,
            "diagnostics_snapshot_error": diagnostics_snapshot_error,
            "auto_fix_attempt_count": auto_fix_attempt_count,
            "auto_fix_success_count": auto_fix_success_count,
            "auto_fix_failure_count": auto_fix_failure_count,
            "notifications_configured": notifications_configured,
            "notifications": notifications,
            "backends": backend_payloads,
            "ok": True,
        }
    except Exception:
        _clear_backend_alerts_running_marker(settings, started_at=now_iso)
        raise


async def send_backend_alert_test_notifications(
    settings: Settings,
    *,
    backend_name: str,
) -> dict[str, Any]:
    logger.info("backend_alerts.test.started", backend=backend_name)
    loaded_backend = await _load_backend_by_name(backend_name)
    hostnames = _enabled_hostnames(loaded_backend) if loaded_backend is not None else []
    if not hostnames:
        hostnames = [f"{backend_name}.example.com"]
    notifications: list[dict[str, Any]] = []
    scenarios = [
        (
            "down",
            f"CNC backend down: {backend_name}",
            _down_message(
                backend=Backend(
                    name=backend_name, kind="app", handoff_port=8337, port=12001
                ),
                hostnames=hostnames,
                diagnostics={
                    "issues": ["private_ip_missing", "loopback_unreachable"],
                    "bootstrap": {"status": "failed"},
                    "private_ip": "",
                    "loopback_port": 12001,
                },
                down_for_seconds=480,
                reminder=False,
            ),
            1,
        ),
        (
            "reminder",
            f"CNC backend still down: {backend_name}",
            _down_message(
                backend=Backend(
                    name=backend_name, kind="app", handoff_port=8337, port=12001
                ),
                hostnames=hostnames,
                diagnostics={
                    "issues": ["private_ip_missing", "loopback_unreachable"],
                    "bootstrap": {"status": "failed"},
                    "private_ip": "",
                    "loopback_port": 12001,
                },
                down_for_seconds=7200,
                reminder=True,
            ),
            1,
        ),
        (
            "recovered",
            f"CNC backend recovered: {backend_name}",
            _recovery_message(
                backend=Backend(
                    name=backend_name, kind="app", handoff_port=8337, port=12001
                ),
                hostnames=hostnames,
                recovered_after_seconds=7260,
            ),
            0,
        ),
    ]

    for kind, title, message, priority in scenarios:
        sent = await send_pushover_notification_async(
            settings,
            title=title,
            message=message,
            priority=priority,
            event=f"backend_test_{kind}",
            source=logger_name,
            backend=backend_name,
        )
        notifications.append({"backend": backend_name, "kind": kind, "sent": sent})

    logger.info(
        "backend_alerts.test.completed",
        backend=backend_name,
        notification_count=len(notifications),
    )
    return {
        "backend": backend_name,
        "notifications_configured": pushover_is_configured(settings),
        "notifications": notifications,
        "ok": all(bool(item["sent"]) for item in notifications),
    }
