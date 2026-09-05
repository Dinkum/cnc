from __future__ import annotations

import asyncio
import json

from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.logger import flush_logging_pipeline
from app.models.entities import (
    Backend,
    Input,
)
from app.services.app_hardening import latest_hardening_summary
from app.services.backend_backup_service import (
    describe_backend_backup,
    latest_successful_backend_backup,
    list_backend_backups,
)
from app.services.backend_metric_history import (
    DEFAULT_METRIC_KEY,
    DEFAULT_TIMEFRAME_KEY,
    build_backend_metric_history,
    build_host_metric_history,
)
from app.services.debug_bundle import (
    build_error_debug_bundle,
    build_support_debug_bundle,
)
from app.services.error_reporting import ErrorCode
from app.services.resource_profile import build_resource_profile
from app.services.status_service import peek_cached_status
from app.ui.errors import (
    operator_error_json as _operator_error_json,
    operator_error_payload as _operator_error_payload,
)
from app.ui.view_models import (
    _backup_summary,
    _pending_backup_history_rows,
)
from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from fastapi.responses import (
    JSONResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ui.routes.shared import (
    _build_input_attach_options,
    _build_output_detail,
    _cluster_nodes,
    _collect_output_runtime_diagnostics_for_ui,
    _enabled_runtime_backends_for_resource_profile,
    _format_metric_data_size,
    _input_kind,
    _input_kind_label,
    _input_value,
    _memory_limit_bytes_for_profile,
    _memory_soft_limit_percent_for_profile,
    _output_signal_cards,
    _planned_backup_coverage_summary,
    _service_metrics_from_status_payload,
    RuntimeDiagnosticsPending,
)


router = APIRouter(tags=["ui"])


@router.get("/api/backends/{backend_id}/input-attach-options")
async def output_input_attach_options(
    backend_id: int,
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object]:
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return {
            **_operator_error_payload(
                "output not found",
                ErrorCode.UI_RESOURCE_NOT_FOUND,
            ),
            "options": [],
            "visible_count": 0,
        }
    inputs = list(
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
    options = _build_input_attach_options(inputs, current_backend_id=backend.id)
    return {
        "options": options,
        "visible_count": sum(1 for item in options if not item["attached"]),
    }


@router.get("/api/backends/{backend_id}/backup-signals", response_model=None)
async def output_backup_signals(
    backend_id: int,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object] | JSONResponse:
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )
    latest_backup = await latest_successful_backend_backup(session, backend_id)
    backup_history = await list_backend_backups(session, backend_id, limit=10)
    coverage_by_id: dict[int, dict[str, object]] = {}
    if latest_backup is not None and latest_backup.bundle_path:
        coverage_by_id[latest_backup.id] = await describe_backend_backup(
            latest_backup, current_backend=backend
        )
    history_rows = _pending_backup_history_rows(backup_history)
    for row in history_rows:
        coverage = coverage_by_id.get(row["id"])
        if coverage is not None:
            row["covered_paths_summary"] = str(
                coverage.get("covered_paths_summary") or "-"
            )
    return {
        "backup_summary_rows": _backup_summary(
            latest_backup,
            coverage_by_id.get(latest_backup.id) if latest_backup else None,
            planned_coverage=_planned_backup_coverage_summary(backend, settings),
        )["rows"],
        "backup_history": history_rows,
    }


@router.get("/api/backends/{backend_id}/runtime-signals", response_model=None)
async def output_runtime_signals(
    backend_id: int,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object] | JSONResponse:
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

    enabled_runtime_backends = (
        [backend] if backend.kind == "app" and backend.enabled else []
    )
    base_profile = build_resource_profile(settings, enabled_runtime_backends)
    attached_input_labels = [
        f"{_input_kind_label(_input_kind(item))}: {_input_value(item)}"
        for item in sorted(backend.inputs, key=lambda item: item.id)
    ]
    try:
        runtime_diagnostics = await _collect_output_runtime_diagnostics_for_ui(
            backend, settings
        )
    except RuntimeDiagnosticsPending:
        return JSONResponse(
            {
                "pending": True,
                "retry_after_ms": 750,
                "runtime_health_label": "Loading",
                "runtime_health_tone": "queued",
                "runtime_health_summary": "Runtime inspection is still running.",
                "output_signal_cards": [],
                "runtime_alert": None,
            },
            status_code=202,
            headers={"Retry-After": "1"},
        )
    detail = _build_output_detail(
        backend=backend,
        service_map={},
        attached_input_ids=backend.input_ids,
        attached_input_labels=attached_input_labels,
        last_applied_at=None,
        base_profile=base_profile,
        runtime_diagnostics=runtime_diagnostics,
    )
    return {
        "runtime_health_label": detail.get("runtime_health_label"),
        "runtime_health_tone": detail.get("runtime_health_tone"),
        "runtime_health_summary": detail.get("runtime_health_summary"),
        "runtime_health_detail": detail.get("runtime_health_detail"),
        "runtime_state_detail": detail.get("runtime_state_detail"),
        "output_signal_cards": _output_signal_cards(detail),
        "runtime_alert": detail.get("runtime_health_alert"),
    }


@router.get("/api/backends/{backend_id}/metrics-history", response_model=None)
async def output_metric_history(
    backend_id: int,
    metric: str = DEFAULT_METRIC_KEY,
    timeframe: str = DEFAULT_TIMEFRAME_KEY,
    width: int | None = None,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object] | JSONResponse:
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return _operator_error_json(
            "output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )

    status_payload = peek_cached_status() or {}
    live_metrics = _service_metrics_from_status_payload(status_payload, backend)
    payload = await build_backend_metric_history(
        session,
        backend,
        metric_key=metric,
        timeframe_key=timeframe,
        chart_width_px=width,
        live_metrics=live_metrics,
    )
    metric_key = (
        payload.get("metric", {}).get("key")
        if isinstance(payload.get("metric"), dict)
        else None
    )
    if metric_key == "memory":
        soft_limit_value = None
        if backend.kind == "app":
            enabled_runtime_backends = (
                await _enabled_runtime_backends_for_resource_profile(session)
            )
            resource_profile = build_resource_profile(
                settings, enabled_runtime_backends
            )
            memory_limits = _memory_limit_bytes_for_profile(backend, resource_profile)
            soft_limit_value = _memory_soft_limit_percent_for_profile(
                backend, resource_profile
            )
        elif backend.kind == "shield" and isinstance(live_metrics, dict):
            live_memory_max = live_metrics.get("memory_max_bytes")
            memory_limits = {
                "soft": None,
                "hard": live_memory_max if isinstance(live_memory_max, int) else None,
            }
        else:
            memory_limits = {"soft": None, "hard": None}
        payload["memory_limit_bytes"] = memory_limits["hard"]
        payload["soft_limit_bytes"] = memory_limits["soft"]
        payload["soft_limit_value"] = soft_limit_value
        payload["soft_limit_label"] = (
            f"SOFT LIMIT ({_format_metric_data_size(memory_limits['soft'])})"
        )
    return payload


@router.get("/api/host/metrics-history")
async def host_metric_history(
    metric: str = DEFAULT_METRIC_KEY,
    timeframe: str = DEFAULT_TIMEFRAME_KEY,
    width: int | None = None,
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object]:
    status_payload = peek_cached_status() or {}
    live_metrics = (
        status_payload.get("host_metrics")
        if isinstance(status_payload.get("host_metrics"), dict)
        else None
    )
    return await build_host_metric_history(
        session,
        metric_key=metric,
        timeframe_key=timeframe,
        chart_width_px=width,
        live_metrics=live_metrics,
    )


@router.get("/api/backends/{backend_id}/hardening")
async def output_hardening_status(
    backend_id: int,
    session: AsyncSession = Depends(db_session_dependency),
) -> dict[str, object]:
    backend = (
        await session.execute(select(Backend.id).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None:
        return {
            "error": "output not found",
            "phase1": None,
            "phase2": None,
            "features": [],
        }
    return await latest_hardening_summary(session, backend_id)


@router.get("/api/support/errors/{error_inst}/bundle.zip")
async def download_error_debug_bundle(
    error_inst: str,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    flush_logging_pipeline()
    try:
        bundle = await build_error_debug_bundle(
            session,
            settings,
            error_inst=error_inst,
        )
    except ValueError as exc:
        return _operator_error_json(
            str(exc),
            ErrorCode.UI_DEBUG_BUNDLE_REQUEST_INVALID,
            status_code=400,
        )
    return Response(
        bundle.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/support/bundle.zip")
async def download_support_debug_bundle(
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    flush_logging_pipeline()
    bundle = await build_support_debug_bundle(session, settings)
    return Response(
        bundle.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/backends/{backend_id}/ssh-key")
async def download_backend_ssh_key(
    backend_id: int,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> Response:
    backend = (
        await session.execute(select(Backend).where(Backend.id == backend_id))
    ).scalar_one_or_none()
    if backend is None or backend.kind != "app":
        return _operator_error_json(
            "app output not found",
            ErrorCode.UI_RESOURCE_NOT_FOUND,
            status_code=404,
        )
    private_key = str(backend.ssh_private_key or "")
    if not private_key:
        return _operator_error_json(
            "ssh key has not been provisioned",
            ErrorCode.UI_ACTION_UNAVAILABLE,
            status_code=409,
        )
    return _backend_ssh_key_download_response(str(backend.name), private_key)


def _backend_ssh_key_download_response(backend_name: str, private_key: str) -> Response:
    filename = f"cnc-{backend_name}-id_ed25519"
    return Response(
        private_key,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/backends/{backend_id}/hardening/phase2/events")
async def output_hardening_phase2_events(
    backend_id: int,
    request: Request,
    session: AsyncSession = Depends(db_session_dependency),
) -> StreamingResponse:
    async def stream():
        last_payload_json = ""
        last_keepalive_at = 0.0
        while True:
            if await request.is_disconnected():
                break
            backend = (
                await session.execute(
                    select(Backend.id).where(Backend.id == backend_id)
                )
            ).scalar_one_or_none()
            if backend is None:
                yield 'event: error\ndata: {"error":"output not found"}\n\n'
                break
            payload = await latest_hardening_summary(session, backend_id)
            payload_json = json.dumps(payload, separators=(",", ":"), default=str)
            now = asyncio.get_running_loop().time()
            if payload_json != last_payload_json:
                yield f"event: hardening\ndata: {payload_json}\n\n"
                last_payload_json = payload_json
                last_keepalive_at = now
            elif now - last_keepalive_at >= 15:
                yield ": keepalive\n\n"
                last_keepalive_at = now
            phase2 = (
                payload.get("phase2") if isinstance(payload.get("phase2"), dict) else {}
            )
            if str(phase2.get("status") or "").lower() not in {"queued", "running"}:
                break
            await session.rollback()
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ui/settings/nodes/data")
async def cluster_node_data(
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    if not settings.multi_node_enabled:
        return JSONResponse(
            status_code=400, content={"detail": "multi node mode is disabled"}
        )
    return JSONResponse({"nodes": await _cluster_nodes(session, settings)})
