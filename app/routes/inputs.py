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
from app.models.entities import Input
from app.routes.apply_errors import raise_apply_failure
from app.schemas.inputs import InputIn, InputOut, InputStateIn, InputUpdate
from app.services import input_commands
from app.services.mutation_apply import commit_and_apply
from app.services.status_service import invalidate_status_cache
from app.services.validators import ValidationError


router = APIRouter(prefix="/api/inputs", tags=["inputs"])
logger = get_logger("api.inputs")


@router.get("", response_model=list[InputOut])
async def list_inputs(
    session: AsyncSession = Depends(db_session_dependency),
) -> list[Input]:
    return (
        (
            await session.execute(
                select(Input)
                .options(selectinload(Input.backends))
                .order_by(Input.id.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post("", response_model=InputOut)
async def create_input(
    payload: InputIn,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Input:
    logger.info(
        "input.create.requested",
        kind=payload.kind,
        value=payload.value,
        backend_ids=payload.backend_ids,
        enabled=payload.enabled,
    )
    try:
        item = await input_commands.create_input(session, payload, commit=False)
        result = await commit_and_apply(session, settings, operation="api.input.create")
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        item = await input_commands.require_input(session, item.id)
        logger.info(
            "input.create.succeeded",
            input_id=item.id,
            kind=item.kind,
            value=item.hostname,
            backend_ids=item.backend_ids,
            enabled=item.enabled,
        )
        invalidate_status_cache(settings, prefill=True)
        return item
    except LookupError as exc:
        await session.rollback()
        logger.warning(
            "input.create.related_missing",
            kind=payload.kind,
            value=payload.value,
            error=str(exc),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "input.create.validation_failed",
            kind=payload.kind,
            value=payload.value,
            backend_ids=payload.backend_ids,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=exc.payload()) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.warning("input.create.conflict", kind=payload.kind, value=payload.value)
        raise HTTPException(status_code=409, detail="input already exists") from exc


@router.post("/{input_id}", response_model=InputOut)
async def update_input(
    input_id: int,
    payload: InputUpdate,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Input:
    try:
        item = await input_commands.update_input(
            session, input_id, payload, commit=False
        )
        result = await commit_and_apply(session, settings, operation="api.input.update")
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        item = await input_commands.require_input(session, item.id)
        logger.info(
            "input.update.succeeded",
            input_id=item.id,
            kind=item.kind,
            value=item.hostname,
            backend_ids=item.backend_ids,
            enabled=item.enabled,
        )
        invalidate_status_cache(settings, prefill=True)
        return item
    except LookupError as exc:
        await session.rollback()
        logger.warning("input.update.missing", input_id=input_id, error=str(exc))
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        await session.rollback()
        logger.warning(
            "input.update.validation_failed", input_id=input_id, error=str(exc)
        )
        raise HTTPException(status_code=400, detail=exc.payload()) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.warning("input.update.conflict", input_id=input_id)
        raise HTTPException(status_code=409, detail="input already exists") from exc


@router.post("/{input_id}/toggle", response_model=InputOut)
async def toggle_input(
    input_id: int,
    payload: InputStateIn,
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> Input:
    try:
        item = await input_commands.require_input(session, input_id)
        if item.enabled == payload.enabled:
            logger.info(
                "input.toggle.idempotent_noop",
                input_id=item.id,
                kind=item.kind,
                value=item.hostname,
                enabled=item.enabled,
            )
            return item
        item = await input_commands.update_input(
            session,
            input_id,
            InputUpdate(enabled=payload.enabled),
            commit=False,
        )
        result = await commit_and_apply(session, settings, operation="api.input.toggle")
        if not result.applied:
            raise_apply_failure(result.apply_response.details)
        item = await input_commands.require_input(session, item.id)
    except LookupError as exc:
        await session.rollback()
        logger.warning("input.toggle.missing", input_id=input_id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "input.toggle.succeeded",
        input_id=item.id,
        kind=item.kind,
        value=item.hostname,
        backend_ids=item.backend_ids,
        enabled=item.enabled,
    )
    invalidate_status_cache(settings, prefill=True)
    return item
