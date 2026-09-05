from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from typing import Any, Iterable

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.models.entities import ControlEvent
from app.services.notifications import (
    admin_dashboard_url,
    send_pushover_notification_async,
)
from app.services.history_retention import prune_control_plane_history


logger = get_logger("control.events")
_SEVERITY_LEVELS = {
    "info": "info",
    "success": "info",
    "warn": "warning",
    "error": "error",
}
_PUSH_SUCCESS_EVENTS = {
    "auto_size_resized",
    "restore_completed",
    "update_completed",
}
_OUTPUT_TIMELINE_HOST_EVENT_KINDS = frozenset(
    {
        "apply_completed",
        "apply_failed",
        "auto_size_resized",
    }
)
_OUTPUT_TIMELINE_HOST_EVENT_LIMIT = 4


def _event_title(kind: str, summary: str) -> str:
    titles = {
        "auto_size_resized": "CNC auto-size resized outputs",
        "restore_completed": "CNC restore completed",
        "update_completed": "CNC update completed",
    }
    return titles.get(kind, f"CNC {summary}".strip())


def _event_display_title(kind: str, summary: str) -> str:
    titles = {
        "auto_size_resized": "Auto-size resized outputs",
        "restore_completed": "Restore completed",
        "update_completed": "Update completed",
    }
    return titles.get(kind, str(summary or "").strip() or kind.replace("_", " "))


def _should_notify_control_event(
    *,
    kind: str,
    severity: str,
    scope: str,
    notify: bool | None,
) -> bool:
    if notify is not None:
        return notify
    if kind in _PUSH_SUCCESS_EVENTS:
        return True
    return scope == "host" and severity in {"warn", "error"}


async def _notify_control_event(
    settings: Settings,
    *,
    kind: str,
    source: str,
    summary: str,
    severity: str,
    scope: str,
    backend_name: str | None,
    affects_all: bool,
    related_backends: list[str],
    subevents: list[dict[str, str]],
    details: dict[str, Any],
    event_time: datetime,
    notify: bool | None,
) -> dict[str, Any]:
    if not _should_notify_control_event(
        kind=kind, severity=severity, scope=scope, notify=notify
    ):
        return {"sent": False, "event": kind, "reason": "policy_skip"}
    run_id = details.get("run_id")
    sent = await send_pushover_notification_async(
        settings,
        title=_event_title(kind, summary),
        message=_render_event_notification_message(
            kind=kind,
            source=source,
            summary=summary,
            severity=severity,
            scope=scope,
            backend_name=backend_name,
            affects_all=affects_all,
            related_backends=related_backends,
            subevents=subevents,
            details=details,
            event_time=event_time,
        ),
        priority=0,
        event=kind,
        source=source,
        backend=backend_name or "",
        run_id=run_id if isinstance(run_id, int) else None,
        url=admin_dashboard_url(
            settings,
            path=f"/outputs/{details.get('backend_id')}"
            if isinstance(details.get("backend_id"), int)
            else "/",
            tab=None
            if isinstance(details.get("backend_id"), int)
            else ("outputs" if scope == "backend" else "home"),
        ),
        url_title="Open CNC",
    )
    return {"sent": sent, "event": kind, "reason": "policy_match"}


