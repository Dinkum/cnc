from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import ApplyRun, HostApplyState
from app.schemas.apply import ApplyResponse
from app.services.app_containers import (
    inspect_container,
    inspect_network,
)
from app.services.tools import TOOLS
from app.services.tools.core import (
    Plan,
    Scope,
    converge,
    record_unwind_outcome,
    unwind_plan,
    unwind_recovery,
)
from app.services.tools.tailscale import (
    tailscale_service_host_precheck_required,
    tailscale_service_host_precheck_skip_details,
    verify_tailscale_service_host_precheck,
)
from app.services.apply_core import (
    ApplyFailed,
    DesiredState,
    clip_output,
    config_revision_for_state_hash,
    desired_state_snapshot,
    desired_state_snapshot_hash,
    desired_state_snapshot_json,
)
from app.services.apply_state import build_desired_state
from app.services.app_runtime import (
    apply_app_backends,
    preflight_app_storage,
)
from app.services.cluster_ingress import reconcile_cluster_followers
from app.services.commands import (
    CommandError,
    run_command,
    run_command_checked_async,
)
from app.services.control_events import emit_control_event
from app.services.error_reporting import CNCError, ErrorCode
from app.services.history_retention import prune_control_plane_history
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
)
from app.services.outcome_presentation import present_mutation_outcome
from app.services.operations import (
    HostMutationLockError,
    OperationHandle,
    host_mutation_operation,
)
from app.services.retry import retry_async_call
from app.services.runtime_services import AppRuntimeServices
from app.services.runtime_assets import reconcile_managed_systemd_assets
from app.services.self_audit import (
    ControlPlaneSelfAuditError,
    run_control_plane_self_audit,
)
from app.services.ssh_access import reconcile_backend_ssh_access
from app.services.status_service import invalidate_status_cache
from app.services.tailscale_admin import (
    TailscaleAdminExposureError,
    verify_tailscale_admin_exposure,
)


logger = get_logger("apply")
MANAGED_PREFIX = "cnc-"
ApplyProgressPlanCallback = Callable[[DesiredState], Awaitable[None] | None]
ApplyProgressCallback = Callable[[str, str], Awaitable[None] | None]
APPLY_PHASE_ORDER = (
    "app_runtime",
    "ssh_access",
    "sync_files",
    "nginx_validate",
    "nginx_reload",
    "cluster_followers",
    "tailscale_paths",
    "tailscale_services",
    "tailscale_admin_verify",
    "control_plane_self_audit",
)


@dataclass
class ApplyServices:
    preflight_app_storage: Callable[..., dict[str, Any]] = field(
        default_factory=lambda: preflight_app_storage
    )
    runtime_services: AppRuntimeServices = field(
        default_factory=lambda: AppRuntimeServices(
            inspect_container=inspect_container,
            inspect_network=inspect_network,
            run_command=run_command,
            run_command_checked_async=run_command_checked_async,
        )
    )
    apply_app_backends: Callable[..., Awaitable[dict[str, Any]]] = field(
        default_factory=lambda: apply_app_backends
    )
    reconcile_backend_ssh_access: Callable[..., Awaitable[dict[str, Any]]] = field(
        default_factory=lambda: reconcile_backend_ssh_access
    )
    reconcile_managed_systemd_assets: Callable[[Settings], dict[str, Any]] = field(
        default_factory=lambda: reconcile_managed_systemd_assets
    )
    reconcile_cluster_followers: Callable[..., dict[str, Any]] = field(
        default_factory=lambda: reconcile_cluster_followers
    )
    verify_tailscale_admin_exposure: Callable[[Settings], dict[str, Any]] = field(
        default_factory=lambda: verify_tailscale_admin_exposure
    )
    run_control_plane_self_audit: Callable[[Settings], dict[str, Any]] = field(
        default_factory=lambda: run_control_plane_self_audit
    )
    progress_callback: ApplyProgressCallback | None = None
    progress_plan_callback: ApplyProgressPlanCallback | None = None
    cleanup_deleted_app_filesystem: bool = True


def default_apply_services() -> ApplyServices:
    return ApplyServices()


async def _emit_apply_progress(
    services: ApplyServices, phase: str, substate: str
) -> None:
    if services.progress_callback is None:
        return
    try:
        result = services.progress_callback(phase, substate)
        if result is not None:
            await result
    except Exception as exc:
        logger.warning(
            "apply.progress_callback_failed",
            phase=phase,
            substate=substate,
            error=str(exc),
        )


