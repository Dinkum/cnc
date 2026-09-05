from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Backend, Input


async def load_backends_by_ids(
    session: AsyncSession, backend_ids: list[int]
) -> tuple[list[Backend], list[int]]:
    unique_ids = sorted(set(backend_ids))
    if not unique_ids:
        return [], []
    backends = (
        (
            await session.execute(
                select(Backend)
                .where(Backend.id.in_(unique_ids))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .all()
    )
    found_ids = {backend.id for backend in backends}
    missing = [backend_id for backend_id in unique_ids if backend_id not in found_ids]
    return backends, missing


async def load_inputs_by_ids(
    session: AsyncSession, input_ids: list[int]
) -> tuple[list[Input], list[int]]:
    unique_ids = sorted(set(input_ids))
    if not unique_ids:
        return [], []
    inputs = (
        (
            await session.execute(
                select(Input).where(Input.id.in_(unique_ids)).order_by(Input.id.asc())
            )
        )
        .scalars()
        .all()
    )
    found_ids = {item.id for item in inputs}
    missing = [input_id for input_id in unique_ids if input_id not in found_ids]
    return inputs, missing
