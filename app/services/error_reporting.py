from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import secrets
from typing import Any


ERROR_CODE_RE = re.compile(r"^CNC-\d{5}$")
ERROR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ErrorCode(Enum):
    VALIDATION_FAILED = "CNC-01001"
    HTTP_REQUEST_REJECTED = "CNC-01002"

    APPLY_NGINX_CONFIG_INVALID = "CNC-02001"
    APPLY_PORT_COLLISION = "CNC-02002"
    APPLY_FAILED = "CNC-02099"

    OUTPUT_RUNTIME_FAILED = "CNC-03001"
    OUTPUT_CLONE_FAILED = "CNC-03002"
    OUTPUT_DELETE_FAILED = "CNC-03003"

    BACKUP_CREATE_FAILED = "CNC-04001"
    BACKUP_CHECKSUM_MISMATCH = "CNC-04002"
    BACKUP_DELETE_FAILED = "CNC-04003"
    BACKUP_IMPORT_FAILED = "CNC-04004"

    RESTORE_MOUNT_PATH_MISMATCH = "CNC-05001"
    RESTORE_TAR_SYMLINK_ESCAPES = "CNC-05002"
    RESTORE_TAR_HARDLINK_UNSAFE = "CNC-05003"
    RESTORE_TAR_DEVICE_MEMBER = "CNC-05004"
    RESTORE_FAILED = "CNC-05099"

    UPDATE_FAILED = "CNC-06001"

    CLUSTER_TRANSFER_FAILED = "CNC-07001"
    CLUSTER_REPLICA_SETUP_FAILED = "CNC-07002"

    UI_BACKEND_CREATE_CONFLICT = "CNC-08001"
    UI_BACKEND_CREATE_FAILED = "CNC-08002"
    UI_BACKEND_UPDATE_FAILED = "CNC-08003"
    UI_BACKEND_INPUTS_FAILED = "CNC-08004"
    UI_BACKEND_STATE_FAILED = "CNC-08005"
    UI_INPUT_CREATE_CONFLICT = "CNC-08006"
    UI_INPUT_CREATE_FAILED = "CNC-08007"
    UI_INPUT_UPDATE_CONFLICT = "CNC-08008"
    UI_INPUT_UPDATE_FAILED = "CNC-08009"
    UI_INPUT_DELETE_FAILED = "CNC-08010"
    UI_BACKEND_INTERFACE_FAILED = "CNC-08011"
    UI_RESOURCE_NOT_FOUND = "CNC-08012"
    UI_ACTION_UNAVAILABLE = "CNC-08013"
    UI_DEBUG_BUNDLE_REQUEST_INVALID = "CNC-08014"
    UI_ACTION_FAILED = "CNC-08015"

    INTERNAL_UNHANDLED_EXCEPTION = "CNC-09001"


