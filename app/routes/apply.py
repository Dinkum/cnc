from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import (
    csrf_api_dependency,
    db_session_dependency,
    settings_dependency,
)
from app.logger import get_logger
from app.schemas.apply import ApplyResponse
from app.services.apply_service import run_apply


router = APIRouter(prefix="/api", tags=["apply"])
logger = get_logger("api.apply")


@router.post("/apply", response_model=ApplyResponse)
async def apply_all(
    session: AsyncSession = Depends(db_session_dependency),
    settings: Settings = Depends(settings_dependency),
    _csrf: None = Depends(csrf_api_dependency),
) -> ApplyResponse:
    logger.info("apply.api.requested")
    response = await run_apply(session, settings)
    logger.info("apply.api.completed", status=response.status)
    if (
        response.status == "error"
        and str(response.details.get("phase") or "") == "lock"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": response.message,
                "apply": response.details,
                "run_id": response.run_id,
            },
        )
    return response
