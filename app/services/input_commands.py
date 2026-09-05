from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entities import Backend, Input
from app.schemas.inputs import InputIn, InputUpdate
from app.services.backend_commands import validate_config_graph
from app.services.queries import load_backends_by_ids
from app.services.validators import ValidationError, ensure_input_value


async def load_input(session: AsyncSession, input_id: int) -> Input | None:
    return (
        await session.execute(
            select(Input)
            .options(selectinload(Input.backends))
            .where(Input.id == input_id)
        )
    ).scalar_one_or_none()


async def require_input(session: AsyncSession, input_id: int) -> Input:
    item = await load_input(session, input_id)
    if item is None:
        raise LookupError("input not found")
    return item


async def load_backends(session: AsyncSession, backend_ids: list[int]) -> list[Backend]:
    backends, missing = await load_backends_by_ids(session, backend_ids)
    if missing:
        raise LookupError(f"backend not found: {missing[0]}")
    return backends


async def create_input(
    session: AsyncSession,
    payload: InputIn,
    *,
    commit: bool = True,
) -> Input:
    value = ensure_input_value(payload.kind, payload.value)
    item = Input(
        kind=payload.kind,
        hostname=value,
        enabled=payload.enabled,
        shield_enabled=payload.shield_enabled,
        shield_code_hash=payload.shield_code_hash,
        shield_access_code=payload.shield_access_code,
    )
    item.backends = await load_backends(session, payload.backend_ids)
    session.add(item)
    if commit:
        await validate_config_graph(session)
        await session.commit()
        await session.refresh(item)
        item = await require_input(session, item.id)
    return item


async def update_input(
    session: AsyncSession,
    input_id: int,
    payload: InputUpdate,
    *,
    commit: bool = True,
) -> Input:
    item = await require_input(session, input_id)
    values = payload.model_dump(exclude_unset=True)
    next_kind = str(values.get("kind") or item.kind or "domain")
    if "value" in values or "kind" in values:
        item.hostname = ensure_input_value(
            next_kind, str(values.pop("value", item.hostname))
        )
        item.kind = next_kind
    if "backend_ids" in values:
        item.backends = await load_backends(session, values.pop("backend_ids") or [])
    for key, value in values.items():
        setattr(item, key, value)
    if commit:
        await validate_config_graph(session)
        await session.commit()
        await session.refresh(item)
        item = await require_input(session, item.id)
    return item


async def delete_input(
    session: AsyncSession,
    input_id: int,
) -> Input:
    item = await require_input(session, input_id)
    await session.delete(item)
    return item


__all__ = [
    "ValidationError",
    "create_input",
    "delete_input",
    "load_backends",
    "load_input",
    "require_input",
    "update_input",
]
