from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import ApplyRun, HostApplyState
from app.schemas.apply import ApplyResponse
from app.services.app_containers import (
    inspect_container,
    inspect_network,
    read_saved_spec,
)
from app.services.apply_core import (
    APPLY_SLICE_SCHEMA_VERSION,
    ApplySlice,
    ApplyFailed,
    DesiredState,
    clip_output,
    config_revision_for_state_hash,
    desired_state_snapshot,
    desired_state_snapshot_hash,
    desired_state_snapshot_json,
    hash_apply_slice_payload,
    nginx_managed_slice_payload,
    render_apply_slices,
    runtime_backend_slice_name,
    tailscale_slice_payload,
)
from app.services.apply_state import build_desired_state
from app.services.app_quadlet import (
    AppQuadletWriteError,
    quadlet_container_path,
    quadlet_network_path,
    verify_app_quadlet_dir_writable,
)
from app.services.app_runtime import (
    apply_app_backends,
    legacy_direct_podman_migration_backend_reasons,
)
from app.services.cluster_ingress import reconcile_cluster_followers
from app.services.commands import (
    CommandError,
    command_result_is_retryable,
    run_command,
    run_command_checked_async,
)
from app.services.control_events import emit_control_event
from app.services.error_reporting import CNCError, ErrorCode
from app.services.host_state import write_json_atomic
from app.services.history_retention import prune_control_plane_history
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
)
from app.services.netdata_runtime import (
    netdata_native_runtime_live_matches,
    reconcile_netdata_native_runtime,
)
from app.services.operations import (
    HostMutationLockError,
    OperationHandle,
    host_mutation_operation,
)
from app.services.retry import retry_async_call
from app.services.renderers import container_name
from app.services.runtime_services import AppRuntimeServices
from app.services.runtime_assets import reconcile_managed_systemd_assets
from app.services.self_audit import (
    ControlPlaneSelfAuditError,
    run_control_plane_self_audit,
)
from app.services.shield_runtime import (
    SHIELD_CONTAINER_SERVICE,
    remove_shield_runtime_assets,
    render_shield_config,
    render_shield_env,
    render_shield_quadlet,
    shield_quadlet_path,
    write_shield_runtime_assets,
)
from app.services.ssh_access import reconcile_backend_ssh_access
from app.services.status_service import invalidate_status_cache
from app.services.tailscale_admin import (
    TailscaleAdminExposureError,
    verify_tailscale_admin_exposure,
)


logger = get_logger("apply")
MANAGED_PREFIX = "cnc-"
TAILSCALE_SERVICE_TAG_REQUIRED_MESSAGE = (
    "Tailscale Services require this CNC host to use a tag-based Tailscale identity "
    "before publishing tailscale subdomain inputs."
)
ApplyProgressCallback = Callable[[str, str], Awaitable[None] | None]
ApplyProgressPlanCallback = Callable[[DesiredState, Any], Awaitable[None] | None]
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
    services: ApplyServices, desired: DesiredState, apply_plan: Any
) -> None:
    if services.progress_plan_callback is None:
        return
    try:
        result = services.progress_plan_callback(desired, apply_plan)
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


