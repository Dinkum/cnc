from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database import isolated_database_session
from app.models.entities import Backend
from app.services.operations import OperationHandle


def background_db_session(
    settings: Settings,
) -> AbstractAsyncContextManager[AsyncSession]:
    return isolated_database_session(settings.database_url)


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
