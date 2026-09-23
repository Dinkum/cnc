from __future__ import annotations

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
from app.services.operations import (
    OperationHandle,
    host_mutation_lock,
    validate_operation_handle_contract,
)
from app.services.outcome_presentation import present_mutation_outcome
from app.services.status_service import invalidate_status_cache
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
    """Converge the host, then make the edited rows durable if it converged.

    The lock is held across the whole sequence rather than only across
    convergence. Between the last tool and the commit there is verification,
    the durable record, and the commit itself, and a failure at any of them
    unwinds the host. A competing mutation arriving in that window would be
    restored over. Reentrant, so an apply that takes the lock internally is
    unaffected.
    """
    async with host_mutation_lock(settings):
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


async def _apply_then_commit(
    session: AsyncSession,
    settings: Settings,
    *,
    operation: str,
    actor: str,
    apply_runner: ApplyRunner,
    apply_services: ApplyServices | None,
    operation_handle: OperationHandle | None,
    scope: object | None,
) -> MutationApplyResult:
    await session.flush()
    # Collects the converged plan so its rollback is still available if the
    # database commit fails after every tool succeeded.
    recovery: list[Any] = []
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
            operation_kind=operation,
            actor=actor,
            scope=scope,
            recovery=recovery,
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
        # The runner raised rather than returning a failure, so it did not get
        # to unwind its own plan. The rows roll back below, so anything the
        # host still holds is a change nothing describes. Already-unwound
        # plans have an empty stack and cost nothing here.
        apply_response.details.update(await unwind_recovery(recovery))
    state: MutationApplyState = (
        "applied" if apply_response.status == "success" else "apply_failed"
    )
    if state == "applied":
        try:
            await session.commit()
        except Exception as exc:
            # Nothing between here and the durable-outcome resolution may
            # raise. An `after_commit` handler that throws is the case this
            # exists for: the transaction is already committed, and SQLAlchemy
            # then leaves the session in a state where `rollback()` can throw
            # too. If that escaped, the one step that establishes what the
            # database actually kept would be skipped, and the caller would see
            # a bare exception instead of a resolved outcome.
            rollback_error = await _rollback_quietly(session)
            _log_quietly(
                "mutation_apply.commit_failed_after_apply",
                operation=operation,
                apply_run_id=apply_response.run_id,
                error=str(exc),
            )
            # A raised commit does not prove the transaction was discarded.
            # An after-commit callback can throw over a write that is already
            # durable. Resolve what the database actually holds before touching
            # the host: restoring over a commit that succeeded is the same
            # disagreement in the opposite direction.
            try:
                unwind_details = await _resolve_commit_then_unwind(
                    settings, apply_response, recovery
                )
            except Exception as resolve_error:
                # The resolution is already written to swallow its own parts.
                # This is the outer guard: if it fails anyway, the caller still
                # gets a resolved-looking failure rather than an exception, and
                # the host is left alone because nothing is known about it.
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
                # Recording the failure is reporting, not resolution. Losing it
                # must not lose the outcome we just established.
                _log_quietly(
                    "mutation_apply.commit_failure_record_failed",
                    operation=operation,
                    error=str(record_error),
                )
                unwind_details["commit_record_error"] = str(record_error)
            apply_response.details.update(unwind_details)
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
    """Roll back without letting the rollback replace the original failure.

    After an `after_commit` handler raises, the transaction is already
    committed and the session can refuse to roll back. That is information to
    record, not a reason to abandon durable-outcome resolution.
    """
    try:
        await session.rollback()
    except Exception as exc:
        return str(exc)
    return None


def _log_quietly(event: str, **fields: Any) -> None:
    """Logging must never be the thing that skips the resolution below it."""
    try:
        logger.exception(event, **fields)
    except Exception:  # pragma: no cover - a logger that throws is not fatal
        pass


async def _resolve_commit_then_unwind(
    settings: Settings,
    apply_response: ApplyResponse,
    recovery: list[Any],
) -> dict[str, Any]:
    """Decide what the database actually holds, then act on that.

    Three outcomes, and only one restores the host:

    ``rolled_back``
        The durable record does not carry this apply, so the reversible host
        effects come back with it.
    ``persisted``
        The record carries this apply despite the exception. The database
        describes the new configuration, so the host is left matching it.
    ``unknown``
        The resolving read itself failed. Restoring blind could revert a change
        the database kept, so nothing is touched and the operator is told.
    """
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
    """Whether this apply's row survived, read on a connection of its own."""
    run_id = apply_response.run_id
    if run_id is None:
        return "rolled_back"
    try:
        engine = create_configured_async_engine(settings.database_url)
        try:
            maker = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with maker() as probe:
                found = await probe.get(ApplyRun, run_id)
                return "persisted" if found is not None else "rolled_back"
        finally:
            await engine.dispose()
    except Exception as exc:
        logger.warning("mutation_apply.commit_outcome_unresolved", error=str(exc))
        return "unknown"


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
    # No signature introspection. Every caller passes a runner with the apply
    # contract, so the arguments are simply passed; a runner that cannot accept
    # them is a bug to surface rather than silently work around.
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
