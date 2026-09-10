from __future__ import annotations

import codecs
import json
import os
import re
from pathlib import Path
import stat

from app.config import Settings
from app.services.file_locks import FileLock
from app.services.sandbox_profiles import app_sandbox_rootfs_path


GUEST_JOB_ROOT = "var/lib/cnc-command-jobs"
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024


def reservation_path(settings: Settings, output: str) -> Path:
    return settings.app_control_dir / output / "command-job.json"


def artifact_name(operation_id: int, execution_token: str) -> str:
    if (
        type(operation_id) is not int
        or operation_id <= 0
        or not isinstance(execution_token, str)
        or not re.fullmatch(r"[0-9a-f]{32}", execution_token)
    ):
        raise ValueError("invalid command execution identity")
    return f"{operation_id}-{execution_token}"


def reserve_job(
    settings: Settings, output: str, operation_id: int, *, execution_token: str
) -> None:
    artifact_name(operation_id, execution_token)
    path = reservation_path(settings, output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(
            {"operation_id": operation_id, "execution_token": execution_token}, handle
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def reserved_job(settings: Settings, output: str) -> int | None:
    try:
        payload = json.loads(reservation_path(settings, output).read_text())
    except FileNotFoundError:
        return None
    except (UnicodeError, ValueError) as exc:
        raise OSError("invalid background command reservation") from exc
    # Corrupt guard state must fail closed, not admit another guest command.
    if not isinstance(payload, dict) or type(payload.get("operation_id")) is not int:
        raise OSError("invalid background command reservation")
    operation_id = payload["operation_id"]
    if operation_id <= 0:
        raise OSError("invalid background command reservation")
    execution_token = payload.get("execution_token")
    try:
        artifact_name(operation_id, execution_token)
    except ValueError as exc:
        raise OSError("invalid background command reservation") from exc
    if (
        read_job_result(settings, output, operation_id, execution_token=execution_token)
        is not None
    ):
        return None
    return operation_id


def release_job(
    settings: Settings, output: str, operation_id: int, *, execution_token: str
) -> None:
    artifact_name(operation_id, execution_token)
    path = reservation_path(settings, output)
    lock_path = path.parent / "exec-probe.lock"
    lock = FileLock(lock_path, blocking=False, lock_path=lock_path)
    try:
        lock.__enter__()
    except BlockingIOError:
        # A submitter can already own this lock when inspecting an existing job.
        # Terminal reservations do not block admission, so defer cleanup safely.
        return
    try:
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (UnicodeError, ValueError) as exc:
            raise OSError("invalid background command reservation") from exc
        if not isinstance(payload, dict):
            raise OSError("invalid background command reservation")
        # Compare and remove while holding the same lock that serializes reserve.
        # An older observer must never unlink a newer command's reservation.
        if (
            type(payload.get("operation_id")) is int
            and payload["operation_id"] == operation_id
            and payload.get("execution_token") == execution_token
        ):
            path.unlink()
    finally:
        lock.__exit__(None, None, None)


def read_guest_file(
    settings: Settings,
    output: str,
    operation_id: int,
    name: str,
    *,
    execution_token: str,
    offset: int = 0,
    limit: int = MAX_READ_BYTES,
) -> bytes:
    return (
        _read_guest_file(
            settings,
            output,
            operation_id,
            name,
            execution_token=execution_token,
            offset=offset,
            limit=limit,
        )
        or b""
    )


def _read_guest_file(
    settings: Settings,
    output: str,
    operation_id: int,
    name: str,
    *,
    execution_token: str,
    offset: int = 0,
    limit: int = MAX_READ_BYTES,
) -> bytes | None:
    """Read bounded regular files without following guest-controlled symlinks."""
    if operation_id <= 0 or name not in {
        "result",
        "stdout",
        "stderr",
        "stdout.truncated",
        "stderr.truncated",
    }:
        raise ValueError("invalid command output file")
    root = app_sandbox_rootfs_path(settings, output)
    descriptors: list[int] = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in (
            *GUEST_JOB_ROOT.split("/"),
            artifact_name(operation_id, execution_token),
        ):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("command output is not a regular file")
        os.lseek(fd, max(0, offset), os.SEEK_SET)
        return os.read(fd, min(max(0, limit), MAX_READ_BYTES))
    except FileNotFoundError:
        return None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def read_job_result(
    settings: Settings, output: str, operation_id: int, *, execution_token: str
) -> dict | None:
    raw = read_guest_file(
        settings,
        output,
        operation_id,
        "result",
        execution_token=execution_token,
        limit=512,
    )
    if not raw:
        return None
    try:
        fields = raw.decode("ascii").strip().split("\t")
    except UnicodeError as exc:
        raise OSError("invalid command completion record") from exc
    if (
        len(fields) != 3
        or fields[0]
        not in {
            "success",
            "exit-code",
            "signal",
            "core-dump",
            "timeout",
            "resources",
            "protocol",
            "start-limit-hit",
            "watchdog",
            "oom-kill",
            "exec-condition",
        }
        or fields[1] not in {"exited", "killed", "dumped", "none"}
    ):
        raise OSError("invalid command completion record")
    if fields[1] == "exited" and (
        not fields[2].isdigit() or not 0 <= int(fields[2]) <= 255
    ):
        raise OSError("invalid command exit status")
    return {
        "service_result": fields[0],
        "exit_kind": fields[1],
        "exit_status": fields[2],
        "exit_code": int(fields[2])
        if fields[1] == "exited" and fields[2].isdigit()
        else None,
    }


def read_job_stream(
    settings: Settings,
    output: str,
    operation_id: int,
    stream: str,
    *,
    execution_token: str,
    offset: int,
    terminal: bool,
) -> dict:
    if (
        not isinstance(stream, str)
        or stream not in {"stdout", "stderr"}
        or type(offset) is not int
        or not 0 <= offset <= MAX_STREAM_BYTES + 1
    ):
        raise ValueError(
            "stream must be stdout or stderr and offset must be nonnegative"
        )
    retained_data = _read_guest_file(
        settings,
        output,
        operation_id,
        stream,
        execution_token=execution_token,
        offset=offset,
    )
    raw = retained_data or b""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    data = decoder.decode(raw, final=terminal and len(raw) < MAX_READ_BYTES)
    pending = decoder.getstate()[0]
    return {
        "stream": stream,
        "data": data,
        "offset": offset,
        "next_offset": offset + len(raw) - len(pending),
        "retained": retained_data is not None,
        "truncated": bool(
            read_guest_file(
                settings,
                output,
                operation_id,
                f"{stream}.truncated",
                execution_token=execution_token,
                limit=1,
            )
        ),
        "eof": terminal and len(raw) < MAX_READ_BYTES,
    }
