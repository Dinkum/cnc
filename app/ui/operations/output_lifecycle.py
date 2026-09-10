from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.logger import flush_logging_pipeline_async, get_logger
from app.services import backend_commands
from app.services.app_runtime import remove_deleted_app_filesystem_artifacts
from app.services.apply_service import default_apply_services, run_apply
from app.services.error_reporting import ErrorCode, error_context
from app.services.mutation_apply import commit_and_apply
from app.services.operation_runtime import (
    create_backend_runtime_progress,
    create_backend_runtime_summary,
    delete_output_progress_plan,
    operation_progress_plan,
    progress_plan_value,
)
from app.services.operations import OperationHandle
from app.services.ssh_access import remove_backend_ssh_access
from app.services.validators import ValidationError
from app.ui.errors import operator_action_error
from app.ui.operations.common import background_db_session, operation_backend
from app.ui.progress import (
    _CreateBackendProgressRecorder,
    _create_backend_apply_progress_reporter,
    _create_output_progress_plan_for_apply,
    _create_output_progress_value,
    _delete_output_apply_progress_reporter,
    _planned_operation_progress_details,
)
from app.ui.view_models import _save_apply_feedback


logger = get_logger("ui")


def operator_validation_error(exc: ValidationError) -> str:
    return str(exc)


