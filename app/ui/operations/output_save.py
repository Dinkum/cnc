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
from app.services.mutation_queue import mutation_runner
from app.services.apply_service import default_apply_services, run_apply
from app.services.error_reporting import ErrorCode
from app.services.config_change import ConfigChange, converge_change
from app.ui.operations.action import Action
from app.ui.operations.save import Reporter, SaveResult, Subject, run_save
from app.services.inter_app_interfaces import (
    apply_inbound_inter_app_interface_statuses,
    inter_app_interface_peer_names,
)
from app.services.operations import OperationHandle
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.ui.forms import (
    _backend_inputs_payload_from_form,
    _backend_update_payload_from_form,
)
from app.ui.operations.common import background_db_session, operation_backend
from app.ui.outputs.settings import backend_section_payload
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
    related_outputs: set[str] | None = None,
) -> ApplyResponse:
    services = replace(
        default_apply_services(),
        progress_callback=_output_save_apply_progress_reporter(
            operation,
            backend_id=backend_id,
            backend_name=backend_name,
        ),
    )
    mutation = await converge_change(
        session,
        settings,
        ConfigChange(
            kind=operation_name,
            outputs=frozenset({backend_name, *(related_outputs or ())}),
        ),
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
    action: Action,
    save: Callable[
        [AsyncSession, OperationHandle, Backend, str], Awaitable[_OutputSaveResult]
    ],
    progress_extra: dict[str, object],
    failure_extra: dict[str, object],
) -> None:

    async def mutate(session, operation, backend, report):
        name = str(backend.name or "")
        await report.with_name(name).step(
            operation,
            "Saving desired state.",
            phase="Save desired state",
            substate="Saving desired state",
        )
        result = await save(session, operation, backend, name)
        return SaveResult(
            apply_response=result.apply_response,
            name=result.backend_name,
            extra_details=result.extra_details,
        )

    async def complete(operation, result):
        await _complete_output_save_operation(
            operation,
            result.apply_response,
            success_message=action.success,
            failure_prefix=action.failure_prefix,
            backend_id=backend_id,
            backend_name=result.name,
            extra_details=result.extra_details,
        )

    await run_save(
        settings,
        operation_id,
        action=action,
        subject=Subject(
            id=backend_id,
            load=lambda session: operation_backend(session, backend_id),
        ),
        reporter=Reporter(action, Subject(id=backend_id), extra=progress_extra),
        mutate=mutate,
        complete=complete,
        session_factory=background_db_session,
    )


@mutation_runner("update_backend")
async def _run_update_backend_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    form_values: dict[str, object],
    shield_access_code: str = "",
) -> None:
    if shield_access_code and "section" in form_values:
        form_values = {
            **form_values,
            "values": {**form_values["values"], "shield_access_code": shield_access_code},
        }
    elif shield_access_code:
        form_values = {**form_values, "shield_access_code": shield_access_code}
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        section = str(form_values.get("section") or "")
        if section:
            payload = backend_section_payload(
                backend, settings, section, form_values["values"]
            )
        else:
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
        saved_values = {}
        if section:
            saved_values = {
                key: getattr(backend, key, None)
                for key in form_values["values"]
                if key != "shield_access_code"
            }
            if section == "shield":
                saved_values["shield_enabled"] = bool(backend.shield_enabled)
            if section == "resources":
                saved_values["resource_size"] = form_values["values"]["resource_size"]
            if section == "health" and backend.healthcheck_mode != "http":
                saved_values.pop("healthcheck_path", None)
                saved_values.pop("healthcheck_host_header", None)
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
            extra_details={
                "changed_keys": sorted(changed_keys),
                "section": section,
                "saved_values": saved_values,
            },
        )

    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        action=Action(
            kind="ui.backend.update",
            success="Output saved.",
            label="Output save",
            error_code=ErrorCode.UI_BACKEND_UPDATE_FAILED,
            detail="Unexpected error while saving the output. Check server logs with this instance.",
        ),
        save=save,
        progress_extra={"section": str(form_values.get("section") or "")},
        failure_extra={"section": str(form_values.get("section") or "")},
    )


@mutation_runner("update_backend_inputs")
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
        action=Action(
            kind="ui.backend.inputs",
            success="Attached inputs saved.",
            label="Attached inputs save",
            error_code=ErrorCode.UI_BACKEND_INPUTS_FAILED,
            detail="Unexpected error while saving attached inputs. Check server logs with this instance.",
        ),
        save=save,
        progress_extra={},
        failure_extra={},
    )


@mutation_runner("update_backend_state")
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
        action=Action(
            kind="ui.backend.state",
            success="Output enabled." if target_enabled else "Output disabled.",
            label="Output state save",
            error_code=ErrorCode.UI_BACKEND_STATE_FAILED,
            detail="Unexpected error while updating output state. Check server logs with this instance.",
        ),
        save=save,
        progress_extra={"enabled": target_enabled},
        failure_extra={"enabled": target_enabled},
    )


@mutation_runner("update_backend_interface")
async def _run_update_backend_interface_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    *,
    source_backend_id: int,
    interface_name: str,
    status: str,
) -> None:
    async def save(
        session: AsyncSession,
        operation: OperationHandle,
        backend: Backend,
        backend_name: str,
    ) -> _OutputSaveResult:
        peers = await inter_app_interface_peer_names(session, backend.id)
        await apply_inbound_inter_app_interface_statuses(
            session,
            target_backend_id=backend.id,
            source_backend_ids=[str(source_backend_id)],
            names=[interface_name],
            statuses=[status],
        )
        peers |= await inter_app_interface_peer_names(session, backend.id)
        apply_response = await _commit_output_save_apply(
            session,
            settings,
            operation,
            operation_name="ui.backend.interface",
            backend_id=backend_id,
            backend_name=backend_name,
            related_outputs=peers,
        )
        return _OutputSaveResult(
            apply_response=apply_response,
            backend_name=backend_name,
            extra_details={
                "status": status,
                "source_backend_id": source_backend_id,
                "interface_name": interface_name,
            },
        )

    accepted = status == "accepted"
    await _run_output_save_operation(
        settings,
        operation_id,
        backend_id,
        action=Action(
            kind="ui.backend.interface",
            success="Interface confirmed." if accepted else "Interface rejected.",
            label="Interface action",
            error_code=ErrorCode.UI_BACKEND_INTERFACE_FAILED,
            detail="Unexpected error while updating the interface. Check server logs with this instance.",
            failure_prefix_override="Interface saved",
        ),
        save=save,
        progress_extra={
            "source_backend_id": source_backend_id,
            "interface_name": interface_name,
            "status": status,
        },
        failure_extra={
            "source_backend_id": source_backend_id,
            "interface_name": interface_name,
        },
    )
