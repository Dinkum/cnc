from __future__ import annotations

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.logger import get_logger
from app.models.entities import Backend
from app.security import enforce_csrf
from app.services import backend_commands
from app.services.app_runtime import remove_deleted_app_filesystem_artifacts
from app.services.apply_service import (
    default_apply_services,
    run_apply,
)
from app.services.error_reporting import (
    ErrorCode,
    error_context,
)
from app.services.inter_app_interfaces import (
    INTERFACE_STATUS_ACCEPTED,
    INTERFACE_STATUS_REJECTED,
    clean_interface_direction,
    clean_interface_status,
)
from app.services.mutation_apply import commit_and_apply
from app.services.operation_runtime import (
    create_output_progress_plan,
    delete_output_progress_plan,
    progress_plan_value,
)
from app.services.operations import (
    active_host_mutation_blocker,
    create_operation,
)
from app.services.shield_service import make_code_hash
from app.services.ssh_access import remove_backend_ssh_access
from app.services.validators import ValidationError
from app.ui.dashboard.context import cached_dashboard_context
from app.ui.errors import operator_action_error as _operator_action_error
from app.ui.errors import operator_error_json as _operator_error_json
from app.ui.forms import (
    _backend_create_payload_from_form,
    _backend_inputs_payload_from_form,
    _backend_update_payload_from_form,
    _form_string_or_default,
    _nonblank_form_values,
    operator_validation_error,
)
from app.ui.http import (
    dashboard_redirect,
    dashboard_refresh_response,
    dashboard_tab_url,
    output_redirect,
    render_dashboard_template,
    render_output_template,
    request_prefers_json,
    ui_request_mode,
)
from app.ui.mutation_feedback import (
    backend_update_changed_keys,
    host_mutation_blocked_message,
    host_mutation_preflight_message,
)
from app.ui.operations.common import operation_response as _operation_response
from app.ui.operations.output_lifecycle import (
    _run_create_backend_operation,
    _run_delete_operation,
)
from app.ui.operations.output_save import (
    _run_update_backend_inputs_operation,
    _run_update_backend_interface_operation,
    _run_update_backend_operation,
    _run_update_backend_state_operation,
)
from app.ui.outputs.context import output_page_context
from app.ui.progress import (
    _output_save_progress_details,
    _planned_operation_progress_details,
)
from app.ui.view_models import _save_apply_feedback

logger = get_logger("ui")


router = APIRouter(tags=["ui"])


async def _output_host_mutation_preflight(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    backend_id: int,
    action: str,
) -> Response | None:
    message = await host_mutation_preflight_message(settings, action)
    if message is None:
        return None
    if request_prefers_json(request):
        return _operator_error_json(
            message,
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=409,
        )
    context = await output_page_context(session, settings, backend_id)
    context["request"] = request
    context["flash_error"] = message
    return render_output_template(request, settings, context, status_code=409)


