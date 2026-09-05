from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.models.entities import Backend
from app.schemas.apply import ApplyResponse
from app.services.operation_runtime import (
    CREATE_BACKEND_OPERATION_SUBSTATE_PROGRESS_POINTS,
    create_backend_progress_value,
    create_output_progress_plan,
    input_progress_value,
    operation_progress_value,
    progress_plan_value,
)
from app.services.operations import OperationHandle, OperationHandleProxy
from app.ui.view_models import _save_apply_feedback

ProgressStepRule = tuple[tuple[tuple[str, ...], ...], tuple[str, str]]


INPUT_APPLY_STEP_RULES = (
    ((("building host plan",),), ("Save desired state", "Building host plan")),
    ((("tailnet path",),), ("Apply host access", "Updating tailnet paths")),
    ((("tailnet service",),), ("Apply host access", "Updating tailnet services")),
    (
        (("validating host proxy",), ("reload",)),
        ("Apply host access", "Validating host proxy config"),
    ),
    (
        (("checking admin exposure",), ("runtime assets",)),
        ("Apply host access", "Loading runtime state"),
    ),
)

DELETE_OUTPUT_APPLY_STEP_RULES = (
    ((("building host plan",),), ("Delete output", "Building host plan")),
    ((("inspect", "container"),), ("Plan runtime", "Removing app runtime")),
    ((("backend ssh",),), ("Apply host access", "Reconciling backend SSH")),
    ((("staging host proxy",),), ("Apply host access", "Staging host proxy config")),
    (
        (("validating host proxy",),),
        ("Apply host access", "Validating host proxy config"),
    ),
    ((("reload",),), ("Apply host access", "Reloading host proxy")),
    ((("verifying admin",),), ("Verify output", "Verifying admin exposure")),
    ((("audit",),), ("Verify output", "Auditing control plane")),
)

BACKUP_EXACT_SUBSTATES = {
    "backup requested": "Backup requested",
    "gathering app data": "Gathering app data",
    "collecting mounted paths": "Collecting mounted paths",
    "exporting container snapshot": "Exporting container snapshot",
    "verifying backup": "Verifying backup",
    "backup verified": "Backup verified",
    "saving backup record": "Saving backup record",
}

BACKUP_PREFIX_SUBSTATES = (
    (
        ("capturing container snapshot", "container snapshot"),
        "Capturing container snapshot",
    ),
    (("measured ",), "Measuring source data"),
    (("exporting path ", "exported path "), "Exporting mounted paths"),
    (("compressing backup bundle",), "Compressing backup bundle"),
    (("compressed backup",), "Writing backup bundle"),
)


def _create_output_progress_value(
    phase: str, current: int = 0, *, substate: str = ""
) -> int:
    substate_progress = CREATE_BACKEND_OPERATION_SUBSTATE_PROGRESS_POINTS.get(substate)
    if substate_progress is not None:
        return max(current, substate_progress)
    return create_backend_progress_value(phase, current, substep=substate)


def _operation_progress_details(
    pipeline: str,
    progress: int,
    message: str,
    *,
    phase: str = "",
    substate: str = "",
    **extra: object,
) -> dict[str, object]:
    normalized_phase = phase or message
    normalized_substate = substate or message
    return {
        "progress": operation_progress_value(
            pipeline,
            phase=normalized_phase,
            message=normalized_substate,
            current=progress,
        ),
        "message": message,
        **({"substate": normalized_substate} if normalized_substate else {}),
        **extra,
    }


def _planned_operation_progress_details(
    plan: dict[str, object],
    current: int,
    message: str,
    *,
    phase: str,
    substate: str,
    **extra: object,
) -> dict[str, object]:
    progress = progress_plan_value(
        plan,
        phase=phase,
        message=substate or message,
        current=current,
    )
    return {
        "progress": progress,
        "message": message,
        "substate": substate,
        "progress_plan": plan,
        **extra,
    }