async def _run_create_backend_operation(
    settings: Settings, operation_id: int | None, payload
) -> None:
    operation = _CreateBackendProgressRecorder(
        OperationHandle(id=operation_id, kind="create_backend", settings=settings)
    )
    progress_plan = _create_output_progress_plan_for_payload(payload)

    def current_progress_plan() -> dict[str, object]:
        return progress_plan

    heartbeat_task: asyncio.Task[None] | None = None
    try:
        await operation.update(
            status="running",
            phase="Validate output",
            details=_planned_operation_progress_details(
                progress_plan,
                operation.last_progress,
                "Checking ports and health settings",
                phase="Validate output",
                substate="Checking ports and health settings",
            ),
        )
        async with background_db_session(settings) as session:
            await operation.update(
                status="running",
                phase="Save desired state",
                details=_planned_operation_progress_details(
                    progress_plan,
                    operation.last_progress,
                    "Opening save transaction",
                    phase="Save desired state",
                    substate="Opening save transaction",
                ),
            )
            await operation.update(
                status="running",
                phase="Save desired state",
                details=_planned_operation_progress_details(
                    progress_plan,
                    operation.last_progress,
                    "Staging output config",
                    phase="Save desired state",
                    substate="Staging output config",
                ),
            )
            backend = await backend_commands.create_backend(
                session, payload, commit=False
            )
            await session.flush()
            backend_name = str(backend.name)
            backend_id = backend.id
            progress_plan = _create_output_progress_plan_for_apply(backend)
            await operation.update(
                status="running",
                phase="Save desired state",
                details={
                    **_planned_operation_progress_details(
                        progress_plan,
                        operation.last_progress,
                        "Staging attached input routes",
                        phase="Save desired state",
                        substate="Staging attached input routes",
                    ),
                    "backend_name": backend_name,
                },
            )
            await operation.update(
                status="running",
                phase="Save desired state",
                details={
                    **_planned_operation_progress_details(
                        progress_plan,
                        operation.last_progress,
                        "Saving route graph",
                        phase="Save desired state",
                        substate="Saving route graph",
                    ),
                    "backend_name": backend_name,
                },
            )
            await operation.update(
                status="running",
                phase="Save desired state",
                details={
                    **_planned_operation_progress_details(
                        progress_plan,
                        operation.last_progress,
                        "Waiting for host mutation lock",
                        phase="Save desired state",
                        substate="Waiting for host mutation lock",
                    ),
                    "backend_name": backend_name,
                },
            )

            async def update_progress_plan_from_apply(
                desired: object, apply_plan: object
            ) -> None:
                nonlocal progress_plan
                progress_plan = _create_output_progress_plan_for_apply(
                    backend, desired=desired, apply_plan=apply_plan
                )
                await operation.update(
                    status="running",
                    phase="Save desired state",
                    details={
                        **_planned_operation_progress_details(
                            progress_plan,
                            operation.last_progress,
                            "Building host plan",
                            phase="Save desired state",
                            substate="Building host plan",
                        ),
                        "backend_name": backend_name,
                    },
                )

            heartbeat_task = asyncio.create_task(
                _heartbeat_create_backend_operation(
                    operation,
                    backend_name=backend_name,
                    progress_plan_getter=current_progress_plan,
                )
            )
            try:
                mutation = await asyncio.wait_for(
                    commit_and_apply(
                        session,
                        settings,
                        operation="ui.backend.create",
                        apply_runner=run_apply,
                        apply_services=_create_backend_apply_services(
                            operation,
                            backend_name=backend_name,
                            progress_plan_getter=current_progress_plan,
                            progress_plan_callback=update_progress_plan_from_apply,
                        ),
                        operation_handle=operation,
                    ),
                    timeout=max(settings.command_timeout_apply_sec + 30, 60),
                )
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            apply_response = mutation.apply_response
            success_flash, error_flash = _save_apply_feedback(
                apply_response,
                success_message="Output created.",
                failure_prefix="Output saved",
            )
            status = "success" if apply_response.status == "success" else "failed"
            details: dict[str, object] = {
                "progress": 100,
                "message": success_flash or error_flash or "Output create finished.",
                "flash_success": success_flash,
                "flash_error": error_flash,
                "apply_status": apply_response.status,
                "run_id": apply_response.run_id,
                "progress_plan": progress_plan,
                "backend_name": backend_name,
                "backend_id": backend_id
                if apply_response.status == "success"
                else None,
            }
            if apply_response.status == "success":
                runtime_summary = create_backend_runtime_summary(settings, backend_name)
                if runtime_summary:
                    details["runtime"] = runtime_summary
            await operation.complete(
                status, phase="Create output", error=error_flash, details=details
            )
    except ValidationError as exc:
        flash_error = operator_validation_error(exc)
        await operation.complete(
            "failed",
            phase="Validate output",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Output validation failed.",
                "flash_error": flash_error,
            },
        )
    except IntegrityError as exc:
        error_fields = error_context(ErrorCode.UI_BACKEND_CREATE_CONFLICT)
        flash_error = operator_action_error(
            "Output create",
            "name already exists. Use a unique output name.",
            error_fields,
        )
        await operation.complete(
            "failed",
            phase="Save desired state",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Output name already exists.",
                "flash_error": flash_error,
                **error_fields,
            },
        )
        logger.warning("ui.backend.create.conflict", **error_fields, error=str(exc))
    except Exception as exc:
        error_fields = error_context(ErrorCode.UI_BACKEND_CREATE_FAILED)
        runtime_summary = create_backend_runtime_summary(
            settings, getattr(payload, "name", "")
        )
        message = "Output create failed."
        if runtime_summary and runtime_summary.get("message"):
            message = f"Output create failed while {runtime_summary['message']}."
        flash_error = operator_action_error(
            "Output create",
            "Unexpected error while creating the output. Check server logs with this instance.",
            error_fields,
        )
        await operation.complete(
            "failed",
            phase="Create output",
            error=flash_error,
            details={
                "progress": 100,
                "message": message,
                "flash_error": flash_error,
                **error_fields,
                **({"runtime": runtime_summary} if runtime_summary else {}),
            },
        )
        logger.exception(
            "ui.backend.create.operation_failed", **error_fields, error=str(exc)
        )


def _create_output_progress_plan_for_payload(payload: object) -> dict[str, object]:
    from app.services.operation_runtime import create_output_progress_plan

    return create_output_progress_plan(
        backend_kind=str(getattr(payload, "kind", "app") or "app"),
        input_kinds=(),
    )


