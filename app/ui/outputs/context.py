"""Coordinate output page loading and its pure template projection."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ui.outputs.data import load_output_page_data
from app.ui.outputs.presentation import build_output_page_context


async def output_page_context(
    session: AsyncSession,
    settings: Settings,
    backend_id: int,
    *,
    prefer_cached_runtime: bool = False,
) -> dict[str, object]:
    data = await load_output_page_data(
        session, settings, backend_id, prefer_cached_runtime=prefer_cached_runtime
    )
    return build_output_page_context(
        data, settings, prefer_cached_runtime=prefer_cached_runtime
    )