@dataclass(frozen=True)
class ApplySlicePlan:
    schema_version: int
    slice_hashes: dict[str, str]
    previous_slice_hashes: dict[str, str]
    changed_slices: tuple[str, ...]
    skipped_slices: tuple[str, ...]
    full_apply_required: bool = False
    fallback_reason: str = ""

    @property
    def changed_runtime_slice_names(self) -> set[str]:
        return {
            name for name in self.changed_slices if name.startswith("runtime.backend.")
        }

    @property
    def deleted_runtime_slice_names(self) -> set[str]:
        return {
            name
            for name in self.previous_slice_hashes
            if name.startswith("runtime.backend.") and name not in self.slice_hashes
        }

    def slice_changed(self, name: str) -> bool:
        return self.full_apply_required or name in self.changed_slices

    def as_details(self) -> dict[str, object]:
        return {
            "slice_schema_version": self.schema_version,
            "slice_hashes": {
                name: self.slice_hashes[name] for name in sorted(self.slice_hashes)
            },
            "previous_slice_hashes_available": bool(self.previous_slice_hashes),
            "changed_slices": list(self.changed_slices),
            "skipped_slices": list(self.skipped_slices),
            "full_apply_required": self.full_apply_required,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class _ApplySliceDecision:
    plan: ApplySlicePlan | None

    @property
    def full_apply(self) -> bool:
        return self.plan is None or self.plan.full_apply_required

    def changed(self, name: str) -> bool:
        return self.full_apply or bool(self.plan and self.plan.slice_changed(name))

    def any_changed(self, names: tuple[str, ...]) -> bool:
        return self.full_apply or any(self.changed(name) for name in names)


def _successful_apply_plan_details(
    apply_plan: ApplySlicePlan, apply_details: dict[str, Any]
) -> dict[str, object]:
    details = apply_plan.as_details()
    if not apply_details.get("cluster_followers_unavailable"):
        return details

    slice_hashes = details.get("slice_hashes")
    if not isinstance(slice_hashes, dict):
        return details

    previous_hash = apply_plan.previous_slice_hashes.get("cluster_followers")
    if previous_hash:
        slice_hashes["cluster_followers"] = previous_hash
    else:
        slice_hashes.pop("cluster_followers", None)
    details["unapplied_slices"] = ["cluster_followers"]
    return details


def _apply_output_event_details(
    desired: DesiredState, apply_plan: ApplySlicePlan
) -> dict[str, object]:
    previous_hashes = apply_plan.previous_slice_hashes
    current_backend_names = {
        runtime_backend_slice_name(backend): backend.name
        for backend in desired.known_app_backends
    }
    changed_runtime_slices = apply_plan.changed_runtime_slice_names
    created_names = sorted(
        backend_name
        for slice_name, backend_name in current_backend_names.items()
        if slice_name in changed_runtime_slices and slice_name not in previous_hashes
    )
    updated_names = sorted(
        backend_name
        for slice_name, backend_name in current_backend_names.items()
        if slice_name in changed_runtime_slices and slice_name in previous_hashes
    )
    deleted_count = len(apply_plan.deleted_runtime_slice_names)
    return {
        "outputs_total": len(desired.known_app_backends),
        "outputs_created": len(created_names),
        "outputs_updated": len(updated_names),
        "outputs_deleted": deleted_count,
        "output_created_names": created_names,
        "output_updated_names": updated_names,
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
    """Force the next apply to rebuild every slice after live state may have drifted."""
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


async def _load_previous_successful_slice_hashes(
    session: AsyncSession,
) -> tuple[dict[str, str], str, str]:
    row = await session.get(HostApplyState, 1)
    if row is None or row.last_successful_apply_run_id is None:
        return {}, "no_previous_successful_apply", ""
    run = await session.get(ApplyRun, row.last_successful_apply_run_id)
    if run is None:
        return {}, "previous_successful_apply_missing", ""
    desired_state_hash = str(run.desired_state_hash or "")
    try:
        details = json.loads(run.details_json or "{}")
    except json.JSONDecodeError:
        return {}, "previous_successful_apply_details_invalid", desired_state_hash
    if not isinstance(details, dict):
        return {}, "previous_successful_apply_details_invalid", desired_state_hash
    if details.get("slice_schema_version") != APPLY_SLICE_SCHEMA_VERSION:
        return {}, "slice_schema_unavailable", desired_state_hash
    raw_hashes = details.get("slice_hashes")
    if not isinstance(raw_hashes, dict):
        return {}, "slice_hashes_unavailable", desired_state_hash
    hashes = {
        str(name): str(value)
        for name, value in raw_hashes.items()
        if isinstance(name, str) and isinstance(value, str) and name and value
    }
    if not hashes:
        return {}, "slice_hashes_unavailable", desired_state_hash
    return hashes, "", desired_state_hash


def _plan_apply_slices(
    slices: dict[str, ApplySlice],
    previous_hashes: dict[str, str],
    *,
    fallback_reason: str = "",
) -> ApplySlicePlan:
    current_hashes = {name: slices[name].hash for name in sorted(slices)}
    if fallback_reason or not previous_hashes:
        return ApplySlicePlan(
            schema_version=APPLY_SLICE_SCHEMA_VERSION,
            slice_hashes=current_hashes,
            previous_slice_hashes=previous_hashes,
            changed_slices=tuple(sorted(current_hashes)),
            skipped_slices=(),
            full_apply_required=True,
            fallback_reason=fallback_reason or "slice_hashes_unavailable",
        )
    changed = sorted(
        name
        for name, current_hash in current_hashes.items()
        if previous_hashes.get(name) != current_hash
    )
    deleted = sorted(name for name in previous_hashes if name not in current_hashes)
    skipped = sorted(
        name
        for name, current_hash in current_hashes.items()
        if previous_hashes.get(name) == current_hash
        and _slice_can_skip_first_pass(name)
    )
    return ApplySlicePlan(
        schema_version=APPLY_SLICE_SCHEMA_VERSION,
        slice_hashes=current_hashes,
        previous_slice_hashes=previous_hashes,
        changed_slices=tuple(sorted(set(changed + deleted))),
        skipped_slices=tuple(skipped),
    )


def _slice_can_skip_first_pass(name: str) -> bool:
    return name in {
        "host_base",
        "nginx.managed",
        "tailscale",
        "ssh_access",
        "shield",
        "netdata",
        "network_isolation",
        "cluster_followers",
    } or name.startswith("runtime.backend.")


def _slice_hash_map(slices: dict[str, ApplySlice]) -> dict[str, str]:
    return {name: slices[name].hash for name in sorted(slices)}


def _live_first_pass_slice_hashes(
    desired: DesiredState,
    settings: Settings,
    slices: dict[str, ApplySlice],
    *,
    services: ApplyServices,
) -> dict[str, str]:
    hashes: dict[str, str] = {
        "host_base": slices["host_base"].hash,
        "nginx.managed": _live_managed_nginx_slice_hash(settings.nginx_generated_dir),
        "tailscale": hash_apply_slice_payload(
            "tailscale",
            tailscale_slice_payload(
                _read_tailscale_state(settings.tailscale_serve_state_path),
                _read_tailscale_service_state(settings.tailscale_serve_state_path),
            ),
        ),
    }
    if _shield_runtime_live_matches(desired, settings):
        hashes["shield"] = slices["shield"].hash
    if _netdata_runtime_live_matches(desired, settings):
        hashes["netdata"] = slices["netdata"].hash
    if not desired.cluster_nodes:
        hashes["cluster_followers"] = slices["cluster_followers"].hash

    for backend in sorted(
        desired.known_app_backends, key=lambda item: (item.id or 0, item.name)
    ):
        name = runtime_backend_slice_name(backend)
        if backend.enabled:
            saved_spec = read_saved_spec(settings, backend.name)
            if isinstance(saved_spec, dict):
                # First-pass baselining trusts an existing CNC-owned runtime as live.
                # Seed/profile upgrades belong to explicit runtime changes or repair flows,
                # not unrelated route/input/output saves on legacy installs.
                hashes[name] = slices[name].hash
            continue

        saved_spec = read_saved_spec(settings, backend.name)
        _inspect_result, inspect_payload = services.runtime_services.inspect_container(
            container_name(backend.name),
            timeout_sec=settings.command_timeout_status_sec,
        )
        if saved_spec is None and inspect_payload is None:
            hashes[name] = slices[name].hash

    return {name: hashes[name] for name in sorted(hashes)}


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
) -> ApplyResponse:
    services = services or default_apply_services()
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
            async with logger.operation("apply.run"):
                try:
                    await _emit_apply_progress(
                        services, "Save desired state", "Building host plan"
                    )
                    desired = await build_desired_state(session, settings)
                    logger.info(
                        "apply.desired_state.ready",
                        nginx_files=len(desired.nginx_files),
                        app_backends=len(desired.enabled_app_backends),
                    )
                    desired_record = _desired_state_record(desired)
                    slices = render_apply_slices(desired, settings)
                    (
                        previous_slice_hashes,
                        fallback_reason,
                        previous_desired_state_hash,
                    ) = await _load_previous_successful_slice_hashes(session)
                    slice_hashes_bootstrapped = False
                    live_slice_hashes_bootstrapped = False
                    if (
                        fallback_reason
                        and previous_desired_state_hash
                        and previous_desired_state_hash
                        == desired_record.desired_state_hash
                    ):
                        previous_slice_hashes = _slice_hash_map(slices)
                        fallback_reason = ""
                        slice_hashes_bootstrapped = True
                    elif (
                        fallback_reason
                        and not previous_slice_hashes
                        and previous_desired_state_hash
                    ):
                        previous_slice_hashes = _live_first_pass_slice_hashes(
                            desired,
                            settings,
                            slices,
                            services=services,
                        )
                        fallback_reason = ""
                        live_slice_hashes_bootstrapped = True
                    apply_plan = _plan_apply_slices(
                        slices,
                        previous_slice_hashes,
                        fallback_reason=fallback_reason,
                    )
                    await _emit_apply_progress_plan(services, desired, apply_plan)
                    tailscale_service_host_precheck = (
                        _tailscale_service_host_precheck_skip_details(
                            desired,
                            settings,
                            apply_plan,
                        )
                    )
                    if _tailscale_service_host_precheck_required(
                        desired, settings, apply_plan
                    ):
                        await _emit_apply_progress(
                            services, "Prepare host", "Checking tailnet service host"
                        )
                        tailscale_service_host_precheck = (
                            await _verify_tailscale_service_host_precheck(
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
                            "slice_hashes_bootstrapped": slice_hashes_bootstrapped,
                            "live_slice_hashes_bootstrapped": live_slice_hashes_bootstrapped,
                            **apply_plan.as_details(),
                        },
                    )
                    if apply_plan.slice_changed("host_base"):
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
                    else:
                        runtime_assets = {
                            "skipped": True,
                            "reason": "host_base slice unchanged",
                        }
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
                        apply_plan=apply_plan,
                        operation_id=operation.id,
                    )
                    live_apply_completed = True
                    apply_plan_details = _successful_apply_plan_details(
                        apply_plan, details
                    )
                    response_details = {
                        "runtime_assets": runtime_assets,
                        "tailscale_admin_precheck": admin_exposure_precheck,
                        "tailscale_service_host_precheck": tailscale_service_host_precheck,
                        "config_revision": desired_record.config_revision,
                        "desired_state_hash": desired_record.desired_state_hash,
                        "slice_hashes_bootstrapped": slice_hashes_bootstrapped,
                        "live_slice_hashes_bootstrapped": live_slice_hashes_bootstrapped,
                        **apply_plan_details,
                        **details,
                    }
                    output_event = _apply_output_event_details(desired, apply_plan)
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
                    return ApplyResponse(
                        status="success",
                        message=success_message,
                        details=response_details,
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
                        details = {
                            **details,
                            "failure_mode": "partial",
                            "manual_review_required": True,
                            "operator_action": "rerun apply to reconcile durable desired state",
                        }
                    if desired_record is not None:
                        details = {
                            "config_revision": desired_record.config_revision,
                            "desired_state_hash": desired_record.desired_state_hash,
                            **details,
                        }
                    message = (
                        "apply partially failed"
                        if str(details.get("failure_mode") or "").strip() == "partial"
                        else "apply failed"
                    )
                    partial_failure = (
                        str(details.get("failure_mode") or "").strip() == "partial"
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
                        summary="Host apply partially failed"
                        if partial_failure
                        else "Host apply failed",
                        severity="warn" if partial_failure else "error",
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
                                "value": "partial" if partial_failure else "failed",
                            },
                        ],
                        details={
                            "run_id": run.id,
                            "status": "partial" if partial_failure else "error",
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
                            "Apply partially failed."
                            if partial_failure
                            else "Apply failed.",
                            status="warning" if partial_failure else "failure",
                            facts=_apply_failure_notification_facts(
                                details,
                                run_id=run.id,
                                manual_review_required=partial_failure,
                            ),
                            action="cnc-admin logs admin --errors",
                        ),
                        priority=0,
                        event="apply_failed",
                        source="apply",
                        run_id=run.id,
                        url=admin_dashboard_url(settings, tab="home"),
                        url_title="Open CNC",
                    )
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
                "Apply could not start cleanly.",
                status="failure",
                facts=_apply_failure_notification_facts(failure_details),
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