def _backend_input_kinds(backend: Backend) -> list[str]:
    return [
        str(getattr(item, "kind", None) or "domain").strip().lower() or "domain"
        for item in getattr(backend, "inputs", []) or []
    ]


def _apply_slice_changed(apply_plan: object | None, name: str) -> bool:
    if apply_plan is None:
        return True
    slice_changed = getattr(apply_plan, "slice_changed", None)
    if callable(slice_changed):
        return bool(slice_changed(name))
    changed_slices = getattr(apply_plan, "changed_slices", ())
    return name in set(changed_slices or ())


def _create_output_progress_plan_for_apply(
    backend: Backend,
    *,
    desired: object | None = None,
    apply_plan: object | None = None,
) -> dict[str, object]:
    input_kinds = _backend_input_kinds(backend)
    full_apply = bool(getattr(apply_plan, "full_apply_required", False))
    changed_slices = {
        str(item) for item in (getattr(apply_plan, "changed_slices", ()) or ())
    }
    runtime_slice_changed = full_apply or any(
        item.startswith("runtime.backend.") for item in changed_slices
    )
    known_app_backends = getattr(desired, "known_app_backends", ()) if desired else ()
    desired_tailnet_paths = getattr(desired, "tailscale_paths", {}) if desired else {}
    desired_tailnet_services = (
        getattr(desired, "tailscale_services", {}) if desired else {}
    )
    desired_cluster_nodes = getattr(desired, "cluster_nodes", ()) if desired else ()
    desired_shield_required = bool(getattr(desired, "shield_required", False))
    ingress_changed = _apply_slice_changed(apply_plan, "nginx.managed")
    tailscale_changed = _apply_slice_changed(apply_plan, "tailscale")
    audit_changed = any(
        _apply_slice_changed(apply_plan, name)
        for name in (
            "host_base",
            "network_isolation",
            "nginx.managed",
            "cluster_followers",
            "shield",
            "tailscale",
        )
    )
    return create_output_progress_plan(
        backend_kind=backend.kind,
        input_kinds=input_kinds,
        include_runtime=backend.kind == "app"
        and (apply_plan is None or runtime_slice_changed),
        include_runtime_inspection=(
            backend.kind == "app" or bool(known_app_backends) or runtime_slice_changed
        ),
        include_tailnet_paths=(
            (tailscale_changed and bool(desired_tailnet_paths))
            if desired is not None
            else "tailnet_path" in input_kinds
        ),
        include_tailnet_services=(
            (tailscale_changed and bool(desired_tailnet_services))
            if desired is not None
            else "tailnet_service" in input_kinds
        ),
        include_host_assets=_apply_slice_changed(apply_plan, "host_base"),
        include_shield=(
            backend.kind == "shield"
            or bool(getattr(backend, "shield_enabled", False))
            or (
                desired is not None
                and _apply_slice_changed(apply_plan, "shield")
                and desired_shield_required
            )
        ),
        include_cluster=(
            _apply_slice_changed(apply_plan, "cluster_followers")
            and bool(desired_cluster_nodes)
        ),
        include_ingress=ingress_changed,
        include_admin_verify=ingress_changed or tailscale_changed,
        include_audit=apply_plan is None or audit_changed,
    )


def _normalized_progress_message(message: str) -> str:
    return " ".join(str(message or "").strip().rstrip(".").split())


def _backup_progress_substate(message: str) -> str:
    normalized = _normalized_progress_message(message)
    lowered = normalized.lower()
    exact = BACKUP_EXACT_SUBSTATES.get(lowered)
    if exact is not None:
        return exact
    for prefixes, label in BACKUP_PREFIX_SUBSTATES:
        if lowered.startswith(prefixes):
            return label
    return normalized


_restore_progress_substate = _clone_progress_substate = _normalized_progress_message


def _output_save_progress_details(
    progress: int,
    message: str,
    *,
    phase: str = "",
    substate: str = "",
    **extra: object,
) -> dict[str, object]:
    return _operation_progress_details(
        "outputSave",
        progress,
        message,
        phase=phase,
        substate=substate,
        **extra,
    )


