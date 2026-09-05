from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.services import input_commands
from app.services.apply_service import default_apply_services, run_apply
from app.services.error_reporting import ErrorCode, error_context
from app.services.mutation_apply import commit_and_apply
from app.services.operation_runtime import delete_input_progress_plan
from app.services.operations import OperationHandle
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.ui.errors import operator_action_error
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


async def _fail_input_operation(
    operation: OperationHandle,
    *,
    phase: str,
    substate: str,
    message: str,
    flash_error: str,
    input_id: int | None = None,
    input_value: str = "",
    error_fields: dict[str, str] | None = None,
) -> None:
    await operation.complete(
        "failed",
        phase=phase,
        error=flash_error,
        details={
            "progress": 100,
            "message": message,
            "substate": substate,
            "flash_error": flash_error,
            **({"input_id": input_id} if input_id is not None else {}),
            **({"input_value": input_value} if input_value else {}),
            **(error_fields or {}),
        },
    )


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


async def _commit_input_operation_apply(
    session: AsyncSession,
    settings: Settings,
    operation: OperationHandle,
    *,
    pipeline: str,
    operation_name: str,
    success_message: str,
    failure_prefix: str,
    input_id: int | None = None,
    input_value: str = "",
    backend_ids: list[int] | None = None,
    progress_plan: dict[str, object] | None = None,
) -> None:
    services = default_apply_services()
    services.progress_callback = _input_apply_progress_reporter(
        operation,
        pipeline=pipeline,
        input_id=input_id,
        input_value=input_value,
        progress_plan=progress_plan,
    )
    await _update_input_operation_progress(
        operation,
        pipeline=pipeline,
        message="Waiting for host mutation lock.",
        phase="Save desired state",
        substate="Waiting for host mutation lock",
        input_id=input_id,
        input_value=input_value,
        backend_ids=backend_ids,
        progress_plan=progress_plan,
    )
    mutation = await commit_and_apply(
        session,
        settings,
        operation=operation_name,
        apply_runner=run_apply,
        apply_services=services,
        operation_handle=operation,
    )
    await _complete_input_operation(
        operation,
        mutation.apply_response,
        success_message=success_message,
        failure_prefix=failure_prefix,
        input_id=input_id,
        input_value=input_value,
        extra_details={
            **({"backend_ids": backend_ids} if backend_ids is not None else {}),
            **({"progress_plan": progress_plan} if progress_plan is not None else {}),
        },
    )