@router.post("/ui/backends")
async def create_backend_form(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    name: str = Form(...),
    kind: str | None = Form(None),
    port: str = Form(""),
    static_root: str = Form(""),
    sandbox_profile: str = Form(""),
    sandbox_image: str = Form(""),
    handoff_port: int = Form(8000),
    healthcheck_mode: str = Form("none"),
    healthcheck_path: str = Form("/"),
    healthcheck_host_header: str = Form(""),
    resource_mode: str = Form("auto"),
    resource_size: str = Form("auto"),
    memory_high_override: str = Form(""),
    memory_max_override: str = Form(""),
    cpu_quota_override: str = Form(""),
    volumes_json: str = Form("[]"),
    no_new_privileges: bool = Form(False),
    drop_capabilities: bool = Form(False),
    notes: str = Form(""),
    enabled: bool = Form(False),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    form = await request.form()
    normalized_kind = (kind or "").strip().lower()
    logger.info(
        "ui.backend.create.requested",
        backend_name=name.strip().lower(),
        kind=normalized_kind,
        enabled=enabled,
        input_ids=_nonblank_form_values(form.getlist("input_ids")),
    )
    return_dashboard = request.headers.get("x-cnc-dashboard-refresh") == "1"
    try:
        blocker = await active_host_mutation_blocker(settings)
        if blocker is not None:
            flash_error = host_mutation_blocked_message("Output create", blocker)
            if return_dashboard:
                return await dashboard_refresh_response(
                    request,
                    session,
                    settings,
                    active_tab="outputs",
                    flash_error=flash_error,
                    status_code=409,
                )
            context = await cached_dashboard_context(
                session, settings, active_tab="outputs"
            )
            context["request"] = request
            context["active_tab"] = "outputs"
            context["flash_error"] = flash_error
            return render_dashboard_template(request, context, status_code=409)
        payload = _backend_create_payload_from_form(
            name=name,
            kind=kind,
            port=port,
            static_root=static_root,
            sandbox_profile=sandbox_profile,
            sandbox_image=sandbox_image,
            handoff_port=handoff_port,
            healthcheck_mode=healthcheck_mode,
            healthcheck_path=healthcheck_path,
            healthcheck_host_header=healthcheck_host_header,
            resource_mode=resource_mode,
            resource_size=resource_size,
            memory_high_override=memory_high_override,
            memory_max_override=memory_max_override,
            cpu_quota_override=cpu_quota_override,
            volumes_json=volumes_json,
            notes=notes,
            enabled=enabled,
            input_ids=form.getlist("input_ids"),
        )
        if return_dashboard:
            progress_plan = create_output_progress_plan(
                backend_kind=payload.kind,
                input_kinds=(),
            )
            operation = await create_operation(
                settings,
                kind="create_backend",
                actor="ui",
                phase="queued",
                details={
                    "progress": progress_plan_value(
                        progress_plan,
                        phase="Validate output",
                        message="Checking ports and health settings",
                        current=0,
                    ),
                    "message": "Output create queued.",
                    "substate": "Checking ports and health settings",
                    "progress_plan": progress_plan,
                    "backend_name": payload.name,
                    "backend_kind": payload.kind,
                },
            )
            background_tasks.add_task(
                _run_create_backend_operation, settings, operation.id, payload
            )
            return _operation_response(operation, message="Creating output.")
        backend = await backend_commands.create_backend(session, payload, commit=False)
        attached_input_ids = [item.id for item in backend.inputs]
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.create",
            apply_runner=run_apply,
        )
        apply_response = mutation.apply_response
        if apply_response.status != "success":
            logger.info(
                "ui.backend.create.apply_failed",
                backend_name=backend.name,
                mutation_state=mutation.state,
            )
            context = await cached_dashboard_context(
                session, settings, active_tab="outputs"
            )
            context["request"] = request
            context["active_tab"] = "outputs"
            success_flash, error_flash = _save_apply_feedback(
                apply_response,
                success_message="Output created.",
                failure_prefix="Output saved",
            )
            context["flash_success"] = success_flash
            context["flash_error"] = error_flash
            return render_dashboard_template(request, context, status_code=200)
        await session.refresh(backend)
        logger.info(
            "ui.backend.create.succeeded",
            backend_id=backend.id,
            backend_name=backend.name,
            kind=backend.kind,
            enabled=backend.enabled,
            port=backend.port,
            input_ids=attached_input_ids,
            mutation_state=mutation.state,
        )
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Output created.",
            failure_prefix="Output saved",
        )
        context = await output_page_context(session, settings, backend.id)
        context["request"] = request
        context["flash_success"] = success_flash
        context["flash_error"] = error_flash
        return render_output_template(
            request,
            settings,
            context,
            status_code=201 if apply_response.status == "success" else 200,
        )
    except ValidationError as exc:
        await session.rollback()
        logger.warning("ui.backend.create.validation_failed", error=str(exc))
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["active_tab"] = "outputs"
        context["flash_error"] = operator_validation_error(exc)
        return render_dashboard_template(request, context, status_code=400)
    except IntegrityError as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_BACKEND_CREATE_CONFLICT)
        logger.warning("ui.backend.create.conflict", **error_fields, error=str(exc))
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["active_tab"] = "outputs"
        context["flash_error"] = _operator_action_error(
            "Output create",
            "name already exists. Use a unique output name.",
            error_fields,
        )
        return render_dashboard_template(request, context, status_code=400)
    except Exception as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_BACKEND_CREATE_FAILED)
        logger.exception("ui.backend.create.failed", **error_fields, error=str(exc))
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["active_tab"] = "outputs"
        context["flash_error"] = _operator_action_error(
            "Output create",
            "Unexpected error while creating the output. Check server logs with this instance.",
            error_fields,
        )
        return render_dashboard_template(request, context, status_code=400)


