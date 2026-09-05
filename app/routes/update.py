from fastapi import APIRouter, Depends, HTTPException, status

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import (
    csrf_api_dependency,
    db_session_dependency,
    settings_dependency,
)
from app.logger import get_logger
from app.schemas.apply import ApplyResponse
from app.services.update_service import (
    UpdateRejectedError,
    refresh_update_version_info,
    run_update,
)


router = APIRouter(prefix="/api", tags=["update"])
logger = get_logger("api.update")


@router.post(
    "/update", response_model=ApplyResponse, status_code=status.HTTP_202_ACCEPTED
)
async def update_now(
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> ApplyResponse:
    logger.info("update.api.requested")
    try:
        response = await run_update(session, settings)
    except UpdateRejectedError as exc:
        raise HTTPException(status_code=409, detail=exc.payload()) from exc
    if response.status == "error" and response.details.get("phase") == "lock":
        raise HTTPException(status_code=409, detail=response.details) from None
    logger.info("update.api.completed", status=response.status)
    return response


@router.post("/update/check")
async def check_for_updates(
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> dict:
    logger.info("update.check.api.requested")
    return await refresh_update_version_info(session, settings)
