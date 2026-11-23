from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
import json
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import ApplyRun
from app.schemas.apply import ApplyResponse
from app.services.apply_core import DesiredState
from app.services.apply_service import (
    ApplyServices,
    default_apply_services,
    restore_host_to_desired_state,
    run_apply,
)
from app.services.apply_state import (
    build_desired_state,
    committed_desired_state_and_hash,
)
from app.services.change_scope import Scope, ToolScope
from app.services.control_events import emit_control_event
from app.services.error_reporting import ErrorCode, ensure_cnc_error
from app.services.host_journal import commit_verified
from app.services.operations import (
    OperationHandle,
    host_mutation_lock,
    host_mutation_operation,
    validate_operation_handle_contract,
)
from app.services.status_service import invalidate_status_cache
from app.services.thread_workers import run_to_completion
from app.services.tools.core import unwind_recovery


MutationApplyState = Literal["pending", "applying", "applied", "apply_failed"]
ApplyRunner = Callable[..., Awaitable[ApplyResponse]]

logger = get_logger("mutation_apply")


@dataclass(frozen=True)
class MutationApplyResult:
    operation: str
    state: MutationApplyState
    apply_response: ApplyResponse
    state_history: tuple[MutationApplyState, ...]

    @property
    def applied(self) -> bool:
        return self.state == "applied"


async def commit_and_apply(
    session: AsyncSession,
    settings: Settings,
    *,
    operation: str,
    actor: str = "system",
    apply_runner: ApplyRunner = run_apply,
    apply_services: ApplyServices | None = None,
    operation_handle: OperationHandle | None = None,
    scope: object | None = None,
) -> MutationApplyResult:
    async with host_mutation_lock(settings):
        if operation_handle is not None:
            return await _apply_then_commit(
                session,
                settings,
                operation=operation,
                actor=actor,
                apply_runner=apply_runner,
                apply_services=apply_services,
                operation_handle=operation_handle,
                scope=scope,
            )
        async with host_mutation_operation(
            settings, kind=operation, actor=actor, phase="apply"
        ) as owned_operation:
            result = await _apply_then_commit(
                session,
                settings,
                operation=operation,
                actor=actor,
                apply_runner=apply_runner,
                apply_services=apply_services,
                operation_handle=owned_operation,
                scope=scope,
            )
            if not owned_operation.completed:
                await _complete_owned_operation(owned_operation, result)
            return result


async def _complete_owned_operation(
    operation: OperationHandle, result: MutationApplyResult
) -> None:
    response = result.apply_response
    details = response.details
    if result.applied:
        await operation.complete(
            "success",
            phase="completed",
            details={
                "apply_run_id": response.run_id,
                "status": "success",
                "app_healthcheck_status": details.get("app_healthcheck_status"),
            },
        )
        return
    await operation.complete(
        "partial" if details.get("failure_mode") == "partial" else "failed",
        phase=str(details.get("phase") or "apply"),
        error=str(details.get("error") or response.message or "apply failed"),
        details={
            key: details[key]
            for key in (
                "phase",
                "failure_mode",
                "commit_outcome",
                "host_restored",
                "restore_error",
                "manual_review_required",
            )
            if key in details
        },
    )


async def _read_committed_baseline(
    session: AsyncSession, settings: Settings
) -> tuple[DesiredState | None, str | None]:
    try:
        return await committed_desired_state_and_hash(
            session, settings, scope=Scope.everything()
        )
    except Exception as exc:
        logger.debug("mutation_apply.baseline_read_skipped", error=str(exc))
        return None, None


