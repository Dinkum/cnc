from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.dependencies import (
    csrf_api_dependency,
    db_session_dependency,
    settings_dependency,
)
from app.logger import get_logger
from app.models.entities import Backend
from app.routes.apply_errors import raise_apply_failure
from app.schemas.backends import (
    BackendCloneIn,
    BackendIn,
    BackendOut,
    BackendStateIn,
    BackendUpdate,
)
from app.services import backend_commands
from app.services.mutation_apply import commit_and_apply
from app.services.status_service import invalidate_status_cache
from app.services.validators import ValidationError


router = APIRouter(prefix="/api/backends", tags=["backends"])
logger = get_logger("api.backends")


@router.get("", response_model=list[BackendOut])
async def list_backends(
    session: AsyncSession = Depends(db_session_dependency),
) -> list[Backend]:
    return (
        (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post("", response_model=BackendOut)
async def create_backend(
    payload: BackendIn,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Backend:
    logger.info(
        "backend.create.requested",
        backend_name=payload.name,
        kind=payload.kind,
        enabled=payload.enabled,
        port=payload.port,
    )
    try:
        backend = await backend_commands.create_backend(session, payload, commit=False)
        result = await commit_and_apply(
            session, settings, operation="api.backend.create"
        )
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        backend = await backend_commands.require_backend(session, backend.id)
        logger.info(
            "backend.create.succeeded",
            backend_id=backend.id,
            backend_name=backend.name,
            kind=backend.kind,
            enabled=backend.enabled,
            port=backend.port,
            input_ids=backend.input_ids,
        )
        invalidate_status_cache(settings, prefill=True)
        return backend
    except LookupError as exc:
        await session.rollback()
        logger.warning(
            "backend.create.related_missing", backend_name=payload.name, error=str(exc)
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "backend.create.validation_failed",
            backend_name=payload.name,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=exc.payload()) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.warning(
            "backend.create.conflict",
            backend_name=payload.name,
        )
        raise HTTPException(
            status_code=409, detail="backend name already exists"
        ) from exc


@router.post("/{backend_id}", response_model=BackendOut)
async def update_backend(
    backend_id: int,
    payload: BackendUpdate,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Backend:
    try:
        backend = await backend_commands.update_backend(
            session, backend_id, payload, commit=False
        )
        result = await commit_and_apply(
            session, settings, operation="api.backend.update"
        )
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        backend = await backend_commands.require_backend(session, backend.id)
        logger.info(
            "backend.update.succeeded",
            backend_id=backend.id,
            backend_name=backend.name,
            kind=backend.kind,
            enabled=backend.enabled,
            port=backend.port,
            input_ids=backend.input_ids,
        )
        invalidate_status_cache(settings, prefill=True)
        return backend
    except LookupError as exc:
        await session.rollback()
        logger.warning("backend.update.missing", backend_id=backend_id, error=str(exc))
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "backend.update.validation_failed",
            backend_id=backend_id,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=exc.payload()) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.warning(
            "backend.update.conflict",
            backend_id=backend_id,
        )
        raise HTTPException(
            status_code=409, detail="backend name already exists"
        ) from exc


@router.post("/{backend_id}/toggle", response_model=BackendOut)
async def toggle_backend(
    backend_id: int,
    payload: BackendStateIn,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Backend:
    try:
        backend = await backend_commands.require_backend(session, backend_id)
        if backend.enabled == payload.enabled:
            logger.info(
                "backend.toggle.idempotent_noop",
                backend_id=backend.id,
                backend_name=backend.name,
                enabled=backend.enabled,
            )
            return backend
        backend = await backend_commands.update_backend(
            session,
            backend_id,
            BackendUpdate(enabled=payload.enabled),
            commit=False,
        )
        result = await commit_and_apply(
            session, settings, operation="api.backend.toggle"
        )
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        backend = await backend_commands.require_backend(session, backend.id)
    except LookupError as exc:
        await session.rollback()
        logger.warning("backend.toggle.missing", backend_id=backend_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "backend.toggle.succeeded",
        backend_id=backend.id,
        backend_name=backend.name,
        enabled=backend.enabled,
    )
    invalidate_status_cache(settings, prefill=True)
    return backend


@router.post("/{backend_id}/clone", response_model=BackendOut)
async def clone_backend(
    backend_id: int,
    payload: BackendCloneIn,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Backend:
    try:
        source_backend = await backend_commands.require_backend(session, backend_id)
        result = await backend_commands.clone_backend(
            session, backend_id, payload, settings
        )
        cloned_backend = result["backend"]
        restored_paths = (
            result.get("restored_paths") if isinstance(result, dict) else []
        )
        logger.info(
            "backend.clone.succeeded",
            backend_id=source_backend.id,
            backend_name=source_backend.name,
            clone_id=cloned_backend.id,
            clone_name=cloned_backend.name,
            clone_port=cloned_backend.port,
            restored_paths=len(restored_paths)
            if isinstance(restored_paths, list)
            else 0,
        )
        invalidate_status_cache(settings, prefill=True)
        return cloned_backend
    except LookupError as exc:
        await session.rollback()
        status_code = 404 if str(exc) == "backend not found" else 409
        logger.warning(
            "backend.clone.lookup_error",
            backend_id=backend_id,
            error=str(exc),
            status_code=status_code,
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.warning(
            "backend.clone.integrity_error", backend_id=backend_id, error=str(exc)
        )
        raise HTTPException(
            status_code=409, detail="clone name or port already exists"
        ) from exc
    except (ValidationError, RuntimeError) as exc:
        await session.rollback()
        logger.warning("backend.clone.failed", backend_id=backend_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