def _create_backend_apply_services(
    operation: _CreateBackendProgressRecorder,
    *,
    backend_name: str,
    progress_plan_getter,
    progress_plan_callback,
):
    from dataclasses import replace

    return replace(
        default_apply_services(),
        progress_callback=_create_backend_apply_progress_reporter(
            operation,
            backend_name=backend_name,
            progress_plan_getter=progress_plan_getter,
        ),
        progress_plan_callback=progress_plan_callback,
    )


async def _heartbeat_create_backend_operation(
    operation: _CreateBackendProgressRecorder,
    *,
    backend_name: str,
    progress_plan_getter,
) -> None:
    stages = [
        (
            _create_output_progress_value(
                "Plan runtime", substate="Inspecting app container"
            ),
            "Plan runtime",
            "Inspecting app container",
        ),
    ]
    index = 0
    while True:
        progress, phase, message = stages[min(index, len(stages) - 1)]
        runtime_progress = create_backend_runtime_progress(
            operation.settings, backend_name
        )
        if runtime_progress:
            phase = runtime_progress["phase"]
            message = runtime_progress["substate"] or runtime_progress["message"]
            progress = int(
                runtime_progress.get("progress")
                or _create_output_progress_value(phase, progress, substate=message)
            )
        progress_plan = progress_plan_getter() if progress_plan_getter else None
        if progress_plan is not None:
            progress = progress_plan_value(
                progress_plan,
                phase=str(phase),
                message=str(message),
                current=operation.last_progress,
            )
        substate = message
        await operation.update(
            status="running",
            phase=phase,
            details={
                "progress": progress,
                "message": message,
                "substate": substate,
                "backend_name": backend_name,
                **({"progress_plan": progress_plan} if progress_plan else {}),
                **({"runtime": runtime_progress} if runtime_progress else {}),
            },
        )
        index += 1
        await asyncio.sleep(4)


