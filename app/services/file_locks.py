from __future__ import annotations

from contextlib import AbstractContextManager
import fcntl
import os
from pathlib import Path
import threading
import time


DEFAULT_SHARED_LOCK_ROOT = Path("/var/lib/cnc/locks")
_PROCESS_LOCK_OWNERS: dict[Path, int] = {}
_PROCESS_LOCK_OWNERS_GUARD = threading.Lock()


def lock_path(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}.lock")


def shared_lock_path(path: Path, *, root: Path | None = None) -> Path:
    lock_root = root or DEFAULT_SHARED_LOCK_ROOT
    normalized = path.expanduser()
    if normalized.is_absolute():
        relative_parts = normalized.parts[1:]
    else:
        relative_parts = ("relative", *normalized.parts)
    return lock_root.joinpath(*relative_parts).with_suffix(f"{normalized.suffix}.lock")


class FileLock(AbstractContextManager["FileLock"]):
    def __init__(
        self,
        path: Path,
        *,
        blocking: bool = True,
        lock_path: Path | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        self._path = lock_path or globals()["lock_path"](path)
        self._blocking = blocking
        self._timeout_sec = timeout_sec
        self._handle = None
        self._owner_key: Path | None = None

    def __enter__(self) -> "FileLock":
        owner_key = self._path.expanduser().resolve(strict=False)
        owner_thread = threading.get_ident()
        with _PROCESS_LOCK_OWNERS_GUARD:
            if (
                self._blocking
                and self._timeout_sec is None
                and _PROCESS_LOCK_OWNERS.get(owner_key) == owner_thread
            ):
                raise RuntimeError(f"reentrant file lock acquisition: {owner_key}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        flags = fcntl.LOCK_EX
        if not self._blocking or self._timeout_sec is not None:
            flags |= fcntl.LOCK_NB
        deadline = (
            time.monotonic() + self._timeout_sec
            if self._timeout_sec is not None
            else None
        )
        try:
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), flags)
                    break
                except BlockingIOError:
                    if not self._blocking:
                        raise
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise
                        time.sleep(min(0.05, remaining))
                    else:
                        raise
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            raise
        with _PROCESS_LOCK_OWNERS_GUARD:
            _PROCESS_LOCK_OWNERS[owner_key] = owner_thread
        self._owner_key = owner_key
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        if self._owner_key is not None:
            with _PROCESS_LOCK_OWNERS_GUARD:
                if _PROCESS_LOCK_OWNERS.get(self._owner_key) == threading.get_ident():
                    _PROCESS_LOCK_OWNERS.pop(self._owner_key, None)
            self._owner_key = None
