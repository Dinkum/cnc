"""Output resource-profile queries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from app.config import Settings
from app.models.entities import Backend, ControlEvent, Input
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.clone_defaults import next_clone_name, next_clone_port
from app.services.control_events import list_recent_output_events
from app.services.placement_config import clean_node_uid
from app.services.replica_readiness import (
    ReplicaReadinessStatus,
    backend_replica_readiness_statuses,
)
from app.services.resource_profile import (
    ResourceProfile,
    build_resource_profile,
)
from app.services.status_service import peek_cached_status
from app.ui.cluster import cluster_nodes_for_context
from app.ui.outputs.health import runtime_diagnostics_from_status_payload
from app.ui.read_models import latest_successful_apply_at


async def enabled_runtime_backends_for_resource_profile(
    session: AsyncSession,
) -> list[Backend]:
    return list(
        (
            await session.execute(
                select(Backend)
                .options(load_only(Backend.id, Backend.kind, Backend.resource_size))
                .where(Backend.kind == "app", Backend.enabled.is_(True))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class OutputPageData:
    backend: Backend
    last_applied_at: datetime | None
    base_profile: ResourceProfile
    cached_status_payload: dict[str, object] | None
    runtime_diagnostics: dict[str, object] | None
    shield_backends: list[Backend]
    shield_inputs: list[Input]
    total_input_count: int
    recent_events: list[ControlEvent]
    clone_defaults: dict[str, str | int | None]
    multi_node_app_enabled: bool
    cluster_nodes: list[dict[str, str]]
    placement_backends: list[Backend]
    placement_readiness: dict[str, ReplicaReadinessStatus]


async def load_output_page_data(
    session: AsyncSession,
    settings: Settings,
    backend_id: int,
    *,
    prefer_cached_runtime: bool = False,
) -> OutputPageData:
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        raise LookupError("backend not found")
    last_applied_at = await latest_successful_apply_at(session)
    base_profile = build_resource_profile(
        settings, await enabled_runtime_backends_for_resource_profile(session)
    )

    cached_status_payload = peek_cached_status() if prefer_cached_runtime else None
    runtime_diagnostics: dict[str, object] | None = None
    if backend.kind == "app":
        if prefer_cached_runtime:
            runtime_diagnostics = runtime_diagnostics_from_status_payload(
                cached_status_payload,
                backend.name,
                kind=backend.kind,
            )
        if runtime_diagnostics is None and not prefer_cached_runtime:
            runtime_diagnostics = await asyncio.to_thread(
                collect_app_backend_diagnostics, backend, settings
            )

    shield_backends = list(
        (
            await session.execute(
                select(Backend).where(Backend.kind == "shield").limit(1)
            )
        )
        .scalars()
        .all()
    )
    shield_inputs = (
        list(
            (
                await session.execute(
                    select(Input)
                    .options(selectinload(Input.backends))
                    .where(Input.kind == "shield")
                    .order_by(Input.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if settings.shield_enabled or backend.kind == "shield"
        else []
    )
    total_input_count = int(
        (await session.execute(select(func.count(Input.id)))).scalar_one() or 0
    )

    recent_events = await list_recent_output_events(
        session,
        backend_name=backend.name,
        since=backend.created_at,
        limit=12,
    )

    clone_defaults = {
        "name": await next_clone_name(session, backend.name),
        "port": await next_clone_port(session, backend.port, settings)
        if backend.kind == "app"
        else None,
    }

    multi_node_app_enabled = settings.multi_node_enabled and backend.kind == "app"
    cluster_nodes = (
        await cluster_nodes_for_context(session, settings)
        if multi_node_app_enabled
        else []
    )
    placement_backends = (
        list(
            (
                await session.execute(
                    select(Backend)
                    .where(Backend.kind == "app", Backend.id != backend.id)
                    .order_by(Backend.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if multi_node_app_enabled
        else []
    )
    placement_readiness = (
        await backend_replica_readiness_statuses(
            session,
            settings,
            backend=backend,
            node_uids=tuple(
                clean_node_uid(node.get("id", "")) for node in cluster_nodes
            ),
        )
        if multi_node_app_enabled
        else {}
    )

    return OutputPageData(
        backend=backend,
        last_applied_at=last_applied_at,
        base_profile=base_profile,
        cached_status_payload=cached_status_payload,
        runtime_diagnostics=runtime_diagnostics,
        shield_backends=shield_backends,
        shield_inputs=shield_inputs,
        total_input_count=total_input_count,
        recent_events=recent_events,
        clone_defaults=clone_defaults,
        multi_node_app_enabled=multi_node_app_enabled,
        cluster_nodes=cluster_nodes,
        placement_backends=placement_backends,
        placement_readiness=placement_readiness,
    )
