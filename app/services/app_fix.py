from __future__ import annotations

import asyncio
from typing import Any, Sequence

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.app_repair import (
    OBSERVATION_ONLY_DIAGNOSES,
    plan_app_backend_repair,
    repair_app_backend,
)
from app.services.app_runtime import AppRuntimeReconciler
from app.services.apply_core import ApplyFailed, AppRuntimeReconcileResult
from app.services.control_events import emit_control_event
from app.services.operations import host_mutation_operation
from app.services.resource_profile import build_resource_profile
from app.services.runtime_services import (
    AppRuntimeServices,
    default_app_runtime_services,
)


logger = get_logger("app.repair")


async def _collect_after_diagnostics(
    backend: Backend, settings: Settings, *, should_wait: bool
) -> dict[str, Any]:
    diagnostics = await asyncio.to_thread(
        collect_app_backend_diagnostics, backend, settings
    )
    if not should_wait or bool(diagnostics.get("ok")):
        return diagnostics

    deadline = asyncio.get_running_loop().time() + min(
        30, max(5, settings.command_timeout_status_sec * 6)
    )
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2)
        diagnostics = await asyncio.to_thread(
            collect_app_backend_diagnostics, backend, settings
        )
        if bool(diagnostics.get("ok")):
            return diagnostics
    return diagnostics


def _serialize_reconcile_result(result: AppRuntimeReconcileResult) -> dict[str, Any]:
    return {
        "backend": result.backend,
        "container": result.container,
        "created": bool(result.created),
        "recreated": bool(result.recreated),
        "bootstrapped": bool(result.bootstrapped),
        "target_ip": result.target_ip,
        "dns_servers": list(result.dns_servers or []),
        "phases": result.phases or {},
    }


def _fix_changed(
    cleanup: dict[str, Any],
    reconcile: AppRuntimeReconcileResult | None,
) -> bool:
    if cleanup.get("changed"):
        return True
    if reconcile is None:
        return False
    if reconcile.created or reconcile.recreated or reconcile.bootstrapped:
        return True
    phases = reconcile.phases or {}
    create_details = (
        phases.get("create", {}).get("details")
        if isinstance(phases.get("create"), dict)
        else {}
    )
    if isinstance(create_details, dict):
        if create_details.get("container_started"):
            return True
        guest_rootfs = create_details.get("guest_rootfs")
        if isinstance(guest_rootfs, dict) and guest_rootfs.get("seeded"):
            return True
    return False


def _compact_repair_evidence(diagnostics: dict[str, Any]) -> dict[str, Any]:
    cgroup = diagnostics.get("cgroup")
    cgroup = cgroup if isinstance(cgroup, dict) else {}
    memory = cgroup.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    pids = cgroup.get("pids")
    pids = pids if isinstance(pids, dict) else {}
    circuit = diagnostics.get("backend_exec_circuit")
    circuit = circuit if isinstance(circuit, dict) else {}
    exec_available = bool(diagnostics.get("backend_exec_available"))
    circuit_open = bool(circuit.get("open"))
    exec_status = str(diagnostics.get("backend_exec_status") or "").strip().lower()
    if not exec_status:
        exec_status = (
            "available"
            if exec_available
            else ("circuit_open" if circuit_open else "unavailable")
        )
    return {
        "diagnosis": str(diagnostics.get("diagnosis") or "unknown"),
        "exec": {
            "available": exec_available,
            "status": exec_status,
            "error": str(diagnostics.get("backend_exec_error") or "")[:2000],
            "circuit": {
                "open": circuit_open,
                **{
                    key: circuit[key]
                    for key in ("opened_at", "expires_at", "timeout_sec")
                    if key in circuit
                },
            },
        },
        "memory": {
            "current": memory.get("current"),
            "max": memory.get("max"),
            "events": memory.get("events")
            if isinstance(memory.get("events"), dict)
            else {},
            "pressure": memory.get("pressure")
            if isinstance(memory.get("pressure"), dict)
            else {},
        },
        "pids": {
            "current": pids.get("current"),
            "max": pids.get("max"),
            "events": pids.get("events")
            if isinstance(pids.get("events"), dict)
            else {},
        },
    }


