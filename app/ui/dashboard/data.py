"""Tab-scoped dashboard queries and status freshness policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models.entities import Backend, Input
from app.services.resource_profile import ResourceProfile, build_resource_profile
from app.services.status_service import (
    collect_status,
    peek_cached_status,
)
from app.ui.cluster import cluster_nodes_for_context
from app.ui.read_models import (
    DashboardRows,
    DashboardScope,
    dashboard_scope,
    dashboard_status_fallback,
    latest_successful_apply_at,
)
from app.ui.view_models import app_version, stylesheet_version


async def load_dashboard_rows(
    session: AsyncSession, *, scope: DashboardScope
) -> DashboardRows:
    backend_query = select(Backend).order_by(Backend.id.asc())
    input_query = select(Input).order_by(Input.id.asc())
    if scope.needs_visible_entities:
        backend_query = backend_query.options(selectinload(Backend.inputs))
    if scope.includes("home", "inputs", "routing", "outputs", "settings"):
        input_query = input_query.options(selectinload(Input.backends))
    backends = list((await session.execute(backend_query)).scalars().all())
    inputs = list((await session.execute(input_query)).scalars().all())
    last_applied_at = await latest_successful_apply_at(session)
    return DashboardRows(
        backends=backends,
        inputs=inputs,
        last_applied_at=last_applied_at,
    )


def empty_dashboard_status() -> dict[str, object]:
    return dashboard_status_fallback(app_version())


async def dashboard_status(
    session: AsyncSession,
    settings: Settings,
    *,
    scope: DashboardScope,
    prefer_cached_status: bool,
    defer_status: bool = False,
) -> dict[str, object]:
    if defer_status:
        return peek_cached_status() or empty_dashboard_status()
    if scope.needs_status:
        if prefer_cached_status:
            if scope.active_tab == "home":
                return peek_cached_status() or empty_dashboard_status()
            return await collect_status(session, settings, allow_stale=True)
        return await collect_status(session, settings)
    return peek_cached_status() or empty_dashboard_status()


@dataclass(frozen=True)
class DashboardData:
    scope: DashboardScope
    rows: DashboardRows
    status: dict[str, object]
    cluster_nodes: list[dict[str, str]]
    resource_profile: ResourceProfile
    app_version: str
    asset_version: str


async def load_dashboard_data(
    session: AsyncSession,
    settings: Settings,
    *,
    prefer_cached_status: bool = False,
    active_tab: str | None = None,
    defer_status: bool = False,
) -> DashboardData:
    scope = dashboard_scope(active_tab)
    rows = await load_dashboard_rows(session, scope=scope)
    status = await dashboard_status(
        session,
        settings,
        scope=scope,
        prefer_cached_status=prefer_cached_status,
        defer_status=defer_status,
    )
    cluster_nodes = (
        await cluster_nodes_for_context(session, settings)
        if scope.needs_settings
        else []
    )
    resource_profile = build_resource_profile(
        settings,
        [
            backend
            for backend in rows.backends
            if backend.kind == "app" and backend.enabled
        ],
    )

    return DashboardData(
        scope=scope,
        rows=rows,
        status=status,
        cluster_nodes=cluster_nodes,
        resource_profile=resource_profile,
        app_version=app_version(),
        asset_version=stylesheet_version(settings),
    )
