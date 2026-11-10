from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.logger import get_logger
from app.services import input_commands
from app.services.mutation_queue import mutation_runner
from app.services.apply_service import run_apply
from app.services.error_reporting import ErrorCode
from app.services.operation_runtime import delete_input_progress_plan
from app.services.operations import OperationHandle
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.services.config_change import route_change
from app.services.config_change import converge_change
from app.ui.operations.action import Action
from app.ui.operations.save import (
    ErrorRule,
    Reporter,
    SaveResult,
    Subject,
    apply_services_with,
    commit_and_converge,
    run_save,
)
from app.ui.forms import (
    _input_create_payload_from_form,
    _input_update_payload_from_form,
)
from app.ui.operations.common import background_db_session
from app.ui.progress import (
    _complete_input_operation,
    _input_apply_progress_reporter,
    _input_progress_details,
    _planned_operation_progress_details,
)


logger = get_logger("ui")


@dataclass(frozen=True)
class InputOperationMessages:
    create_saved: str = "Input saved."
    update_saved: str = "Input saved."
    delete_saved: str = "Input deleted."


def operator_validation_error(exc: ValidationError) -> str:
    return str(exc)


async def _update_input_operation_progress(
    operation: OperationHandle,
    *,
    pipeline: str,
    message: str,
    phase: str,
    substate: str,
    input_id: int | None = None,
    input_value: str = "",
    backend_ids: list[int] | None = None,
    progress_plan: dict[str, object] | None = None,
) -> None:
    detail_builder = (
        _planned_operation_progress_details
        if progress_plan is not None
        else _input_progress_details
    )
    details = detail_builder(
        progress_plan if progress_plan is not None else pipeline,
        0,
        message,
        phase=phase,
        substate=substate,
        **({"input_id": input_id} if input_id is not None else {}),
        **({"input_value": input_value} if input_value else {}),
        **({"backend_ids": backend_ids} if backend_ids is not None else {}),
    )
    await operation.update(status="running", phase=phase, details=details)


CREATE_INPUT = Action(
    kind="ui.input.create",
    success="Input saved.",
    label="Input create",
    error_code=ErrorCode.UI_INPUT_CREATE_FAILED,
    detail=(
        "Unexpected error while saving the input. Check server logs with this instance."
    ),
    noun="input",
    subject="input",
    pipeline="createInput",
)


@mutation_runner("create_input")
async def _run_create_input_operation(
    settings: Settings,
    operation_id: int | None,
    form_values: dict[str, object],
) -> None:
    action = CREATE_INPUT
    input_value = str(form_values.get("value") or "")

    async def mutate(session, operation, _row, _report):
        payload = _input_create_payload_from_form(
            kind=str(form_values.get("kind") or "domain"),
            value=input_value,
            backend_ids=[str(item) for item in form_values.get("backend_ids") or []],
            enabled=bool(form_values.get("enabled")),
            https_enabled=bool(form_values.get("https_enabled")),
            connection=str(form_values.get("connection") or "public_ip"),
        )
        report = _input_reporter(action, None, payload.value, None)
        await report.step(
            operation,
            "Resolving attached outputs.",
            phase="Map route graph",
            substate="Resolving attached outputs",
        )
        await report.step(
            operation,
            "Checking uniqueness.",
            phase="Save desired state",
            substate="Checking uniqueness",
        )
        item = await input_commands.create_input(session, payload, commit=False)
        attached = [backend.id for backend in item.backends]
        attached_names = {str(backend.name) for backend in item.backends}
        report = _input_reporter(action, getattr(item, "id", None), item.hostname, None)
        await report.step(
            operation,
            "Staging input config.",
            phase="Save desired state",
            substate="Staging input config",
        )
        return await _converge_input(
            session,
            settings,
            operation,
            action=action,
            input_id=getattr(item, "id", None),
            input_value=item.hostname,
            plan=None,
            backend_ids=attached,
            output_names=attached_names,
        )

    await run_save(
        settings,
        operation_id,
        action=action,
        subject=Subject(name=input_value),
        reporter=_input_reporter(action, None, input_value, None),
        mutate=mutate,
        complete=lambda operation, result: _complete_input_save(
            operation, action, result
        ),
        session_factory=background_db_session,
        rules=(
            ErrorRule(
                exception=IntegrityError,
                error_code=ErrorCode.UI_INPUT_CREATE_CONFLICT,
                detail=lambda value: (
                    f"{value} already exists. Use a unique input value."
                ),
                substate="Checking uniqueness",
            ),
        ),
    )


