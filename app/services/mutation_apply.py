from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.schemas.apply import ApplyResponse
from app.services.apply_service import (
    ApplyServices,
    invalidate_apply_convergence,
    run_apply,
)
from app.services.control_events import emit_control_event
from app.services.error_reporting import ErrorCode, ensure_cnc_error
from app.services.operations import OperationHandle, validate_operation_handle_contract
from app.services.status_service import invalidate_status_cache


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
    apply_runner: ApplyRunner = run_apply,
    apply_services: ApplyServices | None = None,
    operation_handle: OperationHandle | None = None,
) -> MutationApplyResult:
    await session.flush()
    state_history: tuple[MutationApplyState, ...] = ("pending",)
    logger.info("mutation_apply.pending", operation=operation, state="pending")
    state_history = (*state_history, "applying")
    logger.info("mutation_apply.applying", operation=operation, state="applying")
    try:
        if operation_handle is not None:
            validate_operation_handle_contract(operation_handle)
        apply_response = await _run_apply_runner(
            apply_runner,
            session,
            settings,
            apply_services=apply_services,
            operation_handle=operation_handle,
        )
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
    state: MutationApplyState = (
        "applied" if apply_response.status == "success" else "apply_failed"
    )
    if state == "applied":
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.exception(
                "mutation_apply.commit_failed_after_apply",
                operation=operation,
                apply_run_id=apply_response.run_id,
                error=str(exc),
            )
            apply_response = await _record_deferred_commit_failure(
                settings,
                apply_response,
                exc,
                operation_handle=operation_handle,
            )
            state = "apply_failed"
        else:
            await _complete_deferred_apply_operation(
                settings,
                apply_response,
                operation_handle=operation_handle,
            )
            await _emit_deferred_apply_completed_event(settings, apply_response)
    else:
        await session.rollback()
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


async def _run_apply_runner(
    apply_runner: ApplyRunner,
    session: AsyncSession,
    settings: Settings,
    *,
    apply_services: ApplyServices | None,
    operation_handle: OperationHandle | None,
) -> ApplyResponse:
    kwargs: dict[str, object] = {}
    parameters = inspect.signature(apply_runner).parameters
    if apply_services is not None:
        kwargs["services"] = apply_services
    if "commit_on_success" in parameters:
        kwargs["commit_on_success"] = False
    if "emit_success_event" in parameters:
        kwargs["emit_success_event"] = False
    if operation_handle is not None and "operation_handle" in parameters:
        kwargs["operation_handle"] = operation_handle
    return await apply_runner(session, settings, **kwargs)


async def _complete_deferred_apply_operation(
    settings: Settings,
    apply_response: ApplyResponse,
    *,
    operation_handle: OperationHandle | None,
) -> None:
    if operation_handle is not None:
        return
    operation_id = apply_response.details.get("deferred_operation_id")
    operation_kind = str(
        apply_response.details.get("deferred_operation_kind") or "apply_host"
    )
    if not isinstance(operation_id, int):
        return
    operation = OperationHandle(id=operation_id, kind=operation_kind, settings=settings)
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


async def _record_deferred_commit_failure(
    settings: Settings,
    apply_response: ApplyResponse,
    exc: Exception,
    *,
    operation_handle: OperationHandle | None,
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
    }
    if convergence_invalidation_error:
        details["convergence_invalidation_error"] = convergence_invalidation_error
    operation: OperationHandle | None = operation_handle
    if operation is None:
        operation_id = apply_response.details.get("deferred_operation_id")
        operation_kind = str(
            apply_response.details.get("deferred_operation_kind") or "apply_host"
        )
        if isinstance(operation_id, int):
            operation = OperationHandle(
                id=operation_id, kind=operation_kind, settings=settings
            )
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
        source="apply",
        summary="Changes applied but database commit failed",
        severity="error",
        scope="host",
        affects_all=True,
        subevents=[
            {"label": "phase", "value": "database_commit"},
            {"label": "status", "value": "partial"},
        ],
        details=details,
        notify=False,
    )
    return ApplyResponse(
        status="error",
        message="apply completed but database commit failed",
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
