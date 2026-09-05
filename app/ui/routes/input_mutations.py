from __future__ import annotations

from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.models.entities import Input
from app.security import enforce_csrf
from app.services import input_commands
from app.services.apply_service import run_apply
from app.services.error_reporting import (
    ErrorCode,
    error_context,
)
from app.services.mutation_apply import commit_and_apply
from app.services.operation_runtime import delete_input_progress_plan
from app.services.operations import (
    active_host_mutation_blocker,
    create_operation,
)
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.ui.errors import (
    operator_action_error as _operator_action_error,
    operator_coded_message_payload as _operator_coded_message_payload,
    operator_error_json as _operator_error_json,
)
from app.ui.forms import (
    _form_string_or_default,
    _input_create_payload_from_form,
    _input_update_payload_from_form,
    _nonblank_form_values,
)
from app.ui.operations.common import operation_response as _operation_response
from app.ui.operations.inputs import (
    _run_create_input_operation,
    _run_delete_input_operation,
    _run_update_input_operation,
)
from app.ui.progress import (
    _input_progress_details,
    _planned_operation_progress_details,
)
from app.ui.routes.shared import (
    _dashboard_redirect,
    _dashboard_refresh_response,
    _host_mutation_blocked_message,
    _host_mutation_preflight_message,
    _operator_validation_error,
    _request_prefers_json,
    logger,
)
from app.ui.view_models import _save_apply_feedback
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
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


router = APIRouter(tags=["ui"])


async def _input_mutation_preflight(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    action: str,
) -> HTMLResponse | JSONResponse | None:
    message = await _host_mutation_preflight_message(settings, action=action)
    if message is None:
        return None
    if _request_prefers_json(request):
        return _operator_error_json(
            message,
            ErrorCode.UI_ACTION_UNAVAILABLE,
            key="flash_error",
            status_code=409,
        )
    return await _dashboard_refresh_response(
        request,
        session,
        settings,
        active_tab="inputs",
        flash_error=message,
        status_code=409,
    )


@router.post("/ui/inputs")
async def create_input_form(
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    kind: str = Form("domain"),
    value: str = Form(...),
    enabled: bool = Form(False),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    form = await request.form()
    logger.info(
        "ui.input.create.requested",
        kind=kind,
        value=value,
        backend_ids=_nonblank_form_values(form.getlist("backend_ids")),
        enabled=enabled,
    )
    return_dashboard = request.headers.get("x-cnc-dashboard-refresh") == "1"
    async_dashboard = return_dashboard and _request_prefers_json(request)
    try:
        blocker = await active_host_mutation_blocker(settings)
        if blocker is not None:
            flash_error = _host_mutation_blocked_message("Input create", blocker)
            if async_dashboard:
                return JSONResponse(
                    _operator_coded_message_payload(flash_error, key="flash_error"),
                    status_code=409,
                )
            if return_dashboard:
                return await _dashboard_refresh_response(
                    request,
                    session,
                    settings,
                    active_tab="inputs",
                    flash_error=flash_error,
                    status_code=409,
                )
            return _dashboard_redirect(
                request, active_tab="inputs", flash_error=flash_error
            )
        payload = _input_create_payload_from_form(
            kind=kind,
            value=value,
            backend_ids=form.getlist("backend_ids"),
            enabled=enabled,
        )
        if async_dashboard:
            operation = await create_operation(
                settings,
                kind="ui.input.create",
                actor="ui",
                phase="Validate input",
                details=_input_progress_details(
                    "createInput",
                    0,
                    "Reading submitted config.",
                    phase="Validate input",
                    substate="Reading submitted config",
                    input_value=payload.value,
                ),
            )
            background_tasks = BackgroundTasks()
            background_tasks.add_task(
                _run_create_input_operation,
                settings,
                operation.id,
                {
                    "kind": kind,
                    "value": value,
                    "backend_ids": [str(item) for item in form.getlist("backend_ids")],
                    "enabled": enabled,
                },
            )
            response = _operation_response(operation, message="Input create started.")
            response.background = background_tasks
            return response
        item = await input_commands.create_input(session, payload, commit=False)
        attached_backend_ids = [backend.id for backend in item.backends]
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.input.create",
            apply_runner=run_apply,
        )
        apply_response = mutation.apply_response
        if apply_response.status == "success":
            await session.refresh(item)
        logger.info(
            "ui.input.create.succeeded",
            input_id=item.id if apply_response.status == "success" else None,
            kind=item.kind,
            value=item.hostname,
            backend_ids=attached_backend_ids,
            enabled=item.enabled,
            mutation_state=mutation.state,
        )
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Input saved.",
            failure_prefix="Input saved",
        )
        if return_dashboard:
            return await _dashboard_refresh_response(
                request,
                session,
                settings,
                active_tab="inputs",
                flash_success=success_flash,
                flash_error=error_flash,
                status_code=201 if apply_response.status == "success" else 200,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_success=success_flash,
            flash_error=error_flash,
        )
    except ValidationError as exc:
        await session.rollback()
        logger.warning("ui.input.create.validation_failed", error=str(exc))
        if async_dashboard:
            return JSONResponse(
                {"flash_error": _operator_validation_error(exc)}, status_code=400
            )
        if return_dashboard:
            return await _dashboard_refresh_response(
                request,
                session,
                settings,
                active_tab="inputs",
                flash_error=_operator_validation_error(exc),
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=_operator_validation_error(exc),
        )
    except IntegrityError as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_INPUT_CREATE_CONFLICT)
        logger.warning(
            "ui.input.create.conflict",
            kind=kind,
            value=value,
            **error_fields,
            error=str(exc),
        )
        flash_error = _operator_action_error(
            "Input create",
            f"{value} already exists. Use a unique input value.",
            error_fields,
        )
        if async_dashboard:
            return JSONResponse(
                {"flash_error": flash_error, **error_fields},
                status_code=400,
            )
        if return_dashboard:
            return await _dashboard_refresh_response(
                request,
                session,
                settings,
                active_tab="inputs",
                flash_error=flash_error,
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=flash_error,
        )
    except Exception as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_INPUT_CREATE_FAILED)
        logger.warning("ui.input.create.failed", **error_fields, error=str(exc))
        flash_error = _operator_action_error(
            "Input create",
            "Unexpected error while saving the input. Check server logs with this instance.",
            error_fields,
        )
        if async_dashboard:
            return JSONResponse(
                {"flash_error": flash_error, **error_fields},
                status_code=400,
            )
        if return_dashboard:
            return await _dashboard_refresh_response(
                request,
                session,
                settings,
                active_tab="inputs",
                flash_error=flash_error,
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=flash_error,
        )