UPDATE_INPUT = Action(
    kind="ui.input.update",
    success="Input saved.",
    label="Input save",
    error_code=ErrorCode.UI_INPUT_UPDATE_FAILED,
    detail=(
        "Unexpected error while saving the input. Check server logs with this instance."
    ),
    noun="input",
    subject="input",
    pipeline="saveInput",
)


def _shield_fields(item, settings: Settings, form_values: dict[str, object]):
    code_hash = item.shield_code_hash
    display = str(getattr(item, "shield_access_code", "") or "").strip()
    submitted = str(form_values.get("shield_access_code") or "").strip()
    if submitted:
        code_hash = make_code_hash(settings.shield_secret_key, submitted)
        display = submitted
    enabled = (
        bool(form_values.get("shield_enabled")) if item.kind == "domain" else False
    )
    if item.kind == "domain" and enabled and not code_hash:
        raise ValidationError("shield requires an access code before it can be enabled")
    return enabled, code_hash, display


@mutation_runner("update_input")
async def _run_update_input_operation(
    settings: Settings,
    operation_id: int | None,
    input_id: int,
    form_values: dict[str, object],
    shield_access_code: str = "",
) -> None:
    if shield_access_code:
        form_values = {**form_values, "shield_access_code": shield_access_code}
    action = UPDATE_INPUT

    async def mutate(session, operation, item, _report):
        shield_enabled, shield_code_hash, shield_display = _shield_fields(
            item, settings, form_values
        )
        payload = _input_update_payload_from_form(
            kind=item.kind,
            value=item.hostname,
            backend_ids=[str(each) for each in form_values.get("backend_ids") or []],
            enabled=bool(form_values.get("enabled")),
            https_enabled=bool(form_values.get("https_enabled")),
            shield_enabled=shield_enabled,
            shield_code_hash=shield_code_hash,
            shield_access_code=shield_display,
        )
        report = _input_reporter(action, input_id, item.hostname, None)
        await report.step(
            operation,
            "Resolving attached outputs.",
            phase="Map route graph",
            substate="Resolving attached outputs",
        )
        await report.step(
            operation,
            "Staging input config.",
            phase="Save desired state",
            substate="Staging input config",
        )
        was_attached = {str(backend.name) for backend in item.backends}
        updated = await input_commands.update_input(
            session, input_id, payload, commit=False
        )
        attached = [backend.id for backend in updated.backends]
        attached_names = was_attached | {
            str(backend.name) for backend in updated.backends
        }
        await report.step(
            operation,
            "Checking uniqueness.",
            phase="Save desired state",
            substate="Checking uniqueness",
        )
        return await _converge_input(
            session,
            settings,
            operation,
            action=action,
            input_id=input_id,
            input_value=updated.hostname,
            plan=None,
            backend_ids=attached,
            output_names=attached_names,
        )

    await run_save(
        settings,
        operation_id,
        action=action,
        subject=Subject(
            id=input_id,
            load=lambda session: input_commands.require_input(session, input_id),
        ),
        reporter=_input_reporter(action, input_id, "", None),
        mutate=mutate,
        complete=lambda operation, result: _complete_input_save(
            operation, action, result
        ),
        session_factory=background_db_session,
        rules=(
            ErrorRule(
                exception=IntegrityError,
                error_code=ErrorCode.UI_INPUT_UPDATE_CONFLICT,
                detail=lambda _value: (
                    "input value already exists. Use a unique input value."
                ),
                substate="Checking uniqueness",
            ),
        ),
    )