def _changed_runtime_backend_names(
    desired: DesiredState, apply_plan: ApplySlicePlan | None
) -> set[str]:
    if apply_plan is None:
        return {backend.name for backend in desired.known_app_backends}
    if apply_plan.slice_changed("apps_memory"):
        return {backend.name for backend in desired.known_app_backends}
    changed_slice_names = apply_plan.changed_runtime_slice_names
    return {
        backend.name
        for backend in desired.known_app_backends
        if runtime_backend_slice_name(backend) in changed_slice_names
    }


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


def _live_managed_nginx_files(nginx_dir: Path) -> dict[str, str] | None:
    live_files: dict[str, str] = {}
    if nginx_dir.exists():
        try:
            paths = list(nginx_dir.iterdir())
        except OSError:
            return None
        for path in paths:
            if not path.name.startswith(MANAGED_PREFIX):
                continue
            if not path.is_file():
                return None
            try:
                live_files[path.name] = path.read_text(encoding="utf-8")
            except OSError:
                return None
    return live_files


def _live_managed_nginx_slice_hash(nginx_dir: Path) -> str:
    live_files = _live_managed_nginx_files(nginx_dir) or {}
    live_hash = hash_apply_slice_payload(
        "nginx.managed", nginx_managed_slice_payload(live_files)
    )
    return live_hash