@router.post("/ui/backends/{backend_id}")
async def update_backend_form(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    name: str = Form(...),
    kind: str = Form(...),
    port: str = Form(""),
    static_root: str = Form(""),
    sandbox_profile: str = Form(""),
    sandbox_image: str = Form(""),
    handoff_port: int = Form(8000),
    healthcheck_mode: str = Form("none"),
    healthcheck_path: str = Form("/"),
    healthcheck_host_header: str = Form(""),
    resource_mode: str = Form("auto"),
    resource_size: str = Form("auto"),
    memory_high_override: str = Form(""),
    memory_max_override: str = Form(""),
    cpu_quota_override: str = Form(""),
    volumes_json: str = Form("[]"),
    no_new_privileges: bool = Form(False),
    drop_capabilities: bool = Form(False),
    notes: str = Form(""),
    shield_enabled: bool = Form(False),
    shield_access_code: str = Form(""),
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
        if request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "backend not found"
        return render_dashboard_template(request, context, status_code=404)
    blocked_response = await _output_host_mutation_preflight(
        request,
        session,
        settings,
        backend_id=backend.id,
        action="Output save",
    )
    if blocked_response is not None:
        return blocked_response
    if request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="ui.backend.update",
            actor="ui",
            backend_id=backend.id,
            phase="Validate output",
            details=_output_save_progress_details(
                0,
                "Reading output form.",
                phase="Validate output",
                substate="Reading output form",
                backend_id=backend.id,
                backend_name=backend.name,
            ),
        )
        background_tasks = BackgroundTasks()
        background_tasks.add_task(
            _run_update_backend_operation,
            settings,
            operation.id,
            backend.id,
            {
                "name": name,
                "kind": kind,
                "port": port,
                "static_root": static_root,
                "sandbox_profile": sandbox_profile,
                "sandbox_image": sandbox_image,
                "handoff_port": handoff_port,
                "healthcheck_mode": healthcheck_mode,
                "healthcheck_path": healthcheck_path,
                "healthcheck_host_header": healthcheck_host_header,
                "resource_mode": resource_mode,
                "resource_size": resource_size,
                "memory_high_override": memory_high_override,
                "memory_max_override": memory_max_override,
                "cpu_quota_override": cpu_quota_override,
                "volumes_json": volumes_json,
                "no_new_privileges": no_new_privileges,
                "drop_capabilities": drop_capabilities,
                "notes": notes,
                "shield_enabled": shield_enabled,
                "shield_access_code": shield_access_code,
            },
        )
        response = _operation_response(operation, message="Output save started.")
        response.background = background_tasks
        return response
    current_enabled = bool(backend.enabled)
    try:
        shield_code_hash = backend.shield_code_hash
        shield_access_code_display = str(
            getattr(backend, "shield_access_code", "") or ""
        ).strip()
        normalized_shield_access_code = str(shield_access_code or "").strip()
        if normalized_shield_access_code:
            shield_code_hash = make_code_hash(
                settings.shield_secret_key, normalized_shield_access_code
            )
            shield_access_code_display = normalized_shield_access_code
        if backend.kind == "app" and shield_enabled and not shield_code_hash:
            raise ValidationError(
                "shield requires an access code before it can be enabled"
            )
        payload = _backend_update_payload_from_form(
            name=name,
            kind=backend.kind,
            port=port,
            static_root=static_root,
            sandbox_profile=sandbox_profile,
            sandbox_image=sandbox_image,
            handoff_port=handoff_port,
            healthcheck_mode=healthcheck_mode,
            healthcheck_path=healthcheck_path,
            healthcheck_host_header=healthcheck_host_header,
            resource_mode=resource_mode,
            resource_size=resource_size,
            current_resource_size=backend.resource_size,
            memory_high_override=memory_high_override,
            memory_max_override=memory_max_override,
            cpu_quota_override=cpu_quota_override,
            volumes_json=volumes_json,
            notes=notes,
            shield_enabled=shield_enabled if backend.kind == "app" else False,
            shield_code_hash=shield_code_hash,
            shield_access_code=shield_access_code_display,
        )
        changed_keys = backend_update_changed_keys(backend, payload)
        backend = await backend_commands.update_backend(
            session, backend_id, payload, commit=False
        )
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.update",
            apply_runner=run_apply,
        )
        apply_response = mutation.apply_response
        mutation_state = mutation.state
        logger.info(
            "ui.backend.update.succeeded",
            backend_id=backend_id,
            enabled=current_enabled,
            mutation_state=mutation_state,
            changed_keys=sorted(changed_keys),
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Output saved.",
            failure_prefix="Output saved",
        )
        context["flash_success"] = success_flash
        context["flash_error"] = error_flash
        return render_output_template(request, settings, context, status_code=200)
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "ui.backend.update.validation_failed", backend_id=backend_id, error=str(exc)
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = operator_validation_error(exc)
        return render_output_template(request, settings, context, status_code=400)
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.update.failed", backend_id=backend_id, error=str(exc)
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"failed to update output: {exc}"
        return render_output_template(request, settings, context, status_code=400)


