from __future__ import annotations

import asyncio
import ipaddress
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.access import internal_backend_ssh_request_is_authorized
from app.config import Settings
from app.dependencies import db_session_dependency, settings_dependency
from app.logger import get_logger
from app.models.entities import Backend
from app.services.llm_help import (
    backend_status_context,
    build_backend_llm_help_payload,
    cached_service_map,
    render_backend_llm_help_text,
)
from app.services.renderers import container_name
from app.services.status_service import peek_cached_status
from app.services.tailscale_urls import configured_tailnet_admin_host


router = APIRouter(include_in_schema=False)
logger = get_logger("backend.ssh")
_BACKEND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HELP_TIMEOUT_SEC = 1.5


def _server_host(request: Request, settings: Settings) -> str:
    candidate = str(request.headers.get("x-cnc-ssh-server") or "").strip()
    if candidate:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return str(settings.ssh_advertise_host or "").strip() or "SERVER_IP"


async def _load_backend(
    session: AsyncSession,
    backend_name: str,
) -> Backend | None:
    return (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.name == backend_name)
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/internal/backend-ssh/llm-help/{backend_name}")
async def backend_ssh_llm_help(
    request: Request,
    backend_name: str,
    json_output: bool = Query(default=False, alias="json"),
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
) -> Response:
    if not internal_backend_ssh_request_is_authorized(request):
        raise HTTPException(status_code=404, detail="not found")
    if not _BACKEND_NAME_RE.fullmatch(backend_name):
        raise HTTPException(status_code=404, detail="backend not found")

    try:
        async with asyncio.timeout(_HELP_TIMEOUT_SEC):
            backend = await _load_backend(session, backend_name)
            if backend is None:
                raise HTTPException(status_code=404, detail="backend not found")
            service_map = cached_service_map(peek_cached_status())
            status = backend_status_context(
                backend,
                service_map.get(container_name(backend.name)),
            )
            host = _server_host(request, settings)
            payload = build_backend_llm_help_payload(
                backend,
                host=host,
                backend_ssh_host=configured_tailnet_admin_host(settings) or host,
                status_value=status[0],
                service_state=status[1],
                runtime_diagnosis=status[2],
                runtime_issues=status[3],
            )
    except TimeoutError:
        logger.warning(
            "backend.ssh.llm_help.timed_out",
            backend=backend_name,
            timeout_sec=_HELP_TIMEOUT_SEC,
        )
        raise HTTPException(
            status_code=503, detail="llm-help temporarily unavailable"
        ) from None

    if json_output:
        return Response(
            content=json.dumps(payload, indent=2, sort_keys=True) + "\n",
            media_type="application/json",
        )
    return Response(
        content=render_backend_llm_help_text(payload) + "\n",
        media_type="text/plain",
    )