def _live_managed_nginx_matches(nginx_dir: Path, desired_files: dict[str, str]) -> bool:
    live_files = _live_managed_nginx_files(nginx_dir)
    if live_files is None:
        return False
    live_hash = hash_apply_slice_payload(
        "nginx.managed", nginx_managed_slice_payload(live_files)
    )
    desired_hash = hash_apply_slice_payload(
        "nginx.managed", nginx_managed_slice_payload(desired_files)
    )
    return live_hash == desired_hash


def _tailscale_state_matches_desired(desired: DesiredState, settings: Settings) -> bool:
    state_path = settings.tailscale_serve_state_path
    return (
        _read_tailscale_state(state_path) == desired.tailscale_paths
        and _read_tailscale_service_state(state_path) == desired.tailscale_services
    )


def _shield_runtime_live_matches(desired: DesiredState, settings: Settings) -> bool:
    if not desired.shield_required:
        return not _shield_runtime_assets_exist(settings)
    expected_files = {
        shield_quadlet_path(settings): render_shield_quadlet(settings),
        settings.shield_env_file_path: render_shield_env(settings),
        settings.shield_config_path: render_shield_config(
            desired.shield_output_code_hashes
        ),
    }
    for path, expected_content in expected_files.items():
        try:
            if path.read_text(encoding="utf-8") != expected_content:
                return False
        except OSError:
            return False
    return True


def _netdata_runtime_live_matches(desired: DesiredState, settings: Settings) -> bool:
    if not desired.netdata_required:
        return True
    return netdata_native_runtime_live_matches(settings)


def _disabled_runtime_absent_backend_names(
    desired: DesiredState,
    settings: Settings,
    backend_names: set[str],
    *,
    services: ApplyServices,
) -> set[str]:
    known_backends = {backend.name: backend for backend in desired.known_app_backends}
    absent: set[str] = set()
    for backend_name in sorted(backend_names):
        backend = known_backends.get(backend_name)
        if (
            backend is None
            or backend.enabled
            or backend_name in desired.runtime_graph.enabled_app_backend_names
        ):
            continue
        if read_saved_spec(settings, backend_name) is not None:
            continue
        if (
            quadlet_container_path(settings, backend_name).exists()
            or quadlet_network_path(settings, backend_name).exists()
        ):
            continue
        _inspect_result, inspect_payload = services.runtime_services.inspect_container(
            container_name(backend_name),
            timeout_sec=settings.command_timeout_status_sec,
        )
        if inspect_payload is None:
            absent.add(backend_name)
    return absent


