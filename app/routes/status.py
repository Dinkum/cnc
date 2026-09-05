import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import verify_db_connection
from app.dependencies import db_session_dependency, settings_dependency
from app.models.entities import Operation
from app.security import get_csrf_token
from app.services.operation_progress import read_operation_progress
from app.services.operation_runtime import (
    CREATE_BACKEND_PROGRESS_POINTS,
    create_backend_runtime_progress,
    progress_plan_value,
)
from app.services.startup_state import snapshot_startup_state
from app.services.status_service import collect_status
from app.services.time_utils import utc_isoformat


router = APIRouter(prefix="/api", tags=["status"])
probe_router = APIRouter(tags=["status"])

ACTIVE_OPERATION_STATUSES = ("queued", "running")
DASHBOARD_OPERATION_KINDS = (
    "create_backend",
    "ui.input.create",
    "ui.input.delete",
    "ui.input.update",
)
OUTPUT_DETAIL_OPERATION_KINDS = (
    "backup_backend",
    "clone_backend",
    "delete_backend",
    "delete_backend_backup",
    "import_backend_backup",
    "repair_backend",
    "restore_backend",
    "setup_backend_replica",
    "transfer_backend",
    "ui.backend.inputs",
    "ui.backend.interface",
    "ui.backend.state",
    "ui.backend.update",
)


def _merge_operation_details(
    db_details: object,
    snapshot_details: object,
) -> object:
    if not isinstance(snapshot_details, dict):
        return db_details
    if not isinstance(db_details, dict):
        return snapshot_details
    merged = {**db_details, **snapshot_details}
    db_progress = db_details.get("progress")
    snapshot_progress = snapshot_details.get("progress")
    if isinstance(db_progress, (int, float)) and isinstance(
        snapshot_progress, (int, float)
    ):
        merged["progress"] = max(db_progress, snapshot_progress)
    return merged


