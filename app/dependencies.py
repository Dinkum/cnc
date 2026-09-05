from collections.abc import AsyncGenerator

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db_session
from app.security import enforce_csrf


async def db_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_db_session():
        yield session


def settings_dependency() -> Settings:
    return get_settings()


def csrf_api_dependency(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    settings: Settings = Depends(settings_dependency),
) -> None:
    enforce_csrf(request, settings, x_csrf_token)
