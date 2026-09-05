from __future__ import annotations

import asyncio
import shutil
import tempfile

from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.models.entities import Backend
from app.security import enforce_csrf
from app.services import backend_commands
from app.services.apply_service import run_apply
from app.services.backend_backup_service import (
    create_backend_backup,
    delete_backend_backup,
    get_backend_backup,
    import_backend_backup_bundle,
    restore_backend_backup,
    restore_latest_backend_backup,
)
from app.services.error_reporting import ErrorCode
from app.services.inter_app_interfaces import (
    ConcurrentInterfaceUpdateError,
    apply_inbound_inter_app_interface_statuses,
    apply_inter_app_interfaces,
    clean_interface_direction,
    clean_interface_status,
    inter_app_interfaces_from_form,
    read_inter_app_interfaces,
    reconcile_inter_app_interface_statuses,
    validate_inter_app_interfaces,
)
from app.services.mutation_apply import commit_and_apply
from app.services.operations import (
    active_host_mutation_blocker,
    create_operation,
)
from app.services.placement_config import (
    PLACEMENT_MODE_FAILOVER,
    apply_backend_placement,
    clean_node_uid,
    read_backend_placement,
    unique_node_uids,
    validate_backend_placement_nodes,
)
from app.services.replica_readiness import validate_replica_readiness_for_nodes
from app.services.status_service import invalidate_status_cache
from app.services.validators import ValidationError
from app.ui.errors import (
    operator_coded_error as _operator_coded_error,
    operator_coded_message_payload as _operator_coded_message_payload,
    operator_error_json as _operator_error_json,
)
from app.ui.forms import _backend_clone_payload_from_form
from app.ui.operations.backups import (
    _run_backup_operation,
    _run_clone_operation,
    _run_delete_backup_operation,
    _run_import_backup_operation,
    _run_restore_operation,
)
from app.ui.operations.cluster import (
    _run_replica_setup_operation,
    _run_transfer_operation,
)
from app.ui.operations.common import operation_response as _operation_response
from app.ui.progress import _operation_progress_details
from app.ui.routes.shared import (
    _cached_dashboard_context,
    _copy_upload_file_with_limit,
    _form_truthy,
    _host_mutation_blocked_message,
    _host_mutation_preflight_message,
    _json_save_feedback,
    _operator_validation_error,
    _output_page_context,
    _render_dashboard_template,
    _render_output_template,
    _request_prefers_json,
    logger,
)
from app.ui.view_models import (
    _format_bytes,
    _save_apply_feedback,
)
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
)
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


router = APIRouter(tags=["ui"])


async def _backup_mutation_preflight(
    settings: Settings, *, action: str
) -> JSONResponse | None:
    message = await _host_mutation_preflight_message(settings, action=action)
    if message is None:
        return None
    return _operator_error_json(
        message,
        ErrorCode.UI_ACTION_UNAVAILABLE,
        key="flash_error",
        status_code=409,
    )


@router.post("/ui/backends/{backend_id}/backup")
async def backup_backend_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return _render_dashboard_template(request, context, status_code=404)
    blocked = await _backup_mutation_preflight(settings, action="Backup")
    if blocked is not None:
        return blocked
    if _request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="backup_backend",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details=_operation_progress_details(
                "backup",
                0,
                "Backup queued.",
                phase="Backup",
                operation_role="ui_backup",
                backend_id=backend.id,
            ),
        )
        background_tasks.add_task(
            _run_backup_operation, settings, operation.id, backend.id
        )
        response = _operation_response(operation, message="Backup started.")
        return response
    backup = await create_backend_backup(session, backend, settings)
    context = await _output_page_context(session, settings, backend_id)
    context["request"] = request
    if backup.status == "success":
        context["flash_success"] = (
            f"Backup created: {backup.scope} ({_format_bytes(backup.size_bytes)})."
        )
        return _render_output_template(request, settings, context, status_code=200)
    context["flash_error"] = f"Backup failed: {backup.error or 'unknown error'}"
    return _render_output_template(request, settings, context, status_code=500)