async def _restore_after_failure(
    *,
    previous_desired: DesiredState | None,
    candidate_desired: DesiredState | None,
    settings: Settings,
    apply_services: ApplyServices | None,
    scope: object | None,
    recovery: list[Any],
    prefix: str = "",
) -> dict[str, Any]:
    if recovery and previous_desired is None:
        return await unwind_recovery(recovery, prefix)
    if recovery and any(getattr(plan, "undo", None) for plan in recovery):
        return await unwind_recovery(recovery, prefix)
    if previous_desired is not None:
        resolved_scope = scope if isinstance(scope, Scope) else Scope.everything()
        created_names: frozenset[str] = frozenset()
        if candidate_desired is not None:
            created_names = frozenset(
                {b.name for b in candidate_desired.known_app_backends}
                - {b.name for b in previous_desired.known_app_backends}
            )
        return await restore_host_to_desired_state(
            previous_desired,
            settings,
            services=apply_services or default_apply_services(),
            scope=resolved_scope,
            created_backend_names=created_names,
        )
    return await unwind_recovery(recovery, prefix)


async def _apply_then_commit(
    session: AsyncSession,
    settings: Settings,
    *,
    operation: str,
    actor: str,
    apply_runner: ApplyRunner,
    apply_services: ApplyServices | None,
    operation_handle: OperationHandle,
    scope: object | None,
) -> MutationApplyResult:
    # Do not flush before host convergence: SQLite must not hold a write lock
    # across slow host mutations.
    previous_desired, base_hash = await _read_committed_baseline(session, settings)
    candidate_desired: DesiredState | None = None
    recovery: list[Any] = []
    state_history: tuple[MutationApplyState, ...] = ("pending",)
    logger.info("mutation_apply.pending", operation=operation, state="pending")
    state_history = (*state_history, "applying")
    logger.info("mutation_apply.applying", operation=operation, state="applying")
    resolved_scope = scope if isinstance(scope, Scope) else Scope.everything()

    try:
        validate_operation_handle_contract(operation_handle)
        if isinstance(scope, Scope) and scope.tools == ToolScope.NONE:
            candidate_desired = await build_desired_state(
                session, settings, scope=scope
            )
            details = {
                **scope.as_details(),
                "selected_tools": [],
                "changed_tools": [],
                "host_changed": False,
            }
            apply_response = ApplyResponse(
                status="success",
                message="Configuration saved",
                details=details,
                host_changed=False,
            )
        else:
            if apply_runner is run_apply:
                candidate_desired = await build_desired_state(
                    session, settings, scope=resolved_scope
                )
            apply_response = await _run_apply_runner(
                apply_runner,
                session,
                settings,
                apply_services=apply_services,
                operation_handle=operation_handle,
                operation_kind=operation,
                actor=actor,
                scope=scope,
                recovery=recovery,
                desired=candidate_desired,
                previous_desired=previous_desired,
            )
    except asyncio.CancelledError:
        await run_to_completion(
            _restore_after_failure(
                previous_desired=previous_desired,
                candidate_desired=candidate_desired,
                settings=settings,
                apply_services=apply_services,
                scope=scope,
                recovery=recovery,
            )
        )
        await run_to_completion(_rollback_quietly(session))
        raise
    except Exception as exc:
        classified = ensure_cnc_error(exc, ErrorCode.APPLY_FAILED)
        error_fields = classified.log_context()
        logger.exception(
            "mutation_apply.apply_runner_failed",
            operation=operation,
            error=str(exc),
            **error_fields,
        )
        apply_response = ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "apply",
                **classified.details,
                "error": str(classified),
                **error_fields,
            },
            run_id=None,
        )
        apply_response.details.update(
            await _restore_after_failure(
                previous_desired=previous_desired,
                candidate_desired=candidate_desired,
                settings=settings,
                apply_services=apply_services,
                scope=scope,
                recovery=recovery,
            )
        )

    state: MutationApplyState = (
        "applied" if apply_response.status == "success" else "apply_failed"
    )
    if state == "applied":
        # Compare-and-swap: fail cleanly if committed configuration changed while applying.
        if base_hash is not None:
            current_desired, current_hash = await _read_committed_baseline(
                session, settings
            )
            if current_hash is not None and current_hash != base_hash:
                await _rollback_quietly(session)
                restore_details: dict[str, Any] = {}
                if apply_response.host_changed is not False:
                    restore_details = await _restore_after_failure(
                        previous_desired=current_desired or previous_desired,
                        candidate_desired=candidate_desired,
                        settings=settings,
                        apply_services=apply_services,
                        scope=scope,
                        recovery=recovery,
                    )
                details = {
                    **apply_response.details,
                    **restore_details,
                    "phase": "database_commit",
                    "error": "config changed while saving",
                    "commit_outcome": "rolled_back",
                }
                apply_response = ApplyResponse(
                    status="error",
                    message="config changed while saving",
                    details=details,
                    run_id=None,
                )
                state = "apply_failed"

    if state == "applied":
        pending_run_fields = apply_response.details.pop("_apply_run_fields", None)
        witness_run_id = apply_response.run_id
        if witness_run_id is None:
            run_kwargs: dict[str, Any] = (
                dict(pending_run_fields)
                if isinstance(pending_run_fields, dict)
                else {"operation_id": operation_handle.id}
            )
            run = ApplyRun(
                status="success",
                message=apply_response.message,
                details_json=json.dumps(apply_response.details, default=str),
                **run_kwargs,
            )
            session.add(run)
            await session.flush()
            if (
                isinstance(scope, Scope)
                and scope.tools is ToolScope.HOST
                and scope.is_full
                and isinstance(pending_run_fields, dict)
            ):
                from app.services.apply_service import _record_last_applied_state, DesiredStateRecord

                record = DesiredStateRecord(
                    config_revision=str(pending_run_fields.get("config_revision") or ""),
                    desired_state_hash=str(pending_run_fields.get("desired_state_hash") or ""),
                    desired_state_json=str(pending_run_fields.get("desired_state_json") or "{}"),
                    generated_nginx_files_json=str(pending_run_fields.get("generated_nginx_files_json") or "{}"),
                    runtime_graph_json=str(pending_run_fields.get("runtime_graph_json") or "{}"),
                    route_contracts_json=str(pending_run_fields.get("route_contracts_json") or "[]"),
                    backend_contracts_json=str(pending_run_fields.get("backend_contracts_json") or "[]"),
                    resource_profile_json=str(pending_run_fields.get("resource_profile_json") or "{}"),
                )
                await _record_last_applied_state(
                    session,
                    run=run,
                    operation_id=operation_handle.id,
                    desired_state=record,
                )
            witness_run_id = run.id
            apply_response = ApplyResponse(
                status=apply_response.status,
                message=apply_response.message,
                details=apply_response.details,
                host_changed=apply_response.host_changed,
                created_at=run.created_at,
                run_id=run.id,
            )

        commit = await commit_verified(session, settings, ApplyRun, witness_run_id)
        if commit.error:
            apply_response.details.update(
                commit_outcome=commit.outcome, commit_error=commit.error
            )
            if commit.rollback_error:
                apply_response.details["commit_rollback_error"] = commit.rollback_error
        if commit.outcome == "persisted":
            try:
                await _emit_deferred_apply_completed_event(settings, apply_response)
            except Exception as exc:
                apply_response.details["post_commit_reporting_error"] = str(exc)
                logger.exception(
                    "mutation_apply.completion_reporting_failed", error=str(exc)
                )
        else:
            details = dict(apply_response.details)
            details["convergence_invalidated"] = await _invalidate_host_apply_state(
                settings
            )
            if commit.outcome == "rolled_back":
                details.update(
                    await _restore_after_failure(
                        previous_desired=previous_desired,
                        candidate_desired=candidate_desired,
                        settings=settings,
                        apply_services=apply_services,
                        scope=scope,
                        recovery=recovery,
                        prefix="commit_",
                    )
                )
            else:
                details.update(
                    failure_mode="partial",
                    manual_review_required=True,
                    operator_action="compare the saved configuration against the host before rerunning",
                )
            details.update(
                phase="database_commit",
                error=commit.error,
                apply_run_id=apply_response.run_id,
            )
            apply_response = ApplyResponse(
                status="error",
                message="Database commit failed",
                run_id=apply_response.run_id,
                details=details,
            )
            state = "apply_failed"
    else:
        await _rollback_quietly(session)
        apply_response.details.setdefault("commit_outcome", "rolled_back")

    invalidate_status_cache(
        settings,
        prefill=state == "applied",
        isolation_changed=ToolScope.PODMAN in resolved_scope.tools,
    )
    state_history = (*state_history, state)
    logger.info(
        "mutation_apply.completed",
        operation=operation,
        state=state,
        apply_status=apply_response.status,
        apply_run_id=apply_response.run_id,
    )
    return MutationApplyResult(
        operation=operation,
        state=state,
        apply_response=apply_response,
        state_history=state_history,
    )


