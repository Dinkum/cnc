from __future__ import annotations

import asyncio
from typing import Literal

from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.models.entities import Backend
from app.security import enforce_csrf
from app.services.app_hardening import (
    active_hardening_run,
    cancel_latest_hardening_run,
    create_hardening_run,
    create_resumed_phase2_run,
    latest_hardening_summary,
    latest_resumable_phase2_run,
    request_phase1_monitor_stop,
    run_phase1_monitor,
    run_phase2_test,
)
from app.services.backend_ssh_keys import ensure_backend_ssh_keypair
from app.services.hardening_policy import hardening_configuration, hardening_preview
from app.services.error_reporting import ErrorCode
from app.services.operations import (
    HostMutationLockError,
    host_mutation_operation,
    create_operation,
)
from app.services.ssh_access import reconcile_backend_ssh_access
from app.ui.errors import operator_error_json as _operator_error_json
from app.ui.routes.reads import _backend_ssh_key_download_response
from app.ui.routes.shared import logger, _host_mutation_preflight_message
from app.ui.operations.common import operation_backend, operation_response
from app.ui.operations.hardening import run_hardening_apply
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    Request,
    HTTPException,
)
from pydantic import ValidationError as PolicyValidationError
from fastapi.responses import (
    JSONResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


router = APIRouter(tags=["ui"])


def require_hardening_enabled(settings: Settings) -> None:
    if not settings.beta_hardening:
        raise HTTPException(
            status_code=403,
            detail="Enable Security hardening in CNC Settings → Beta features first.",
        )


async def _hardening_backend(
    session: AsyncSession, settings: Settings, backend_id: int
) -> Backend:
    require_hardening_enabled(settings)
    try:
        backend = await operation_backend(session, backend_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="App output not found.") from exc
    if backend.kind != "app":
        raise HTTPException(
            status_code=400, detail="Hardening is only available for app outputs."
        )
    return backend


def _configuration_preview(
    backend: Backend, summary: dict, mode: str, configuration: str
) -> dict:
    try:
        return hardening_preview(backend, summary, mode, configuration)
    except PolicyValidationError as exc:
        raise HTTPException(
            status_code=400, detail=exc.errors(include_input=False)[0]["msg"]
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/backends/{backend_id}/hardening/configuration")
async def read_hardening_configuration(
    backend_id: int,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict:
    backend = await _hardening_backend(session, settings, backend_id)
    summary = await latest_hardening_summary(session, backend_id)
    return hardening_configuration(backend, summary)


@router.post("/ui/backends/{backend_id}/hardening/preview")
async def preview_hardening_configuration(
    backend_id: int,
    request: Request,
    csrf_token: str = Form(...),
    mode: Literal["recommended", "manual", "previous"] = Form(...),
    configuration: str = Form("{}", max_length=16_384),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict:
    enforce_csrf(request, settings, csrf_token)
    backend = await _hardening_backend(session, settings, backend_id)
    return _configuration_preview(
        backend,
        await latest_hardening_summary(session, backend_id),
        mode,
        configuration,
    )


@router.post("/ui/backends/{backend_id}/hardening/apply")
async def apply_hardening_configuration(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
    mode: Literal["recommended", "manual", "previous"] = Form(...),
    configuration: str = Form("{}", max_length=16_384),
    revision: str = Form(..., min_length=64, max_length=64),
    reviewed: bool = Form(False),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    backend = await _hardening_backend(session, settings, backend_id)
    if reviewed is not True:
        raise HTTPException(
            status_code=400,
            detail="Review the recommendations and acknowledge the possible impact before applying.",
        )
    plan = _configuration_preview(
        backend,
        await latest_hardening_summary(session, backend_id),
        mode,
        configuration,
    )
    if plan["revision"] != revision:
        raise HTTPException(
            status_code=409,
            detail="The output or recommendations changed. Review the configuration again.",
        )
    blocked = await _host_mutation_preflight_message(
        settings, action="Security configuration apply"
    )
    if blocked:
        raise HTTPException(status_code=409, detail=blocked)
    # Release the read transaction before the separate operation writer opens SQLite.
    await session.rollback()
    operation = await create_operation(
        settings,
        kind="ui.backend.hardening",
        actor="ui",
        backend_id=backend_id,
        phase="Apply security configuration",
        details={"message": "Security configuration queued.", "backend_id": backend_id},
    )
    background_tasks.add_task(
        run_hardening_apply,
        settings,
        operation.id,
        backend_id,
        mode=mode,
        configuration=configuration,
        revision=revision,
    )
    return operation_response(operation, message="Security configuration queued.")


@router.post("/api/backends/{backend_id}/ssh-key")
async def provision_backend_ssh_key(
    backend_id: int,
    request: Request,
    csrf_token: str = Form(...),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    enforce_csrf(request, settings, csrf_token)
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None or backend.kind != "app":
        return _operator_error_json(
            "app output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )
    try:
        async with host_mutation_operation(
            settings,
            kind="backend_ssh_key",
            actor="ui",
            backend_id=backend_id,
            phase="Provision SSH access",
        ):
            changed = await asyncio.to_thread(ensure_backend_ssh_keypair, backend)
            if changed:
                await session.commit()
                await session.refresh(backend)
            if backend.enabled:
                enabled_backends = list(
                    (
                        await session.execute(
                            select(Backend)
                            .where(Backend.kind == "app", Backend.enabled.is_(True))
                            .order_by(Backend.id.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                await reconcile_backend_ssh_access(enabled_backends, settings)
            private_key = str(backend.ssh_private_key or "")
            backend_name = str(backend.name)
            if not private_key:
                raise RuntimeError("ssh key unavailable after provisioning")
        return _backend_ssh_key_download_response(backend_name, private_key)
    except HostMutationLockError as exc:
        await session.rollback()
        return _operator_error_json(
            str(exc),
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=409,
        )
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ui.backend.ssh_key.provision_failed",
            backend_id=backend_id,
            error=str(exc),
        )
        return _operator_error_json(
            "ssh key provisioning failed",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=500,
        )


@router.post("/ui/backends/{backend_id}/hardening/phase1")
async def start_hardening_phase1(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    require_hardening_enabled(settings)
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
    if backend.kind != "app":
        return _operator_error_json(
            "hardening is only available for app outputs",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    run = await create_hardening_run(
        session,
        backend,
        settings,
        phase="phase1",
        details={
            "message": "Phase 1 monitor queued.",
            "duration_sec": settings.hardening_phase1_default_sec,
        },
    )
    background_tasks.add_task(
        run_phase1_monitor,
        settings,
        run.id,
        backend.id,
        settings.hardening_phase1_default_sec,
    )
    return JSONResponse(
        {
            "message": "Phase 1 monitor started.",
            "run_id": run.id,
            **(await latest_hardening_summary(session, backend.id)),
        }
    )


@router.post("/ui/backends/{backend_id}/hardening/phase1/stop")
async def stop_hardening_phase1(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    await request_phase1_monitor_stop(session, backend_id)
    return JSONResponse(
        {
            "message": "Phase 1 monitor stopped.",
            **(await latest_hardening_summary(session, backend_id)),
        }
    )


@router.post("/ui/backends/{backend_id}/hardening/phase2")
async def start_hardening_phase2(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    require_hardening_enabled(settings)
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
    if backend.kind != "app":
        return _operator_error_json(
            "hardening is only available for app outputs",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    summary = await latest_hardening_summary(session, backend.id)
    phase1 = summary.get("phase1")
    if not isinstance(phase1, dict) or phase1.get("status") != "success":
        return _operator_error_json(
            "run Phase 1 before Phase 2",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    active_run = await active_hardening_run(session, backend.id, "phase2")
    if active_run is not None:
        return JSONResponse(
            {
                "message": "Phase 2 clone test already running.",
                "run_id": active_run.id,
                **summary,
            }
        )
    run = await create_hardening_run(
        session,
        backend,
        settings,
        phase="phase2",
        details={"message": "Phase 2 clone test queued."},
    )
    background_tasks.add_task(run_phase2_test, settings, run.id, backend.id)
    return JSONResponse(
        {
            "message": "Phase 2 clone test started.",
            "run_id": run.id,
            **(await latest_hardening_summary(session, backend.id)),
        }
    )


@router.post("/ui/backends/{backend_id}/hardening/phase2/resume")
async def resume_hardening_phase2(
    backend_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    require_hardening_enabled(settings)
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
    if backend.kind != "app":
        return _operator_error_json(
            "hardening is only available for app outputs",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    summary = await latest_hardening_summary(session, backend.id)
    phase1 = summary.get("phase1")
    if not isinstance(phase1, dict) or phase1.get("status") != "success":
        return _operator_error_json(
            "run Phase 1 before Phase 2",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=400,
        )
    active_run = await active_hardening_run(session, backend.id, "phase2")
    if active_run is not None:
        return JSONResponse(
            {
                "message": "Phase 2 clone test already running.",
                "run_id": active_run.id,
                **summary,
            }
        )
    source_run = await latest_resumable_phase2_run(session, backend.id)
    if source_run is None:
        return JSONResponse(
            {"error": "no interrupted Phase 2 run to resume"}, status_code=400
        )
    run = await create_resumed_phase2_run(session, backend, settings, source_run)
    background_tasks.add_task(run_phase2_test, settings, run.id, backend.id)
    return JSONResponse(
        {
            "message": "Phase 2 resume started.",
            "run_id": run.id,
            **(await latest_hardening_summary(session, backend.id)),
        }
    )


@router.post("/ui/backends/{backend_id}/hardening/phase2/stop")
async def stop_hardening_phase2(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    csrf_token: str = Form(...),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    await cancel_latest_hardening_run(session, backend_id, "phase2")
    return JSONResponse(
        {
            "message": "Phase 2 clone test stopped.",
            **(await latest_hardening_summary(session, backend_id)),
        }
    )