@router.post("/ui/backends/{backend_id}/restore")
async def restore_backend_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    backup_id: int | None = Form(None),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    resolved_backup_id = backup_id if isinstance(backup_id, int) else None
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return _render_dashboard_template(request, context, status_code=404)
    blocked = await _backup_mutation_preflight(settings, action="Restore")
    if blocked is not None:
        return blocked
    if _request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="restore_backend",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details=_operation_progress_details(
                "restore",
                0,
                "Restore queued.",
                phase="Restore backup",
                backup_id=resolved_backup_id,
            ),
        )
        background_tasks.add_task(
            _run_restore_operation,
            settings,
            operation.id,
            backend.id,
            resolved_backup_id,
        )
        response = _operation_response(operation, message="Restore started.")
        return response
    try:
        if resolved_backup_id is None:
            restore_result = await restore_latest_backend_backup(
                session, backend, settings
            )
            restored_backup = restore_result.get("backup")
        else:
            selected_backup = await get_backend_backup(
                session, backend.id, resolved_backup_id
            )
            if selected_backup is None:
                raise LookupError("backup not found for this output")
            restore_result = await restore_backend_backup(
                session, backend, selected_backup, settings
            )
            restored_backup = selected_backup
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        restored_paths = restore_result.get("restored_paths") or []
        backup_label = (
            f"backup #{restored_backup.id}"
            if restored_backup is not None
            and getattr(restored_backup, "id", None) is not None
            else "latest backup"
        )
        if restored_paths:
            context["flash_success"] = (
                f"Restore completed from {backup_label}. Restored {len(restored_paths)} mounted path(s)."
            )
        else:
            context["flash_success"] = f"Restore completed from {backup_label}."
        return _render_output_template(request, settings, context, status_code=200)
    except LookupError as exc:
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = _operator_coded_error(
            str(exc), ErrorCode.UI_RESOURCE_NOT_FOUND
        )
        return _render_output_template(request, settings, context, status_code=404)
    except Exception as exc:
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"Restore failed: {exc}"
        return _render_output_template(request, settings, context, status_code=400)


@router.post("/ui/backends/{backend_id}/backups/{backup_id}/delete")
async def delete_backup_form(
    backend_id: int,
    backup_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return _render_dashboard_template(request, context, status_code=404)

    backup = await get_backend_backup(session, backend.id, backup_id)
    if backup is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "backup not found for this output",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = "backup not found for this output"
        return _render_output_template(request, settings, context, status_code=404)

    blocked = await _backup_mutation_preflight(settings, action="Backup delete")
    if blocked is not None:
        return blocked

    if _request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="delete_backend_backup",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details=_operation_progress_details(
                "deleteBackup",
                0,
                f"Backup #{backup_id} delete queued.",
                phase="Delete backup",
                backup_id=backup_id,
            ),
        )
        background_tasks.add_task(
            _run_delete_backup_operation, settings, operation.id, backend.id, backup_id
        )
        return _operation_response(operation, message="Backup delete started.")

    try:
        await delete_backend_backup(session, backend.id, backup_id, settings)
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_success"] = f"Backup #{backup_id} deleted."
        return _render_output_template(request, settings, context, status_code=200)
    except Exception as exc:
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"Backup delete failed: {exc}"
        return _render_output_template(request, settings, context, status_code=400)