def _input_reporter(
    action: Action,
    input_id: int | None,
    input_value: str,
    plan: dict[str, object] | None,
) -> Reporter:
    return Reporter(
        action,
        Subject(id=input_id, name=input_value),
        details_builder=_planned_operation_progress_details
        if plan is not None
        else _input_progress_details,
        plan=plan if plan is not None else action.pipeline,
    )


async def _converge_input(
    session,
    settings: Settings,
    operation: OperationHandle,
    *,
    action: Action,
    input_id: int | None,
    input_value: str,
    plan: dict[str, object] | None,
    backend_ids: list[int] | None = None,
    output_names: set[str] | None = None,
) -> SaveResult:
    services = apply_services_with(
        _input_apply_progress_reporter(
            operation,
            pipeline=action.pipeline,
            input_id=input_id,
            input_value=input_value,
            progress_plan=plan,
        )
    )
    report = _input_reporter(action, input_id, input_value, plan)
    await report.step(
        operation,
        "Waiting for host mutation lock.",
        phase="Save desired state",
        substate="Waiting for host mutation lock",
    )
    response = await commit_and_converge(
        session,
        settings,
        operation,
        action=action,
        services=services,
        apply_runner=run_apply,
        committer=converge_change,
        scope=route_change(action.kind, output_names or set()).scope(),
    )
    return SaveResult(
        apply_response=response,
        name=input_value,
        extra_details={
            **({"backend_ids": backend_ids} if backend_ids is not None else {}),
            **({"progress_plan": plan} if plan is not None else {}),
            "input_id": input_id,
            "input_value": input_value,
        },
    )


async def _complete_input_save(
    operation: OperationHandle, action: Action, result: SaveResult
) -> None:
    extras = dict(result.extra_details)
    await _complete_input_operation(
        operation,
        result.apply_response,
        success_message=action.success,
        failure_prefix=action.failure_prefix,
        input_id=extras.pop("input_id", None),
        input_value=extras.pop("input_value", ""),
        extra_details=extras,
    )


DELETE_INPUT = Action(
    kind="ui.input.delete",
    success="Input deleted.",
    label="Input delete",
    error_code=ErrorCode.UI_INPUT_DELETE_FAILED,
    detail=(
        "Unexpected error while deleting the input. "
        "Check server logs with this instance."
    ),
    noun="input",
    subject="input",
    pipeline="deleteInput",
)


@mutation_runner("delete_input")
async def _run_delete_input_operation(
    settings: Settings,
    operation_id: int | None,
    input_id: int,
) -> None:
    action = DELETE_INPUT

    async def mutate(session, operation, item, _report):
        plan = delete_input_progress_plan(input_kind=item.kind)
        report = _input_reporter(action, input_id, item.hostname, plan)
        await report.step(
            operation,
            "Checking affected outputs.",
            phase="Map route graph",
            substate="Checking affected outputs",
        )
        await report.step(
            operation,
            "Removing input config.",
            phase="Save desired state",
            substate="Removing input config",
        )
        detached_names = {str(backend.name) for backend in item.backends}
        await input_commands.delete_input(session, input_id)
        return await _converge_input(
            session,
            settings,
            operation,
            action=action,
            input_id=input_id,
            input_value=item.hostname,
            plan=plan,
            output_names=detached_names,
        )

    await run_save(
        settings,
        operation_id,
        action=action,
        subject=Subject(
            id=input_id,
            load=lambda session: input_commands.require_input(session, input_id),
        ),
        reporter=_input_reporter(action, input_id, "", None),
        mutate=mutate,
        complete=lambda operation, result: _complete_input_save(
            operation, action, result
        ),
        session_factory=background_db_session,
    )
