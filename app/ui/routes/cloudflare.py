"""Cloudflare connection setup and explicit domain previews."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import db_session_dependency, settings_dependency
from app.security import enforce_csrf
from app.services.cloudflare_api import CloudflareError
from app.services.cloudflare_connection import configure_connection, connection_summary
from app.services.cloudflare_domains import build_domain_preview
from app.ui.operations.cloudflare import run_domain_operation
from app.services.input_commands import require_input
from app.services.operations import active_host_mutation_blocker, create_operation

router = APIRouter(tags=["ui"])


def response(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


@router.post("/ui/settings/cloudflare")
async def configure_cloudflare(
    request: Request,
    csrf_token: Annotated[str, Form()],
    action: Annotated[Literal["connect", "retest", "disconnect"], Form()] = "connect",
    api_token: Annotated[str, Form()] = "",
    settings: Settings = Depends(settings_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        next_settings = await asyncio.to_thread(
            configure_connection, settings, action, api_token
        )
    except CloudflareError as exc:
        return response({"detail": str(exc)}, 400)
    except OSError:
        return response(
            {
                "detail": "Cloudflare settings could not be saved. Check CNC's settings storage and retry."
            },
            500,
        )
    return response(
        {
            "connection": connection_summary(next_settings),
            "message": "Cloudflare disconnected. Existing certificates and DNS records remain."
            if action == "disconnect"
            else "Connection verified. Domain options are now available.",
        }
    )


@router.post("/ui/inputs/{input_id}/cloudflare/preview")
async def preview_cloudflare_domain(
    input_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    provider: Annotated[Literal["self_signed", "cloudflare_origin"], Form()],
    manage_dns: Annotated[bool, Form()] = False,
    ipv4: Annotated[str, Form()] = "",
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        item = await require_input(session, input_id)
        preview = await asyncio.to_thread(
            build_domain_preview,
            settings,
            item,
            provider=provider,
            manage_dns=manage_dns,
            ipv4=ipv4,
        )
    except LookupError:
        raise HTTPException(404, "Input not found.") from None
    except CloudflareError as exc:
        return response({"detail": str(exc)}, 400)
    return response({"preview": preview})


@router.post("/ui/inputs/{input_id}/cloudflare/apply")
async def apply_cloudflare_domain(
    input_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    preview_id: Annotated[str, Form()],
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        await require_input(session, input_id)
    except LookupError:
        raise HTTPException(404, "Input not found.") from None
    if await active_host_mutation_blocker(settings) is not None:
        return response(
            {
                "detail": "Another change is running. Wait for it to finish, then preview again."
            },
            409,
        )
    operation = await create_operation(
        settings,
        kind="cloudflare_domain",
        actor="ui",
        phase="Checking preview",
        details={"input_id": input_id},
    )
    background_tasks.add_task(
        run_domain_operation, settings, operation.id, input_id, preview_id
    )
    return response({"operation_id": operation.id, "operation_status": "queued"}, 202)
