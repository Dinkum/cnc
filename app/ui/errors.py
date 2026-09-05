from __future__ import annotations

from fastapi.responses import JSONResponse

from app.logger import get_logger
from app.services.error_reporting import CNCError, ErrorCode


logger = get_logger("ui.error")


def operator_error_message(message: str, error_fields: dict[str, str]) -> str:
    suffix = _error_suffix(error_fields)
    if not suffix:
        return message
    return f"{message} - {suffix}"


def operator_coded_error(message: str, error_code: ErrorCode) -> str:
    error = CNCError(
        error_code,
        message,
        status_code=400,
        severity="warning",
    )
    _report_operator_error(error)
    return operator_error_message(message, error.log_context())


def operator_error_json(
    message: str,
    error_code: ErrorCode,
    *,
    key: str = "error",
    status_code: int,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    error = CNCError(
        error_code,
        message,
        status_code=status_code,
        severity="warning" if status_code < 500 else "error",
    )
    fields = error.log_context()
    _report_operator_error(error)
    payload = {
        key: operator_error_message(message, fields),
        **fields,
        **(extra or {}),
    }
    return JSONResponse(payload, status_code=status_code)


def operator_error_payload(
    message: str,
    error_code: ErrorCode,
    *,
    key: str = "error",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    error = CNCError(error_code, message, status_code=400, severity="warning")
    fields = error.log_context()
    _report_operator_error(error)
    return {
        key: operator_error_message(message, fields),
        **fields,
        **(extra or {}),
    }


def operator_coded_message_payload(
    message: str,
    *,
    key: str,
    error_code: ErrorCode = ErrorCode.UI_ACTION_FAILED,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    error = CNCError(error_code, message, status_code=400, severity="warning")
    fields = error.log_context()
    _report_operator_error(error)
    return {
        key: operator_error_message(message, fields),
        **fields,
        **(extra or {}),
    }


def operator_error_is_coded(message: object) -> bool:
    if not isinstance(message, str):
        return False
    return "Error CNC-" in message


def ensure_operator_coded_error(
    message: str, error_code: ErrorCode = ErrorCode.UI_ACTION_FAILED
) -> str:
    return (
        message
        if operator_error_is_coded(message)
        else operator_coded_error(message, error_code)
    )


def ensure_context_flash_error_code(context: dict[str, object]) -> None:
    flash_error = context.get("flash_error")
    if isinstance(flash_error, str) and flash_error:
        context["flash_error"] = ensure_operator_coded_error(flash_error)


def operator_action_error(
    action: str,
    detail: str,
    error_fields: dict[str, str] | CNCError,
) -> str:
    message = f"{action} failed: {detail}"
    if isinstance(error_fields, CNCError):
        fields = error_fields.log_context()
        _report_operator_error(error_fields)
    else:
        fields = error_fields
        _report_operator_error_fields(message, fields)
    suffix = _error_suffix(fields)
    if suffix:
        return f"{action} failed - {suffix}: {detail}"
    return message


def _report_operator_error(error: CNCError) -> None:
    log_method = logger.error if error.severity == "error" else logger.warning
    log_method(
        "ui.error.reported",
        **error.log_context(),
        status_code=error.status_code,
        summary=str(error),
    )


def _report_operator_error_fields(message: str, fields: dict[str, str]) -> None:
    logger.warning("ui.error.reported", **fields, summary=message)


def _error_suffix(error_fields: dict[str, str]) -> str:
    code = error_fields.get("error_code")
    instance = error_fields.get("error_inst")
    if code and instance:
        return f"Error {code}-{instance}"
    return error_fields.get("error_ref", "")
