from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.schemas.apply import ApplyResponse
from app.services import backend_commands
from app.services.apply_service import default_apply_services, run_apply
from app.services.error_reporting import ErrorCode, error_context
from app.services.inter_app_interfaces import apply_inbound_inter_app_interface_statuses
from app.services.mutation_apply import commit_and_apply
from app.services.operations import OperationHandle
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.ui.errors import operator_action_error
from app.ui.forms import (
    _backend_inputs_payload_from_form,
    _backend_update_payload_from_form,
)
from app.ui.operations.common import background_db_session, operation_backend
from app.ui.progress import (
    _complete_output_save_operation,
    _output_save_apply_progress_reporter,
    _output_save_progress_details,
)


logger = get_logger("ui")


@dataclass(frozen=True)
class _OutputSaveResult:
    apply_response: ApplyResponse
    backend_name: str
    extra_details: dict[str, object]


def _backend_update_changed_keys(backend: Backend, payload: object) -> set[str]:
    changed: set[str] = set()
    for key, value in vars(payload).items():
        if value is None:
            continue
        if getattr(backend, key, None) != value:
            changed.add(key)
    return changed


def operator_validation_error(exc: ValidationError) -> str:
    return str(exc)


async def _update_output_save_operation_progress(
    operation: OperationHandle,
    *,
    message: str,
    phase: str,
    substate: str,
    backend_id: int,
    backend_name: str = "",
    **extra: object,
) -> None:
    await operation.update(
        status="running",
        phase=phase,
        details=_output_save_progress_details(
            0,
            message,
            phase=phase,
            substate=substate,
            backend_id=backend_id,
            backend_name=backend_name,
            **extra,
        ),
    )


async def _commit_output_save_apply(
    session: AsyncSession,
    settings: Settings,
    operation: OperationHandle,
    *,
    operation_name: str,
    backend_id: int,
    backend_name: str,
) -> ApplyResponse:
    services = replace(
        default_apply_services(),
        progress_callback=_output_save_apply_progress_reporter(
            operation,
            backend_id=backend_id,
            backend_name=backend_name,
        ),
    )
    mutation = await commit_and_apply(
        session,
        settings,
        operation=operation_name,
        apply_runner=run_apply,
        apply_services=services,
        operation_handle=operation,
    )
    return mutation.apply_response


async def _run_output_save_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    *,
    operation_kind: str,
    success_message: str,
    failure_prefix: str,
    failure_action: str,
    failure_detail: str,
    failure_message: str,
    failure_error_code: ErrorCode,
    log_event: str,
    save: Callable[
        [AsyncSession, OperationHandle, Backend, str], Awaitable[_OutputSaveResult]
    ],
    progress_extra: dict[str, object],
    failure_extra: dict[str, object],
) -> None:
    operation = OperationHandle(id=operation_id, kind=operation_kind, settings=settings)
    try:
        await _update_output_save_operation_progress(
            operation,
            message="Validating output.",
            phase="Validate output",
            substate="Validating output",
            backend_id=backend_id,
            **progress_extra,
        )
        async with background_db_session(settings) as session:
            backend = await operation_backend(session, backend_id)
            backend_name = str(backend.name or "")
            await _update_output_save_operation_progress(
                operation,
                message="Saving desired state.",
                phase="Save desired state",
                substate="Saving desired state",
                backend_id=backend_id,
                backend_name=backend_name,
                **progress_extra,
            )
            result = await save(session, operation, backend, backend_name)
            await _complete_output_save_operation(
                operation,
                result.apply_response,
                success_message=success_message,
                failure_prefix=failure_prefix,
                backend_id=backend_id,
                backend_name=result.backend_name,
                extra_details=result.extra_details,
            )
    except ValidationError as exc:
        flash_error = operator_validation_error(exc)
        await operation.complete(
            "failed",
            phase="Validate output",
            error=flash_error,
            details={
                "progress": 100,
                "message": flash_error,
                "substate": "Validating output",
                "flash_error": flash_error,
                "backend_id": backend_id,
                **failure_extra,
            },
        )
    except Exception as exc:
        error_fields = error_context(failure_error_code)
        flash_error = operator_action_error(
            failure_action,
            failure_detail,
            error_fields,
        )
        logger.exception(
            log_event,
            backend_id=backend_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="Save output",
            error=flash_error,
            details={
                "progress": 100,
                "message": failure_message,
                "substate": "Refreshing output",
                "flash_error": flash_error,
                "backend_id": backend_id,
                **error_fields,
                **failure_extra,
            },
        )