@dataclass(frozen=True)
class ErrorDescriptor:
    code: str
    name: str
    message: str

    def log_context(self, error_inst: str) -> dict[str, str]:
        return {
            "error_code": self.code,
            "error_name": self.name,
            "error_inst": error_inst,
        }

    def payload(self, error_inst: str) -> dict[str, str]:
        return {
            "error_code": self.code,
            "error_name": self.name,
            "error_inst": error_inst,
        }


ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_FAILED: "Submitted configuration did not pass validation.",
    ErrorCode.HTTP_REQUEST_REJECTED: "The HTTP request was rejected before the action could run.",
    ErrorCode.APPLY_NGINX_CONFIG_INVALID: "Generated nginx configuration did not pass validation.",
    ErrorCode.APPLY_PORT_COLLISION: "One or more outputs require the same host port.",
    ErrorCode.APPLY_FAILED: "Host apply failed before CNC could converge the requested state.",
    ErrorCode.OUTPUT_RUNTIME_FAILED: "Output runtime reconciliation failed.",
    ErrorCode.OUTPUT_CLONE_FAILED: "Output clone failed.",
    ErrorCode.OUTPUT_DELETE_FAILED: "Output delete failed.",
    ErrorCode.BACKUP_CREATE_FAILED: "Output backup creation failed.",
    ErrorCode.BACKUP_CHECKSUM_MISMATCH: "Backup checksum verification failed.",
    ErrorCode.BACKUP_DELETE_FAILED: "Output backup delete failed.",
    ErrorCode.BACKUP_IMPORT_FAILED: "Backup import failed.",
    ErrorCode.RESTORE_MOUNT_PATH_MISMATCH: "Restore archive contains an unexpected mount path.",
    ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES: "Restore archive contains a symlink that escapes the restore root.",
    ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE: "Restore archive contains an unsafe hardlink.",
    ErrorCode.RESTORE_TAR_DEVICE_MEMBER: "Restore archive contains a device file.",
    ErrorCode.RESTORE_FAILED: "Output restore failed.",
    ErrorCode.UPDATE_FAILED: "CNC update failed.",
    ErrorCode.CLUSTER_TRANSFER_FAILED: "Output transfer to another node failed.",
    ErrorCode.CLUSTER_REPLICA_SETUP_FAILED: "Output replica setup failed.",
    ErrorCode.UI_BACKEND_CREATE_CONFLICT: "Output create failed because the submitted name conflicts with an existing output.",
    ErrorCode.UI_BACKEND_CREATE_FAILED: "Output create failed.",
    ErrorCode.UI_BACKEND_UPDATE_FAILED: "Output settings update failed.",
    ErrorCode.UI_BACKEND_INPUTS_FAILED: "Output input attachment update failed.",
    ErrorCode.UI_BACKEND_STATE_FAILED: "Output enabled state update failed.",
    ErrorCode.UI_INPUT_CREATE_CONFLICT: "Input create failed because the submitted route conflicts with an existing input.",
    ErrorCode.UI_INPUT_CREATE_FAILED: "Input create failed.",
    ErrorCode.UI_INPUT_UPDATE_CONFLICT: "Input update failed because the submitted route conflicts with an existing input.",
    ErrorCode.UI_INPUT_UPDATE_FAILED: "Input update failed.",
    ErrorCode.UI_INPUT_DELETE_FAILED: "Input delete failed.",
    ErrorCode.UI_BACKEND_INTERFACE_FAILED: "Output interface update failed.",
    ErrorCode.UI_RESOURCE_NOT_FOUND: "Requested CNC resource was not found.",
    ErrorCode.UI_ACTION_UNAVAILABLE: "The requested UI action is unavailable in the current state.",
    ErrorCode.UI_DEBUG_BUNDLE_REQUEST_INVALID: "Debug bundle request was invalid.",
    ErrorCode.UI_ACTION_FAILED: "The requested UI action failed.",
    ErrorCode.INTERNAL_UNHANDLED_EXCEPTION: "CNC hit an unhandled internal error.",
}


def _validate_error_registry() -> dict[ErrorCode, ErrorDescriptor]:
    descriptors: dict[ErrorCode, ErrorDescriptor] = {}
    seen_codes: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    missing_messages = set(ErrorCode) - set(ERROR_MESSAGES)
    extra_messages = set(ERROR_MESSAGES) - set(ErrorCode)
    if missing_messages or extra_messages:
        missing = ", ".join(item.name for item in sorted(missing_messages, key=str))
        extra = ", ".join(item.name for item in sorted(extra_messages, key=str))
        raise RuntimeError(
            f"CNC error message registry mismatch; missing={missing or '-'} extra={extra or '-'}"
        )
    for name, item in ErrorCode.__members__.items():
        code = str(item.value)
        message = ERROR_MESSAGES[item].strip()
        if not ERROR_CODE_RE.fullmatch(code):
            raise RuntimeError(f"invalid CNC error code: {code}")
        if not ERROR_NAME_RE.fullmatch(name):
            raise RuntimeError(f"invalid CNC error name: {name}")
        if not message:
            raise RuntimeError(f"missing CNC error message for {name}")
        if code in seen_codes:
            raise RuntimeError(
                f"duplicate CNC error code {code}: {seen_codes[code]} and {name}"
            )
        if name in seen_names:
            raise RuntimeError(
                f"duplicate CNC error name {name}: {seen_names[name]} and {code}"
            )
        seen_codes[code] = name
        seen_names[name] = code
        descriptors[item] = ErrorDescriptor(code=code, name=name, message=message)
    return descriptors


