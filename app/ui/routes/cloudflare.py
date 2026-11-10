from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.entities import Backend, Input
from app.services.cloudflare_tunnel import (
    TUNNEL_METRICS_PORT,
    TUNNEL_ORIGIN_PORT,
    configure_tunnel,
    tunnel_identity,
    tunnel_summary,
)
from app.services.operations import host_mutation_operation
from app.services.thread_workers import BoundedThreadWorker

from app.config import Settings
from app.dependencies import db_session_dependency, settings_dependency
from app.security import enforce_csrf
from app.services.mutation_queue import enqueue_mutation
from app.services.cloudflare_api import CloudflareError
from app.services.cloudflare_connection import configure_connection, connection_summary
from app.services.cloudflare_domains import build_domain_preview
from app.services.input_commands import require_input
from app.services.operations import active_host_mutation_blocker, create_operation

router = APIRouter(tags=["ui"])
_tunnel_workers = BoundedThreadWorker(1)


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


@router.post("/ui/settings/cloudflare-tunnel")
async def configure_cloudflare_tunnel(
    request: Request,
    csrf_token: Annotated[str, Form()],
    action: Annotated[Literal["connect", "retest", "disconnect"], Form()] = "connect",
    tunnel_token: Annotated[str, Form()] = "",
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        async with host_mutation_operation(
            settings, kind="cloudflare_tunnel", phase="Checking connector"
        ):
            if action == "connect":
                collision = await session.scalar(
                    select(Backend.id)
                    .where(
                        Backend.kind == "app",
                        Backend.port.in_([TUNNEL_ORIGIN_PORT, TUNNEL_METRICS_PORT]),
                    )
                    .limit(1)
                )
                if collision is not None:
                    return response(
                        {
                            "detail": "An output uses port 18081 or 18082. Change its host port before connecting the Tunnel."
                        },
                        409,
                    )
            changing_identity = action == "disconnect"
            if action == "connect" and settings.cloudflare_tunnel_token:
                changing_identity = tunnel_identity(
                    tunnel_token.strip()
                ) != tunnel_identity(settings.cloudflare_tunnel_token)
            if changing_identity:
                active = await session.scalar(
                    select(Input.id)
                    .where(
                        Input.connection == "cloudflare_tunnel", Input.enabled.is_(True)
                    )
                    .limit(1)
                )
                if active is not None:
                    return response(
                        {
                            "detail": "Switch or disable the domains using this Tunnel first."
                        },
                        409,
                    )
            next_settings = await _tunnel_workers.run(
                configure_tunnel, settings, action, tunnel_token
            )
    except CloudflareError as exc:
        return response({"detail": str(exc)}, 400)
    except OSError:
        return response(
            {
                "detail": "The Tunnel settings could not be saved. Check CNC's settings storage and retry."
            },
            500,
        )
    return response(
        {
            "connection": tunnel_summary(next_settings),
            "message": "Tunnel disconnected. Cloudflare hostname routes and DNS records remain."
            if action == "disconnect"
            else "Connector verified. Configure each hostname in Cloudflare before using it.",
        }
    )


@router.post("/ui/inputs/{input_id}/cloudflare/preview")
async def preview_cloudflare_domain(
    input_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    provider: Annotated[
        Literal["self_signed", "cloudflare_origin"], Form()
    ] = "self_signed",
    connection: Annotated[
        Literal["public_ip", "cloudflare_tunnel"] | None, Form()
    ] = None,
    route_confirmed: Annotated[bool, Form()] = False,
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
            connection=connection,
            route_confirmed=route_confirmed,
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
    await enqueue_mutation(
        settings,
        operation.id,
        "cloudflare_domain",
        input_id=input_id,
        preview_id=preview_id,
    )
    return response({"operation_id": operation.id, "operation_status": "queued"}, 202)