async def _delete_input_and_redirect(
    input_id: int,
    request: Request,
    settings: Settings,
    session: AsyncSession,
) -> HTMLResponse:
    item = (
        await session.execute(
            select(Input)
            .options(selectinload(Input.backends))
            .where(Input.id == input_id)
        )
    ).scalar_one_or_none()
    if item is None:
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error="input not found",
        )
    snapshot = {
        "hostname": item.hostname,
    }
    await input_commands.delete_input(session, input_id)
    mutation = await commit_and_apply(
        session,
        settings,
        operation="ui.input.delete",
        apply_runner=run_apply,
    )
    apply_response = mutation.apply_response
    logger.info(
        "ui.input.delete.succeeded",
        input_id=input_id,
        value=snapshot["hostname"],
        mutation_state=mutation.state,
    )
    success_flash, error_flash = _save_apply_feedback(
        apply_response,
        success_message="Input deleted.",
        failure_prefix="Input deleted",
    )
    return _dashboard_redirect(
        request,
        active_tab="inputs",
        flash_success=success_flash,
        flash_error=error_flash,
    )


async def _input_delete_operation_response(
    settings: Settings, input_id: int, *, input_kind: str = "domain"
) -> JSONResponse:
    progress_plan = delete_input_progress_plan(input_kind=input_kind)
    operation = await create_operation(
        settings,
        kind="ui.input.delete",
        actor="ui",
        phase="Validate input",
        details=_planned_operation_progress_details(
            progress_plan,
            0,
            "Loading current route graph.",
            phase="Validate input",
            substate="Loading current route graph",
            input_id=input_id,
        ),
    )
    background_tasks = BackgroundTasks()
    background_tasks.add_task(
        _run_delete_input_operation,
        settings,
        operation.id,
        input_id,
    )
    response = _operation_response(operation, message="Input delete started.")
    response.background = background_tasks
    return response