async def _emit_apply_progress_plan(
    services: ApplyServices, desired: DesiredState
) -> None:
    """Rebuild the operator's progress card once desired state is known.

    It used to also take a predicted apply plan and hide the steps that plan
    said would not change. Nothing produced such a plan any more, because
    convergence decides what to do by reading the host, so the argument was
    always None and every step was shown. Passing desired state alone says the
    same thing without implying a prediction exists.
    """
    if services.progress_plan_callback is None:
        return
    try:
        result = services.progress_plan_callback(desired)
        if result is not None:
            await result
    except Exception as exc:
        logger.warning("apply.progress_plan_callback_failed", error=str(exc))


@dataclass(frozen=True)
class DesiredStateRecord:
    config_revision: str
    desired_state_hash: str
    desired_state_json: str
    generated_nginx_files_json: str
    runtime_graph_json: str
    route_contracts_json: str
    backend_contracts_json: str
    resource_profile_json: str

    def apply_run_fields(self, operation_id: int | None) -> dict[str, object]:
        return {
            "operation_id": operation_id,
            "config_revision": self.config_revision,
            "desired_state_hash": self.desired_state_hash,
            "desired_state_json": self.desired_state_json,
            "generated_nginx_files_json": self.generated_nginx_files_json,
            "runtime_graph_json": self.runtime_graph_json,
            "route_contracts_json": self.route_contracts_json,
            "backend_contracts_json": self.backend_contracts_json,
            "resource_profile_json": self.resource_profile_json,
        }


def _apply_output_event_details(
    desired: DesiredState, apply_details: dict[str, Any] | None = None
) -> dict[str, object]:
    """Summarize which outputs moved, from what the podman tool actually did.

    Previously derived by comparing this apply's slice hashes against the
    previous successful apply's. It now reads the selection the tool reports,
    so the summary describes work that happened rather than a difference
    between two database snapshots.
    """
    details = apply_details or {}
    selected = [str(name) for name in details.get("podman_selected") or []]
    created = [str(name) for name in details.get("podman_created") or []]
    updated = sorted(set(selected) - set(created))
    removed = [str(name) for name in details.get("removed_backends") or []]
    return {
        "outputs_total": len(desired.known_app_backends),
        "outputs_created": len(created),
        "outputs_updated": len(updated),
        "outputs_deleted": len(removed),
        "output_created_names": sorted(created),
        "output_updated_names": updated,
    }


def _plural_output_action(singular: str, plural: str, count: int) -> str:
    return singular if count == 1 else plural


def _apply_success_event_summary(
    *,
    degraded_health: bool,
    cluster_follower_warnings: bool,
    output_event: dict[str, object],
) -> str:
    created = int(output_event.get("outputs_created") or 0)
    updated = int(output_event.get("outputs_updated") or 0)
    deleted = int(output_event.get("outputs_deleted") or 0)
    changed_kinds = sum(1 for count in (created, updated, deleted) if count)
    if changed_kinds == 1 and created:
        summary = _plural_output_action("Output created", "Outputs created", created)
    elif changed_kinds == 1 and updated:
        summary = _plural_output_action("Output updated", "Outputs updated", updated)
    elif changed_kinds == 1 and deleted:
        summary = _plural_output_action("Output deleted", "Outputs deleted", deleted)
    elif changed_kinds:
        summary = "Output changes applied"
    else:
        summary = "Changes applied"

    if degraded_health:
        return f"{summary} with app health warnings"
    if cluster_follower_warnings:
        return f"{summary} with cluster warnings"
    return summary


def _compact_output_names(names: object) -> str:
    if not isinstance(names, list):
        return ""
    normalized = [str(name).strip() for name in names if str(name).strip()]
    if not normalized:
        return ""
    if len(normalized) <= 3:
        return ", ".join(normalized)
    return f"{len(normalized)} outputs"


def _apply_success_event_subevents(
    *,
    run_id: int | None,
    output_event: dict[str, object],
    degraded_health: bool,
) -> list[dict[str, str]]:
    subevents = [{"label": "run", "value": f"#{run_id}"}]
    created = int(output_event.get("outputs_created") or 0)
    updated = int(output_event.get("outputs_updated") or 0)
    deleted = int(output_event.get("outputs_deleted") or 0)
    total = int(output_event.get("outputs_total") or 0)
    if created:
        subevents.append(
            {
                "label": "output" if created == 1 else "outputs created",
                "value": _compact_output_names(output_event.get("output_created_names"))
                or str(created),
            }
        )
    if updated:
        subevents.append(
            {
                "label": "output updated" if updated == 1 else "outputs updated",
                "value": _compact_output_names(output_event.get("output_updated_names"))
                or str(updated),
            }
        )
    if deleted:
        subevents.append(
            {
                "label": "output deleted" if deleted == 1 else "outputs deleted",
                "value": str(deleted),
            }
        )
    if not any((created, updated, deleted)):
        subevents.append({"label": "outputs", "value": str(total)})
    subevents.append(
        {"label": "status", "value": "degraded" if degraded_health else "success"}
    )
    return subevents