async def fix_app_backend(
    backend: Backend,
    settings: Settings,
    *,
    enabled_app_backends: Sequence[Backend],
    services: AppRuntimeServices | None = None,
    record_event: bool = True,
) -> dict[str, Any]:
    if str(backend.kind or "").lower() != "app":
        raise ValueError(f"backend {backend.name} is not an app backend")
    if not backend.enabled:
        raise ValueError(f"backend {backend.name} is disabled")

    async with host_mutation_operation(
        settings,
        kind="repair_backend",
        backend_id=backend.id,
        phase="diagnose",
        details={"backend": backend.name},
    ) as operation:
        services = services or default_app_runtime_services()
        before = await asyncio.to_thread(
            collect_app_backend_diagnostics, backend, settings
        )
        diagnosis = str(before.get("diagnosis") or "unknown")
        pre_recovery_evidence = _compact_repair_evidence(before)
        await operation.update(
            phase="plan",
            details={
                "diagnosis": diagnosis,
                "pre_recovery_evidence": pre_recovery_evidence,
            },
        )
        repair_plan = plan_app_backend_repair(before)
        runtime_restart_planned = any(
            step.get("action") == "restart_quadlet" for step in repair_plan
        )
        await operation.update(
            phase="recover",
            details={"repair_plan": repair_plan},
        )
        logger.info(
            "app.repair.planned",
            operation_id=operation.id,
            backend_id=backend.id,
            backend=backend.name,
            container=str(before.get("container") or ""),
            diagnosis=diagnosis,
            exec_status=pre_recovery_evidence["exec"]["status"],
            exec_circuit=pre_recovery_evidence["exec"]["circuit"],
            repair_plan=repair_plan,
            memory=pre_recovery_evidence["memory"],
            pids=pre_recovery_evidence["pids"],
        )
        cleanup = await asyncio.to_thread(
            repair_app_backend,
            backend,
            settings,
            before=before,
            plan=repair_plan,
        )
        host_recovery_only = (
            diagnosis == "backend_exec_unavailable"
            or diagnosis in OBSERVATION_ONLY_DIAGNOSES
        )

        publish: dict[str, Any] | None = None
        verify: dict[str, Any] | None = None
        failure: dict[str, Any] | None = None
        reconcile_state: AppRuntimeReconcileResult | None = None
        cleanup_errors = (
            cleanup.get("errors") if isinstance(cleanup.get("errors"), list) else []
        )
        if cleanup_errors:
            failure = {"error": str(cleanup_errors[0]), "phase": "recover"}
        elif not host_recovery_only:
            base_profile = build_resource_profile(settings, list(enabled_app_backends))
            reconciler = AppRuntimeReconciler(
                backend,
                settings,
                base_profile=base_profile,
                services=services,
            )
            try:
                publish_result = await reconciler.reconcile_for_publish()
                publish = _serialize_reconcile_result(publish_result)
                verify_result = await reconciler.verify_steady_state()
                verify = _serialize_reconcile_result(verify_result)
            except ApplyFailed as exc:
                failure = exc.as_details()
            except Exception as exc:
                failure = {"error": str(exc), "phase": "recover"}
            reconcile_state = reconciler.result

        await operation.update(
            phase="verify",
            details={
                "repair_actions": cleanup.get("actions") or [],
                "changed": _fix_changed(cleanup, reconcile_state),
            },
        )
        after = await _collect_after_diagnostics(
            backend,
            settings,
            should_wait=bool(
                failure is None
                and (
                    runtime_restart_planned
                    or (
                        reconcile_state is not None
                        and (reconcile_state.created or reconcile_state.recreated)
                    )
                )
            ),
        )
        after_diagnosis = str(after.get("diagnosis") or "unknown")
        deferred = bool(
            failure is None
            and diagnosis in OBSERVATION_ONLY_DIAGNOSES
            and after_diagnosis == diagnosis
            and not repair_plan
            and not cleanup.get("changed")
        )
        if failure is None and not deferred and not bool(after.get("ok", False)):
            failure = {
                "error": f"repair verification failed: {after_diagnosis}",
                "phase": "verify",
            }
        reconcile_result = (
            verify
            or publish
            or (
                _serialize_reconcile_result(reconcile_state)
                if reconcile_state is not None
                else None
            )
        )
        payload = {
            "backend": backend.name,
            "kind": backend.kind,
            "enabled": bool(backend.enabled),
            "ok": bool(after.get("ok", False)) and failure is None,
            "deferred": deferred,
            "changed": _fix_changed(cleanup, reconcile_state),
            "operation_id": operation.id,
            "repair_plan": repair_plan,
            "repair_actions": cleanup.get("actions") or [],
            "pre_recovery_evidence": pre_recovery_evidence,
            "before": before,
            "cleanup": cleanup,
            "reconcile": reconcile_result,
            "after": after,
            "failure": failure,
        }
        operation_status = (
            "success" if payload["ok"] else ("partial" if deferred else "failed")
        )
        await operation.complete(
            operation_status,
            phase=(
                "completed"
                if payload["ok"]
                else (
                    "deferred"
                    if deferred
                    else str((failure or {}).get("phase") or "repair")
                )
            ),
            error=None
            if payload["ok"] or deferred
            else str((failure or {}).get("error") or "repair failed"),
            details={
                "backend_id": backend.id,
                "backend": backend.name,
                "changed": payload["changed"],
                "deferred": deferred,
            },
        )
        logger.info(
            "app.repair.completed",
            operation_id=operation.id,
            backend_id=backend.id,
            backend=backend.name,
            container=str(before.get("container") or ""),
            outcome=(
                "recovered" if payload["ok"] else ("deferred" if deferred else "failed")
            ),
            changed=payload["changed"],
            repair_plan=repair_plan,
            repair_actions=payload["repair_actions"],
            after_diagnosis=after_diagnosis,
            failure=failure,
            pre_recovery_evidence=pre_recovery_evidence,
        )
    if record_event:
        after_issues = (
            after.get("issues") if isinstance(after.get("issues"), list) else []
        )
        event_kind = (
            "backend_fix_recovered"
            if payload["ok"]
            else (
                "backend_fix_deferred" if payload["deferred"] else "backend_fix_failed"
            )
        )
        event_summary = (
            "Backend fix recovered"
            if payload["ok"]
            else (
                "Backend fix deferred" if payload["deferred"] else "Backend fix failed"
            )
        )
        await emit_control_event(
            settings,
            kind=event_kind,
            source="fix",
            summary=event_summary,
            severity=(
                "success"
                if payload["ok"]
                else ("info" if payload["deferred"] else "error")
            ),
            scope="backend",
            backend_name=backend.name,
            subevents=[
                {"label": "before", "value": str(before.get("diagnosis") or "-")},
                {"label": "after", "value": str(after.get("diagnosis") or "-")},
                {"label": "changed", "value": "yes" if payload["changed"] else "no"},
                {
                    "label": "issues",
                    "value": ", ".join(str(item) for item in after_issues)
                    if after_issues
                    else "none",
                },
            ],
            details={
                "operation_id": operation.id,
                "backend_id": backend.id,
                "backend": backend.name,
                "container": str(before.get("container") or ""),
                "ok": bool(payload["ok"]),
                "changed": bool(payload["changed"]),
                "repair_plan": repair_plan,
                "repair_actions": cleanup.get("actions") or [],
                "after_diagnosis": after_diagnosis,
                "deferred": bool(payload["deferred"]),
                "failure": failure,
                "pre_recovery_evidence": pre_recovery_evidence,
            },
        )
    return payload
