from __future__ import annotations

from contextlib import AbstractContextManager
import errno
import json
import os
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from app.logger import get_logger
from app.services.file_locks import FileLock, lock_path, shared_lock_path


T = TypeVar("T")
logger = get_logger("host.state")
_SHARED_LOCK_ROOTS = (Path("/etc"),)


def uses_shared_host_state_lock(path: Path) -> bool:
    normalized = path.expanduser()
    for root in _SHARED_LOCK_ROOTS:
        try:
            normalized.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def host_state_lock_path(path: Path) -> Path:
    normalized = path.expanduser()
    # Protected system paths like /etc may be readable but not writable by the
    # admin process. Keep those locks in CNC-owned state storage instead of
    # trying to create sibling lock files next to the managed file itself.
    if uses_shared_host_state_lock(normalized):
        return shared_lock_path(normalized)
    return lock_path(normalized)


def _write_text_in_place(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    with path.open("w", encoding=encoding) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_text_via_replace(
    path: Path, content: str, *, encoding: str = "utf-8"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    existing_stat = None
    try:
        existing_stat = path.stat()
    except FileNotFoundError:
        pass
    try:
        with temp_path.open("w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_stat is not None:
            os.chmod(temp_path, existing_stat.st_mode & 0o7777)
            try:
                os.chown(temp_path, existing_stat.st_uid, existing_stat.st_gid)
            except PermissionError:
                logger.warning(
                    "host_state.owner_preserve_skipped",
                    path=str(path),
                    uid=existing_stat.st_uid,
                    gid=existing_stat.st_gid,
                )
        else:
            os.chmod(temp_path, 0o600)
        temp_path.replace(path)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _can_fallback_to_in_place_write(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    if uses_shared_host_state_lock(path):
        try:
            _write_text_via_replace(path, content, encoding=encoding)
            return
        except OSError as exc:
            if not _can_fallback_to_in_place_write(exc):
                raise
            logger.warning(
                "host_state.atomic_replace_unavailable",
                path=str(path),
                error=str(exc),
            )
            # Some hardened units expose an individual file through ReadWritePaths
            # while keeping its parent directory read-only. Preserve that fallback
            # only when sibling temp-file promotion is unavailable.
            _write_text_in_place(path, content, encoding=encoding)
            return
    _write_text_via_replace(path, content, encoding=encoding)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_host_state_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("host_state.read_failed", path=str(path))
        return {}
    if not isinstance(payload, dict):
        logger.warning("host_state.invalid_payload", path=str(path))
        return {}
    return payload


def write_host_state_unlocked(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)


class LockedStateTransaction(
    AbstractContextManager["LockedStateTransaction[T]"], Generic[T]
):
    def __init__(
        self,
        path: Path,
        *,
        loader: Callable[[Path], T],
        writer: Callable[[Path, T], None],
        blocking: bool = True,
        lock_timeout_sec: float | None = None,
    ) -> None:
        self._path = path
        self._loader = loader
        self._writer = writer
        self._lock = FileLock(
            path,
            blocking=blocking,
            lock_path=host_state_lock_path(path),
            timeout_sec=lock_timeout_sec,
        )
        self._value: T | None = None
        self._dirty = False

    def __enter__(self) -> "LockedStateTransaction[T]":
        self._lock.__enter__()
        self._value = self._loader(self._path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._dirty and self._value is not None:
                self._writer(self._path, self._value)
        finally:
            self._lock.__exit__(exc_type, exc, tb)

    @property
    def value(self) -> T:
        if self._value is None:
            raise RuntimeError("host state transaction accessed before entering")
        return self._value

    @property
    def dirty(self) -> bool:
        return self._dirty

    def replace(self, value: T) -> None:
        self._value = value
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True


class LockedStateSectionTransaction(
    AbstractContextManager["LockedStateSectionTransaction"],
):
    def __init__(
        self,
        path: Path,
        *,
        section: str,
        blocking: bool = True,
        lock_timeout_sec: float | None = None,
    ) -> None:
        self._section = section
        self._state = LockedStateTransaction[dict[str, Any]](
            path,
            loader=read_host_state_unlocked,
            writer=write_host_state_unlocked,
            blocking=blocking,
            lock_timeout_sec=lock_timeout_sec,
        )

    def __enter__(self) -> "LockedStateSectionTransaction":
        self._state.__enter__()
        payload = self._state.value
        current = payload.get(self._section)
        if not isinstance(current, dict):
            payload[self._section] = {}
            self._state.mark_dirty()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._state.__exit__(exc_type, exc, tb)

    @property
    def value(self) -> dict[str, Any]:
        payload = self._state.value.get(self._section)
        if not isinstance(payload, dict):
            raise RuntimeError("host state section accessed before entering")
        return payload

    @property
    def dirty(self) -> bool:
        return self._state.dirty

    def replace(self, value: dict[str, Any]) -> None:
        self._state.value[self._section] = value
        self._state.mark_dirty()

    def mark_dirty(self) -> None:
        self._state.mark_dirty()