@router.post("/ui/backends/{backend_id}/inputs")
async def update_backend_inputs_form(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    form = await request.form()
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        if request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return render_dashboard_template(request, context, status_code=404)
    blocked_response = await _output_host_mutation_preflight(
        request,
        session,
        settings,
        backend_id=backend.id,
        action="Attached inputs save",
    )
    if blocked_response is not None:
        return blocked_response
    if request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="ui.backend.inputs",
            actor="ui",
            backend_id=backend.id,
            phase="Validate output",
            details=_output_save_progress_details(
                0,
                "Reading output form.",
                phase="Validate output",
                substate="Reading output form",
                backend_id=backend.id,
                backend_name=backend.name,
            ),
        )
        background_tasks = BackgroundTasks()
        background_tasks.add_task(
            _run_update_backend_inputs_operation,
            settings,
            operation.id,
            backend.id,
            [str(item) for item in form.getlist("input_ids")],
        )
        response = _operation_response(
            operation, message="Attached inputs save started."
        )
        response.background = background_tasks
        return response
    try:
        payload = _backend_inputs_payload_from_form(input_ids=form.getlist("input_ids"))
        backend = await backend_commands.set_backend_inputs(
            session,
            backend_id,
            payload.input_ids or [],
            commit=False,
        )
        attached_input_ids = [item.id for item in backend.inputs]
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.inputs",
            apply_runner=run_apply,
        )
        logger.info(
            "ui.backend.inputs.succeeded",
            backend_id=backend_id,
            input_ids=attached_input_ids,
            mutation_state=mutation.state,
        )
        apply_response = mutation.apply_response
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Attached inputs saved.",
            failure_prefix="Attached inputs saved",
        )
        context["flash_success"] = success_flash
        context["flash_error"] = error_flash
        return render_output_template(request, settings, context, status_code=200)
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "ui.backend.inputs.validation_failed", backend_id=backend_id, error=str(exc)
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = operator_validation_error(exc)
        return render_output_template(request, settings, context, status_code=400)
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.inputs.failed", backend_id=backend_id, error=str(exc)
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"failed to save attached inputs: {exc}"
        return render_output_template(request, settings, context, status_code=400)


