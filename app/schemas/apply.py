from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ApplyResponse(BaseModel):
    status: str
    message: str
    details: dict[str, Any]
    created_at: datetime | None = None
    run_id: int | None = None