@router.get("/status")
async def status(
    force_refresh: bool = Query(default=False),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict:
    return await collect_status(session, settings, force_refresh=force_refresh)


@router.get("/active-operations")
async def active_operations(
    surface: str = Query(default="dashboard"),
    backend_id: int | None = Query(default=None),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object]:
    normalized_surface = surface.strip().lower()
    operation_kinds = (
        OUTPUT_DETAIL_OPERATION_KINDS
        if normalized_surface == "output_detail"
        else DASHBOARD_OPERATION_KINDS
    )
    query = (
        select(Operation.id)
        .where(Operation.status.in_(ACTIVE_OPERATION_STATUSES))
        .where(Operation.kind.in_(operation_kinds))
        .order_by(Operation.id.asc())
        .limit(12)
    )
    if normalized_surface == "output_detail":
        if backend_id is None:
            raise HTTPException(status_code=400, detail="backend_id is required")
        query = query.where(Operation.backend_id == backend_id)
    operation_ids = list((await session.execute(query)).scalars().all())
    operations = [
        await operation_status_payload(operation_id, settings=settings, session=session)
        for operation_id in operation_ids
    ]
    return {"operations": operations}


@router.get("/operations/{operation_id}")
async def operation_status(
    operation_id: int,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict:
    return await operation_status_payload(
        operation_id, settings=settings, session=session
    )


async def operation_status_payload(
    operation_id: int,
    *,
    settings: Settings,
    session: AsyncSession,
) -> dict:
    operation = (
        await session.execute(
            select(Operation)
            .where(Operation.id == operation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="operation not found")
    try:
        details = json.loads(operation.details_json or "{}")
    except json.JSONDecodeError:
        details = {"raw": operation.details_json}
    phase = operation.phase
    status = operation.status
    error = operation.error
    if operation.status in {"queued", "running"}:
        progress_snapshot = read_operation_progress(settings, operation.id)
        if progress_snapshot:
            snapshot_details = progress_snapshot.get("details")
            details = _merge_operation_details(details, snapshot_details)
            phase = str(progress_snapshot.get("phase") or phase)
            status = str(progress_snapshot.get("status") or status)
            error = str(progress_snapshot.get("error") or error or "") or None
    if (
        isinstance(details, dict)
        and operation.kind == "create_backend"
        and operation.status == "running"
    ):
        backend_name = str(details.get("backend_name") or "").strip()
        runtime_progress = create_backend_runtime_progress(settings, backend_name)
        current_progress = details.get("progress")
        runtime_overlay_allowed = not isinstance(current_progress, (int, float)) or (
            current_progress >= CREATE_BACKEND_PROGRESS_POINTS["Plan runtime"]
        )
        if runtime_progress and runtime_overlay_allowed:
            current_progress = details.get("progress")
            runtime_progress_value = runtime_progress.get("progress")
            progress_plan = details.get("progress_plan")
            if isinstance(progress_plan, dict):
                runtime_progress_value = progress_plan_value(
                    progress_plan,
                    phase=str(runtime_progress.get("phase") or ""),
                    message=str(
                        runtime_progress.get("substate")
                        or runtime_progress.get("message")
                        or ""
                    ),
                    current=int(current_progress)
                    if isinstance(current_progress, (int, float))
                    else 0,
                )
            if not isinstance(current_progress, (int, float)) or (
                isinstance(runtime_progress_value, (int, float))
                and runtime_progress_value >= current_progress
            ):
                details = {
                    **details,
                    "progress": runtime_progress_value,
                    "message": runtime_progress["message"],
                    "substate": runtime_progress["substate"]
                    or runtime_progress["message"],
                    "runtime": runtime_progress,
                }
                phase = str(runtime_progress["phase"])
    return {
        "id": operation.id,
        "kind": operation.kind,
        "status": status,
        "phase": phase,
        "actor": operation.actor,
        "backend_id": operation.backend_id,
        "error": error,
        "details": details,
        "started_at": utc_isoformat(operation.started_at),
        "finished_at": utc_isoformat(operation.finished_at),
    }


@router.get("/operations/{operation_id}/events")
async def operation_events(
    operation_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> StreamingResponse:
    async def stream():
        last_payload_json = ""
        last_keepalive_at = 0.0
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await operation_status_payload(
                    operation_id, settings=settings, session=session
                )
            except HTTPException as exc:
                error_payload = {"detail": exc.detail, "status_code": exc.status_code}
                yield f"event: error\ndata: {json.dumps(error_payload, separators=(',', ':'))}\n\n"
                break
            payload_json = json.dumps(payload, separators=(",", ":"))
            now = asyncio.get_running_loop().time()
            if payload_json != last_payload_json:
                yield f"event: operation\ndata: {payload_json}\n\n"
                last_payload_json = payload_json
                last_keepalive_at = now
            elif now - last_keepalive_at >= 15:
                yield ": keepalive\n\n"
                last_keepalive_at = now
            if str(payload.get("status") or "").lower() not in {"queued", "running"}:
                break
            await session.rollback()
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _startup_phase_is_ok(startup: dict[str, object], phase: str) -> bool:
    phase_state = startup.get(phase) if isinstance(startup.get(phase), dict) else {}
    return str(phase_state.get("status") or "").strip() == "ok"


async def _readiness_response(request: Request) -> JSONResponse:
    startup = snapshot_startup_state(request.app)
    db_error = ""
    try:
        await verify_db_connection()
        live_db_ok = True
    except Exception as exc:
        live_db_ok = False
        db_error = str(exc)

    startup_config_ok = _startup_phase_is_ok(startup, "config")
    startup_db_ok = _startup_phase_is_ok(startup, "db")
    ready = live_db_ok and startup_db_ok and startup_config_ok
    payload: dict[str, object] = {
        "status": "ok" if ready else "not_ready",
        "config": "ok" if startup_config_ok else "error",
        "db": "ok" if live_db_ok and startup_db_ok else "error",
        "startup": startup,
    }
    if db_error:
        payload["db_error"] = db_error
    return JSONResponse(payload, status_code=200 if ready else 503)


@probe_router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@probe_router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    return await _readiness_response(request)


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    return await _readiness_response(request)


@router.get("/csrf")
async def csrf(settings: Settings = Depends(settings_dependency)) -> dict[str, str]:
    return {"csrf_token": get_csrf_token(settings)}