def _input_progress_details(
    pipeline: str,
    progress: int,
    message: str,
    *,
    phase: str = "",
    substate: str = "",
    **extra: object,
) -> dict[str, object]:
    normalized_phase = phase or message
    normalized_substate = substate or message
    return {
        "progress": input_progress_value(
            pipeline,
            phase=normalized_phase,
            message=normalized_substate,
            current=progress,
        ),
        "message": message,
        **({"substate": normalized_substate} if normalized_substate else {}),
        **extra,
    }


def _normalized_step(
    phase: str,
    substate: str,
    rules: tuple[ProgressStepRule, ...],
    default: tuple[str, str],
) -> tuple[str, str]:
    normalized = f"{phase} {substate}".strip().lower()
    for alternatives, step in rules:
        if any(
            all(needle in normalized for needle in needles) for needles in alternatives
        ):
            return step
    return default


def _input_apply_step(phase: str, substate: str) -> tuple[str, str]:
    return _normalized_step(
        phase,
        substate,
        INPUT_APPLY_STEP_RULES,
        ("Apply host access", "Reconciling host routes"),
    )


def _input_apply_progress_reporter(
    operation: OperationHandle,
    *,
    pipeline: str,
    input_id: int | None = None,
    input_value: str = "",
    progress_plan: dict[str, object] | None = None,
) -> Callable[[str, str], Awaitable[None]]:
    last_emit: dict[str, object] = {"substate": "", "progress": 0}

    async def report(phase: str, substate: str) -> None:
        progress_phase, normalized_substate = _input_apply_step(phase, substate)
        progress = (
            progress_plan_value(
                progress_plan,
                phase=progress_phase,
                message=normalized_substate,
                current=int(last_emit["progress"]),
            )
            if progress_plan is not None
            else input_progress_value(
                pipeline,
                phase=progress_phase,
                message=normalized_substate,
                current=int(last_emit["progress"]),
            )
        )
        same_step = last_emit["substate"] == normalized_substate
        last_emit.update({"substate": normalized_substate, "progress": progress})
        if same_step:
            return
        await operation.update(
            status="running",
            phase=progress_phase,
            details={
                "progress": progress,
                "message": normalized_substate,
                "substate": normalized_substate,
                **({"progress_plan": progress_plan} if progress_plan else {}),
                **({"input_id": input_id} if input_id is not None else {}),
                **({"input_value": input_value} if input_value else {}),
            },
        )

    return report


def _delete_output_apply_step(phase: str, substate: str) -> tuple[str, str]:
    return _normalized_step(
        phase,
        substate,
        DELETE_OUTPUT_APPLY_STEP_RULES,
        (phase.strip() or "Apply host access", substate.strip()),
    )


def _delete_output_apply_progress_reporter(
    operation: OperationHandle,
    *,
    progress_plan: dict[str, object],
    backend_name: str,
) -> Callable[[str, str], Awaitable[None]]:
    last_emit: dict[str, object] = {"phase": "", "substate": "", "progress": 0}

    async def report(phase: str, substate: str) -> None:
        progress_phase, normalized_substate = _delete_output_apply_step(phase, substate)
        progress = progress_plan_value(
            progress_plan,
            phase=progress_phase,
            message=normalized_substate,
            current=int(last_emit["progress"]),
        )
        same_step = (
            last_emit["phase"] == progress_phase
            and last_emit["substate"] == normalized_substate
        )
        last_emit.update(
            {
                "phase": progress_phase,
                "substate": normalized_substate,
                "progress": progress,
            }
        )
        if same_step:
            return
        await operation.update(
            status="running",
            phase=progress_phase,
            details={
                "progress": progress,
                "message": normalized_substate or progress_phase,
                "substate": normalized_substate,
                "progress_plan": progress_plan,
                "backend_name": backend_name,
            },
        )

    return report