ERROR_REGISTRY = _validate_error_registry()


def error_descriptor(error_code: ErrorCode) -> ErrorDescriptor:
    return ERROR_REGISTRY[error_code]


def new_error_instance(width: int = 8) -> str:
    if width < 1:
        raise ValueError("error instance width must be positive")
    # Crockford base32 uses 5 bits per character. Rejection is unnecessary
    # because the random integer is bounded to exactly the requested width.
    value = secrets.randbits(width * 5)
    return _encode_crockford(value, width=width)


class CNCError(RuntimeError):
    """One classified failure shared by logs, state, UI, and notifications."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str | None = None,
        *,
        error_inst: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int = 500,
        severity: str = "error",
        cause: Exception | None = None,
    ) -> None:
        descriptor = error_descriptor(error_code)
        resolved_message = (
            str(message or descriptor.message).strip() or descriptor.message
        )
        super().__init__(resolved_message)
        self.error_code = error_code
        self.error_inst = error_inst or new_error_instance()
        self.details = dict(details or {})
        self.status_code = int(status_code)
        self.severity = str(severity or "error").strip().lower()
        self.cause = cause

    @property
    def descriptor(self) -> ErrorDescriptor:
        return error_descriptor(self.error_code)

    @property
    def code(self) -> str:
        return self.descriptor.code

    @property
    def name(self) -> str:
        return self.descriptor.name

    def log_context(self) -> dict[str, str]:
        return self.descriptor.log_context(self.error_inst)

    def payload(self) -> dict[str, object]:
        return {
            "message": str(self),
            **self.details,
            **self.log_context(),
        }

    def as_details(self) -> dict[str, Any]:
        return {
            "error": str(self),
            **self.details,
            **self.log_context(),
        }


def ensure_cnc_error(
    exc: Exception,
    error_code: ErrorCode,
    *,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    status_code: int = 500,
    severity: str = "error",
) -> CNCError:
    if isinstance(exc, CNCError):
        return exc
    return CNCError(
        error_code,
        message or str(exc),
        details=details,
        status_code=status_code,
        severity=severity,
        cause=exc,
    )


def cnc_error_from_payload(
    payload: object,
    *,
    default_error_code: ErrorCode,
    status_code: int,
) -> CNCError:
    outer = payload if isinstance(payload, dict) else {}
    nested = outer.get("apply") if isinstance(outer, dict) else None
    source = nested if isinstance(nested, dict) else outer
    try:
        error_code = ErrorCode(str(source.get("error_code") or ""))
    except ValueError:
        error_code = default_error_code
    message = str(
        outer.get("message")
        or source.get("message")
        or source.get("error")
        or (payload if isinstance(payload, str) else "")
        or ""
    ).strip()
    error_inst = str(source.get("error_inst") or "").strip() or None
    return CNCError(
        error_code,
        message,
        error_inst=error_inst,
        details=dict(source),
        status_code=status_code,
        severity="warning" if status_code < 500 else "error",
    )


def error_context(
    error: ErrorCode | CNCError, *, error_inst: str | None = None
) -> dict[str, str]:
    if isinstance(error, CNCError):
        return error.log_context()
    descriptor = error_descriptor(error)
    return descriptor.log_context(error_inst or new_error_instance())


def error_ledger_rows() -> list[ErrorDescriptor]:
    return sorted(ERROR_REGISTRY.values(), key=lambda descriptor: descriptor.code)


def _encode_crockford(value: int, *, width: int) -> str:
    modulo = 32**width
    number = max(0, int(value)) % modulo
    chars: list[str] = []
    for _ in range(width):
        number, remainder = divmod(number, 32)
        chars.append(CROCKFORD_ALPHABET[remainder])
    return "".join(reversed(chars))