async def _apply_desired_state(
    desired: DesiredState,
    settings: Settings,
    *,
    services: ApplyServices,
    apply_plan: ApplySlicePlan | None = None,
    operation_id: int | None = None,
) -> dict[str, Any]:
    nginx_dir = settings.nginx_generated_dir
    settings.apply_backup_dir.mkdir(parents=True, exist_ok=True)
    progress = _ApplyProgressTracker()
    rollback_nginx_dir: Path | None = None
    slices = _ApplySliceDecision(apply_plan)
    changed_runtime_backends = (
        _changed_runtime_backend_names(desired, apply_plan)
        if apply_plan is not None
        else {backend.name for backend in desired.known_app_backends}
    )
    network_isolation_changed = slices.changed("network_isolation")
    runtime_cleanup_required = bool(
        apply_plan and apply_plan.deleted_runtime_slice_names
    )
    runtime_live_drift_reasons: dict[str, str] = {}
    if not slices.full_apply and desired.known_app_backends:
        runtime_live_drift_reasons = legacy_direct_podman_migration_backend_reasons(
            desired,
            settings,
            services=services.runtime_services,
            candidate_backend_names=desired.runtime_graph.enabled_app_backend_names
            - changed_runtime_backends,
        )
        changed_runtime_backends.update(runtime_live_drift_reasons)
    disabled_runtime_absent_backends = (
        _disabled_runtime_absent_backend_names(
            desired,
            settings,
            changed_runtime_backends,
            services=services,
        )
        if changed_runtime_backends and not slices.full_apply
        else set()
    )
    runtime_reconcile_backends = (
        changed_runtime_backends - disabled_runtime_absent_backends
    )
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
    if runtime_live_drift_reasons:
        details["runtime_live_drift_backends"] = sorted(runtime_live_drift_reasons)
        details["runtime_live_drift_reasons"] = {
            backend: runtime_live_drift_reasons[backend]
            for backend in sorted(runtime_live_drift_reasons)
        }
    if disabled_runtime_absent_backends:
        details["runtime_absent_disabled_backends"] = sorted(
            disabled_runtime_absent_backends
        )
    try:
        if desired.known_app_backends or runtime_cleanup_required:
            await _emit_apply_progress(
                services, "Plan runtime", "Inspecting app container"
            )
            if (
                slices.full_apply
                or runtime_reconcile_backends
                or runtime_cleanup_required
            ):
                if desired.known_app_backends or runtime_cleanup_required:
                    try:
                        details["app_quadlet_dir_preflight"] = (
                            verify_app_quadlet_dir_writable(settings)
                        )
                    except AppQuadletWriteError as exc:
                        raise ApplyFailed(
                            "app Quadlet directory is not writable",
                            phase="app_runtime",
                            details=exc.details,
                        ) from exc
                progress.live_mutation("app_runtime")
                runtime_kwargs = {
                    "services": services.runtime_services,
                    "selected_backend_names": None
                    if slices.full_apply
                    else runtime_reconcile_backends,
                    "cleanup_deleted": slices.full_apply or runtime_cleanup_required,
                    "reconcile_network_isolation": network_isolation_changed,
                    "operation_id": operation_id,
                }
                if not services.cleanup_deleted_app_filesystem:
                    runtime_kwargs["cleanup_deleted_filesystem"] = False
                details.update(
                    await services.apply_app_backends(
                        desired,
                        settings,
                        **runtime_kwargs,
                    )
                )
            else:
                details.update(_skipped_app_runtime_details(desired, settings))
            progress.completed("app_runtime")

        shield_assets_exist = _shield_runtime_assets_exist(settings)
        if shield_assets_exist or desired.shield_required:
            progress.enter("shield_runtime")
            shield_changed = slices.changed("shield")
            shield_live_matches = _shield_runtime_live_matches(desired, settings)
            if shield_changed or not shield_live_matches:
                await _emit_apply_progress(
                    services, "Prepare Shield", "Preparing Shield gate"
                )
                progress.live_mutation()
                try:
                    details["shield_runtime"] = await _reconcile_shield_runtime(
                        desired, settings, services=services
                    )
                except Exception as exc:
                    raise ApplyFailed(
                        "shield runtime reconcile failed",
                        phase="shield_runtime",
                        details={"error": str(exc)},
                    ) from exc
            else:
                details["shield_runtime"] = {
                    "skipped": True,
                    "reason": "shield slice unchanged and live assets match",
                    "required": bool(desired.shield_required),
                }
            progress.completed()

        if desired.netdata_required:
            progress.enter("netdata_runtime")
            netdata_changed = slices.changed("netdata")
            netdata_live_matches = _netdata_runtime_live_matches(desired, settings)
            if netdata_changed or not netdata_live_matches:
                progress.live_mutation()
                try:
                    details["netdata_runtime"] = await _reconcile_netdata_runtime(
                        settings, services=services
                    )
                except Exception as exc:
                    raise ApplyFailed(
                        "netdata runtime reconcile failed",
                        phase="netdata_runtime",
                        details={"error": str(exc)},
                    ) from exc
            else:
                details["netdata_runtime"] = {
                    "skipped": True,
                    "reason": "netdata slice unchanged and live assets match",
                    "required": True,
                }
            progress.completed()

        ssh_access_changed = slices.changed("ssh_access")
        if desired.known_app_backends and ssh_access_changed:
            try:
                progress.enter("ssh_access")
                await _emit_apply_progress(
                    services, "Apply host access", "Reconciling backend SSH"
                )
                progress.live_mutation()
                ssh_details = await services.reconcile_backend_ssh_access(
                    [
                        {
                            "name": backend.name,
                            "public_key": str(backend.ssh_public_key or "").strip()
                            or None,
                        }
                        for backend in desired.enabled_app_backends
                    ],
                    settings,
                )
            except Exception as exc:
                raise ApplyFailed(
                    "backend ssh access reconcile failed",
                    phase="ssh_access",
                    details={"error": str(exc)},
                ) from exc
            details.update(ssh_details)
            progress.completed()
        elif desired.known_app_backends:
            details["ssh_access"] = {
                "skipped": True,
                "reason": "ssh_access slice unchanged",
            }
            progress.completed("ssh_access")

        progress.enter("sync_files")
        nginx_live_matches = _live_managed_nginx_matches(nginx_dir, desired.nginx_files)
        nginx_changed = slices.changed("nginx.managed")
        if nginx_changed or not nginx_live_matches:
            details["nginx_live_drift_reconciled"] = (
                not nginx_changed and not nginx_live_matches
            )
            await _emit_apply_progress(
                services, "Apply host access", "Staging host proxy config"
            )
            try:
                staged_nginx_dir = _write_staged_managed_dir(
                    nginx_dir, desired.nginx_files
                )
                rollback_nginx_dir = _swap_managed_dir(nginx_dir, staged_nginx_dir)
            except OSError as exc:
                raise ApplyFailed(
                    "failed to stage managed nginx files",
                    phase="sync_files",
                    details={
                        "error": str(exc),
                        "target_dir": str(nginx_dir),
                    },
                ) from exc
            progress.completed("sync_files")

            progress.enter("nginx_validate")
            await _emit_apply_progress(
                services, "Apply host access", "Validating host proxy config"
            )
            try:
                await services.runtime_services.run_command_checked_async(
                    ["nginx", "-t"],
                    timeout_sec=settings.command_timeout_apply_sec,
                )
            except CommandError as exc:
                raise ApplyFailed(
                    "nginx -t failed",
                    phase="nginx_validate",
                    error_code=ErrorCode.APPLY_NGINX_CONFIG_INVALID,
                    details={
                        "command": exc.result.command,
                        "stdout": clip_output(exc.result.stdout),
                        "stderr": clip_output(exc.result.stderr),
                    },
                ) from exc
            progress.completed("nginx_validate")

            progress.enter("nginx_reload")
            await _emit_apply_progress(
                services, "Apply host access", "Reloading host proxy"
            )
            progress.live_mutation()
            await services.runtime_services.run_command_checked_async(
                ["systemctl", "reload", "nginx"],
                timeout_sec=settings.command_timeout_apply_sec,
            )
            progress.completed()
        else:
            details["nginx_skipped"] = True
            details["nginx_skip_reason"] = (
                "nginx.managed slice unchanged and live files match"
            )
            progress.completed("sync_files")
            progress.completed("nginx_validate")
            progress.completed("nginx_reload")

        progress.enter("cluster_followers")
        cluster_followers_changed = slices.changed("cluster_followers")
        if cluster_followers_changed:
            await _emit_apply_progress(
                services, "Apply cluster", "Syncing follower ingress"
            )
            if desired.cluster_nodes:
                progress.live_mutation()
            try:
                details.update(
                    await asyncio.to_thread(
                        services.reconcile_cluster_followers,
                        desired,
                        settings,
                    )
                )
            except Exception as exc:
                raise ApplyFailed(
                    "follower ingress reconcile failed",
                    phase="cluster_followers",
                    details={"error": str(exc)},
                ) from exc
        else:
            details["cluster_followers"] = []
            details["cluster_followers_skipped"] = True
            details["cluster_followers_skip_reason"] = (
                "cluster_followers slice unchanged"
            )
        progress.completed()

        progress.enter("tailscale_paths")
        tailscale_changed = slices.changed("tailscale")
        tailscale_state_matches = _tailscale_state_matches_desired(desired, settings)
        if tailscale_changed or not tailscale_state_matches:
            if desired.tailscale_paths:
                await _emit_apply_progress(
                    services, "Apply host access", "Updating tailnet paths"
                )
            progress.live_mutation()
            details.update(
                await _apply_tailscale_paths(
                    desired.tailscale_paths, settings, services=services
                )
            )
            progress.completed()

            progress.enter("tailscale_services")
            if desired.tailscale_services:
                await _emit_apply_progress(
                    services, "Apply host access", "Updating tailnet services"
                )
            progress.live_mutation()
            details.update(
                await _apply_tailscale_services(
                    desired.tailscale_services, settings, services=services
                )
            )
            progress.completed()
        else:
            details["tailscale_paths"] = [
                {"path": path, "target": target}
                for path, target in sorted(desired.tailscale_paths.items())
            ]
            details["tailscale_services"] = [
                {"service": service_name, "target": target}
                for service_name, target in sorted(desired.tailscale_services.items())
            ]
            details["tailscale_skipped"] = True
            details["tailscale_skip_reason"] = (
                "tailscale slice unchanged and local state matches"
            )
            progress.completed("tailscale_paths")
            progress.completed("tailscale_services")
        admin_verify_required = slices.any_changed(("nginx.managed", "tailscale"))
        if admin_verify_required:
            try:
                progress.enter("tailscale_admin_verify")
                await _emit_apply_progress(
                    services, "Verify output", "Verifying admin exposure"
                )
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
        else:
            details["tailscale_admin_verify"] = {
                "skipped": True,
                "reason": "admin exposure was already checked before unchanged ingress apply",
            }
        progress.completed("tailscale_admin_verify")
        self_audit_required = slices.any_changed(
            (
                "host_base",
                "network_isolation",
                "nginx.managed",
                "cluster_followers",
                "shield",
                "tailscale",
            )
        )
        if self_audit_required:
            try:
                progress.enter("control_plane_self_audit")
                await _emit_apply_progress(
                    services, "Verify output", "Auditing control plane"
                )
                audit_payload = await _run_control_plane_self_audit_for_apply(
                    settings, services=services
                )
                details["control_plane_self_audit"] = {
                    "ok": True,
                    **audit_payload,
                }
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
            else:
                progress.completed()
        else:
            details["control_plane_self_audit"] = {
                "skipped": True,
                "reason": "heavy self-audit skipped because host safety slices were unchanged",
            }
            progress.completed("control_plane_self_audit")
        details.update(progress.as_details())
    except Exception as exc:
        apply_failure = progress.failure(exc)
        rollback_mode = _nginx_rollback_mode(apply_failure)
        if rollback_nginx_dir is None:
            rollback_status = "not_staged"
        elif rollback_mode == "restore_previous_live_state":
            await _restore_nginx_live_state_after_failure(
                nginx_dir,
                rollback_nginx_dir,
                settings,
                apply_failure,
                services=services,
            )
            rollback_status = "restored"
        else:
            _rollback_swapped_managed_dir(nginx_dir, rollback_nginx_dir)
            rollback_status = "removed_candidate"
        apply_failure.details["nginx_rollback"] = {
            "mode": rollback_mode,
            "attempted": rollback_mode == "restore_previous_live_state",
            "status": rollback_status,
        }
        raise apply_failure

    _finalize_swapped_managed_dir(rollback_nginx_dir)
    return details