@router.post("/ui/backends/{backend_id}/interfaces/inbound")
async def update_backend_inbound_interface_form(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    source_backend_id: int = Form(...),
    interface_name: str = Form(...),
    status: str = Form(...),
    direction: str = Form(""),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    if not settings.multi_node_enabled:
        return _operator_error_json(
            "multi node mode is disabled",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=400,
        )

    blocker = await active_host_mutation_blocker(settings)
    if blocker is not None:
        return _operator_error_json(
            host_mutation_blocked_message("Interface action", blocker),
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=409,
        )

    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            key="flash_error",
            status_code=404,
        )
    if backend.kind != "app":
        return _operator_error_json(
            "interfaces are only available for app outputs",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=400,
        )

    next_status = clean_interface_status(status)
    next_direction = clean_interface_direction(direction)
    if next_status not in {INTERFACE_STATUS_ACCEPTED, INTERFACE_STATUS_REJECTED}:
        return _operator_error_json(
            "interface action is invalid",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=400,
        )

    try:
        operation = await create_operation(
            settings,
            kind="ui.backend.interface",
            actor="ui",
            backend_id=backend.id,
            phase="Validate output",
            details=_output_save_progress_details(
                0,
                "Validating interface action.",
                phase="Validate output",
                substate="Validating interface action",
                backend_id=backend.id,
                backend_name=backend.name,
                source_backend_id=source_backend_id,
                interface_name=interface_name,
                status=next_status,
                direction=next_direction,
            ),
        )
        background_tasks.add_task(
            _run_update_backend_interface_operation,
            settings,
            operation.id,
            backend.id,
            source_backend_id=source_backend_id,
            interface_name=interface_name,
            status=next_status,
            direction=next_direction,
        )
        return _operation_response(operation, message="Interface action started.")
    except Exception as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_BACKEND_INTERFACE_FAILED)
        logger.exception(
            "ui.backend.interface.failed",
            backend_id=backend_id,
            source_backend_id=source_backend_id,
            interface_name=interface_name,
            **error_fields,
            error=str(exc),
        )
        message = _operator_action_error(
            "Interface action",
            "Unexpected error while updating the interface. Check server logs with this instance.",
            error_fields,
        )
        return JSONResponse({"flash_error": message, **error_fields}, status_code=500)


@router.post("/ui/backends/{backend_id}/state")
async def update_backend_state_form(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    action: str = Form(""),
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
        if request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        context = await cached_dashboard_context(
            session, settings, active_tab="outputs"
        )
        context["request"] = request
        context["flash_error"] = "output not found"
        return render_dashboard_template(request, context, status_code=404)
    blocked_response = await _output_host_mutation_preflight(
        request,
        session,
        settings,
        backend_id=backend.id,
        action="Output state change",
    )
    if blocked_response is not None:
        return blocked_response
    normalized_action = _form_string_or_default(action, "").strip().lower()
    if normalized_action not in {"enable", "disable"}:
        logger.warning(
            "ui.backend.state.invalid_action",
            backend_id=backend_id,
            action=normalized_action or "-",
            request_mode=ui_request_mode(request),
        )
        if request_prefers_json(request):
            return _operator_error_json(
                "invalid output action",
                ErrorCode.UI_ACTION_UNAVAILABLE,
                key="flash_error",
                status_code=400,
            )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = "invalid output action"
        return render_output_template(request, settings, context, status_code=400)
    target_enabled = normalized_action == "enable"
    logger.info(
        "ui.backend.state.requested",
        backend_id=backend_id,
        backend_name=backend.name,
        action=normalized_action,
        target_enabled=target_enabled,
        current_enabled=backend.enabled,
        request_mode=ui_request_mode(request),
    )
    if request_prefers_json(request):
        operation = await create_operation(
            settings,
            kind="ui.backend.state",
            actor="ui",
            backend_id=backend.id,
            phase="Validate output",
            details=_output_save_progress_details(
                0,
                "Reading output form.",
                phase="Validate output",
                substate="Reading output form",
                backend_id=backend.id,
                backend_name=backend.name,
                enabled=target_enabled,
            ),
        )
        background_tasks = BackgroundTasks()
        background_tasks.add_task(
            _run_update_backend_state_operation,
            settings,
            operation.id,
            backend.id,
            target_enabled,
        )
        response = _operation_response(operation, message="Output state save started.")
        response.background = background_tasks
        return response
    try:
        backend = await backend_commands.set_backend_enabled(
            session, backend_id, target_enabled, commit=False
        )
        backend_name = str(backend.name or "")
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.state",
            apply_runner=run_apply,
        )
        logger.info(
            "ui.backend.state.succeeded",
            backend_id=backend_id,
            backend_name=backend_name,
            action=normalized_action,
            enabled=target_enabled,
            mutation_state=mutation.state,
            apply_status=mutation.apply_response.status,
            run_id=mutation.apply_response.run_id,
            request_mode=ui_request_mode(request),
        )
        apply_response = mutation.apply_response
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Output enabled." if target_enabled else "Output disabled.",
            failure_prefix="Output enabled" if target_enabled else "Output disabled",
        )
        context["flash_success"] = success_flash
        context["flash_error"] = error_flash
        return render_output_template(request, settings, context, status_code=200)
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "ui.backend.state.validation_failed",
            backend_id=backend_id,
            action=normalized_action,
            target_enabled=target_enabled,
            request_mode=ui_request_mode(request),
            error=str(exc),
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = operator_validation_error(exc)
        return render_output_template(request, settings, context, status_code=400)
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.state.failed",
            backend_id=backend_id,
            action=normalized_action,
            target_enabled=target_enabled,
            request_mode=ui_request_mode(request),
            error=str(exc),
        )
        context = await output_page_context(session, settings, backend_id)
        context["request"] = request
        context["flash_error"] = f"failed to update output state: {exc}"
        return render_output_template(request, settings, context, status_code=400)