async def _run_update_backend_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    form_values: dict[str, object],
) -> None:
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        shield_code_hash = backend.shield_code_hash
        shield_access_code_display = str(
            getattr(backend, "shield_access_code", "") or ""
        ).strip()
        normalized_shield_access_code = str(
            form_values.get("shield_access_code") or ""
        ).strip()
        if normalized_shield_access_code:
            shield_code_hash = make_code_hash(
                settings.shield_secret_key, normalized_shield_access_code
            )
            shield_access_code_display = normalized_shield_access_code
        if (
            backend.kind == "app"
            and bool(form_values.get("shield_enabled"))
            and not shield_code_hash
        ):
            raise ValidationError(
                "shield requires an access code before it can be enabled"
            )
        payload = _backend_update_payload_from_form(
            name=str(form_values.get("name") or ""),
            kind=backend.kind,
            port=str(form_values.get("port") or ""),
            static_root=str(form_values.get("static_root") or ""),
            sandbox_profile=str(form_values.get("sandbox_profile") or ""),
            sandbox_image=str(form_values.get("sandbox_image") or ""),
            handoff_port=int(form_values.get("handoff_port") or 8000),
            healthcheck_mode=str(form_values.get("healthcheck_mode") or "none"),
            healthcheck_path=str(form_values.get("healthcheck_path") or "/"),
            healthcheck_host_header=str(
                form_values.get("healthcheck_host_header") or ""
            ),
            resource_mode=str(form_values.get("resource_mode") or "auto"),
            resource_size=str(form_values.get("resource_size") or "auto"),
            current_resource_size=backend.resource_size,
            memory_high_override=str(form_values.get("memory_high_override") or ""),
            memory_max_override=str(form_values.get("memory_max_override") or ""),
            cpu_quota_override=str(form_values.get("cpu_quota_override") or ""),
            volumes_json=str(form_values.get("volumes_json") or "[]"),
            notes=str(form_values.get("notes") or ""),
            shield_enabled=bool(form_values.get("shield_enabled"))
            if backend.kind == "app"
            else False,
            shield_code_hash=shield_code_hash,
            shield_access_code=shield_access_code_display,
        )
        changed_keys = _backend_update_changed_keys(backend, payload)
        backend = await backend_commands.update_backend(
            session, backend_id, payload, commit=False
        )
        backend_name = str(backend.name or backend_name)
        await _update_output_save_operation_progress(
            operation,
            message="Checking changed slices.",
            phase="Save desired state",
            substate="Checking changed slices",
            backend_id=backend_id,
            backend_name=backend_name,
            changed_keys=sorted(changed_keys),
        )
        apply_response = await _commit_output_save_apply(
            session,
            settings,
            operation,
            operation_name="ui.backend.update",
            backend_id=backend_id,
            backend_name=backend_name,
        )
        return _OutputSaveResult(
            apply_response=apply_response,
            backend_name=backend_name,
            extra_details={"changed_keys": sorted(changed_keys)},
        )

    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        operation_kind="ui.backend.update",
        success_message="Output saved.",
        failure_prefix="Output saved",
        failure_action="Output save",
        failure_detail="Unexpected error while saving the output. Check server logs with this instance.",
        failure_message="Output save failed.",
        failure_error_code=ErrorCode.UI_BACKEND_UPDATE_FAILED,
        log_event="ui.backend.update.worker_failed",
        save=save,
        progress_extra={},
        failure_extra={},
    )