def _render_event_notification_message(
    *,
    kind: str,
    source: str,
    summary: str,
    severity: str,
    scope: str,
    backend_name: str | None,
    affects_all: bool,
    related_backends: list[str],
    subevents: list[dict[str, str]],
    details: dict[str, Any],
    event_time: datetime,
) -> str:
    timestamp = (
        event_time.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    lines = [
        f"time: {timestamp}",
        f"title: {_event_display_title(kind, summary)}",
        f"status: {severity or 'info'}",
    ]
    if subevents:
        lines.append("values:")
        for item in subevents[:5]:
            lines.append(f"  {item['label']}: {item['value']}")
    else:
        lines.append("values: none")
    detail_payload = {
        "kind": kind,
        "scope": scope,
        "source": source,
        "backend": backend_name,
        "affects_all": bool(affects_all),
        "related_backends": related_backends,
        "details": details,
    }
    lines.append(
        f"details: {json.dumps(detail_payload, sort_keys=True, separators=(',', ':'), default=str)}"
    )
    return "\n".join(lines)


@asynccontextmanager
async def _isolated_event_session(database_url: str):
    engine = create_configured_async_engine(database_url, future=True, echo=False)
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _normalize_string_list(values: Iterable[object] | None) -> list[str]:
    if values is None:
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _normalize_subevents(
    rows: Iterable[dict[str, object]] | None,
) -> list[dict[str, str]]:
    if rows is None:
        return []
    normalized: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label or not value:
            continue
        normalized.append({"label": label, "value": value})
    return normalized


def _log_control_event(
    *,
    kind: str,
    scope: str,
    severity: str,
    source: str,
    summary: str,
    backend_name: str | None,
    affects_all: bool,
    related_backends: list[str],
    subevents: list[dict[str, str]],
    details: dict[str, Any],
) -> None:
    log_method = getattr(logger, _SEVERITY_LEVELS.get(severity, "info"))
    log_method(
        "control_event.recorded",
        kind=kind,
        scope=scope,
        severity=severity,
        source=source,
        summary=summary,
        backend=backend_name,
        affects_all=affects_all,
        related_backends=related_backends,
        subevents=subevents,
        details=details,
    )


async def emit_control_event(
    settings: Settings,
    *,
    kind: str,
    source: str,
    summary: str,
    severity: str = "info",
    scope: str = "host",
    backend_name: str | None = None,
    affects_all: bool = False,
    related_backends: Iterable[object] | None = None,
    subevents: Iterable[dict[str, object]] | None = None,
    details: dict[str, Any] | None = None,
    notify: bool | None = None,
) -> dict[str, Any]:
    normalized_related_backends = _normalize_string_list(related_backends)
    normalized_subevents = _normalize_subevents(subevents)
    normalized_details = details if isinstance(details, dict) else {}
    event_time = datetime.now(UTC)
    _log_control_event(
        kind=kind,
        scope=scope,
        severity=severity,
        source=source,
        summary=summary,
        backend_name=backend_name,
        affects_all=affects_all,
        related_backends=normalized_related_backends,
        subevents=normalized_subevents,
        details=normalized_details,
    )
    try:
        async with _isolated_event_session(settings.database_url) as session:
            session.add(
                ControlEvent(
                    kind=kind,
                    scope=scope,
                    severity=severity,
                    source=source,
                    backend_name=backend_name,
                    affects_all=affects_all,
                    related_backends_json=json.dumps(normalized_related_backends),
                    summary=summary,
                    details_json=json.dumps(normalized_details),
                    subevents_json=json.dumps(normalized_subevents),
                    created_at=event_time,
                )
            )
            await session.flush()
            await prune_control_plane_history(session)
            await session.commit()
    except Exception as exc:
        logger.warning(
            "control_event.persist_failed",
            kind=kind,
            scope=scope,
            source=source,
            summary=summary,
            backend=backend_name,
            error=str(exc),
        )
    return await _notify_control_event(
        settings,
        kind=kind,
        source=source,
        summary=summary,
        severity=severity,
        scope=scope,
        backend_name=backend_name,
        affects_all=affects_all,
        related_backends=normalized_related_backends,
        subevents=normalized_subevents,
        details=normalized_details,
        event_time=event_time,
        notify=notify,
    )


def event_applies_to_backend(event: ControlEvent, backend_name: str) -> bool:
    normalized_backend = str(backend_name or "").strip()
    if not normalized_backend:
        return False
    if str(event.backend_name or "").strip() == normalized_backend:
        return True
    if str(event.scope or "").strip() != "host":
        return False
    if bool(event.affects_all):
        return True
    return normalized_backend in event.related_backends


def _related_backend_exists(backend_name: str):
    valid_related_backends = case(
        (
            func.json_valid(ControlEvent.related_backends_json),
            ControlEvent.related_backends_json,
        ),
        else_="[]",
    )
    related_backend = (
        func.json_each(valid_related_backends)
        .table_valued("key", "value")
        .alias("related_backend")
    )
    return exists(
        select(1)
        .select_from(related_backend)
        .where(related_backend.c.value == backend_name)
    )


async def list_recent_control_events(
    session: AsyncSession,
    *,
    backend_name: str | None = None,
    since: datetime | None = None,
    limit: int = 12,
) -> list[ControlEvent]:
    effective_limit = max(1, limit)
    query = select(ControlEvent)
    if since is not None:
        query = query.where(ControlEvent.created_at >= since)
    if backend_name:
        normalized_backend = str(backend_name).strip()
        if not normalized_backend:
            return []
        query = query.where(
            or_(
                ControlEvent.backend_name == normalized_backend,
                and_(
                    ControlEvent.scope == "host",
                    or_(
                        ControlEvent.affects_all.is_(True),
                        _related_backend_exists(normalized_backend),
                    ),
                ),
            )
        )
    query = query.order_by(ControlEvent.id.desc()).limit(effective_limit)
    return list((await session.execute(query)).scalars())


async def list_recent_output_events(
    session: AsyncSession,
    *,
    backend_name: str,
    since: datetime | None = None,
    limit: int = 12,
) -> list[ControlEvent]:
    """Return a bounded output timeline without letting host chatter starve it."""
    normalized_backend = str(backend_name or "").strip()
    if not normalized_backend:
        return []
    effective_limit = max(1, limit)
    common_filters = [ControlEvent.created_at >= since] if since is not None else []
    direct_query = (
        select(ControlEvent)
        .where(
            *common_filters,
            ControlEvent.backend_name == normalized_backend,
        )
        .order_by(ControlEvent.id.desc())
        .limit(effective_limit)
    )
    related_query = (
        select(ControlEvent)
        .where(
            *common_filters,
            ControlEvent.scope == "host",
            ControlEvent.affects_all.is_(False),
            _related_backend_exists(normalized_backend),
        )
        .order_by(ControlEvent.id.desc())
        .limit(effective_limit)
    )
    host_limit = min(_OUTPUT_TIMELINE_HOST_EVENT_LIMIT, effective_limit)
    selected_host_query = (
        select(ControlEvent)
        .where(
            *common_filters,
            ControlEvent.scope == "host",
            ControlEvent.affects_all.is_(True),
            ControlEvent.kind.in_(_OUTPUT_TIMELINE_HOST_EVENT_KINDS),
            or_(
                ControlEvent.backend_name.is_(None),
                ControlEvent.backend_name != normalized_backend,
            ),
        )
        .order_by(ControlEvent.id.desc())
        .limit(host_limit)
    )
    direct_rows = list((await session.execute(direct_query)).scalars())
    related_rows = list((await session.execute(related_query)).scalars())
    selected_host_rows = list((await session.execute(selected_host_query)).scalars())
    host_ids = {row.id for row in selected_host_rows}
    merged = {row.id: row for row in [*direct_rows, *related_rows, *selected_host_rows]}
    rows: list[ControlEvent] = []
    host_rows_used = 0
    for row in sorted(merged.values(), key=lambda item: item.id, reverse=True):
        if row.id in host_ids:
            if host_rows_used >= host_limit:
                continue
            host_rows_used += 1
        rows.append(row)
        if len(rows) >= effective_limit:
            break
    return rows
