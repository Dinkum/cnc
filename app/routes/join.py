from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import db_session_dependency, settings_dependency
from app.logger import get_logger
from app.services.cluster_nodes import (
    confirm_node,
    current_node_version_label,
    install_script,
    register_node,
)


router = APIRouter()
logger = get_logger("routes.join")
JOIN_CONFIRM_DB_BUSY_ATTEMPTS = 3
JOIN_CONFIRM_DB_BUSY_BACKOFF_SEC = 0.25


def _multi_node_disabled_json() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "not found"})


@router.get("/join/install.sh", response_class=PlainTextResponse)
async def node_join_install_script(
    settings: Settings = Depends(settings_dependency),
) -> PlainTextResponse:
    if not settings.multi_node_enabled:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(
        install_script(current_node_version_label(settings)),
        media_type="text/x-shellscript",
    )


@router.post("/join/register")
async def node_join_register(
    request: Request,
    x_cnc_join_token: str = Header(default="", alias="X-CNC-Join-Token"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    if not settings.multi_node_enabled:
        return _multi_node_disabled_json()
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("join payload must be an object")
        registered = await register_node(
            session,
            settings,
            token=x_cnc_join_token,
            payload=payload,
            request_host=request.url.hostname,
        )
    except ValueError as exc:
        await session.rollback()
        logger.warning("node_join.register.rejected", error=str(exc))
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(
        {
            "node": {
                "id": registered.node.node_uid,
                "name": registered.node.name,
                "role": registered.node.role,
                "state": registered.node.state,
                "tailnet_ip": registered.node.tailnet_ip,
            },
            "leader": {
                "tailnet_ip": registered.leader_tailnet_ip,
            },
        }
    )


@router.post("/join/confirm")
async def node_join_confirm(
    request: Request,
    x_cnc_join_token: str = Header(default="", alias="X-CNC-Join-Token"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    if not settings.multi_node_enabled:
        return _multi_node_disabled_json()
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("join payload must be an object")
        node = await _confirm_node_with_db_busy_retry(
            session, token=x_cnc_join_token, payload=payload
        )
        if node is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "database is busy; retry join confirmation"},
            )
    except ValueError as exc:
        await session.rollback()
        logger.warning("node_join.confirm.rejected", error=str(exc))
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(
        {
            "node": {
                "id": node.node_uid,
                "name": node.name,
                "role": node.role,
                "state": node.state,
                "tailnet_ip": node.tailnet_ip,
                "latency_ms": node.latency_ms,
            }
        }
    )


async def _confirm_node_with_db_busy_retry(
    session: AsyncSession,
    *,
    token: str,
    payload: dict[str, Any],
):
    attempts = max(1, JOIN_CONFIRM_DB_BUSY_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            return await confirm_node(session, token=token, payload=payload)
        except OperationalError as exc:
            if not _is_database_locked_error(exc):
                raise
            await session.rollback()
            retrying = attempt < attempts
            logger.warning(
                "node_join.confirm.database_busy",
                attempt=attempt,
                attempts=attempts,
                retrying=retrying,
                error=str(exc),
            )
            if not retrying:
                return None
            await asyncio.sleep(JOIN_CONFIRM_DB_BUSY_BACKOFF_SEC * attempt)
    return None


def _is_database_locked_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    return "database is locked" in str(original or exc).lower()
