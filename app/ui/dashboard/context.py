"""Assemble dashboard and host-help contexts from feature data and presentation."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.resource_profile import build_resource_profile
from app.services.status_service import peek_cached_status
from app.ui.dashboard.data import (
    empty_dashboard_status,
    load_dashboard_data,
    load_dashboard_rows,
)
from app.ui.dashboard.presentation import build_dashboard_context, build_service_cards
from app.ui.outputs.presentation import build_output_details
from app.ui.read_models import dashboard_scope
from app.ui.routing import build_backend_input_maps


async def dashboard_context(
    session: AsyncSession,
    settings: Settings,
    *,
    prefer_cached_status: bool = False,
    active_tab: str | None = None,
    defer_status: bool = False,
) -> dict[str, object]:
    data = await load_dashboard_data(
        session,
        settings,
        prefer_cached_status=prefer_cached_status,
        active_tab=active_tab,
        defer_status=defer_status,
    )
    return build_dashboard_context(
        settings,
        scope=data.scope,
        rows=data.rows,
        status=data.status,
        cluster_nodes=data.cluster_nodes,
        resource_profile=data.resource_profile,
        current_version=data.app_version,
        asset_version=data.asset_version,
    )


async def cached_dashboard_context(
    session: AsyncSession,
    settings: Settings,
    *,
    active_tab: str,
) -> dict[str, object]:
    return await dashboard_context(
        session,
        settings,
        prefer_cached_status=True,
        active_tab=active_tab,
        defer_status=True,
    )


async def host_llm_help_context(
    session: AsyncSession,
    settings: Settings,
) -> dict[str, object]:
    """Build only the output state used by the host-level LLM help document."""
    rows = await load_dashboard_rows(session, scope=dashboard_scope("outputs"))
    status = peek_cached_status() or empty_dashboard_status()
    service_map, _services_ok = build_service_cards(status.get("services", []))
    backend_input_ids, backend_input_labels = build_backend_input_maps(rows.inputs)
    resource_profile = build_resource_profile(
        settings,
        [
            backend
            for backend in rows.backends
            if backend.kind == "app" and backend.enabled
        ],
    )
    output_details, _pending_backends = build_output_details(
        backends=rows.backends,
        service_map=service_map,
        backend_input_ids=backend_input_ids,
        backend_input_labels=backend_input_labels,
        last_applied_at=rows.last_applied_at,
        base_profile=resource_profile,
    )
    return {"backends": rows.backends, "output_details": output_details}
