
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.schemas.apply import ApplyResponse
from app.services.apply_service import default_apply_services, run_apply
from app.services.error_reporting import error_context
from app.services.config_change import ConfigChange, converge_change
from app.services.operations import OperationHandle
from app.services.validators import ValidationError
from app.ui.errors import operator_action_error
from app.ui.operations.action import Action

logger = get_logger("ui.save")


@dataclass(frozen=True)
class Subject:
    id: int | None = None
    name: str = ""
    load: Callable[[AsyncSession], Awaitable[Any]] | None = None

    def keys(self, action: Action) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.id is not None:
            payload[action.id_key] = self.id
        if self.name:
            payload[action.name_key] = self.name
        return payload


@dataclass(frozen=True)
class ErrorRule:

    exception: type[BaseException]
    error_code: Any
    detail: Callable[[str], str]
    substate: str = ""


@dataclass
class SaveResult:

    apply_response: ApplyResponse
    name: str = ""
    extra_details: dict[str, Any] = field(default_factory=dict)


Mutate = Callable[
    [AsyncSession, OperationHandle, Any, "Reporter"], Awaitable[SaveResult]
]


class Reporter:

    def __init__(
        self,
        action: Action,
        subject: Subject,
        *,
        details_builder: Callable[..., dict[str, Any]] | None = None,
        plan: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.action = action
        self.subject = subject
        self._build = details_builder
        self._plan = plan
        self._extra = extra or {}

    def with_name(self, name: str) -> "Reporter":
        clone = Reporter(
            self.action,
            Subject(id=self.subject.id, name=name or self.subject.name),
            details_builder=self._build,
            plan=self._plan,
            extra=self._extra,
        )
        return clone

    def details(self, message: str, *, phase: str, substate: str) -> dict[str, Any]:
        identity = {**self.subject.keys(self.action), **self._extra}
        if self._build is None:
            return {
                "progress": 0,
                "message": message,
                "substate": substate,
                **identity,
            }
        return self._build(
            self._plan, 0, message, phase=phase, substate=substate, **identity
        )

    async def step(
        self, operation: OperationHandle, message: str, *, phase: str, substate: str
    ) -> None:
        await operation.update(
            status="running",
            phase=phase,
            details=self.details(message, phase=phase, substate=substate),
        )


async def run_save(
    settings: Settings,
    operation_id: int | None,
    *,
    action: Action,
    subject: Subject,
    mutate: Mutate,
    reporter: Reporter | None = None,
    complete: Callable[..., Awaitable[None]] | None = None,
    first_step: tuple[str, str] | None = None,
    session_factory: Callable[[Settings], Any],
    rules: tuple[ErrorRule, ...] = (),
    operation: OperationHandle | None = None,
) -> None:
    operation = operation or OperationHandle(
        id=operation_id, kind=action.kind, settings=settings
    )
    report = reporter or Reporter(action, subject)
    try:
        opening = first_step or (f"{action.validating}.", action.validating)
        await report.step(
            operation,
            opening[0],
            phase=action.validate_phase,
            substate=opening[1],
        )
        async with session_factory(settings) as session:
            row = await subject.load(session) if subject.load else None
            await report.step(
                operation,
                "Saving desired state.",
                phase="Save desired state",
                substate="Saving desired state",
            )
            result = await mutate(session, operation, row, report)
            if complete is not None:
                await complete(operation, result)
    except ValidationError as exc:
        await _fail(operation, action, report, str(exc), validation=True)
    except Exception as exc:
        rule = next((r for r in rules if isinstance(exc, r.exception)), None)
        if rule is not None:
            fields = error_context(rule.error_code)
            flash = operator_action_error(
                action.label, rule.detail(subject.name), fields
            )
            logger.warning(
                f"{action.kind}.worker_conflict",
                operation_id=operation_id,
                **fields,
                error=str(exc),
            )
            await _fail(
                operation,
                action,
                report,
                flash,
                error_fields=fields,
                substate=rule.substate,
            )
            return
        fields = error_context(action.error_code)
        flash = operator_action_error(action.label, action.detail, fields)
        logger.exception(
            action.log_event,
            operation_id=operation_id,
            **subject.keys(action),
            **fields,
            error=str(exc),
        )
        await _fail(operation, action, report, flash, error_fields=fields)


async def _fail(
    operation: OperationHandle,
    action: Action,
    report: Reporter,
    flash_error: str,
    *,
    validation: bool = False,
    error_fields: dict[str, str] | None = None,
    substate: str = "",
) -> None:
    phase = action.validate_phase if validation else action.save_phase
    substate = substate or (action.validating if validation else action.refreshing)
    message = flash_error if validation else action.failure_message
    details = report.details(message, phase=phase, substate=substate)
    await operation.complete(
        "failed",
        phase=phase,
        error=flash_error,
        details={
            **details,
            "progress": 100,
            "flash_error": flash_error,
            **(error_fields or {}),
        },
    )


def apply_services_with(progress_callback: Any) -> Any:
    services = default_apply_services()
    services.progress_callback = progress_callback
    return services


async def commit_and_converge(
    session: AsyncSession,
    settings: Settings,
    operation: OperationHandle,
    *,
    action: Action,
    services: Any,
    apply_runner: Any = None,
    committer: Any = None,
    scope: Any = None,
) -> ApplyResponse:
    commit = committer or converge_change
    mutation = await commit(
        session,
        settings,
        ConfigChange(kind=action.kind, explicit_scope=scope),
        apply_runner=apply_runner or run_apply,
        apply_services=services,
        operation_handle=operation,
    )
    return mutation.apply_response