async def _run_delete_operation(
    settings: Settings, operation_id: int | None, backend_id: int
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="delete_backend", settings=settings
    )
    progress_plan = operation_progress_plan("deleteOutput")
    try:
        logger.info(
            "ui.backend.delete.worker_started",
            backend_id=backend_id,
            operation_id=operation_id,
        )
        await flush_logging_pipeline_async()
        await operation.update(
            status="running",
            phase="Delete output",
            details=_planned_operation_progress_details(
                progress_plan,
                0,
                "Loading output.",
                phase="Delete output",
                substate="Opening delete operation",
            ),
        )
        async with background_db_session(settings) as session:
            backend = await operation_backend(session, backend_id)
            deleted_backend_name = str(backend.name or "").strip()
            progress_plan = delete_output_progress_plan(backend_kind=backend.kind)
            logger.info(
                "ui.backend.delete.worker_loaded",
                backend_id=backend_id,
                backend_name=deleted_backend_name,
                operation_id=operation_id,
                enabled=backend.enabled,
                input_count=len(backend.inputs),
            )
            await flush_logging_pipeline_async()
            await operation.update(
                status="running",
                phase="Delete output",
                details={
                    **_planned_operation_progress_details(
                        progress_plan,
                        0,
                        "Removing desired state.",
                        phase="Delete output",
                        substate="Removing desired state",
                    ),
                    "backend_name": deleted_backend_name,
                },
            )
            await session.delete(backend)
            services = default_apply_services()

            async def _skip_backend_ssh_reconcile(
                backends: list[str], _settings: Settings
            ) -> dict[str, object]:
                desired = sorted(set(backends))
                return {
                    "ssh_backends": desired,
                    "ssh_aliases_enabled": bool(desired),
                    "ssh_reconcile_deferred": True,
                }

            services.reconcile_backend_ssh_access = _skip_backend_ssh_reconcile
            services.cleanup_deleted_app_filesystem = False
            await operation.update(
                status="running",
                phase="Delete output",
                details={
                    **_planned_operation_progress_details(
                        progress_plan,
                        0,
                        "Waiting for host mutation lock.",
                        phase="Delete output",
                        substate="Waiting for host mutation lock",
                    ),
                    "backend_name": deleted_backend_name,
                },
            )
            services.progress_callback = _delete_output_apply_progress_reporter(
                operation,
                progress_plan=progress_plan,
                backend_name=deleted_backend_name,
            )
            mutation = await commit_and_apply(
                session,
                settings,
                operation="ui.backend.delete",
                apply_runner=run_apply,
                apply_services=services,
                operation_handle=operation,
            )
            apply_response = mutation.apply_response
            filesystem_cleanup_error: str | None = None
            logger.info(
                "ui.backend.delete.worker_applied",
                backend_id=backend_id,
                backend_name=deleted_backend_name,
                operation_id=operation_id,
                mutation_state=mutation.state,
                apply_status=apply_response.status,
                run_id=apply_response.run_id,
            )
            await flush_logging_pipeline_async()
            if apply_response.status == "success" and deleted_backend_name:
                try:
                    await remove_deleted_app_filesystem_artifacts(
                        {deleted_backend_name}, settings
                    )
                except Exception as exc:
                    filesystem_cleanup_error = str(exc)
                    logger.exception(
                        "ui.backend.delete.filesystem_cleanup_failed",
                        backend_id=backend_id,
                        backend=deleted_backend_name,
                        error=filesystem_cleanup_error,
                    )
                    await flush_logging_pipeline_async()
                try:
                    await operation.update(
                        status="running",
                        phase="Apply host access",
                        details={
                            **_planned_operation_progress_details(
                                progress_plan,
                                0,
                                "Cleaning backend SSH access.",
                                phase="Apply host access",
                                substate="Cleaning backend SSH access",
                            ),
                            "backend_name": deleted_backend_name,
                            "run_id": apply_response.run_id,
                        },
                    )
                    await remove_backend_ssh_access([deleted_backend_name], settings)
                except Exception as exc:
                    logger.warning(
                        "ui.backend.delete.ssh_cleanup_failed",
                        backend_id=backend_id,
                        backend=deleted_backend_name,
                        error=str(exc),
                    )
                    await flush_logging_pipeline_async()
            success_flash, error_flash = _save_apply_feedback(
                apply_response,
                success_message="Output deleted.",
                failure_prefix="Output deleted",
            )
            if filesystem_cleanup_error:
                success_flash = None
                error_flash = operator_action_error(
                    "Output delete",
                    "the output was deleted, but its persistent filesystem cleanup did not finish. The retained files are recoverable; check server logs and retry cleanup.",
                    error_context(ErrorCode.OUTPUT_DELETE_FAILED),
                )
            details: dict[str, object] = {
                "progress": 100,
                "message": success_flash or error_flash or "Delete finished.",
                "substate": "Refreshing outputs list",
                "flash_success": success_flash,
                "flash_error": error_flash,
                "apply_status": apply_response.status,
                "run_id": apply_response.run_id,
                "progress_plan": progress_plan,
                "redirect_url": "/?tab=outputs&defer_status=1",
                "filesystem_cleanup": {
                    "status": "failed" if filesystem_cleanup_error else "success",
                    "error": filesystem_cleanup_error,
                }
                if apply_response.status == "success"
                else {"status": "deferred"},
            }
            status = (
                "partial"
                if filesystem_cleanup_error
                else "success"
                if apply_response.status == "success"
                else "failed"
            )
            logger.info(
                "ui.backend.delete.worker_finished",
                backend_id=backend_id,
                backend_name=deleted_backend_name,
                operation_id=operation_id,
                operation_status=status,
                apply_status=apply_response.status,
                run_id=apply_response.run_id,
            )
            await flush_logging_pipeline_async()
            await operation.complete(
                status, phase="delete", error=error_flash, details=details
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.OUTPUT_DELETE_FAILED)
        flash_error = operator_action_error(
            "Delete",
            "Unexpected error while deleting the output. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.backend.delete.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await flush_logging_pipeline_async()
        await operation.complete(
            "failed",
            phase="delete",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Delete failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "progress_plan": progress_plan,
                **error_fields,
            },
        )