@router.post("/ui/backends/{backend_id}/import")
async def import_restore_backend_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    bundle: UploadFile = File(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return _render_dashboard_template(request, context, status_code=404)

    if not bundle.filename:
        if _request_prefers_json(request):
            return _operator_error_json(
                "backup file is required",
                ErrorCode.VALIDATION_FAILED,
                key="flash_error",
                status_code=400,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = "backup file is required"
        return _render_output_template(request, settings, context, status_code=400)

    blocked = await _backup_mutation_preflight(settings, action="Backup import")
    if blocked is not None:
        return blocked

    temp_dir = Path(tempfile.mkdtemp(prefix="cnc-import-backup-"))
    temp_path = temp_dir / Path(bundle.filename).name
    try:
        await asyncio.to_thread(
            _copy_upload_file_with_limit,
            bundle.file,
            temp_path,
            max_bytes=settings.backend_backup_upload_max_bytes,
        )
        if _request_prefers_json(request):
            operation = await create_operation(
                settings,
                kind="import_backend_backup",
                actor="ui",
                backend_id=backend.id,
                phase="queued",
                details=_operation_progress_details(
                    "importBackup",
                    0,
                    "Import queued.",
                    phase="Import backup",
                    operation_role="ui_backup_import",
                    filename=bundle.filename,
                ),
            )
            background_tasks.add_task(
                _run_import_backup_operation,
                settings,
                operation.id,
                backend.id,
                str(temp_path),
                bundle.filename,
                str(temp_dir),
            )
            response = _operation_response(operation, message="Import started.")
            return response
        imported_backup = await import_backend_backup_bundle(
            session,
            backend,
            temp_path,
            original_name=bundle.filename,
            settings=settings,
        )
        if imported_backup.status != "success":
            raise RuntimeError(imported_backup.error or "backup import failed")
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_success"] = f"Backup #{imported_backup.id} imported."
        return _render_output_template(request, settings, context, status_code=200)
    except Exception as exc:
        status_code = 413 if isinstance(exc, ValueError) else 400
        if _request_prefers_json(request):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return _operator_error_json(
                f"Import failed: {exc}",
                ErrorCode.BACKUP_IMPORT_FAILED,
                key="flash_error",
                status_code=status_code,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"Import failed: {exc}"
        return _render_output_template(
            request, settings, context, status_code=status_code
        )
    finally:
        try:
            bundle.file.close()
        except Exception:
            pass
        if not _request_prefers_json(request):
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/ui/backends/{backend_id}/clone")
async def clone_backend_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    name: str = Form(""),
    port: str = Form(""),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )

    blocked = await _backup_mutation_preflight(settings, action="Output clone")
    if blocked is not None:
        return blocked

    try:
        payload = _backend_clone_payload_from_form(name=name, port=port)
        if _request_prefers_json(request):
            operation = await create_operation(
                settings,
                kind="clone_backend",
                actor="ui",
                backend_id=backend.id,
                phase="queued",
                details=_operation_progress_details(
                    "clone",
                    0,
                    "Clone queued.",
                    phase="Clone output",
                    clone_name=payload.name,
                ),
            )
            background_tasks.add_task(
                _run_clone_operation, settings, operation.id, backend.id, name, port
            )
            response = _operation_response(operation, message="Clone started.")
            return response
        result = await backend_commands.clone_backend(
            session, backend.id, payload, settings
        )
        invalidate_status_cache(settings, prefill=True)
        cloned_backend = result["backend"]
        restored_paths = (
            result.get("restored_paths") if isinstance(result, dict) else []
        )
        copied_paths = len(restored_paths) if isinstance(restored_paths, list) else 0
        return JSONResponse(
            {
                "backend_id": cloned_backend.id,
                "backend_name": cloned_backend.name,
                "backend_port": cloned_backend.port,
                "copied_paths": copied_paths,
                "message": (
                    f"Clone created as {cloned_backend.name}. "
                    f"Copied {copied_paths} mounted path(s). It starts disabled and has no attached inputs."
                ),
                "redirect_url": f"/outputs/{cloned_backend.id}",
            },
            status_code=201,
        )
    except ValidationError as exc:
        await session.rollback()
        return JSONResponse(
            _operator_coded_message_payload(
                _operator_validation_error(exc),
                key="error",
                error_code=ErrorCode.VALIDATION_FAILED,
            ),
            status_code=400,
        )
    except Exception as exc:
        await session.rollback()
        return _operator_error_json(
            f"failed to clone output: {exc}",
            ErrorCode.OUTPUT_CLONE_FAILED,
            status_code=400,
        )


@router.post("/ui/backends/{backend_id}/transfer")
async def transfer_backend_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    target_node: str = Form(""),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    if not settings.multi_node_enabled:
        return _operator_error_json(
            "multi node mode is disabled",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )
    if backend.kind != "app":
        return _operator_error_json(
            "only app outputs can be transferred",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    blocker = await active_host_mutation_blocker(settings)
    if blocker is not None:
        return JSONResponse(
            {"flash_error": _host_mutation_blocked_message("Transfer", blocker)},
            status_code=409,
        )
    operation = await create_operation(
        settings,
        kind="transfer_backend",
        actor="ui",
        backend_id=backend.id,
        phase="queued",
        details=_operation_progress_details(
            "transferOutput",
            0,
            "Transfer queued.",
            phase="Confirm transfer",
            substate="Reading transfer request",
            target_node=target_node,
        ),
    )
    background_tasks.add_task(
        _run_transfer_operation,
        settings,
        operation.id,
        backend.id,
        target_node,
    )
    return _operation_response(operation, message="Transfer started.")


@router.post("/ui/backends/{backend_id}/replica-setup")
async def setup_backend_replica_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    target_node: str = Form(""),
    setup_mode: str = Form("clone"),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    if not settings.multi_node_enabled:
        return _operator_error_json(
            "multi node mode is disabled",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )
    if backend.kind != "app":
        return _operator_error_json(
            "multi-node setup is only available for app outputs",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    normalized_mode = str(setup_mode or "").strip().lower()
    if normalized_mode not in {"clone", "fresh"}:
        return _operator_error_json(
            "setup mode must be clone or fresh",
            ErrorCode.VALIDATION_FAILED,
            status_code=400,
        )
    blocker = await active_host_mutation_blocker(settings)
    if blocker is not None:
        return JSONResponse(
            {"flash_error": _host_mutation_blocked_message("Replica setup", blocker)},
            status_code=409,
        )
    operation = await create_operation(
        settings,
        kind="setup_backend_replica",
        actor="ui",
        backend_id=backend.id,
        phase="queued",
        details=_operation_progress_details(
            "replicaSetup",
            0,
            "Replica setup queued.",
            phase="Setup request",
            substate="Reading setup request",
            target_node=target_node,
            setup_mode=normalized_mode,
        ),
    )
    background_tasks.add_task(
        _run_replica_setup_operation,
        settings,
        operation.id,
        backend.id,
        target_node,
        normalized_mode,
    )
    return _operation_response(operation, message="Replica setup started.")


@router.post("/ui/backends/{backend_id}/placement")
async def update_backend_placement_form(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    prefers_json = _request_prefers_json(request)
    if not settings.multi_node_enabled:
        if prefers_json:
            return _operator_error_json(
                "multi node mode is disabled",
                ErrorCode.UI_ACTION_UNAVAILABLE,
                key="flash_error",
                status_code=400,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = "multi node mode is disabled"
        return _render_output_template(request, settings, context, status_code=400)

    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        if prefers_json:
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await _cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return _render_dashboard_template(request, context, status_code=404)
    if backend.kind != "app":
        if prefers_json:
            return _operator_error_json(
                "multi-node placement is only available for app outputs",
                ErrorCode.UI_ACTION_UNAVAILABLE,
                key="flash_error",
                status_code=400,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = (
            "multi-node placement is only available for app outputs"
        )
        return _render_output_template(request, settings, context, status_code=400)

    blocker = await active_host_mutation_blocker(settings)
    if blocker is not None:
        flash_error = _host_mutation_blocked_message("Multi-node placement", blocker)
        if prefers_json:
            return JSONResponse({"flash_error": flash_error}, status_code=409)
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = flash_error
        return _render_output_template(request, settings, context, status_code=409)

    form = await request.form()
    enabled = _form_truthy(form.get("placement_enabled"))
    mode = str(form.get("placement_mode") or PLACEMENT_MODE_FAILOVER).strip().lower()
    current_placement = read_backend_placement(backend)
    active_node_uid = clean_node_uid(
        form.get("placement_active_node") or current_placement.active_node_uid
    )
    selected_node_uids = unique_node_uids(
        [str(value) for value in form.getlist("placement_node_uids")]
    )
    interface_names = [
        str(value) for value in form.getlist("inter_app_interface_names")
    ]
    interface_targets = [
        str(value) for value in form.getlist("inter_app_interface_targets")
    ]
    interface_directions = [
        str(value) for value in form.getlist("inter_app_interface_directions")
    ]
    inbound_interface_source_ids = [
        str(value) for value in form.getlist("inter_app_interface_inbound_source_ids")
    ]
    inbound_interface_names = [
        str(value) for value in form.getlist("inter_app_interface_inbound_names")
    ]
    inbound_interface_statuses = [
        str(value) for value in form.getlist("inter_app_interface_inbound_statuses")
    ]
    if not enabled:
        selected_node_uids = ()

    try:
        submitted_inter_app_interfaces = (
            inter_app_interfaces_from_form(
                interface_names,
                interface_targets,
                interface_directions,
            )
            if enabled
            else ()
        )
        inter_app_interfaces = reconcile_inter_app_interface_statuses(
            read_inter_app_interfaces(backend),
            submitted_inter_app_interfaces,
        )
        await validate_backend_placement_nodes(
            session,
            selected_node_uids=selected_node_uids if enabled else (active_node_uid,),
            active_node_uid=active_node_uid,
        )
        await validate_replica_readiness_for_nodes(
            session,
            settings,
            backend=backend,
            node_uids=selected_node_uids if enabled else (active_node_uid,),
        )
        await validate_inter_app_interfaces(
            session,
            backend_id=backend.id,
            interfaces=inter_app_interfaces,
        )
        apply_backend_placement(
            backend,
            enabled=enabled,
            mode=mode,
            active_node_uid=active_node_uid,
            selected_node_uids=selected_node_uids,
        )
        apply_inter_app_interfaces(backend, inter_app_interfaces)
        if enabled:
            await apply_inbound_inter_app_interface_statuses(
                session,
                target_backend_id=backend.id,
                source_backend_ids=inbound_interface_source_ids,
                names=inbound_interface_names,
                statuses=inbound_interface_statuses,
            )
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.placement",
            apply_runner=run_apply,
        )
        apply_response = mutation.apply_response
        await session.refresh(backend)
        placement = read_backend_placement(backend)
        if prefers_json:
            return _json_save_feedback(
                apply_response,
                success_message="Multi-node saved.",
                failure_prefix="Multi-node saved",
                placement={
                    "enabled": placement.enabled,
                    "mode": placement.mode,
                    "active_node_uid": placement.active_node_uid,
                    "selected_node_uids": list(placement.selected_node_uids),
                    "inter_app_interfaces": [
                        {
                            "name": item.name,
                            "target_backend_id": item.target_backend_id,
                            "status": clean_interface_status(item.status),
                            "direction": clean_interface_direction(item.direction),
                        }
                        for item in read_inter_app_interfaces(backend)
                    ],
                },
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Multi-node saved.",
            failure_prefix="Multi-node saved",
        )
        context["flash_success"] = success_flash
        context["flash_error"] = error_flash
        return _render_output_template(request, settings, context, status_code=200)
    except ConcurrentInterfaceUpdateError as exc:
        await session.rollback()
        message = str(exc)
        if prefers_json:
            return _operator_error_json(
                message,
                ErrorCode.UI_ACTION_UNAVAILABLE,
                key="flash_error",
                status_code=409,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = message
        return _render_output_template(request, settings, context, status_code=409)
    except ValidationError as exc:
        await session.rollback()
        message = _operator_validation_error(exc)
        if prefers_json:
            return JSONResponse(
                _operator_coded_message_payload(
                    message,
                    key="flash_error",
                    error_code=ErrorCode.VALIDATION_FAILED,
                ),
                status_code=400,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = message
        return _render_output_template(request, settings, context, status_code=400)
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.placement.failed",
            backend_id=backend_id,
            error=str(exc),
        )
        message = f"failed to save multi-node placement: {exc}"
        if prefers_json:
            return _operator_error_json(
                message,
                ErrorCode.UI_BACKEND_UPDATE_FAILED,
                key="flash_error",
                status_code=400,
            )
        context = await _output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = message
        return _render_output_template(request, settings, context, status_code=400)
