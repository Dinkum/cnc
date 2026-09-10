"""SSH key metadata and one-time creation responses for app outputs."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import db_session_dependency, settings_dependency
from app.logger import get_logger
from app.models.entities import Backend
from app.security import enforce_csrf
from app.services.backend_ssh_keys import (
    list_backend_ssh_keys,
    mutate_backend_ssh_key,
    ssh_key_metadata,
)
from app.services.operations import HostMutationLockError

router = APIRouter(tags=["ui"])
logger = get_logger("ui")


@router.get("/api/backends/{backend_id}/ssh-keys")
async def output_ssh_keys(
    backend_id: int, session: AsyncSession = Depends(db_session_dependency)
) -> JSONResponse:
    backend = await session.get(Backend, backend_id)
    if backend is None or backend.kind != "app":
        raise HTTPException(404, "App output not found.")
    keys = await list_backend_ssh_keys(session, backend_id)
    return JSONResponse(
        {"keys": [ssh_key_metadata(key) for key in keys]},
        headers={"Cache-Control": "no-store"},
    )


async def _change(
    session: AsyncSession, settings: Settings, backend_id: int, **kwargs: str | None
) -> JSONResponse:
    try:
        payload = await mutate_backend_ssh_key(session, settings, backend_id, **kwargs)
    except (ValueError, LookupError, HostMutationLockError) as exc:
        await session.rollback()
        status = (
            404
            if isinstance(exc, LookupError)
            else 409
            if isinstance(exc, HostMutationLockError)
            else 400
        )
        return JSONResponse(
            {"detail": str(exc)},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        await session.rollback()
        logger.exception("ui.backend.ssh_key.update_failed", backend_id=backend_id)
        return JSONResponse(
            {
                "detail": "SSH access update failed. Check the key list before trying again."
            },
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.post("/api/backends/{backend_id}/ssh-keys")
async def create_output_ssh_key(
    backend_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(..., max_length=80),
    mode: str = Form("create"),
    public_key: str = Form("", max_length=4096),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    if mode not in {"create", "import"}:
        raise HTTPException(400, "Choose create or import.")
    return await _change(
        session,
        settings,
        backend_id,
        name=name,
        public_key=public_key if mode == "import" else None,
    )


@router.post("/api/backends/{backend_id}/ssh-keys/{key_id}/revoke")
async def revoke_output_ssh_key(
    backend_id: int,
    key_id: str,
    request: Request,
    csrf_token: str = Form(...),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, csrf_token)
    return await _change(session, settings, backend_id, revoke_id=key_id)
