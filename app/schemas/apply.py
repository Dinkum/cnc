from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ApplyResponse(BaseModel):
    status: str
    message: str
    details: dict[str, Any]
    # ``None`` means a custom or older apply runner did not report the outcome.
    # ``False`` is stronger: every host reconciler observed matching state and
    # none of them mutated it.
    host_changed: bool | None = None
    created_at: datetime | None = None
    run_id: int | None = None