async def _reconcile_shield_runtime(
    desired: DesiredState,
    settings: Settings,
    *,
    services: ApplyServices,
) -> dict[str, Any]:
    if not desired.shield_required:
        had_assets = _shield_runtime_assets_exist(settings)
        if had_assets:
            try:
                await services.runtime_services.run_command_checked_async(
                    ["systemctl", "disable", "--now", SHIELD_CONTAINER_SERVICE],
                    timeout_sec=settings.command_timeout_apply_sec,
                )
            except CommandError:
                pass
        removed = remove_shield_runtime_assets(settings)
        if removed:
            await services.runtime_services.run_command_checked_async(
                ["systemctl", "daemon-reload"],
                timeout_sec=settings.command_timeout_apply_sec,
            )
        return {"required": False, "removed_files": removed}

    result = write_shield_runtime_assets(
        settings,
        output_code_hashes=desired.shield_output_code_hashes,
    )
    await services.runtime_services.run_command_checked_async(
        ["systemctl", "daemon-reload"],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    await services.runtime_services.run_command_checked_async(
        ["systemctl", "restart", SHIELD_CONTAINER_SERVICE],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    return {"required": True, **result}


async def _reconcile_netdata_runtime(
    settings: Settings,
    *,
    services: ApplyServices,
) -> dict[str, Any]:
    return await reconcile_netdata_native_runtime(
        settings, services=services.runtime_services
    )


def _shield_runtime_assets_exist(settings: Settings) -> bool:
    return any(
        path.exists()
        for path in (
            shield_quadlet_path(settings),
            settings.shield_env_file_path,
            settings.shield_config_path,
        )
    )


def _read_tailscale_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_path, raw_target in paths.items():
        if not isinstance(raw_path, str) or not isinstance(raw_target, str):
            continue
        normalized[raw_path] = raw_target
    return normalized


def _read_tailscale_service_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    services = payload.get("services")
    if not isinstance(services, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_service, raw_target in services.items():
        if not isinstance(raw_service, str) or not isinstance(raw_target, str):
            continue
        normalized[raw_service] = raw_target
    return normalized


def _write_tailscale_state(
    path: Path, *, paths: dict[str, str], services: dict[str, str]
) -> None:
    write_json_atomic(path, {"paths": paths, "services": services})


def _tailscale_service_host_precheck_required(
    desired: DesiredState,
    settings: Settings,
    apply_plan: ApplySlicePlan | None,
) -> bool:
    if not desired.tailscale_services:
        return False
    if apply_plan is None or apply_plan.slice_changed("tailscale"):
        return True
    return not _tailscale_state_matches_desired(desired, settings)


def _tailscale_service_host_precheck_skip_details(
    desired: DesiredState,
    settings: Settings,
    apply_plan: ApplySlicePlan | None,
) -> dict[str, object]:
    services = sorted(desired.tailscale_services)
    if not services:
        return {"skipped": True, "reason": "no tailscale service inputs"}
    if not _tailscale_service_host_precheck_required(desired, settings, apply_plan):
        return {
            "skipped": True,
            "reason": "tailscale service slice unchanged and local state matches",
            "services": services,
        }
    return {"skipped": False, "services": services}


async def _verify_tailscale_service_host_precheck(
    services_map: dict[str, str],
    settings: Settings,
    *,
    services: ApplyServices,
) -> dict[str, object]:
    service_names = sorted(services_map)
    command = ["tailscale", "status", "--self", "--json"]
    try:
        result = await _run_retryable_checked_command(
            command,
            timeout_sec=settings.command_timeout_status_sec,
            settings=settings,
            services=services,
        )
    except CommandError as exc:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": exc.result.stderr
                or exc.result.stdout
                or "tailscale status failed",
                "command": exc.result.command,
                "stdout": clip_output(exc.result.stdout),
                "stderr": clip_output(exc.result.stderr),
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        ) from exc

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": "tailscale status returned invalid JSON",
                "command": command,
                "stdout": clip_output(result.stdout),
                "stderr": clip_output(result.stderr),
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        ) from exc

    self_payload = payload.get("Self") if isinstance(payload, dict) else None
    if not isinstance(self_payload, dict):
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": "tailscale status did not include the local node identity",
                "command": command,
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        )

    raw_tags = self_payload.get("Tags")
    tags = [str(tag).strip() for tag in raw_tags] if isinstance(raw_tags, list) else []
    tags = [tag for tag in tags if tag]
    hostname = str(
        self_payload.get("HostName") or self_payload.get("DNSName") or ""
    ).strip()
    precheck = {
        "ok": bool(tags),
        "services": service_names,
        "tagged_host": bool(tags),
        "tags": tags,
        "hostname": hostname,
    }
    if not tags:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": TAILSCALE_SERVICE_TAG_REQUIRED_MESSAGE,
                "command": command,
                "tailscale_service_host_precheck": precheck,
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        )
    return precheck


