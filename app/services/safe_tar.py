from __future__ import annotations

from functools import wraps

from app.services import tar_safety
from app.services.error_reporting import CNCError, ErrorCode


class UnsafeTarArchiveError(CNCError):
    def __init__(
        self, message: str, *, error_code: ErrorCode = ErrorCode.RESTORE_FAILED
    ) -> None:
        super().__init__(error_code, message, status_code=400)


def _with_cnc_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except tar_safety.UnsafeTarArchiveError as exc:
            code = {
                "symlink": ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES,
                "hardlink": ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
                "device": ErrorCode.RESTORE_TAR_DEVICE_MEMBER,
            }.get(exc.error_code, ErrorCode.RESTORE_FAILED)
            raise UnsafeTarArchiveError(str(exc), error_code=code) from exc

    return wrapped


# The standard-library implementation is also bundled with the remote guest
# helper. Keep archive validation identical on CNC and transfer-only hosts.
validate_safe_tar = _with_cnc_errors(tar_safety.validate_safe_tar)
safe_extract_tar = _with_cnc_errors(tar_safety.safe_extract_tar)