async def _run_update_backend_inputs_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    input_ids: list[str],
) -> None:
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        _backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        payload = _backend_inputs_payload_from_form(input_ids=input_ids)
        normalized_input_ids = list(payload.input_ids or [])
        await backend_commands.set_backend_inputs(
            session,
            backend_id,
            normalized_input_ids,
            commit=False,
        )
        apply_response = await _commit_output_save_apply(
            session,
            settings,
            operation,
            operation_name="ui.backend.inputs",
            backend_id=backend_id,
            backend_name=backend_name,
        )
        return _OutputSaveResult(
            apply_response=apply_response,
            backend_name=backend_name,
            extra_details={"input_ids": normalized_input_ids},
        )

    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        operation_kind="ui.backend.inputs",
        success_message="Attached inputs saved.",
        failure_prefix="Attached inputs saved",
        failure_action="Attached inputs save",
        failure_detail="Unexpected error while saving attached inputs. Check server logs with this instance.",
        failure_message="Attached inputs save failed.",
        failure_error_code=ErrorCode.UI_BACKEND_INPUTS_FAILED,
        log_event="ui.backend.inputs.worker_failed",
        save=save,
        progress_extra={},
        failure_extra={},
    )


async def _run_update_backend_state_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    target_enabled: bool,
) -> None:
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        _backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        await backend_commands.set_backend_enabled(
            session, backend_id, target_enabled, commit=False
        )
        apply_response = await _commit_output_save_apply(
            session,
            settings,
            operation,
            operation_name="ui.backend.state",
            backend_id=backend_id,
            backend_name=backend_name,
        )
        return _OutputSaveResult(
            apply_response=apply_response,
            backend_name=backend_name,
            extra_details={"enabled": target_enabled},
        )

    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        operation_kind="ui.backend.state",
        success_message="Output enabled." if target_enabled else "Output disabled.",
        failure_prefix="Output enabled" if target_enabled else "Output disabled",
        failure_action="Output state save",
        failure_detail="Unexpected error while updating output state. Check server logs with this instance.",
        failure_message="Output state save failed.",
        failure_error_code=ErrorCode.UI_BACKEND_STATE_FAILED,
        log_event="ui.backend.state.worker_failed",
        save=save,
        progress_extra={"enabled": target_enabled},
        failure_extra={"enabled": target_enabled},
    )


async def _run_update_backend_interface_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    *,
    source_backend_id: int,
    interface_name: str,
    status: str,
    direction: str,
) -> None:
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        await apply_inbound_inter_app_interface_statuses(
            session,
            target_backend_id=backend.id,
            source_backend_ids=[str(source_backend_id)],
            names=[interface_name],
            statuses=[status],
            directions=[direction],
        )
        apply_response = await _commit_output_save_apply(
            session,
            settings,
            operation,
            operation_name="ui.backend.interface",
            backend_id=backend_id,
            backend_name=backend_name,
        )
        return _OutputSaveResult(
            apply_response=apply_response,
            backend_name=backend_name,
            extra_details={
                "status": status,
                "direction": direction,
                "source_backend_id": source_backend_id,
                "interface_name": interface_name,
            },
        )

    accepted = status == "accepted"
    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        operation_kind="ui.backend.interface",
        success_message="Interface confirmed." if accepted else "Interface rejected.",
        failure_prefix="Interface saved",
        failure_action="Interface action",
        failure_detail="Unexpected error while updating the interface. Check server logs with this instance.",
        failure_message="Interface action failed.",
        failure_error_code=ErrorCode.UI_BACKEND_INTERFACE_FAILED,
        log_event="ui.backend.interface.worker_failed",
        save=save,
        progress_extra={
            "source_backend_id": source_backend_id,
            "interface_name": interface_name,
            "status": status,
            "direction": direction,
        },
        failure_extra={
            "source_backend_id": source_backend_id,
            "interface_name": interface_name,
        },
    )
