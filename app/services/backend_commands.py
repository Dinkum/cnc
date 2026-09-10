from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.services.clone_defaults import next_clone_name, next_clone_port
from app.models.entities import Backend, Input
from app.schemas.backends import BackendCloneIn, BackendIn, BackendUpdate
from app.services.backend_backup_service import (
    BackupProgressCallback,
    clone_backend_as_new_backend,
)
from app.services.netdata_constants import NETDATA_PORT
from app.services.port_preflight import ensure_loopback_port_free
from app.services.queries import load_inputs_by_ids
from app.services.validators import (
    ValidationError,
    validate_backend_collection,
    validate_backend_shape,
    validate_input_bindings,
)


async def validate_config_graph(session: AsyncSession) -> None:
    backends = list(
        (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    inputs = list(
        (
            await session.execute(
                select(Input)
                .options(selectinload(Input.backends))
                .order_by(Input.id.asc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    validate_backend_collection(backends)
    validate_input_bindings(inputs)


async def load_backend(session: AsyncSession, backend_id: int) -> Backend | None:
    return (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()


async def require_backend(session: AsyncSession, backend_id: int) -> Backend:
    backend = await load_backend(session, backend_id)
    if backend is None:
        raise LookupError("backend not found")
    return backend


async def load_inputs(session: AsyncSession, input_ids: list[int]) -> list[Input]:
    inputs, missing = await load_inputs_by_ids(session, input_ids)
    if missing:
        raise LookupError(f"input not found: {missing[0]}")
    return inputs


def backend_from_payload(payload: BackendIn) -> Backend:
    return Backend(
        name=payload.name,
        kind=payload.kind,
        port=payload.port,
        static_root=payload.static_root,
        sandbox_profile=payload.sandbox_profile,
        handoff_port=payload.handoff_port,
        healthcheck_mode=payload.healthcheck_mode,
        healthcheck_path=payload.healthcheck_path,
        healthcheck_host_header=payload.healthcheck_host_header,
        resource_mode=payload.resource_mode,
        resource_size=payload.resource_size,
        memory_high_override=payload.memory_high_override,
        memory_max_override=payload.memory_max_override,
        cpu_quota_override=payload.cpu_quota_override,
        shield_enabled=payload.shield_enabled,
        shield_code_hash=payload.shield_code_hash,
        shield_access_code=payload.shield_access_code,
        volumes_json=payload.volumes_json,
        enabled=payload.enabled,
        notes=payload.notes,
    )


async def create_backend(
    session: AsyncSession,
    payload: BackendIn,
    *,
    commit: bool = True,
) -> Backend:
    backend = backend_from_payload(payload)
    backend.inputs = await load_inputs(session, payload.input_ids)
    validate_backend_shape(backend)
    if backend.kind == "app" and backend.port is not None:
        ensure_loopback_port_free(
            backend.port, field_name=f"backend {backend.name} port"
        )
    session.add(backend)
    if commit:
        await validate_config_graph(session)
        await session.commit()
        await session.refresh(backend)
        backend = await require_backend(session, backend.id)
    return backend


async def update_backend(
    session: AsyncSession,
    backend_id: int,
    payload: BackendUpdate,
    *,
    commit: bool = True,
) -> Backend:
    backend = await require_backend(session, backend_id)
    previous_port = backend.port
    values = payload.model_dump(exclude_unset=True)
    if "kind" in values:
        requested_kind = values.pop("kind")
        if requested_kind is not None and requested_kind != backend.kind:
            raise ValidationError("output type is locked after creation")
    if "input_ids" in values:
        backend.inputs = await load_inputs(session, values.pop("input_ids") or [])
    for key, value in values.items():
        setattr(backend, key, value)
    validate_backend_shape(backend)
    if (
        backend.kind == "app"
        and backend.port is not None
        and backend.port != previous_port
    ):
        ensure_loopback_port_free(
            backend.port, field_name=f"backend {backend.name} port"
        )
    if commit:
        await validate_config_graph(session)
        await session.commit()
        await session.refresh(backend)
        backend = await require_backend(session, backend.id)
    return backend


async def set_backend_inputs(
    session: AsyncSession,
    backend_id: int,
    input_ids: list[int],
    *,
    commit: bool = True,
) -> Backend:
    return await update_backend(
        session, backend_id, BackendUpdate(input_ids=input_ids), commit=commit
    )


async def set_backend_enabled(
    session: AsyncSession,
    backend_id: int,
    enabled: bool,
    *,
    commit: bool = True,
) -> Backend:
    return await update_backend(
        session, backend_id, BackendUpdate(enabled=enabled), commit=commit
    )


async def clone_backend(
    session: AsyncSession,
    backend_id: int,
    payload: BackendCloneIn,
    settings: Settings,
    *,
    progress_callback: BackupProgressCallback | None = None,
) -> dict[str, object]:
    backend = await require_backend(session, backend_id)
    clone_name = payload.name or await next_clone_name(session, backend.name)
    clone_port = None
    if backend.kind == "app":
        clone_port = payload.port
        if clone_port is None:
            clone_port = await next_clone_port(session, backend.port, settings)
        if clone_port is None:
            raise ValidationError(
                f"no free app ports in range {settings.port_range_start}-{settings.port_range_end}"
            )
    if backend.kind == "app" and clone_port == NETDATA_PORT:
        raise ValidationError(
            f"clone {clone_name} port uses reserved host port: {clone_port}"
        )
    if backend.kind == "app" and clone_port is not None:
        ensure_loopback_port_free(clone_port, field_name=f"clone {clone_name} port")
    result = await clone_backend_as_new_backend(
        session,
        backend,
        settings,
        backend_name=clone_name,
        target_port=clone_port,
        progress_callback=progress_callback,
    )
    return result


__all__ = [
    "ValidationError",
    "backend_from_payload",
    "clone_backend",
    "create_backend",
    "load_backend",
    "load_inputs",
    "require_backend",
    "set_backend_enabled",
    "set_backend_inputs",
    "update_backend",
]