def _json_dump_snapshot_value(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _desired_state_record(desired: DesiredState) -> DesiredStateRecord:
    snapshot = desired_state_snapshot(desired)
    snapshot_json = desired_state_snapshot_json(snapshot)
    state_hash = desired_state_snapshot_hash(snapshot_json)
    return DesiredStateRecord(
        config_revision=config_revision_for_state_hash(state_hash),
        desired_state_hash=state_hash,
        desired_state_json=snapshot_json,
        generated_nginx_files_json=_json_dump_snapshot_value(
            snapshot["generated_nginx_files"]
        ),
        runtime_graph_json=_json_dump_snapshot_value(snapshot["runtime_graph"]),
        route_contracts_json=_json_dump_snapshot_value(snapshot["route_contracts"]),
        backend_contracts_json=_json_dump_snapshot_value(snapshot["backend_contracts"]),
        resource_profile_json=_json_dump_snapshot_value(snapshot["resource_profile"]),
    )


async def _record_last_applied_state(
    session: AsyncSession,
    *,
    run: ApplyRun,
    operation_id: int | None,
    desired_state: DesiredStateRecord,
) -> None:
    row = await session.get(HostApplyState, 1)
    if row is None:
        row = HostApplyState(id=1)
        session.add(row)
    row.last_applied_state_hash = desired_state.desired_state_hash
    row.last_successful_operation_id = operation_id
    row.last_successful_apply_run_id = run.id
    row.last_applied_at = run.created_at or datetime.now(UTC)


async def invalidate_apply_convergence(session: AsyncSession) -> bool:
    """Clear the convergence marker after live state may have drifted.

    Durable, unlike the in-process unwind stack: a process killed mid-apply
    leaves this cleared, so the next apply has no trusted baseline and every
    tool re-observes the host.
    """
    row = await session.get(HostApplyState, 1)
    if row is None:
        return False
    had_marker = bool(
        row.last_applied_state_hash
        or row.last_successful_operation_id
        or row.last_successful_apply_run_id
        or row.last_applied_at
    )
    row.last_applied_state_hash = None
    row.last_successful_operation_id = None
    row.last_successful_apply_run_id = None
    row.last_applied_at = None
    return had_marker


async def run_apply(
    session: AsyncSession,
    settings: Settings,
    *,
    services: ApplyServices | None = None,
    commit_on_success: bool = True,
    operation_kind: str = "apply_host",
    actor: str = "system",
    operation_handle: OperationHandle | None = None,
    emit_success_event: bool = True,
    scope: Scope | None = None,
    recovery: list[Any] | None = None,
) -> ApplyResponse:
    services = services or default_apply_services()
    # Owned here even when the caller did not ask for it, so a failure between
    # convergence and the durable record can still put the host back. The
    # caller's list, when given, is the same object so recovery survives out to
    # the commit boundary.
    if recovery is None:
        recovery = []
    apply_operation = (
        operation_handle.with_deferred_db_updates()
        if operation_handle is not None
        else None
    )
    try:
        async with host_mutation_operation(
            settings,
            kind=operation_kind,
            actor=actor,
            phase="apply",
            operation=apply_operation,
        ) as operation:
            desired_record: DesiredStateRecord | None = None
            live_apply_completed = False
            async with logger.operation("apply.run") as log_operation:
                try:
                    await _emit_apply_progress(
                        services, "Save desired state", "Building host plan"
                    )
                    desired = await build_desired_state(session, settings)
                    log_operation.step(
                        "apply.desired_state.ready",
                        nginx_files=len(desired.nginx_files),
                        app_backends=len(desired.enabled_app_backends),
                    )
                    desired_record = _desired_state_record(desired)
                    await _emit_apply_progress_plan(services, desired)
                    tailscale_service_host_precheck = (
                        tailscale_service_host_precheck_skip_details(
                            desired,
                            settings,
                        )
                    )
                    if tailscale_service_host_precheck_required(desired, settings):
                        await _emit_apply_progress(
                            services, "Prepare host", "Checking tailnet service host"
                        )
                        tailscale_service_host_precheck = (
                            await verify_tailscale_service_host_precheck(
                                desired.tailscale_services,
                                settings,
                                services=services,
                            )
                        )
                    await operation.update(
                        config_revision=desired_record.config_revision,
                        desired_state_hash=desired_record.desired_state_hash,
                        details={
                            "config_revision": desired_record.config_revision,
                            "desired_state_hash": desired_record.desired_state_hash,
                        },
                    )
                    try:
                        await _emit_apply_progress(
                            services,
                            "Prepare host",
                            "Reconciling CNC runtime assets",
                        )
                        runtime_assets = await asyncio.to_thread(
                            services.reconcile_managed_systemd_assets, settings
                        )
                    except Exception as exc:
                        raise ApplyFailed(
                            "managed runtime asset reconcile failed",
                            phase="runtime_assets",
                            details={
                                "error": str(exc),
                                "failure_mode": "partial",
                                "manual_review_required": True,
                                "operator_action": "inspect systemd unit drift and rerun apply",
                            },
                        ) from exc
                    await _emit_apply_progress(
                        services, "Prepare host", "Checking admin exposure"
                    )
                    admin_exposure_precheck = await asyncio.to_thread(
                        services.verify_tailscale_admin_exposure,
                        settings,
                    )
                    details = await _apply_desired_state(
                        desired,
                        settings,
                        services=services,
                        scope=scope or Scope.everything(),
                        recovery=recovery,
                        operation_id=operation.id,
                    )
                    live_apply_completed = True
                    response_details = {
                        "runtime_assets": runtime_assets,
                        "tailscale_admin_precheck": admin_exposure_precheck,
                        "tailscale_service_host_precheck": tailscale_service_host_precheck,
                        "config_revision": desired_record.config_revision,
                        "desired_state_hash": desired_record.desired_state_hash,
                        **details,
                    }
                    response_details["host_changed"] = bool(
                        response_details.get("changed_tools")
                        or runtime_assets.get("changed_units")
                        or runtime_assets.get("changed_files")
                        or runtime_assets.get("timer_reconciled")
                    )
                    output_event = _apply_output_event_details(desired, details)
                    degraded_health = (
                        response_details.get("app_healthcheck_status") == "degraded"
                    )
                    cluster_follower_warnings = bool(
                        response_details.get("cluster_followers_unavailable")
                    )
                    if degraded_health:
                        success_message = "apply completed with app health warnings"
                    elif cluster_follower_warnings:
                        success_message = "apply completed with cluster warnings"
                    else:
                        success_message = "apply completed"
                    success_summary = _apply_success_event_summary(
                        degraded_health=degraded_health,
                        cluster_follower_warnings=cluster_follower_warnings,
                        output_event=output_event,
                    )
                    response_details.update(output_event)
                    response_details["apply_event_summary"] = success_summary
                    run = ApplyRun(
                        status="success",
                        message=success_message,
                        details_json=json.dumps(response_details),
                        **desired_record.apply_run_fields(operation.id),
                    )
                    session.add(run)
                    await session.flush()
                    await session.refresh(run)
                    await _record_last_applied_state(
                        session,
                        run=run,
                        operation_id=operation.id,
                        desired_state=desired_record,
                    )
                    await prune_control_plane_history(session)
                    if commit_on_success:
                        await session.commit()
                    else:
                        await session.flush()
                    invalidate_status_cache(settings, prefill=False)
                    logger.info(
                        "apply.run.succeeded",
                        app_healthcheck_status=response_details.get(
                            "app_healthcheck_status"
                        ),
                    )
                    operation_success_details = {
                        "apply_run_id": run.id,
                        "status": "success",
                        "app_healthcheck_status": response_details.get(
                            "app_healthcheck_status"
                        ),
                    }
                    if commit_on_success:
                        await operation.complete(
                            "success",
                            phase="completed",
                            details=operation_success_details,
                        )
                    else:
                        await operation.update(
                            status="running",
                            phase="database_commit",
                            details={
                                **operation_success_details,
                                "deferred_completion": True,
                            },
                        )
                        response_details["deferred_operation_id"] = operation.id
                        response_details["deferred_operation_kind"] = operation.kind
                        operation.completed = True
                    success_subevents = _apply_success_event_subevents(
                        run_id=run.id,
                        output_event=output_event,
                        degraded_health=degraded_health,
                    )
                    response_details["apply_event_subevents"] = success_subevents
                    if emit_success_event:
                        await emit_control_event(
                            settings,
                            kind="apply_completed",
                            source="apply",
                            summary=success_summary,
                            severity="warn"
                            if degraded_health or cluster_follower_warnings
                            else "success",
                            scope="host",
                            affects_all=True,
                            subevents=success_subevents,
                            details={
                                "run_id": run.id,
                                "status": "success",
                                "message": success_message,
                                **output_event,
                                "app_healthcheck_status": response_details.get(
                                    "app_healthcheck_status"
                                ),
                                "app_healthcheck_failures": response_details.get(
                                    "app_healthcheck_failures"
                                )
                                or [],
                            },
                        )
                    log_operation.result(
                        success_message, status="success", run_id=run.id
                    )
                    return ApplyResponse(
                        status="success",
                        message=success_message,
                        details=response_details,
                        host_changed=response_details["host_changed"],
                        created_at=run.created_at,
                        run_id=run.id,
                    )
                except Exception as exc:
                    await session.rollback()
                    if isinstance(exc, ApplyFailed):
                        details = exc.as_details()
                    elif isinstance(exc, ControlPlaneSelfAuditError):
                        details = {
                            "error": str(exc),
                            "phase": "control_plane_self_audit",
                            **exc.as_details(),
                        }
                    elif isinstance(exc, TailscaleAdminExposureError):
                        details = {
                            "error": str(exc),
                            "phase": "tailscale_admin_verify",
                        }
                    elif isinstance(exc, CommandError):
                        details = {
                            "error": "command failed",
                            "phase": "command",
                            "command": exc.result.command,
                            "stdout": clip_output(exc.result.stdout),
                            "stderr": clip_output(exc.result.stderr),
                        }
                    else:
                        details = {"error": str(exc), "phase": "unknown"}
                    if live_apply_completed:
                        # Convergence finished and something after it failed.
                        # The change is being rejected, so the reversible host
                        # effects come back rather than being reported as a
                        # partial and left in place. Whether that leaves
                        # anything for an operator is the unwind's answer, not
                        # an assumption made here.
                        restored = await unwind_recovery(recovery)
                        details = {**details, **restored}
                        if restored.get("manual_review_required"):
                            details["operator_action"] = (
                                "rerun apply to reconcile durable desired state"
                            )
                    if desired_record is not None:
                        details = {
                            "config_revision": desired_record.config_revision,
                            "desired_state_hash": desired_record.desired_state_hash,
                            **details,
                        }
                    partial_failure = (
                        str(details.get("failure_mode") or "").strip() == "partial"
                    )
                    message = "apply failed"
                    presentation = present_mutation_outcome(
                        "partial" if partial_failure else "failed", details
                    )
                    if partial_failure:
                        details["convergence_invalidated"] = True
                        details[
                            "previous_convergence_marker_cleared"
                        ] = await invalidate_apply_convergence(session)
                    run = ApplyRun(
                        status="error",
                        message=message,
                        details_json=json.dumps(details),
                        **(
                            desired_record.apply_run_fields(operation.id)
                            if desired_record is not None
                            else {"operation_id": operation.id}
                        ),
                    )
                    session.add(run)
                    await session.flush()
                    await prune_control_plane_history(session)
                    await session.commit()
                    await session.refresh(run)
                    invalidate_status_cache(settings, prefill=False)
                    logger.warning("apply.run.failed", details=details)
                    failed_phase = (
                        str(
                            details.get("phase") or details.get("failed_phase") or ""
                        ).strip()
                        or "unknown"
                    )
                    await operation.complete(
                        "partial" if partial_failure else "failed",
                        phase=failed_phase,
                        error=str(
                            details.get("error")
                            or details.get("stderr")
                            or "apply failed"
                        ),
                        details={"apply_run_id": run.id, **details},
                    )
                    await emit_control_event(
                        settings,
                        kind="apply_failed",
                        source="apply",
                        summary="Host apply failed",
                        severity="error",
                        scope="host",
                        affects_all=True,
                        subevents=[
                            {"label": "run", "value": f"#{run.id}"},
                            {"label": "phase", "value": failed_phase},
                            *(
                                [
                                    {
                                        "label": "backends completed",
                                        "value": str(
                                            len(details.get("completed_backends") or [])
                                        ),
                                    },
                                    {
                                        "label": "backends pending",
                                        "value": str(
                                            len(details.get("pending_backends") or [])
                                        ),
                                    },
                                ]
                                if details.get("selected_backends")
                                else []
                            ),
                            {
                                "label": "status",
                                "value": "failed",
                            },
                        ],
                        details={
                            "run_id": run.id,
                            "status": "error",
                            "phase": failed_phase,
                            "failure_mode": details.get("failure_mode"),
                            "operation_id": operation.id,
                            "convergence_invalidated": details.get(
                                "convergence_invalidated"
                            ),
                            "selected_backends": details.get("selected_backends") or [],
                            "completed_backends": details.get("completed_backends")
                            or [],
                            "failed_backend": details.get("failed_backend"),
                            "pending_backends": details.get("pending_backends") or [],
                            "error": details.get("error")
                            or details.get("stderr")
                            or "",
                        },
                        notify=False,
                    )
                    await send_pushover_notification_async(
                        settings,
                        title="CNC apply failed",
                        message=build_operator_message(
                            "Apply failed.",
                            status="failure",
                            facts=[
                                ("recovery", presentation.recovery_detail),
                                *_apply_failure_notification_facts(
                                    details,
                                    run_id=run.id,
                                    manual_review_required=partial_failure,
                                ),
                            ],
                            action="cnc-admin logs admin --errors",
                        ),
                        priority=0,
                        event="apply_failed",
                        source="apply",
                        run_id=run.id,
                        url=admin_dashboard_url(settings, tab="home"),
                        url_title="Open CNC",
                    )
                    log_operation.failed(message, status="error", run_id=run.id)
                    return ApplyResponse(
                        status="error",
                        message=message,
                        details=details,
                        created_at=run.created_at,
                        run_id=run.id,
                    )
    except (ApplyFailed, HostMutationLockError) as exc:
        failure_details = (
            exc.as_details()
            if isinstance(exc, ApplyFailed)
            else {
                "error": str(exc),
                "phase": "lock",
                "failure_mode": "clean",
                "manual_review_required": False,
            }
        )
        await emit_control_event(
            settings,
            kind="apply_failed",
            source="apply",
            summary="Host apply failed",
            severity="error",
            scope="host",
            affects_all=True,
            subevents=[
                {
                    "label": "phase",
                    "value": str(
                        failure_details.get("phase")
                        or failure_details.get("failed_phase")
                        or "unknown"
                    ),
                },
                {"label": "status", "value": "failed"},
            ],
            details=failure_details,
            notify=False,
        )
        await send_pushover_notification_async(
            settings,
            title="CNC apply failed",
            message=build_operator_message(
                "Apply failed.",
                status="failure",
                facts=[
                    (
                        "recovery",
                        present_mutation_outcome(
                            "failed", failure_details
                        ).recovery_detail,
                    ),
                    *_apply_failure_notification_facts(failure_details),
                ],
                action="cnc-admin logs admin --errors",
            ),
            priority=-1,
            event="apply_failed",
            source="apply",
            url=admin_dashboard_url(settings, tab="home"),
            url_title="Open CNC",
        )
        return ApplyResponse(
            status="error",
            message="apply failed",
            details=failure_details,
            created_at=None,
            run_id=None,
        )


def _apply_failure_notification_facts(
    details: dict[str, Any],
    *,
    run_id: int | None = None,
    manual_review_required: bool = False,
) -> list[tuple[str, object]]:
    target_path = (
        details.get("quadlet_dir")
        or details.get("target_dir")
        or details.get("path")
        or ""
    )
    process = details.get("process")
    service_unit = process.get("systemd_unit") if isinstance(process, dict) else ""
    return [
        ("run_id", f"#{run_id}" if run_id is not None else ""),
        ("phase", details.get("phase") or details.get("failed_phase") or ""),
        ("reason", details.get("reason") or ""),
        ("service", service_unit or ""),
        ("target", target_path),
        ("errno", details.get("errno") or ""),
        ("hint", details.get("operator_hint") or ""),
        ("error", details.get("error") or details.get("stderr") or ""),
        ("manual_review", "required" if manual_review_required else ""),
    ]


def _skipped_app_runtime_details(
    desired: DesiredState, settings: Settings
) -> dict[str, object]:
    containers = sorted(
        node.container for node in desired.runtime_graph.app_backends.values()
    )
    return {
        "app_runtime_skipped": True,
        "app_runtime_skip_reason": "runtime backend slices unchanged",
        "app_containers": containers,
        "app_containers_created": [],
        "app_containers_recreated": [],
        "app_containers_bootstrapped": [],
        "app_containers_running": [],
        "app_containers_stopped": [],
        "app_containers_removed": [],
        "app_networks_removed": [],
        "app_quadlet_files_removed": [],
        "app_legacy_reboot_bridges_removed": [],
        "app_filesystem_artifacts_removed": [],
        "app_disabled_runtime_cleanup": [],
        "app_container_dns": {},
        "app_reconcile_phases": {},
        "app_healthcheck_status": "not_checked",
        "app_healthcheck_failures": [],
        "app_network_isolation": {
            "skipped": True,
            "reason": "runtime backend slices unchanged",
        },
        "app_control_dir": str(settings.app_control_dir),
    }


async def _apply_desired_state(
    desired: DesiredState,
    settings: Settings,
    *,
    services: ApplyServices,
    scope: Scope | None = None,
    operation_id: int | None = None,
    recovery: list[Any] | None = None,
) -> dict[str, Any]:
    settings.apply_backup_dir.mkdir(parents=True, exist_ok=True)
    progress = _ApplyProgressTracker()
    details = {
        "nginx_files": sorted(desired.nginx_files.keys()),
        "cluster_nodes": list(desired.cluster_nodes),
        "cluster_app_nginx_files": {
            node_uid: sorted(files)
            for node_uid, files in sorted(desired.cluster_node_app_nginx_files.items())
        },
        "cluster_admin_nginx_files": {
            node_uid: sorted(files)
            for node_uid, files in sorted(
                desired.cluster_node_admin_nginx_files.items()
            )
        },
        "route_contracts": desired.route_contracts,
        "backend_contracts": desired.backend_contracts,
        "runtime_graph": desired.runtime_graph.as_dict(),
        "backup_dir": str(settings.apply_backup_dir),
        "resource_profile": desired.resource_profile.as_dict(),
        "apply_phase_order": list(APPLY_PHASE_ORDER),
        "rollback_contract": _apply_rollback_contract(),
    }
    try:
        tool_plan = Plan(
            desired=desired,
            settings=settings,
            services=services,
            details=details,
            scope=scope or Scope.everything(),
            extras={
                "operation_id": operation_id,
            },
        )
        details.update(tool_plan.scope.as_details())
        if recovery is not None:
            # Published so the commit boundary can still put the host back if
            # the database commit fails after every tool succeeded.
            recovery.append(tool_plan)

        # The whole host convergence: walk the tools in order, each one
        # reading the box and closing its own gap. Phase bookkeeping and
        # clean-versus-partial classification are driven from what the tools
        # declare, not from anything written out here.
        details["changed_tools"] = await converge(
            TOOLS, tool_plan, progress=progress
        )

        progress.enter("tailscale_admin_verify")
        await _emit_apply_progress(
            services, "Verify output", "Verifying admin exposure"
        )
        try:
            details["tailscale_admin_verify"] = await asyncio.to_thread(
                services.verify_tailscale_admin_exposure,
                settings,
            )
        except TailscaleAdminExposureError as exc:
            raise ApplyFailed(
                "tailscale admin exposure verification failed",
                phase="tailscale_admin_verify",
                details={"error": str(exc)},
            ) from exc
        progress.completed("tailscale_admin_verify")
        progress.enter("control_plane_self_audit")
        await _emit_apply_progress(services, "Verify output", "Auditing control plane")
        try:
            audit_payload = await _run_control_plane_self_audit_for_apply(
                settings, services=services
            )
            details["control_plane_self_audit"] = {"ok": True, **audit_payload}
        except ControlPlaneSelfAuditError as exc:
            details["control_plane_self_audit"] = {"ok": False, **exc.as_details()}
            log_event = (
                "apply.control_plane_self_audit.failed"
                if exc.blocks_apply
                else "apply.control_plane_self_audit.warning"
            )
            log_fn = logger.error if exc.blocks_apply else logger.warning
            log_fn(log_event, details=exc.as_details())
            if exc.blocks_apply:
                raise ApplyFailed(
                    "control-plane self-audit failed",
                    phase="control_plane_self_audit",
                    details=exc.as_details(),
                ) from exc
        progress.completed("control_plane_self_audit")
        details.update(progress.as_details())
    except Exception as exc:
        # The undo stack outlives converge on purpose. Admin verification and
        # the control-plane self-audit run after every tool has finished, and a
        # failure there means the change is being rejected just as surely as a
        # tool failure, so the reversible host effects have to come back.
        failure = progress.failure(exc)
        unwound, unwind_errors = await unwind_plan(tool_plan)
        record_unwind_outcome(failure, unwound, unwind_errors, tool_plan.touched)
        raise failure

    return details


async def _run_control_plane_self_audit_for_apply(
    settings: Settings,
    *,
    services: ApplyServices,
) -> dict[str, Any]:
    attempts = settings.transient_command_retry_attempts + 1
    retry_count = 0

    async def run_audit() -> dict[str, Any]:
        return await asyncio.to_thread(services.run_control_plane_self_audit, settings)

    def should_retry(exc: Exception) -> bool:
        return isinstance(exc, ControlPlaneSelfAuditError) and exc.blocks_apply

    def log_retry(
        exc: Exception, attempt: int, max_attempts: int, delay_sec: float
    ) -> None:
        nonlocal retry_count
        retry_count = attempt
        details = (
            exc.as_details()
            if isinstance(exc, ControlPlaneSelfAuditError)
            else {"error": str(exc)}
        )
        logger.warning(
            "apply.control_plane_self_audit.retrying",
            attempt=attempt,
            max_attempts=max_attempts,
            backoff_sec=delay_sec,
            details=details,
        )

    payload = await retry_async_call(
        run_audit,
        attempts=attempts,
        backoff_sec=settings.transient_command_retry_backoff_sec,
        should_retry=should_retry,
        on_retry=log_retry,
    )
    if retry_count:
        return {**payload, "blocking_retry_count": retry_count}
    return payload


@dataclass
class _ApplyProgressTracker:
    current_phase: str = "app_runtime"
    completed_phases: list[str] = field(default_factory=list)
    live_mutation_phases: list[str] = field(default_factory=list)

    def enter(self, phase: str) -> None:
        self.current_phase = phase

    def completed(self, phase: str | None = None) -> None:
        _append_unique(self.completed_phases, phase or self.current_phase)

    def live_mutation(self, phase: str | None = None) -> None:
        _append_unique(self.live_mutation_phases, phase or self.current_phase)

    def as_progress(self) -> dict[str, list[str]]:
        return {
            "completed_phases": list(self.completed_phases),
            "live_mutation_phases": list(self.live_mutation_phases),
        }

    def as_details(self) -> dict[str, list[str]]:
        return self.as_progress()

    def failure(self, error: Exception) -> ApplyFailed:
        return _enrich_apply_failure(
            error,
            progress=self.as_progress(),
            failed_phase=self.current_phase,
        )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _format_apply_error_details(error: Exception) -> dict[str, Any]:
    if isinstance(error, CNCError):
        return error.as_details()
    if isinstance(error, CommandError):
        classified = CNCError(
            ErrorCode.APPLY_FAILED,
            "command failed",
            details={
                "phase": "command",
                "command": error.result.command,
                "stdout": clip_output(error.result.stdout),
                "stderr": clip_output(error.result.stderr),
            },
            cause=error,
        )
        return classified.as_details()
    return CNCError(
        ErrorCode.APPLY_FAILED,
        str(error),
        details={"phase": "unknown"},
        cause=error,
    ).as_details()


def _apply_rollback_contract() -> dict[str, object]:
    return {
        "phase_order": list(APPLY_PHASE_ORDER),
        "ingress_cutover_after": [
            "app_runtime",
            "ssh_access",
            "sync_files",
            "nginx_validate",
        ],
        "traffic_unchanged_before_ingress_cutover": [
            "app_runtime",
            "ssh_access",
            "sync_files",
            "nginx_validate",
        ],
        "partial_failure_after_live_mutation": [
            "app_runtime",
            "ssh_access",
            "nginx_reload",
            "cluster_followers",
            "tailscale_paths",
            "tailscale_services",
        ],
        "nginx_restored_after_failed_cutover": [
            "nginx_reload",
            "cluster_followers",
            "tailscale_paths",
            "tailscale_services",
            "tailscale_admin_verify",
        ],
        "nginx_candidate_removed_before_cutover": ["sync_files", "nginx_validate"],
        "manual_review_on_partial_failure": True,
    }


def _enrich_apply_failure(
    error: Exception,
    *,
    progress: dict[str, list[str]],
    failed_phase: str,
) -> ApplyFailed:
    live_mutation_phases = list(progress.get("live_mutation_phases") or [])
    failure_mode = "partial" if live_mutation_phases else "clean"
    manual_review_required = failure_mode == "partial"
    context = {
        "failed_phase": failed_phase,
        "completed_phases": list(progress.get("completed_phases") or []),
        "live_mutation_phases": live_mutation_phases,
        "failure_mode": failure_mode,
        "manual_review_required": manual_review_required,
        "rollback_contract": _apply_rollback_contract(),
    }
    if manual_review_required:
        context["operator_action"] = "manual review required"

    if isinstance(error, ApplyFailed):
        return ApplyFailed(
            str(error),
            phase=error.phase,
            details={**error.details, **context},
            error_code=error.error_code,
        )
    if isinstance(error, CNCError):
        return ApplyFailed(
            str(error),
            phase=failed_phase or "validation",
            details={**error.details, **context},
            error_code=error.error_code,
        )
    if isinstance(error, CommandError):
        details = _format_apply_error_details(error)
        return ApplyFailed(
            str(details.get("error") or "command failed"),
            phase=str(details.get("phase") or failed_phase or "command"),
            details={**details, **context},
        )
    return ApplyFailed(str(error), phase=failed_phase or "unknown", details=context)