@router.post("/ui/backends/{backend_id}/delete")
async def delete_backend_form(
    backend_id: int,
    request: Request,
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
        logger.warning(
            "ui.backend.delete.missing",
            backend_id=backend_id,
            request_mode=ui_request_mode(request),
        )
        if request_prefers_json(request):
            return _operator_error_json(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        return dashboard_redirect(
            request, active_tab="outputs", flash_error="output not found"
        )

    blocked_response = await _output_host_mutation_preflight(
        request,
        session,
        settings,
        backend_id=backend.id,
        action="Output delete",
    )
    if blocked_response is not None:
        return blocked_response

    logger.info(
        "ui.backend.delete.requested",
        backend_id=backend.id,
        backend_name=backend.name,
        enabled=backend.enabled,
        input_count=len(backend.inputs),
        request_mode=ui_request_mode(request),
    )
    if request_prefers_json(request):
        progress_plan = delete_output_progress_plan(backend_kind=backend.kind)
        operation = await create_operation(
            settings,
            kind="delete_backend",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details=_planned_operation_progress_details(
                progress_plan,
                0,
                "Delete queued.",
                phase="Delete output",
                substate="Opening delete operation",
                redirect_url=dashboard_tab_url(request, "outputs", defer_status=True),
            ),
        )
        logger.info(
            "ui.backend.delete.operation_created",
            backend_id=backend.id,
            backend_name=backend.name,
            operation_id=operation.id,
            request_mode="async",
        )
        background_tasks = BackgroundTasks()
        background_tasks.add_task(
            _run_delete_operation, settings, operation.id, backend.id
        )
        response = _operation_response(operation, message="Delete started.")
        response.background = background_tasks
        return response

    deleted_backend_name = str(backend.name or "").strip()
    try:
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
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.backend.delete",
            apply_runner=run_apply,
            apply_services=services,
        )
        logger.info(
            "ui.backend.delete.deleted",
            backend_id=backend_id,
            backend_name=deleted_backend_name,
            mutation_state=mutation.state,
            apply_status=mutation.apply_response.status,
            run_id=mutation.apply_response.run_id,
            request_mode=ui_request_mode(request),
        )
        apply_response = mutation.apply_response
        filesystem_cleanup_error: str | None = None
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
            try:
                await remove_backend_ssh_access([deleted_backend_name], settings)
            except Exception as exc:
                logger.warning(
                    "ui.backend.delete.ssh_cleanup_failed",
                    backend_id=backend_id,
                    backend=deleted_backend_name,
                    error=str(exc),
                )
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Output deleted.",
            failure_prefix="Output deleted",
        )
        if filesystem_cleanup_error:
            success_flash = None
            error_flash = _operator_action_error(
                "Output delete",
                "the output was deleted, but its persistent filesystem cleanup did not finish. The retained files are recoverable; check server logs and retry cleanup.",
                error_context(ErrorCode.OUTPUT_DELETE_FAILED),
            )
        return dashboard_redirect(
            request,
            active_tab="outputs",
            flash_success=success_flash,
            flash_error=error_flash,
        )
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.delete.failed",
            backend_id=backend_id,
            request_mode=ui_request_mode(request),
            error=str(exc),
        )
        return output_redirect(
            request,
            backend_id=backend_id,
            flash_error=f"failed to delete output: {exc}",
        )
