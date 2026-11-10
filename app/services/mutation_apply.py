from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import create_configured_async_engine
from app.models.entities import ApplyRun
from app.logger import get_logger
from app.schemas.apply import ApplyResponse
from app.services.apply_service import (
    ApplyServices,
    invalidate_apply_convergence,
    run_apply,
)
from app.services.control_events import emit_control_event
from app.services.error_reporting import ErrorCode, ensure_cnc_error
from app.services.host_journal import durable_row_exists
from app.services.operations import (
    OperationHandle,
    host_mutation_lock,
    host_mutation_operation,
    validate_operation_handle_contract,
)
from app.services.outcome_presentation import present_mutation_outcome
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
                "manual_review_required",
            )
            if key in details
        },
    )


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
    await session.flush()
    recovery: list[Any] = []
    state_history: tuple[MutationApplyState, ...] = ("pending",)
    logger.info("mutation_apply.pending", operation=operation, state="pending")
    state_history = (*state_history, "applying")
    logger.info("mutation_apply.applying", operation=operation, state="applying")
    try:
        validate_operation_handle_contract(operation_handle)
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
        )
    except asyncio.CancelledError:
        # The edited rows were never committed, so the host goes back too.
        await run_to_completion(unwind_recovery(recovery))
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
        apply_response.details.update(await unwind_recovery(recovery))
    state: MutationApplyState = (
        "applied" if apply_response.status == "success" else "apply_failed"
    )
    if state == "applied":
        try:
            await session.commit()
        except Exception as exc:
            rollback_error = await _rollback_quietly(session)
            _log_quietly(
                "mutation_apply.commit_failed_after_apply",
                operation=operation,
                apply_run_id=apply_response.run_id,
                error=str(exc),
            )
            try:
                unwind_details = await _resolve_commit_then_unwind(
                    settings, apply_response, recovery
                )
            except Exception as resolve_error:
                _log_quietly(
                    "mutation_apply.commit_outcome_resolution_failed",
                    operation=operation,
                    error=str(resolve_error),
                )
                unwind_details = {
                    "commit_outcome": "unknown",
                    "commit_unwind": "skipped; outcome resolution itself failed",
                    "commit_resolution_error": str(resolve_error),
                    "failure_mode": "partial",
                    "manual_review_required": True,
                    "operator_action": (
                        "compare the saved configuration against the host "
                        "before rerunning"
                    ),
                }
            if rollback_error is not None:
                unwind_details["commit_rollback_error"] = rollback_error
            try:
                apply_response = await _record_deferred_commit_failure(
                    settings,
                    apply_response,
                    exc,
                    operation_handle=operation_handle,
                    resolution_details=unwind_details,
                )
            except Exception as record_error:
                _log_quietly(
                    "mutation_apply.commit_failure_record_failed",
                    operation=operation,
                    error=str(record_error),
                )
                unwind_details["commit_record_error"] = str(record_error)
            apply_response.details.update(unwind_details)
            state = "apply_failed"
        else:
            await _emit_deferred_apply_completed_event(settings, apply_response)
    else:
        await session.rollback()
        apply_response.details.setdefault("commit_outcome", "rolled_back")
    invalidate_status_cache(settings, prefill=False)
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


async def _rollback_quietly(session: AsyncSession) -> str | None:
    try:
        await session.rollback()
    except Exception as exc:
        return str(exc)
    return None


def _log_quietly(event: str, **fields: Any) -> None:
    try:
        logger.exception(event, **fields)
    except Exception:
        pass


async def _resolve_commit_then_unwind(
    settings: Settings,
    apply_response: ApplyResponse,
    recovery: list[Any],
) -> dict[str, Any]:
    outcome = await _durable_commit_outcome(settings, apply_response)
    if outcome == "persisted":
        return {
            "commit_outcome": "persisted",
            "commit_unwind": "skipped; the database kept this change",
            "failure_mode": "partial",
            "manual_review_required": True,
        }
    if outcome == "unknown":
        return {
            "commit_outcome": "unknown",
            "commit_unwind": "skipped; could not determine what the database kept",
            "failure_mode": "partial",
            "manual_review_required": True,
            "operator_action": (
                "compare the saved configuration against the host before rerunning"
            ),
        }
    return {
        "commit_outcome": "rolled_back",
        **await unwind_recovery(recovery, "commit_"),
    }


async def _durable_commit_outcome(
    settings: Settings, apply_response: ApplyResponse
) -> str:
    if apply_response.run_id is None:
        return "rolled_back"
    return await durable_row_exists(settings, ApplyRun, apply_response.run_id)


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
) -> ApplyResponse:
    return await apply_runner(
        session,
        settings,
        services=apply_services,
        commit_on_success=False,
        emit_success_event=False,
        operation_handle=operation_handle,
        operation_kind=operation_kind,
        actor=actor,
        scope=scope,
        recovery=recovery,
    )


async def _record_deferred_commit_failure(
    settings: Settings,
    apply_response: ApplyResponse,
    exc: Exception,
    *,
    operation_handle: OperationHandle,
    resolution_details: dict[str, Any],
) -> ApplyResponse:
    convergence_invalidated = False
    convergence_invalidation_error = ""
    try:
        convergence_invalidated = await _invalidate_apply_convergence_durably(settings)
    except Exception as invalidation_exc:
        convergence_invalidation_error = str(invalidation_exc)
        logger.exception(
            "mutation_apply.convergence_invalidation_failed",
            apply_run_id=apply_response.run_id,
            error=convergence_invalidation_error,
        )
    details = {
        "phase": "database_commit",
        "error": str(exc),
        "failure_mode": "partial",
        "manual_review_required": True,
        "apply_run_id": apply_response.run_id,
        "message": "Host apply finished, but the database commit failed.",
        "convergence_invalidated": convergence_invalidated,
        **resolution_details,
    }
    if convergence_invalidation_error:
        details["convergence_invalidation_error"] = convergence_invalidation_error
    await operation_handle.complete(
        "partial",
        phase="database_commit",
        error=str(exc),
        details=details,
    )
    presentation = present_mutation_outcome("partial", details)
    await emit_control_event(
        settings,
        kind="apply_failed",
        source="apply",
        summary=(
            "Configuration saved; final confirmation failed"
            if presentation.status_label == "saved"
            else "Save failed during database commit"
        ),
        severity="error",
        scope="host",
        affects_all=True,
        subevents=[
            {"label": "phase", "value": "database_commit"},
            {"label": "status", "value": presentation.status_label},
        ],
        details={**details, "recovery": presentation.recovery_detail},
        notify=False,
    )
    return ApplyResponse(
        status="error",
        message=(
            "configuration saved; final confirmation failed"
            if presentation.status_label == "saved"
            else "save failed during database commit"
        ),
        details=details,
        created_at=apply_response.created_at,
        run_id=apply_response.run_id,
    )


async def _invalidate_apply_convergence_durably(settings: Settings) -> bool:
    engine = create_configured_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as recovery_session:
            invalidated = await invalidate_apply_convergence(recovery_session)
            await recovery_session.commit()
            return invalidated
    finally:
        await engine.dispose()


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
