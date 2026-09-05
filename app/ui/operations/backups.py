from __future__ import annotations

import asyncio
from pathlib import Path
import shutil

from app.config import Settings
from app.logger import get_logger
from app.services import backend_commands
from app.services.backend_backup_service import (
    create_backend_backup,
    delete_backend_backup,
    get_backend_backup,
    import_backend_backup_bundle,
    restore_backend_backup,
    restore_latest_backend_backup,
)
from app.services.error_reporting import ErrorCode, error_context
from app.services.operations import OperationHandle
from app.ui.errors import operator_action_error
from app.ui.forms import _backend_clone_payload_from_form
from app.ui.operations.common import background_db_session, operation_backend
from app.ui.progress import (
    _backup_progress_substate,
    _clone_progress_substate,
    _operation_progress_details,
    _restore_progress_substate,
)
from app.ui.view_models import _format_bytes


logger = get_logger("ui")


async def _run_backup_operation(
    settings: Settings, operation_id: int | None, backend_id: int
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="backup_backend", settings=settings
    )
    try:
        last_progress = {"value": 0}
        initial_details = _operation_progress_details(
            "backup",
            0,
            "Backup requested.",
            phase="Backup",
            substate="Backup requested",
            operation_role="ui_backup",
            backend_id=backend_id,
        )
        last_progress["value"] = int(initial_details["progress"])
        await operation.update(
            status="running",
            phase="Backup",
            details=initial_details,
        )
        async with background_db_session(settings) as session:
            backend = await operation_backend(session, backend_id)
            loop = asyncio.get_running_loop()

            def report_backup_progress(_progress: int, message: str) -> None:
                substate = _backup_progress_substate(message)
                details = _operation_progress_details(
                    "backup",
                    int(last_progress["value"]),
                    message,
                    phase="Backup",
                    substate=substate,
                    operation_role="ui_backup",
                    backend_id=backend_id,
                )
                last_progress["value"] = int(details["progress"])
                future = asyncio.run_coroutine_threadsafe(
                    operation.update(
                        status="running",
                        phase="Backup",
                        details=details,
                    ),
                    loop,
                )
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    logger.warning("ui.backup.progress_update_failed", error=str(exc))

            backup = await create_backend_backup(
                session,
                backend,
                settings,
                progress_callback=report_backup_progress,
                operation=operation,
            )
            if backup.status != "success":
                raise RuntimeError(backup.error or "backup failed")
            final_running_details = _operation_progress_details(
                "backup",
                int(last_progress["value"]),
                "Saving backup record.",
                phase="Backup",
                substate="Saving backup record",
                operation_role="ui_backup",
                backend_id=backend_id,
                backup_id=backup.id,
            )
            last_progress["value"] = int(final_running_details["progress"])
            await operation.update(
                status="running",
                phase="Backup",
                details=final_running_details,
            )
            await operation.complete(
                "success",
                phase="completed",
                details={
                    "progress": 100,
                    "message": "Backup created!",
                    "flash_success": f"Backup created: {backup.scope} ({_format_bytes(backup.size_bytes)}).",
                    "backup_id": backup.id,
                    "backup_status": backup.status,
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.BACKUP_CREATE_FAILED)
        flash_error = operator_action_error(
            "Backup",
            "Unexpected error while creating the backup. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.backup.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="Backup",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Backup failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                **error_fields,
            },
        )