async def _run_create_input_operation(
    settings: Settings,
    operation_id: int | None,
    form_values: dict[str, object],
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="ui.input.create", settings=settings
    )
    input_value = str(form_values.get("value") or "")
    try:
        await _update_input_operation_progress(
            operation,
            pipeline="createInput",
            message="Checking route shape.",
            phase="Validate input",
            substate="Checking route shape",
            input_value=input_value,
        )
        async with background_db_session(settings) as session:
            payload = _input_create_payload_from_form(
                kind=str(form_values.get("kind") or "domain"),
                value=input_value,
                backend_ids=[
                    str(item) for item in form_values.get("backend_ids") or []
                ],
                enabled=bool(form_values.get("enabled")),
            )
            await _update_input_operation_progress(
                operation,
                pipeline="createInput",
                message="Resolving attached outputs.",
                phase="Map route graph",
                substate="Resolving attached outputs",
                input_value=payload.value,
            )
            await _update_input_operation_progress(
                operation,
                pipeline="createInput",
                message="Checking uniqueness.",
                phase="Save desired state",
                substate="Checking uniqueness",
                input_value=payload.value,
            )
            item = await input_commands.create_input(session, payload, commit=False)
            attached_backend_ids = [backend.id for backend in item.backends]
            await _update_input_operation_progress(
                operation,
                pipeline="createInput",
                message="Staging input config.",
                phase="Save desired state",
                substate="Staging input config",
                input_value=item.hostname,
                backend_ids=attached_backend_ids,
            )
            await _commit_input_operation_apply(
                session,
                settings,
                operation,
                pipeline="createInput",
                operation_name="ui.input.create",
                success_message="Input saved.",
                failure_prefix="Input saved",
                input_id=getattr(item, "id", None),
                input_value=item.hostname,
                backend_ids=attached_backend_ids,
            )
    except ValidationError as exc:
        flash_error = operator_validation_error(exc)
        await _fail_input_operation(
            operation,
            phase="Validate input",
            substate="Checking route shape",
            message=flash_error,
            flash_error=flash_error,
            input_value=input_value,
        )
    except IntegrityError as exc:
        error_fields = error_context(ErrorCode.UI_INPUT_CREATE_CONFLICT)
        flash_error = operator_action_error(
            "Input create",
            f"{input_value} already exists. Use a unique input value.",
            error_fields,
        )
        logger.warning(
            "ui.input.create.worker_conflict",
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await _fail_input_operation(
            operation,
            phase="Save desired state",
            substate="Checking uniqueness",
            message="Input create failed.",
            flash_error=flash_error,
            input_value=input_value,
            error_fields=error_fields,
        )
    except Exception as exc:
        error_fields = error_context(ErrorCode.UI_INPUT_CREATE_FAILED)
        logger.exception(
            "ui.input.create.worker_failed",
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await _fail_input_operation(
            operation,
            phase="Save desired state",
            substate="Staging input config",
            message="Input create failed.",
            flash_error=operator_action_error(
                "Input create",
                "Unexpected error while saving the input. Check server logs with this instance.",
                error_fields,
            ),
            input_value=input_value,
            error_fields=error_fields,
        )


async def _run_update_input_operation(
    settings: Settings,
    operation_id: int | None,
    input_id: int,
    form_values: dict[str, object],
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="ui.input.update", settings=settings
    )
    try:
        await _update_input_operation_progress(
            operation,
            pipeline="saveInput",
            message="Checking route shape.",
            phase="Validate input",
            substate="Checking route shape",
            input_id=input_id,
        )
        async with background_db_session(settings) as session:
            item = await input_commands.require_input(session, input_id)
            input_value = item.hostname
            shield_code_hash = item.shield_code_hash
            shield_access_code_display = str(
                getattr(item, "shield_access_code", "") or ""
            ).strip()
            normalized_shield_access_code = str(
                form_values.get("shield_access_code") or ""
            ).strip()
            if normalized_shield_access_code:
                shield_code_hash = make_code_hash(
                    settings.shield_secret_key, normalized_shield_access_code
                )
                shield_access_code_display = normalized_shield_access_code
            shield_enabled = (
                bool(form_values.get("shield_enabled"))
                if item.kind == "domain"
                else False
            )
            if item.kind == "domain" and shield_enabled and not shield_code_hash:
                raise ValidationError(
                    "shield requires an access code before it can be enabled"
                )
            payload = _input_update_payload_from_form(
                kind=item.kind,
                value=item.hostname,
                backend_ids=[
                    str(item) for item in form_values.get("backend_ids") or []
                ],
                enabled=bool(form_values.get("enabled")),
                shield_enabled=shield_enabled,
                shield_code_hash=shield_code_hash,
                shield_access_code=shield_access_code_display,
            )
            await _update_input_operation_progress(
                operation,
                pipeline="saveInput",
                message="Resolving attached outputs.",
                phase="Map route graph",
                substate="Resolving attached outputs",
                input_id=input_id,
                input_value=input_value,
            )
            await _update_input_operation_progress(
                operation,
                pipeline="saveInput",
                message="Staging input config.",
                phase="Save desired state",
                substate="Staging input config",
                input_id=input_id,
                input_value=input_value,
            )
            item = await input_commands.update_input(
                session, input_id, payload, commit=False
            )
            attached_backend_ids = [backend.id for backend in item.backends]
            await _update_input_operation_progress(
                operation,
                pipeline="saveInput",
                message="Checking uniqueness.",
                phase="Save desired state",
                substate="Checking uniqueness",
                input_id=input_id,
                input_value=item.hostname,
                backend_ids=attached_backend_ids,
            )
            await _commit_input_operation_apply(
                session,
                settings,
                operation,
                pipeline="saveInput",
                operation_name="ui.input.update",
                success_message="Input saved.",
                failure_prefix="Input saved",
                input_id=input_id,
                input_value=item.hostname,
                backend_ids=attached_backend_ids,
            )
    except ValidationError as exc:
        flash_error = operator_validation_error(exc)
        await _fail_input_operation(
            operation,
            phase="Validate input",
            substate="Checking route shape",
            message=flash_error,
            flash_error=flash_error,
            input_id=input_id,
        )
    except IntegrityError as exc:
        error_fields = error_context(ErrorCode.UI_INPUT_UPDATE_CONFLICT)
        logger.warning(
            "ui.input.update.worker_conflict",
            input_id=input_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await _fail_input_operation(
            operation,
            phase="Save desired state",
            substate="Checking uniqueness",
            message="Input save failed.",
            flash_error=operator_action_error(
                "Input save",
                "input value already exists. Use a unique input value.",
                error_fields,
            ),
            input_id=input_id,
            error_fields=error_fields,
        )
    except Exception as exc:
        error_fields = error_context(ErrorCode.UI_INPUT_UPDATE_FAILED)
        logger.exception(
            "ui.input.update.worker_failed",
            input_id=input_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await _fail_input_operation(
            operation,
            phase="Save desired state",
            substate="Staging input config",
            message="Input save failed.",
            flash_error=operator_action_error(
                "Input save",
                "Unexpected error while saving the input. Check server logs with this instance.",
                error_fields,
            ),
            input_id=input_id,
            error_fields=error_fields,
        )


async def _run_delete_input_operation(
    settings: Settings,
    operation_id: int | None,
    input_id: int,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="ui.input.delete", settings=settings
    )
    try:
        async with background_db_session(settings) as session:
            item = await input_commands.require_input(session, input_id)
            input_value = item.hostname
            progress_plan = delete_input_progress_plan(input_kind=item.kind)
            await _update_input_operation_progress(
                operation,
                pipeline="deleteInput",
                message="Loading current route graph.",
                phase="Validate input",
                substate="Loading current route graph",
                input_id=input_id,
                input_value=input_value,
                progress_plan=progress_plan,
            )
            await _update_input_operation_progress(
                operation,
                pipeline="deleteInput",
                message="Checking affected outputs.",
                phase="Map route graph",
                substate="Checking affected outputs",
                input_id=input_id,
                input_value=input_value,
                progress_plan=progress_plan,
            )
            await _update_input_operation_progress(
                operation,
                pipeline="deleteInput",
                message="Removing input config.",
                phase="Save desired state",
                substate="Removing input config",
                input_id=input_id,
                input_value=input_value,
                progress_plan=progress_plan,
            )
            await input_commands.delete_input(session, input_id)
            await _commit_input_operation_apply(
                session,
                settings,
                operation,
                pipeline="deleteInput",
                operation_name="ui.input.delete",
                success_message="Input deleted.",
                failure_prefix="Input deleted",
                input_id=input_id,
                input_value=input_value,
                progress_plan=progress_plan,
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.UI_INPUT_DELETE_FAILED)
        logger.warning(
            "ui.input.delete.worker_failed",
            input_id=input_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await _fail_input_operation(
            operation,
            phase="Save desired state",
            substate="Removing input config",
            message="Input delete failed.",
            flash_error=operator_action_error(
                "Input delete",
                "Unexpected error while deleting the input. Check server logs with this instance.",
                error_fields,
            ),
            input_id=input_id,
            error_fields=error_fields,
        )