async def _invalidate_host_apply_state(settings: Settings) -> bool:
    from app.database import isolated_database_session
    from app.models.entities import HostApplyState

    try:
        async with isolated_database_session(settings.database_url) as recovery_session:
            marker = await recovery_session.get(HostApplyState, 1)
            if marker is not None:
                marker.last_applied_state_hash = None
                marker.last_successful_operation_id = None
                marker.last_successful_apply_run_id = None
                await recovery_session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "mutation_apply.convergence_invalidation_failed", error=str(exc)
        )
        return False


async def _rollback_quietly(session: AsyncSession) -> str | None:
    try:
        await session.rollback()
    except Exception as exc:
        return str(exc)
    return None


async def _run_apply_runner(
    apply_runner: ApplyRunner,
    session: AsyncSession,
    settings: Settings,
    *,
    apply_services: ApplyServices | None,
    operation_handle: OperationHandle | None,
    operation_kind: str,
    actor: str,
    scope: object | None = None,
    recovery: list[Any] | None = None,
    desired: DesiredState | None = None,
    previous_desired: DesiredState | None = None,
) -> ApplyResponse:
    kwargs: dict[str, Any] = {
        "services": apply_services,
        "persist_run": False,
        "emit_success_event": False,
        "operation_handle": operation_handle,
        "operation_kind": operation_kind,
        "actor": actor,
        "scope": scope,
        "recovery": recovery,
        "desired": desired,
        "previous_desired": previous_desired,
    }
    signature = inspect.signature(apply_runner)
    accepts_var_kw = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    if not accepts_var_kw:
        kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
    return await apply_runner(session, settings, **kwargs)


