from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.config import get_settings
from app.database import SessionLocal
from app.services.agent_channel import (
    AGENT_CHANNEL_PATH,
    AgentChannelAuthError,
    authenticate_agent_websocket,
    run_agent_channel,
)


router = APIRouter(tags=["agent"])


@router.websocket(AGENT_CHANNEL_PATH)
async def agent_channel(websocket: WebSocket) -> None:
    settings = get_settings()
    try:
        await authenticate_agent_websocket(websocket, settings)
    except AgentChannelAuthError:
        await websocket.close(code=1008)
        return

    await run_agent_channel(websocket, settings, SessionLocal)