async def _run_restore_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    backup_id: int | None,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="restore_backend", settings=settings
    )
    try:
        last_progress = {"value": 0}
        initial_details = _operation_progress_details(
            "restore",
            0,
            "Preparing restore.",
            phase="Restore backup",
            substate="Preparing restore",
            backend_id=backend_id,
            backup_id=backup_id,
        )
        last_progress["value"] = int(initial_details["progress"])
        await operation.update(
            status="running",
            phase="Restore backup",
            details=initial_details,
        )
        async with background_db_session(settings) as session:
            loop = asyncio.get_running_loop()
            progress_updates = []

            def report_restore_progress(_progress: int, message: str) -> None:
                substate = _restore_progress_substate(message)
                details = _operation_progress_details(
                    "restore",
                    int(last_progress["value"]),
                    message,
                    phase="Restore backup",
                    substate=substate,
                    backend_id=backend_id,
                    backup_id=backup_id,
                )
                last_progress["value"] = int(details["progress"])
                progress_updates.append(
                    asyncio.run_coroutine_threadsafe(
                        operation.update(
                            status="running",
                            phase="Restore backup",
                            details=details,
                        ),
                        loop,
                    )
                )

            backend = await operation_backend(session, backend_id)
            if backup_id is None:
                restore_result = await restore_latest_backend_backup(
                    session,
                    backend,
                    settings,
                    progress_callback=report_restore_progress,
                    operation=operation,
                )
                restored_backup = restore_result.get("backup")
            else:
                selected_backup = await get_backend_backup(
                    session, backend.id, backup_id
                )
                if selected_backup is None:
                    raise LookupError("backup not found for this output")
                restore_result = await restore_backend_backup(
                    session,
                    backend,
                    selected_backup,
                    settings,
                    progress_callback=report_restore_progress,
                    operation=operation,
                )
                restored_backup = selected_backup
            for future in progress_updates:
                try:
                    await asyncio.wrap_future(future)
                except Exception as exc:
                    logger.warning("ui.restore.progress_update_failed", error=str(exc))
            restored_paths = restore_result.get("restored_paths") or []
            backup_label = (
                f"backup #{restored_backup.id}"
                if restored_backup is not None
                and getattr(restored_backup, "id", None) is not None
                else "latest backup"
            )
            flash_success = (
                f"Restore completed from {backup_label}. Restored {len(restored_paths)} mounted path(s)."
                if restored_paths
                else f"Restore completed from {backup_label}."
            )
            await operation.complete(
                "success",
                phase="completed",
                details={
                    "progress": 100,
                    "message": "Restore completed.",
                    "flash_success": flash_success,
                    "restored_paths": len(restored_paths),
                    "backup_id": getattr(restored_backup, "id", None),
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.RESTORE_FAILED)
        flash_error = operator_action_error(
            "Restore",
            "Unexpected error while restoring the backup. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.restore.worker_failed",
            backend_id=backend_id,
            backup_id=backup_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="Restore backup",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Restore failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "backup_id": backup_id,
                **error_fields,
            },
        )


async def _run_delete_backup_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    backup_id: int,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="delete_backend_backup", settings=settings
    )
    try:
        await operation.update(
            status="running",
            phase="locating",
            details=_operation_progress_details(
                "deleteBackup",
                16,
                f"Finding backup #{backup_id}.",
                phase="Delete backup",
                substate="Finding backup",
                backup_id=backup_id,
            ),
        )
        async with background_db_session(settings) as session:
            backup = await get_backend_backup(session, backend_id, backup_id)
            if backup is None:
                raise LookupError("backup not found for this output")
            bundle_name = (
                Path(str(backup.bundle_path or "-")).name if backup.bundle_path else "-"
            )
            await operation.update(
                status="running",
                phase="removing",
                details={
                    **_operation_progress_details(
                        "deleteBackup",
                        54,
                        f"Deleting {bundle_name}.",
                        phase="Delete backup",
                        substate="Removing backup bundle",
                    ),
                    "backup_id": backup_id,
                    "bundle": bundle_name,
                },
            )
            result = await delete_backend_backup(
                session, backend_id, backup_id, settings
            )
            await operation.update(
                status="running",
                phase="refreshing",
                details={
                    **_operation_progress_details(
                        "deleteBackup",
                        82,
                        "Refreshing backup history.",
                        phase="Delete backup",
                    ),
                    "backup_id": backup_id,
                    "bundle": result.get("bundle", bundle_name),
                },
            )
            await operation.complete(
                "success",
                phase="deleted",
                details={
                    "progress": 100,
                    "message": f"Backup #{backup_id} deleted.",
                    "flash_success": f"Backup #{backup_id} deleted.",
                    **result,
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.BACKUP_DELETE_FAILED)
        flash_error = operator_action_error(
            "Backup delete",
            "Unexpected error while deleting the backup. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.backup_delete.worker_failed",
            backend_id=backend_id,
            backup_id=backup_id,
            operation_id=operation_id,
            **error_fields,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="delete",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Backup delete failed.",
                "flash_error": flash_error,
                "backup_id": backup_id,
                "backend_id": backend_id,
                **error_fields,
            },
        )


