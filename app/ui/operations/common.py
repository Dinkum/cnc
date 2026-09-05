from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database import create_configured_async_engine
from app.models.entities import Backend
from app.services.operations import OperationHandle


@asynccontextmanager
async def background_db_session(settings: Settings):
    engine = create_configured_async_engine(
        settings.database_url, future=True, echo=False
    )
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def operation_backend(session: AsyncSession, backend_id: int) -> Backend:
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        raise LookupError("output not found")
    return backend


def operation_response(operation: OperationHandle, *, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "operation_id": operation.id,
            "operation_status": "queued",
            "message": message,
        },
        status_code=202,
    )