async def _apply_tailscale_paths(
    paths: dict[str, str],
    settings: Settings,
    *,
    services: ApplyServices | None = None,
) -> dict[str, Any]:
    services = services or default_apply_services()
    state_path = settings.tailscale_serve_state_path
    previous_paths = _read_tailscale_state(state_path)
    previous_services = _read_tailscale_service_state(state_path)
    if not paths and not previous_paths:
        return {"tailscale_paths": []}

    state_path.parent.mkdir(parents=True, exist_ok=True)
    await _run_retryable_checked_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=settings.command_timeout_status_sec,
        settings=settings,
        services=services,
    )

    for stale_path in sorted(set(previous_paths) - set(paths)):
        for stale_path_variant in _tailscale_path_off_variants(stale_path):
            await _run_tailscale_serve_off_idempotent(
                [
                    "tailscale",
                    "serve",
                    "--bg",
                    "--yes",
                    "--https=443",
                    f"--set-path={stale_path_variant}",
                    "off",
                ],
                timeout_sec=settings.command_timeout_apply_sec,
                settings=settings,
                services=services,
            )

    for path, target in sorted(paths.items()):
        await _run_retryable_checked_command(
            [
                "tailscale",
                "serve",
                "--bg",
                "--yes",
                "--https=443",
                f"--set-path={path}",
                target,
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    _write_tailscale_state(state_path, paths=paths, services=previous_services)
    return {
        "tailscale_paths": [
            {"path": path, "target": target} for path, target in sorted(paths.items())
        ]
    }


async def _apply_tailscale_services(
    services_map: dict[str, str],
    settings: Settings,
    *,
    services: ApplyServices | None = None,
) -> dict[str, Any]:
    services = services or default_apply_services()
    state_path = settings.tailscale_serve_state_path
    previous_paths = _read_tailscale_state(state_path)
    previous_services = _read_tailscale_service_state(state_path)
    if not services_map and not previous_services:
        return {"tailscale_services": []}

    state_path.parent.mkdir(parents=True, exist_ok=True)
    await _run_retryable_checked_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=settings.command_timeout_status_sec,
        settings=settings,
        services=services,
    )

    for stale_service in sorted(set(previous_services) - set(services_map)):
        await _run_tailscale_serve_off_idempotent(
            [
                "tailscale",
                "serve",
                "--yes",
                f"--service=svc:{stale_service}",
                "--https=443",
                "off",
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    for service_name, target in sorted(services_map.items()):
        await _run_retryable_checked_command(
            [
                "tailscale",
                "serve",
                "--yes",
                f"--service=svc:{service_name}",
                "--https=443",
                target,
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    _write_tailscale_state(state_path, paths=previous_paths, services=services_map)
    return {
        "tailscale_services": [
            {"service": service_name, "target": target}
            for service_name, target in sorted(services_map.items())
        ]
    }


async def _run_retryable_checked_command(
    command: list[str],
    *,
    timeout_sec: int,
    settings: Settings,
    services: ApplyServices,
) -> Any:
    attempts = settings.transient_command_retry_attempts + 1

    def should_retry(exc: Exception) -> bool:
        return isinstance(exc, CommandError) and command_result_is_retryable(exc.result)

    def log_retry(
        exc: Exception, attempt: int, max_attempts: int, delay_sec: float
    ) -> None:
        logger.warning(
            "apply.command.retrying",
            command=" ".join(command),
            attempt=attempt,
            max_attempts=max_attempts,
            backoff_sec=delay_sec,
            error=str(exc),
        )

    return await retry_async_call(
        lambda: services.runtime_services.run_command_checked_async(
            command, timeout_sec=timeout_sec
        ),
        attempts=attempts,
        backoff_sec=settings.transient_command_retry_backoff_sec,
        should_retry=should_retry,
        on_retry=log_retry,
    )


async def _run_tailscale_serve_off_idempotent(
    command: list[str],
    *,
    timeout_sec: int,
    settings: Settings,
    services: ApplyServices,
) -> Any:
    try:
        return await _run_retryable_checked_command(
            command,
            timeout_sec=timeout_sec,
            settings=settings,
            services=services,
        )
    except CommandError as exc:
        if _tailscale_serve_off_missing_handler(exc):
            logger.info("tailscale.serve_off.already_absent", command=" ".join(command))
            return exc.result
        raise


def _tailscale_serve_off_missing_handler(exc: CommandError) -> bool:
    result = exc.result
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "handler does not exist" in output or "failed to remove web serve" in output


def _tailscale_path_off_variants(path: str) -> list[str]:
    variants = [path]
    if path != "/" and path.endswith("/"):
        variants.append(path.rstrip("/"))
    elif path != "/":
        variants.append(f"{path}/")
    return list(dict.fromkeys(variants))


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


def _write_staged_managed_dir(
    target_dir: Path,
    desired_files: dict[str, str],
    *,
    prefix: str = MANAGED_PREFIX,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    staged_dir = Path(
        tempfile.mkdtemp(prefix=f".{target_dir.name}.stage.", dir=str(target_dir))
    )

    for filename, content in desired_files.items():
        if not filename.startswith(prefix):
            raise ApplyFailed(
                f"managed filename must start with {prefix}: {filename}",
                phase="sync_files",
            )
        _write_file_atomic(staged_dir / filename, content)

    return staged_dir


def _swap_managed_dir(target_dir: Path, staged_dir: Path) -> Path | None:
    rollback_dir = Path(
        tempfile.mkdtemp(prefix=f".{target_dir.name}.rollback.", dir=str(target_dir))
    )
    for path in list(target_dir.iterdir()):
        if path == staged_dir or path == rollback_dir:
            continue
        if not path.name.startswith(MANAGED_PREFIX):
            continue
        os.replace(path, rollback_dir / path.name)
    for path in list(staged_dir.iterdir()):
        os.replace(path, target_dir / path.name)
    staged_dir.rmdir()
    return rollback_dir


def _rollback_swapped_managed_dir(target_dir: Path, rollback_dir: Path | None) -> None:
    if target_dir.exists():
        for path in list(target_dir.iterdir()):
            if rollback_dir is not None and path == rollback_dir:
                continue
            if path.name.startswith(MANAGED_PREFIX):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
    if rollback_dir is not None and rollback_dir.exists():
        for path in list(rollback_dir.iterdir()):
            os.replace(path, target_dir / path.name)
        rollback_dir.rmdir()


def _finalize_swapped_managed_dir(rollback_dir: Path | None) -> None:
    if rollback_dir is not None and rollback_dir.exists():
        shutil.rmtree(rollback_dir)


async def _restore_nginx_live_state_after_failure(
    target_dir: Path,
    rollback_dir: Path | None,
    settings: Settings,
    original_error: Exception,
    *,
    services: ApplyServices,
) -> None:
    _rollback_swapped_managed_dir(target_dir, rollback_dir)
    try:
        await services.runtime_services.run_command_checked_async(
            ["systemctl", "reload", "nginx"],
            timeout_sec=settings.command_timeout_apply_sec,
        )
    except CommandError as rollback_exc:
        raise ApplyFailed(
            "apply failed and nginx rollback reload failed",
            phase="nginx_rollback",
            details={
                "rollback_command": rollback_exc.result.command,
                "rollback_stdout": clip_output(rollback_exc.result.stdout),
                "rollback_stderr": clip_output(rollback_exc.result.stderr),
                "apply_error": _format_apply_error_details(original_error),
            },
        ) from original_error


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


def _nginx_rollback_mode(error: ApplyFailed) -> str:
    completed_phases = error.details.get("completed_phases")
    completed = completed_phases if isinstance(completed_phases, list) else []
    return (
        "restore_previous_live_state"
        if "nginx_reload" in completed
        else "remove_unloaded_candidate"
    )


def _write_file_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{target.name}.tmp.", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
