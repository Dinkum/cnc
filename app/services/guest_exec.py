from __future__ import annotations

from contextlib import contextmanager
from enum import StrEnum
import json
import os
from pathlib import Path
import time
from typing import Callable, Iterator

from app.config import Settings
from app.logger import get_logger
from app.services.app_containers import control_dir
from app.services.commands import CommandResult, run_command, run_podman_exec
from app.services.file_locks import FileLock
from app.services.renderers import container_name


EXEC_CIRCUIT_OPEN_RETURN_CODE = 75
EXEC_GUARD_BUSY_RETURN_CODE = 76
EXEC_GUARD_UNAVAILABLE_RETURN_CODE = 77
logger = get_logger("guest.exec")


class BackendGuestExecGuardState(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


def _state_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "exec-circuit.json"


def _lock_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "exec-probe.lock"


def _write_state(path: Path, payload: dict[str, object]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def read_guest_exec_circuit(
    settings: Settings, backend_name: str
) -> dict[str, object] | None:
    path = _state_path(settings, backend_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    opened_at = payload.get("opened_at")
    if not isinstance(opened_at, (int, float)):
        return None
    return {**payload, "open": True}


def clear_guest_exec_circuit(
    settings: Settings, backend_name: str, *, reason: str = "recovered"
) -> None:
    try:
        _state_path(settings, backend_name).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "guest_exec.circuit_clear_failed",
            backend=backend_name,
            container=container_name(backend_name),
            reason=reason,
            error=str(exc),
        )
        return
    logger.info(
        "guest_exec.circuit_closed",
        backend=backend_name,
        container=container_name(backend_name),
        reason=reason,
    )


def open_guest_exec_circuit(
    settings: Settings,
    backend_name: str,
    *,
    timeout_sec: float,
    error: str,
    source: str = "guest_probe",
) -> None:
    persisted = _write_state(
        _state_path(settings, backend_name),
        {
            "opened_at": time.time(),
            "timeout_sec": timeout_sec,
            "error": error,
        },
    )
    logger.warning(
        "guest_exec.circuit_opened",
        backend=backend_name,
        container=container_name(backend_name),
        source=source,
        timeout_sec=timeout_sec,
        error=str(error or "")[:2000],
        state_persisted=persisted,
    )


@contextmanager
def backend_guest_exec_guard(
    settings: Settings,
    backend_name: str,
    wait_sec: float = 0,
    *,
    job_id: int | None = None,
) -> Iterator[BackendGuestExecGuardState]:
    wait_sec = max(0.0, wait_sec)
    lock = FileLock(
        _lock_path(settings, backend_name),
        blocking=wait_sec > 0,
        timeout_sec=wait_sec if wait_sec > 0 else None,
        lock_path=_lock_path(settings, backend_name),
    )
    try:
        lock.__enter__()
    except BlockingIOError:
        yield BackendGuestExecGuardState.BUSY
        return
    except OSError:
        try:
            lock.__exit__(None, None, None)
        except OSError:
            pass
        yield BackendGuestExecGuardState.UNAVAILABLE
        return
    try:
        from app.services.command_job_files import reserved_job

        try:
            reservation = reserved_job(settings, backend_name)
        except (OSError, ValueError):
            yield BackendGuestExecGuardState.UNAVAILABLE
            return
        if reservation is not None and reservation != job_id:
            yield BackendGuestExecGuardState.BUSY
            return
        yield BackendGuestExecGuardState.ACQUIRED
    finally:
        lock.__exit__(None, None, None)


def run_backend_guest_command(
    backend_name: str,
    settings: Settings,
    guest_command: list[str],
    *,
    timeout_sec: float,
    command_runner: Callable[[list[str], float], CommandResult] = run_command,
    wait_sec: float = 0,
    source: str = "guest_probe",
    job_id: int | None = None,
    container_id: str | None = None,
) -> CommandResult:
    container = container_id or container_name(backend_name)
    plain_command = ["podman", "exec", container, *guest_command]
    circuit = read_guest_exec_circuit(settings, backend_name)
    if circuit is not None:
        return CommandResult(
            command=plain_command,
            returncode=EXEC_CIRCUIT_OPEN_RETURN_CODE,
            stdout="",
            stderr="guest exec circuit open after a timeout",
        )

    started_at = time.monotonic()
    with backend_guest_exec_guard(
        settings,
        backend_name,
        wait_sec=min(max(0.0, timeout_sec), max(0.0, wait_sec)),
        **({"job_id": job_id} if job_id is not None else {}),
    ) as guard_state:
        if guard_state == BackendGuestExecGuardState.BUSY:
            return CommandResult(
                command=plain_command,
                returncode=EXEC_GUARD_BUSY_RETURN_CODE,
                stdout="",
                stderr="guest exec guard is busy",
            )
        if guard_state == BackendGuestExecGuardState.UNAVAILABLE:
            return CommandResult(
                command=plain_command,
                returncode=EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
                stdout="",
                stderr="guest exec guard is unavailable",
            )
        if read_guest_exec_circuit(settings, backend_name):
            return CommandResult(
                command=plain_command,
                returncode=EXEC_CIRCUIT_OPEN_RETURN_CODE,
                stdout="",
                stderr="guest exec circuit open after a timeout",
            )

        remaining = max(0.0, timeout_sec - (time.monotonic() - started_at))
        result = run_podman_exec(
            container,
            guest_command,
            timeout_sec=remaining,
            command_runner=command_runner,
        )
        if result.returncode == 124:
            open_guest_exec_circuit(
                settings,
                backend_name,
                timeout_sec=timeout_sec,
                error=result.stderr,
                source=source,
            )
        return result