async def _run_import_backup_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    temp_path: str,
    original_name: str,
    temp_dir: str,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="import_backend_backup", settings=settings
    )
    try:
        await operation.update(
            status="running",
            phase="upload",
            details=_operation_progress_details(
                "importBackup",
                12,
                "Reading backup bundle.",
                phase="Import backup",
                operation_role="ui_backup_import",
            ),
        )
        async with background_db_session(settings) as session:
            backend = await operation_backend(session, backend_id)
            await operation.update(
                status="running",
                phase="verify",
                details=_operation_progress_details(
                    "importBackup",
                    38,
                    "Verifying backup bundle.",
                    phase="Import backup",
                    operation_role="ui_backup_import",
                    filename=original_name,
                ),
            )
            imported_backup = await import_backend_backup_bundle(
                session,
                backend,
                Path(temp_path),
                original_name=original_name,
                settings=settings,
                operation=operation,
            )
            if imported_backup.status != "success":
                raise RuntimeError(imported_backup.error or "backup import failed")
            await operation.update(
                status="running",
                phase="history",
                details=_operation_progress_details(
                    "importBackup",
                    78,
                    "Adding backup to history.",
                    phase="Import backup",
                    operation_role="ui_backup_import",
                    backup_id=imported_backup.id,
                ),
            )
            await operation.complete(
                "success",
                phase="imported",
                details={
                    "progress": 100,
                    "operation_role": "ui_backup_import",
                    "message": f"Backup #{imported_backup.id} imported.",
                    "flash_success": f"Backup #{imported_backup.id} imported.",
                    "backup_id": imported_backup.id,
                    "backup_status": imported_backup.status,
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.BACKUP_IMPORT_FAILED)
        flash_error = operator_action_error(
            "Import",
            "Unexpected error while importing the backup. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.backup_import.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            **error_fields,
            filename=original_name,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="import",
            error=flash_error,
            details={
                "progress": 100,
                "operation_role": "ui_backup_import",
                "message": "Import failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "filename": original_name,
                **error_fields,
            },
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _run_clone_operation(
    settings: Settings,
    operation_id: int | None,
    backend_id: int,
    name: str,
    port: str,
) -> None:
    operation = OperationHandle(
        id=operation_id, kind="clone_backend", settings=settings
    )
    try:
        last_progress = {"value": 0}
        initial_details = _operation_progress_details(
            "clone",
            0,
            "Preparing clone.",
            phase="Clone output",
            substate="Preparing clone",
            backend_id=backend_id,
        )
        last_progress["value"] = int(initial_details["progress"])
        await operation.update(
            status="running",
            phase="Clone output",
            details=initial_details,
        )
        async with background_db_session(settings) as session:
            loop = asyncio.get_running_loop()
            progress_updates = []

            def report_clone_progress(_progress: int, message: str) -> None:
                substate = _clone_progress_substate(message)
                details = _operation_progress_details(
                    "clone",
                    int(last_progress["value"]),
                    message,
                    phase="Clone output",
                    substate=substate,
                    backend_id=backend_id,
                )
                last_progress["value"] = int(details["progress"])
                progress_updates.append(
                    asyncio.run_coroutine_threadsafe(
                        operation.update(
                            status="running",
                            phase="Clone output",
                            details=details,
                        ),
                        loop,
                    )
                )

            payload = _backend_clone_payload_from_form(name=name, port=port)
            result = await backend_commands.clone_backend(
                session,
                backend_id,
                payload,
                settings,
                progress_callback=report_clone_progress,
            )
            for future in progress_updates:
                try:
                    await asyncio.wrap_future(future)
                except Exception as exc:
                    logger.warning("ui.clone.progress_update_failed", error=str(exc))
            cloned_backend = result["backend"]
            restored_paths = (
                result.get("restored_paths") if isinstance(result, dict) else []
            )
            copied_paths = (
                len(restored_paths) if isinstance(restored_paths, list) else 0
            )
            await operation.complete(
                "success",
                phase="completed",
                details={
                    "progress": 100,
                    "message": f"Clone created as {cloned_backend.name}.",
                    "flash_success": f"Clone created as {cloned_backend.name}.",
                    "backend_id": cloned_backend.id,
                    "backend_name": cloned_backend.name,
                    "backend_port": cloned_backend.port,
                    "copied_paths": copied_paths,
                    "redirect_url": f"/outputs/{cloned_backend.id}",
                },
            )
    except Exception as exc:
        error_fields = error_context(ErrorCode.OUTPUT_CLONE_FAILED)
        flash_error = operator_action_error(
            "Clone",
            "Unexpected error while cloning the output. Check server logs with this instance.",
            error_fields,
        )
        logger.exception(
            "ui.clone.worker_failed",
            backend_id=backend_id,
            operation_id=operation_id,
            **error_fields,
            clone_name=name,
            error=str(exc),
        )
        await operation.complete(
            "failed",
            phase="Clone output",
            error=flash_error,
            details={
                "progress": 100,
                "message": "Clone failed.",
                "flash_error": flash_error,
                "backend_id": backend_id,
                "clone_name": name,
                **error_fields,
            },
        )
