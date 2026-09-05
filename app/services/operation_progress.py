from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from typing import Any

from app.config import Settings


def operation_progress_path(settings: Settings, operation_id: int) -> Path:
    return settings.app_control_dir / "operation-progress" / f"{operation_id}.json"


def read_operation_progress(
    settings: Settings, operation_id: int
) -> dict[str, Any] | None:
    path = operation_progress_path(settings, operation_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_operation_progress(settings: Settings, operation_id: int) -> None:
    try:
        operation_progress_path(settings, operation_id).unlink()
    except FileNotFoundError:
        return


def write_operation_progress(
    settings: Settings,
    operation_id: int,
    *,
    status: str | None,
    phase: str | None,
    details: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    path = operation_progress_path(settings, operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_operation_progress(settings, operation_id)
    existing_status = existing.get("status") if isinstance(existing, dict) else None
    existing_phase = existing.get("phase") if isinstance(existing, dict) else None
    existing_error = existing.get("error") if isinstance(existing, dict) else None
    existing_details = existing.get("details") if isinstance(existing, dict) else None
    existing_progress = (
        existing_details.get("progress") if isinstance(existing_details, dict) else None
    )
    incoming_details = details if isinstance(details, dict) else {}
    next_details = (
        {**existing_details, **incoming_details}
        if isinstance(existing_details, dict)
        else dict(incoming_details)
    )
    next_progress = next_details.get("progress")
    terminal = status in {"success", "failed", "partial", "cancelled"}
    if (
        isinstance(existing_progress, (int, float))
        and isinstance(next_progress, (int, float))
        and next_progress < existing_progress
    ):
        next_details["progress"] = existing_progress
    if not terminal:
        status = status if status is not None else existing_status
        phase = phase if phase is not None else existing_phase
        error = error if error is not None else existing_error
    payload = {
        "status": status,
        "phase": phase,
        "details": next_details,
        "error": error,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, sort_keys=True, default=str)
        temp_path = Path(handle.name)
    temp_path.replace(path)