async def _complete_input_operation(
    operation: OperationHandle,
    apply_response: ApplyResponse,
    *,
    success_message: str,
    failure_prefix: str,
    input_id: int | None = None,
    input_value: str = "",
    extra_details: dict[str, object] | None = None,
) -> None:
    success_flash, error_flash = _save_apply_feedback(
        apply_response,
        success_message=success_message,
        failure_prefix=failure_prefix,
    )
    details: dict[str, object] = {
        "progress": 100,
        "message": success_flash or error_flash or success_message,
        "substate": "Refreshing dashboard",
        "flash_success": success_flash,
        "flash_error": error_flash,
        "apply_status": apply_response.status,
        "run_id": apply_response.run_id,
        **({"input_id": input_id} if input_id is not None else {}),
        **({"input_value": input_value} if input_value else {}),
        **(extra_details or {}),
    }
    status = "success" if apply_response.status == "success" else "failed"
    await operation.complete(
        status, phase="Finalize", error=error_flash, details=details
    )


class _CreateBackendProgressRecorder(OperationHandleProxy):
    def __init__(self, operation: OperationHandle) -> None:
        super().__init__(operation)
        self._timings: list[dict[str, object]] = []
        self._timing_index: dict[tuple[str, str], int] = {}
        self._last_progress = 0

    @property
    def last_progress(self) -> int:
        return self._last_progress

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _snapshot_timings(self) -> list[dict[str, object]]:
        return [dict(item) for item in self._timings]

    def _record_timing(
        self, phase: str, substate: str, *, progress: object = None
    ) -> list[dict[str, object]]:
        normalized_phase = phase.strip()
        normalized_substate = substate.strip()
        if not normalized_phase or not normalized_substate:
            return self._snapshot_timings()
        now = self._timestamp()
        key = (normalized_phase, normalized_substate)
        existing_index = self._timing_index.get(key)
        if existing_index is None:
            self._timing_index[key] = len(self._timings)
            item: dict[str, object] = {
                "phase": normalized_phase,
                "substate": normalized_substate,
                "first_seen_at": now,
                "last_seen_at": now,
                "updates": 1,
            }
            if isinstance(progress, (int, float)):
                item["progress"] = progress
            self._timings.append(item)
            return self._snapshot_timings()
        item = self._timings[existing_index]
        item["last_seen_at"] = now
        item["updates"] = int(item.get("updates") or 0) + 1
        if isinstance(progress, (int, float)):
            item["progress"] = progress
        return self._snapshot_timings()

    def _details_with_timings(
        self,
        phase: str | None,
        details: dict[str, object] | None,
        *,
        record: bool,
    ) -> dict[str, object] | None:
        if details is None:
            return None
        enriched = dict(details)
        progress = enriched.get("progress")
        if record and isinstance(progress, (int, float)):
            clamped_progress = max(self._last_progress, int(progress))
            enriched["progress"] = clamped_progress
            self._last_progress = clamped_progress
        if record and phase:
            substate = str(
                enriched.get("substate") or enriched.get("message") or ""
            ).strip()
            timings = self._record_timing(
                phase, substate, progress=enriched.get("progress")
            )
        else:
            timings = self._snapshot_timings()
        if timings:
            enriched["progress_timings"] = timings
        return enriched

    async def update(
        self,
        *,
        status: str | None = None,
        phase: str | None = None,
        error: str | None = None,
        config_revision: str | None = None,
        desired_state_hash: str | None = None,
        details: dict[str, object] | None = None,
        finished: bool = False,
    ) -> None:
        enriched_details = self._details_with_timings(
            phase,
            details,
            record=status in {None, "queued", "running"},
        )
        await self.operation.update(
            status=status,
            phase=phase,
            error=error,
            config_revision=config_revision,
            desired_state_hash=desired_state_hash,
            details=enriched_details,
            finished=finished,
        )

    async def complete(
        self,
        status: str,
        *,
        phase: str | None = None,
        error: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        enriched_details = self._details_with_timings(phase, details, record=False)
        await self.operation.complete(
            status, phase=phase, error=error, details=enriched_details
        )


def _create_backend_apply_progress_reporter(
    operation: _CreateBackendProgressRecorder,
    *,
    backend_name: str,
    progress_plan_getter: Callable[[], dict[str, object]] | None = None,
) -> Callable[[str, str], Awaitable[None]]:
    last_emit: dict[str, object] = {
        "phase": "",
        "substate": "",
        "progress": 0,
    }

    async def report(phase: str, substate: str) -> None:
        normalized_phase = phase.strip() or "Create output"
        normalized_substate = substate.strip()
        progress_plan = progress_plan_getter() if progress_plan_getter else None
        progress = (
            progress_plan_value(
                progress_plan,
                phase=normalized_phase,
                message=normalized_substate,
                current=int(last_emit["progress"]),
            )
            if progress_plan is not None
            else _create_output_progress_value(
                normalized_phase,
                int(last_emit["progress"]),
                substate=normalized_substate,
            )
        )
        same_step = (
            last_emit["phase"] == normalized_phase
            and last_emit["substate"] == normalized_substate
        )
        if same_step:
            last_emit["phase"] = normalized_phase
            last_emit["substate"] = normalized_substate
            last_emit["progress"] = progress
            return
        last_emit.update(
            {
                "phase": normalized_phase,
                "substate": normalized_substate,
                "progress": progress,
            }
        )
        await operation.update(
            status="running",
            phase=normalized_phase,
            details={
                "progress": progress,
                "message": normalized_substate or normalized_phase,
                "substate": normalized_substate,
                **({"progress_plan": progress_plan} if progress_plan else {}),
                "backend_name": backend_name,
            },
        )

    return report


def _output_save_apply_progress_reporter(
    operation: OperationHandle,
    *,
    backend_id: int,
    backend_name: str = "",
) -> Callable[[str, str], Awaitable[None]]:
    last_emit: dict[str, object] = {"phase": "", "substate": "", "progress": 0}

    async def report(phase: str, substate: str) -> None:
        normalized_phase = phase.strip() or "Save output"
        normalized_substate = substate.strip()
        progress = operation_progress_value(
            "outputSave",
            phase=normalized_phase,
            message=normalized_substate,
            current=int(last_emit["progress"]),
        )
        same_step = (
            last_emit["phase"] == normalized_phase
            and last_emit["substate"] == normalized_substate
        )
        last_emit.update(
            {
                "phase": normalized_phase,
                "substate": normalized_substate,
                "progress": progress,
            }
        )
        if same_step:
            return
        await operation.update(
            status="running",
            phase=normalized_phase,
            details={
                "progress": progress,
                "message": normalized_substate or normalized_phase,
                "substate": normalized_substate,
                "backend_id": backend_id,
                **({"backend_name": backend_name} if backend_name else {}),
            },
        )

    return report


async def _complete_output_save_operation(
    operation: OperationHandle,
    apply_response: ApplyResponse,
    *,
    success_message: str,
    failure_prefix: str,
    backend_id: int,
    backend_name: str = "",
    extra_details: dict[str, object] | None = None,
) -> None:
    success_flash, error_flash = _save_apply_feedback(
        apply_response,
        success_message=success_message,
        failure_prefix=failure_prefix,
    )
    details: dict[str, object] = {
        "progress": 100,
        "message": success_flash or error_flash or success_message,
        "substate": "Refreshing output",
        "flash_success": success_flash,
        "flash_error": error_flash,
        "apply_status": apply_response.status,
        "run_id": apply_response.run_id,
        "backend_id": backend_id,
        **({"backend_name": backend_name} if backend_name else {}),
        **(extra_details or {}),
    }
    status = "success" if apply_response.status == "success" else "failed"
    await operation.complete(
        status, phase="Finalize", error=error_flash, details=details
    )