@router.post("/ui/inputs/{input_id}")
async def update_input_form(
    input_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    kind: str | None = Form(None),
    value: str | None = Form(None),
    enabled: bool = Form(False),
    shield_enabled: bool = Form(False),
    shield_access_code: str = Form(""),
    action: str = Form("save"),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    form = await request.form()
    normalized_action = (
        _form_string_or_default(action, "save").strip().lower() or "save"
    )
    item = (
        await session.execute(
            select(Input)
            .options(selectinload(Input.backends))
            .where(Input.id == input_id)
        )
    ).scalar_one_or_none()
    if item is None:
        if _request_prefers_json(request):
            return _operator_error_json(
                "input not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
                key="flash_error",
                status_code=404,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error="input not found",
        )
    blocked = await _input_mutation_preflight(
        request,
        session,
        settings,
        action="Input delete" if normalized_action == "delete" else "Input save",
    )
    if blocked is not None:
        return blocked
    try:
        if normalized_action == "delete":
            if _request_prefers_json(request):
                return await _input_delete_operation_response(
                    settings, input_id, input_kind=item.kind
                )
            return await _delete_input_and_redirect(
                input_id, request, settings, session
            )
        next_enabled = enabled
        if normalized_action == "enable":
            next_enabled = True
        elif normalized_action == "disable":
            next_enabled = False
        shield_code_hash = item.shield_code_hash
        shield_access_code_display = str(
            getattr(item, "shield_access_code", "") or ""
        ).strip()
        normalized_shield_access_code = str(shield_access_code or "").strip()
        if normalized_shield_access_code:
            shield_code_hash = make_code_hash(
                settings.shield_secret_key, normalized_shield_access_code
            )
            shield_access_code_display = normalized_shield_access_code
        if item.kind == "domain" and shield_enabled and not shield_code_hash:
            raise ValidationError(
                "shield requires an access code before it can be enabled"
            )
        payload = _input_update_payload_from_form(
            kind=item.kind,
            value=item.hostname,
            backend_ids=form.getlist("backend_ids"),
            enabled=next_enabled,
            shield_enabled=shield_enabled if item.kind == "domain" else False,
            shield_code_hash=shield_code_hash,
            shield_access_code=shield_access_code_display,
        )
        if _request_prefers_json(request):
            operation = await create_operation(
                settings,
                kind="ui.input.update",
                actor="ui",
                phase="Validate input",
                details=_input_progress_details(
                    "saveInput",
                    0,
                    "Reading submitted config.",
                    phase="Validate input",
                    substate="Reading submitted config",
                    input_id=input_id,
                    input_value=item.hostname,
                ),
            )
            background_tasks = BackgroundTasks()
            background_tasks.add_task(
                _run_update_input_operation,
                settings,
                operation.id,
                input_id,
                {
                    "backend_ids": [
                        str(value) for value in form.getlist("backend_ids")
                    ],
                    "enabled": next_enabled,
                    "shield_enabled": shield_enabled,
                    "shield_access_code": shield_access_code,
                },
            )
            response = _operation_response(operation, message="Input save started.")
            response.background = background_tasks
            return response
        item = await input_commands.update_input(
            session, input_id, payload, commit=False
        )
        attached_backend_ids = [backend.id for backend in item.backends]
        mutation = await commit_and_apply(
            session,
            settings,
            operation="ui.input.update",
            apply_runner=run_apply,
        )
        logger.info(
            "ui.input.update.succeeded",
            input_id=item.id,
            kind=item.kind,
            value=item.hostname,
            backend_ids=attached_backend_ids,
            enabled=item.enabled,
            mutation_state=mutation.state,
        )
        apply_response = mutation.apply_response
        success_flash, error_flash = _save_apply_feedback(
            apply_response,
            success_message="Input saved.",
            failure_prefix="Input saved",
        )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_success=success_flash,
            flash_error=error_flash,
        )
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "ui.input.update.validation_failed", input_id=input_id, error=str(exc)
        )
        if _request_prefers_json(request):
            return JSONResponse(
                {"flash_error": _operator_validation_error(exc)}, status_code=400
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=_operator_validation_error(exc),
        )
    except IntegrityError as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_INPUT_UPDATE_CONFLICT)
        logger.warning(
            "ui.input.update.conflict",
            input_id=input_id,
            **error_fields,
            error=str(exc),
        )
        flash_error = _operator_action_error(
            "Input save",
            "input value already exists. Use a unique input value.",
            error_fields,
        )
        if _request_prefers_json(request):
            return JSONResponse(
                {"flash_error": flash_error, **error_fields},
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=flash_error,
        )
    except Exception as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_INPUT_UPDATE_FAILED)
        logger.exception(
            "ui.input.update.failed",
            input_id=input_id,
            **error_fields,
            error=str(exc),
        )
        flash_error = _operator_action_error(
            "Input save",
            "Unexpected error while saving the input. Check server logs with this instance.",
            error_fields,
        )
        if _request_prefers_json(request):
            return JSONResponse(
                {"flash_error": flash_error, **error_fields},
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=flash_error,
        )


@router.post("/ui/inputs/{input_id}/delete")
async def delete_input_form(
    input_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        blocked = await _input_mutation_preflight(
            request, session, settings, action="Input delete"
        )
        if blocked is not None:
            return blocked
        if _request_prefers_json(request):
            item = await input_commands.load_input(session, input_id)
            if item is None:
                return _operator_error_json(
                    "input not found",
                    ErrorCode.UI_RESOURCE_NOT_FOUND,
                    key="flash_error",
                    status_code=404,
                )
            return await _input_delete_operation_response(
                settings, input_id, input_kind=item.kind
            )
        return await _delete_input_and_redirect(input_id, request, settings, session)
    except Exception as exc:
        await session.rollback()
        error_fields = error_context(ErrorCode.UI_INPUT_DELETE_FAILED)
        logger.exception(
            "ui.input.delete.failed",
            input_id=input_id,
            **error_fields,
            error=str(exc),
        )
        flash_error = _operator_action_error(
            "Input delete",
            "Unexpected error while deleting the input. Check server logs with this instance.",
            error_fields,
        )
        if _request_prefers_json(request):
            return JSONResponse(
                {"flash_error": flash_error, **error_fields},
                status_code=400,
            )
        return _dashboard_redirect(
            request,
            active_tab="inputs",
            flash_error=flash_error,
        )