async def _emit_deferred_apply_completed_event(
    settings: Settings, apply_response: ApplyResponse
) -> None:
    degraded_health = apply_response.details.get("app_healthcheck_status") == "degraded"
    event_summary = str(
        apply_response.details.get("apply_event_summary")
        or (
            "Changes applied with app health warnings"
            if degraded_health
            else "Changes applied"
        )
    )
    raw_subevents = apply_response.details.get("apply_event_subevents")
    subevents = raw_subevents if isinstance(raw_subevents, list) else []
    if not subevents:
        subevents = [
            {"label": "run", "value": f"#{apply_response.run_id}"},
            {"label": "status", "value": "degraded" if degraded_health else "success"},
        ]
    await emit_control_event(
        settings,
        kind="apply_completed",
        source="apply",
        summary=event_summary,
        severity="warn" if degraded_health else "success",
        scope="host",
        affects_all=True,
        subevents=subevents,
        details={
            "run_id": apply_response.run_id,
            "status": "success",
            "message": apply_response.message,
            "outputs_total": apply_response.details.get("outputs_total"),
            "outputs_created": apply_response.details.get("outputs_created"),
            "outputs_updated": apply_response.details.get("outputs_updated"),
            "outputs_deleted": apply_response.details.get("outputs_deleted"),
            "output_created_names": apply_response.details.get("output_created_names")
            or [],
            "output_updated_names": apply_response.details.get("output_updated_names")
            or [],
            "app_healthcheck_status": apply_response.details.get(
                "app_healthcheck_status"
            ),
            "app_healthcheck_failures": apply_response.details.get(
                "app_healthcheck_failures"
            )
            or [],
        },
    )
