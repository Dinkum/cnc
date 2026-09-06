from __future__ import annotations

import asyncio
import codecs
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
import fcntl
import json
import os
import pty
import shlex
import signal
import struct
import termios
import time
from typing import Any, AsyncContextManager

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.access import (
    access_key_is_configured,
    verify_access_key_async,
)
from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.guest_exec import (
    BackendGuestExecGuardState,
    backend_guest_exec_guard,
    open_guest_exec_circuit,
    read_guest_exec_circuit,
)
from app.services.renderers import container_name


AGENT_CHANNEL_PATH = "/api/agent/channel"
AGENT_CHANNEL_VERSION = "beta.1"
MAX_FRAME_BYTES = 64 * 1024
MAX_OUTPUT_CHUNK_BYTES = 16 * 1024
DEFAULT_EXEC_TIMEOUT_SEC = 60
MAX_EXEC_TIMEOUT_SEC = 60 * 30
DEFAULT_PTY_IDLE_TIMEOUT_SEC = 60 * 15
MAX_PTY_IDLE_TIMEOUT_SEC = 60 * 60
logger = get_logger("agent_channel")


SendJson = Callable[[dict[str, Any]], Awaitable[None]]
SessionFactory = Callable[[], AsyncContextManager[AsyncSession]]


@dataclass
class ResolvedBackend:
    name: str
    container: str
    id: int | None = None


class AgentChannelAuthError(RuntimeError):
    pass


class AgentChannelFrameError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _agent_auth_attempt_key(client_host: str) -> str:
    return f"agent:{client_host or 'unknown'}"


def _guest_exec_frame_error(
    state: BackendGuestExecGuardState,
) -> AgentChannelFrameError:
    if state == BackendGuestExecGuardState.BUSY:
        return AgentChannelFrameError(
            "backend_busy", "another CNC-managed guest command is already running"
        )
    return AgentChannelFrameError(
        "backend_exec_unavailable", "guest exec guard is unavailable"
    )


def _guest_exec_circuit_error() -> AgentChannelFrameError:
    return AgentChannelFrameError(
        "backend_exec_unavailable",
        "guest exec circuit is open after a timeout; restart the runtime to recover",
    )


class PtySession:
    def __init__(
        self,
        *,
        frame_id: str,
        backend: ResolvedBackend,
        shell: str,
        cols: int,
        rows: int,
        idle_timeout_sec: int,
        send_json: SendJson,
        settings: Settings,
    ) -> None:
        self.frame_id = frame_id
        self.backend = backend
        self.shell = shell
        self.cols = cols
        self.rows = rows
        self.idle_timeout_sec = idle_timeout_sec
        self.send_json = send_json
        self.settings = settings
        self.master_fd: int | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.pump_task: asyncio.Task[None] | None = None
        self.wait_task: asyncio.Task[None] | None = None
        self.last_activity = time.monotonic()
        self.closed = False
        self.started_at: float | None = None
        self.close_reason = "unknown"
        self.lifecycle_closed_logged = False
        self.exec_guard: AbstractContextManager[BackendGuestExecGuardState] | None = (
            None
        )

    async def start(self) -> None:
        if read_guest_exec_circuit(self.settings, self.backend.name):
            raise _guest_exec_circuit_error()
        guard = backend_guest_exec_guard(self.settings, self.backend.name)
        guard_state = guard.__enter__()
        if guard_state != BackendGuestExecGuardState.ACQUIRED:
            guard.__exit__(None, None, None)
            raise _guest_exec_frame_error(guard_state)
        if read_guest_exec_circuit(self.settings, self.backend.name):
            guard.__exit__(None, None, None)
            raise _guest_exec_circuit_error()
        self.exec_guard = guard
        try:
            master_fd, slave_fd = pty.openpty()
            self.master_fd = master_fd
            try:
                os.set_blocking(master_fd, False)
                _set_pty_size(slave_fd, self.cols, self.rows)
                self.process = await asyncio.create_subprocess_exec(
                    "podman",
                    "exec",
                    "-i",
                    "-t",
                    self.backend.container,
                    self.shell,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                )
            finally:
                os.close(slave_fd)
            self.pump_task = asyncio.create_task(
                self._pump_output(), name=f"agent-pty-pump-{self.frame_id}"
            )
            self.wait_task = asyncio.create_task(
                self._wait(), name=f"agent-pty-wait-{self.frame_id}"
            )
            self.started_at = time.monotonic()
            logger.info(
                "agent_channel.pty_opened",
                backend_id=self.backend.id,
                backend=self.backend.name,
                container=self.backend.container,
                frame_id=self.frame_id,
                process_pid=self.process.pid,
                idle_timeout_sec=self.idle_timeout_sec,
            )
            await self.send_json(
                {
                    "id": self.frame_id,
                    "type": "pty.start",
                    "output": self.backend.name,
                    "container": self.backend.container,
                }
            )
        except BaseException:
            try:
                await self.close(reason="startup_failed")
            except BaseException as cleanup_error:
                logger.warning(
                    "agent_channel.pty_start_cleanup_failed",
                    backend_id=self.backend.id,
                    backend=self.backend.name,
                    container=self.backend.container,
                    frame_id=self.frame_id,
                    process_pid=self.process.pid if self.process is not None else None,
                    reason="startup_failed",
                    error=str(cleanup_error),
                )
            raise

    async def write(self, data: str) -> None:
        if self.closed or self.master_fd is None:
            raise AgentChannelFrameError("pty_closed", "pty session is closed")
        self.last_activity = time.monotonic()
        payload = data.encode("utf-8", errors="replace")
        try:
            await _write_nonblocking(self.master_fd, payload)
        except OSError:
            raise AgentChannelFrameError(
                "pty_closed", "pty session is closed"
            ) from None

    async def resize(self, cols: int, rows: int) -> None:
        if self.closed or self.master_fd is None:
            raise AgentChannelFrameError("pty_closed", "pty session is closed")
        self.cols = cols
        self.rows = rows
        self.last_activity = time.monotonic()
        _set_pty_size(self.master_fd, cols, rows)
        await self.send_json(
            {"id": self.frame_id, "type": "pty.resize", "cols": cols, "rows": rows}
        )

    async def close(self, *, reason: str = "client_close") -> None:
        await self._shutdown(reason=reason)

    async def expire(self, *, reason: str = "idle_timeout") -> None:
        if self.closed:
            return
        await self._shutdown(reason=reason)

    async def _shutdown(self, *, reason: str) -> None:
        self.close_reason = reason
        self.closed = True
        self._close_master_fd()
        tasks = self._background_tasks()
        for task in tasks:
            if task is not self.wait_task:
                task.cancel()
        try:
            if self.process is not None and self.process.returncode is None:
                await _terminate_process(self.process)
            for task in tasks:
                if not task.done():
                    task.cancel()
        except BaseException:
            for task in tasks:
                task.cancel()
            raise
        finally:
            try:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._close_master_fd()
                self._release_guest_guard()

    def _background_tasks(self) -> list[asyncio.Task[None]]:
        current = asyncio.current_task()
        return [
            task
            for task in (self.pump_task, self.wait_task)
            if task is not None and task is not current and not task.done()
        ]

    def _close_master_fd(self) -> None:
        if self.master_fd is None:
            return
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.master_fd = None

    def _release_guest_guard(self) -> None:
        if self.exec_guard is None:
            return
        try:
            self.exec_guard.__exit__(None, None, None)
        finally:
            self.exec_guard = None
            if self.started_at is not None and not self.lifecycle_closed_logged:
                self.lifecycle_closed_logged = True
                logger.info(
                    "agent_channel.pty_closed",
                    backend_id=self.backend.id,
                    backend=self.backend.name,
                    container=self.backend.container,
                    frame_id=self.frame_id,
                    process_pid=self.process.pid if self.process is not None else None,
                    process_code=self.process.returncode
                    if self.process is not None
                    else None,
                    reason=self.close_reason,
                    duration_ms=_duration_ms(self.started_at),
                )

    async def _pump_output(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not self.closed:
            remaining = self.idle_timeout_sec - (time.monotonic() - self.last_activity)
            if remaining <= 0:
                logger.warning(
                    "agent_channel.pty_idle_timeout",
                    backend_id=self.backend.id,
                    backend=self.backend.name,
                    container=self.backend.container,
                    frame_id=self.frame_id,
                    idle_timeout_sec=self.idle_timeout_sec,
                )
                await self.send_json(
                    {
                        "id": self.frame_id,
                        "type": "error",
                        "code": "pty_idle_timeout",
                        "message": "pty session idle timeout exceeded",
                    }
                )
                await self.expire(reason="idle_timeout")
                return
            master_fd = self.master_fd
            if master_fd is None:
                return
            try:
                await _wait_for_fd(master_fd, writable=False, timeout=remaining)
            except TimeoutError:
                continue
            try:
                chunk = os.read(master_fd, MAX_OUTPUT_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError:
                tail = decoder.decode(b"", final=True)
                if tail:
                    await self.send_json(
                        {"id": self.frame_id, "type": "pty.output", "data": tail}
                    )
                return
            if not chunk:
                tail = decoder.decode(b"", final=True)
                if tail:
                    await self.send_json(
                        {"id": self.frame_id, "type": "pty.output", "data": tail}
                    )
                return
            self.last_activity = time.monotonic()
            await self.send_json(
                {
                    "id": self.frame_id,
                    "type": "pty.output",
                    "data": decoder.decode(chunk),
                }
            )

    async def _wait(self) -> None:
        try:
            if self.process is None:
                return
            code = await self.process.wait()
            if (
                self.pump_task is not None
                and self.pump_task is not asyncio.current_task()
            ):
                try:
                    await asyncio.wait_for(asyncio.shield(self.pump_task), timeout=1)
                except TimeoutError:
                    self.pump_task.cancel()
                    await asyncio.gather(self.pump_task, return_exceptions=True)
            if not self.closed:
                self.closed = True
                self.close_reason = "exit"
                await self.send_json(
                    {"id": self.frame_id, "type": "pty.exit", "code": code}
                )
        finally:
            self._close_master_fd()
            self._release_guest_guard()


async def authenticate_agent_websocket(
    websocket: WebSocket, settings: Settings
) -> None:
    client = getattr(websocket, "client", None)
    client_host = str(getattr(client, "host", "") or "")
    if not access_key_is_configured(settings):
        logger.warning(
            "agent_channel.auth_rejected",
            client_host=client_host,
            reason="access_key_not_configured",
        )
        raise AgentChannelAuthError("access key setup required")
    authorization = str(websocket.headers.get("authorization") or "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        logger.warning(
            "agent_channel.auth_rejected",
            client_host=client_host,
            reason="bearer_token_missing",
        )
        raise AgentChannelAuthError("bearer token required")
    valid, retry_after = await verify_access_key_async(
        settings,
        token.strip(),
        attempt_key=_agent_auth_attempt_key(client_host),
    )
    if retry_after > 0:
        logger.warning(
            "agent_channel.auth_rejected",
            client_host=client_host,
            reason="backoff",
            retry_after_sec=retry_after,
        )
        raise AgentChannelAuthError("authentication temporarily unavailable")
    if not valid:
        logger.warning(
            "agent_channel.auth_rejected",
            client_host=client_host,
            reason="bearer_token_invalid",
        )
        raise AgentChannelAuthError("invalid bearer token")
    logger.info(
        "agent_channel.authenticated",
        client_host=client_host,
    )


async def run_agent_channel(
    websocket: WebSocket,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()
    exec_tasks: dict[str, asyncio.Task[None]] = {}
    terminal_exec_ids: set[str] = set()
    pty_session: PtySession | None = None

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload, separators=(",", ":")))
            if payload.get("type") in {"error", "exit"} and payload.get("id"):
                terminal_exec_ids.add(str(payload["id"]))

    await send_json(
        {
            "type": "ready",
            "version": AGENT_CHANNEL_VERSION,
            "modes": ["exec", "pty"],
            "scope": "tenant",
        }
    )

    try:
        while True:
            try:
                raw_frame = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            if len(raw_frame.encode("utf-8", errors="replace")) > MAX_FRAME_BYTES:
                await send_json(
                    {
                        "type": "error",
                        "code": "frame_too_large",
                        "message": "agent channel frame is too large",
                    }
                )
                continue
            frame: dict[str, Any] | None = None
            try:
                frame = _parse_frame(raw_frame)
                frame_type = str(frame.get("type") or "")
                frame_id = _frame_id(frame)
                if frame_type == "exec":
                    if frame_id in exec_tasks:
                        raise AgentChannelFrameError(
                            "duplicate_id",
                            "an operation with this id is already running",
                        )
                    backend = await _resolve_backend(session_factory, frame)
                    terminal_exec_ids.discard(frame_id)
                    task = asyncio.create_task(
                        _run_exec_frame(
                            frame,
                            frame_id,
                            backend,
                            send_json,
                            settings=settings,
                        ),
                        name=f"agent-exec-{frame_id}",
                    )
                    exec_tasks[frame_id] = task
                    task.add_done_callback(
                        lambda _task, _frame_id=frame_id: exec_tasks.pop(
                            _frame_id, None
                        )
                    )
                    continue
                if frame_type == "cancel":
                    target_id = str(
                        frame.get("target_id") or frame.get("id") or ""
                    ).strip()
                    if target_id in exec_tasks:
                        task = exec_tasks[target_id]
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        if target_id not in terminal_exec_ids:
                            await send_json(
                                {
                                    "id": target_id,
                                    "type": "error",
                                    "code": "cancelled",
                                    "message": "exec cancelled",
                                }
                            )
                        continue
                    if pty_session is not None and target_id == pty_session.frame_id:
                        await pty_session.close(reason="cancel")
                        pty_session = None
                        await send_json({"id": target_id, "type": "cancelled"})
                        continue
                    raise AgentChannelFrameError(
                        "unknown_operation",
                        "no running operation matches the cancel target",
                    )
                if frame_type == "pty.open":
                    if pty_session is not None and not pty_session.closed:
                        raise AgentChannelFrameError(
                            "pty_already_open", "this channel already has an open pty"
                        )
                    backend = await _resolve_backend(session_factory, frame)
                    candidate_pty = PtySession(
                        frame_id=frame_id,
                        backend=backend,
                        shell=_shell_value(frame, default="/bin/bash"),
                        cols=_bounded_int(
                            frame.get("cols"), default=120, min_value=20, max_value=240
                        ),
                        rows=_bounded_int(
                            frame.get("rows"), default=32, min_value=8, max_value=120
                        ),
                        idle_timeout_sec=_bounded_int(
                            frame.get("idle_timeout_sec"),
                            default=DEFAULT_PTY_IDLE_TIMEOUT_SEC,
                            min_value=30,
                            max_value=MAX_PTY_IDLE_TIMEOUT_SEC,
                        ),
                        send_json=send_json,
                        settings=settings,
                    )
                    try:
                        await candidate_pty.start()
                    except BaseException:
                        try:
                            await candidate_pty.close(reason="startup_failed")
                        except BaseException as cleanup_error:
                            logger.warning(
                                "agent_channel.pty_candidate_cleanup_failed",
                                backend_id=backend.id,
                                backend=backend.name,
                                container=backend.container,
                                frame_id=frame_id,
                                process_pid=candidate_pty.process.pid
                                if candidate_pty.process is not None
                                else None,
                                reason="startup_failed",
                                error=str(cleanup_error),
                            )
                        raise
                    pty_session = candidate_pty
                    continue
                if frame_type == "pty.stdin":
                    if pty_session is None:
                        raise AgentChannelFrameError(
                            "pty_not_open", "no pty is open on this channel"
                        )
                    _require_matching_pty(frame, pty_session)
                    await pty_session.write(str(frame.get("data") or ""))
                    continue
                if frame_type == "pty.resize":
                    if pty_session is None:
                        raise AgentChannelFrameError(
                            "pty_not_open", "no pty is open on this channel"
                        )
                    _require_matching_pty(frame, pty_session)
                    await pty_session.resize(
                        _bounded_int(
                            frame.get("cols"),
                            default=pty_session.cols,
                            min_value=20,
                            max_value=240,
                        ),
                        _bounded_int(
                            frame.get("rows"),
                            default=pty_session.rows,
                            min_value=8,
                            max_value=120,
                        ),
                    )
                    continue
                if frame_type == "pty.close":
                    if pty_session is None:
                        raise AgentChannelFrameError(
                            "pty_not_open", "no pty is open on this channel"
                        )
                    _require_matching_pty(frame, pty_session)
                    await pty_session.close(reason="client_close")
                    await send_json({"id": pty_session.frame_id, "type": "pty.closed"})
                    pty_session = None
                    continue
                raise AgentChannelFrameError(
                    "unknown_type", "unsupported agent channel frame type"
                )
            except AgentChannelFrameError as exc:
                payload: dict[str, Any] = {
                    "type": "error",
                    "code": exc.code,
                    "message": exc.message,
                }
                if frame is not None and frame.get("id"):
                    payload["id"] = str(frame.get("id"))
                await send_json(payload)
    finally:
        for task in exec_tasks.values():
            task.cancel()
        if exec_tasks:
            await asyncio.gather(*exec_tasks.values(), return_exceptions=True)
        if pty_session is not None:
            await pty_session.close(reason="disconnect")


async def _resolve_backend(
    session_factory: SessionFactory, frame: dict[str, Any]
) -> ResolvedBackend:
    output = str(frame.get("output") or frame.get("backend") or "").strip()
    if not output:
        raise AgentChannelFrameError(
            "missing_output", "frame must include an output name"
        )
    async with session_factory() as session:
        backend = (
            await session.execute(select(Backend).where(Backend.name == output))
        ).scalar_one_or_none()
    if backend is None:
        raise AgentChannelFrameError("backend_not_found", f"output not found: {output}")
    if str(backend.kind or "").lower() != "app":
        raise AgentChannelFrameError(
            "backend_not_app", f"output is not an app backend: {output}"
        )
    if not backend.enabled:
        raise AgentChannelFrameError(
            "backend_disabled", f"output is disabled: {output}"
        )
    return ResolvedBackend(
        name=backend.name,
        container=container_name(backend.name),
        id=backend.id,
    )


async def _run_exec_frame(
    frame: dict[str, Any],
    frame_id: str,
    backend: ResolvedBackend,
    send_json: SendJson,
    *,
    settings: Settings,
) -> None:
    started_at = time.monotonic()
    timeout_sec = _bounded_int(
        frame.get("timeout_sec"),
        default=DEFAULT_EXEC_TIMEOUT_SEC,
        min_value=1,
        max_value=MAX_EXEC_TIMEOUT_SEC,
    )
    command = _exec_command(frame, backend.container, timeout_sec=timeout_sec)
    if read_guest_exec_circuit(settings, backend.name):
        await _send_frame_error(frame_id, _guest_exec_circuit_error(), send_json)
        return
    with backend_guest_exec_guard(settings, backend.name) as guard_state:
        if guard_state != BackendGuestExecGuardState.ACQUIRED:
            await _send_frame_error(
                frame_id, _guest_exec_frame_error(guard_state), send_json
            )
            return
        if read_guest_exec_circuit(settings, backend.name):
            await _send_frame_error(frame_id, _guest_exec_circuit_error(), send_json)
            return

        process: asyncio.subprocess.Process | None = None
        stream_tasks: list[asyncio.Task[None]] = []
        exec_finished_logged = False
        try:
            await send_json(
                {
                    "id": frame_id,
                    "type": "start",
                    "output": backend.name,
                    "container": backend.container,
                    "mode": "shell" if "shell" in frame else "argv",
                }
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info(
                "agent_channel.exec_started",
                backend_id=backend.id,
                backend=backend.name,
                container=backend.container,
                frame_id=frame_id,
                process_pid=process.pid,
                mode="shell" if "shell" in frame else "argv",
                timeout_sec=timeout_sec,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            stream_tasks = [
                asyncio.create_task(
                    _stream_reader(process.stdout, frame_id, "stdout", send_json)
                ),
                asyncio.create_task(
                    _stream_reader(process.stderr, frame_id, "stderr", send_json)
                ),
            ]
            try:
                code = await asyncio.wait_for(process.wait(), timeout=timeout_sec)
            except TimeoutError:
                logger.warning(
                    "agent_channel.exec_timeout",
                    backend_id=backend.id,
                    backend=backend.name,
                    container=backend.container,
                    frame_id=frame_id,
                    process_pid=process.pid,
                    timeout_kind="host",
                    timeout_sec=timeout_sec,
                    duration_ms=_duration_ms(started_at),
                )
                open_guest_exec_circuit(
                    settings,
                    backend.name,
                    timeout_sec=timeout_sec,
                    error="agent exec timeout exceeded",
                    source="agent_exec",
                )
                await _finish_exec_process(process, stream_tasks, terminate=True)
                _log_exec_finished(
                    backend,
                    frame_id,
                    process,
                    started_at,
                    outcome="timeout",
                )
                exec_finished_logged = True
                await send_json(
                    {
                        "id": frame_id,
                        "type": "error",
                        "code": "timeout",
                        "message": "exec timeout exceeded",
                        "duration_ms": _duration_ms(started_at),
                    }
                )
                return
            await _finish_exec_process(process, stream_tasks)
            if code == 124:
                logger.warning(
                    "agent_channel.exec_timeout",
                    backend_id=backend.id,
                    backend=backend.name,
                    container=backend.container,
                    frame_id=frame_id,
                    process_pid=process.pid,
                    timeout_kind="guest",
                    timeout_sec=timeout_sec,
                    duration_ms=_duration_ms(started_at),
                )
                open_guest_exec_circuit(
                    settings,
                    backend.name,
                    timeout_sec=timeout_sec,
                    error="guest command timeout exceeded",
                    source="agent_exec",
                )
            _log_exec_finished(
                backend,
                frame_id,
                process,
                started_at,
                outcome="timeout" if code == 124 else "exited",
                exit_code=code,
            )
            exec_finished_logged = True
            await send_json(
                {
                    "id": frame_id,
                    "type": "exit",
                    "code": code,
                    "duration_ms": _duration_ms(started_at),
                }
            )
        except asyncio.CancelledError:
            await _finish_exec_process(process, stream_tasks, terminate=True)
            _log_exec_finished(
                backend,
                frame_id,
                process,
                started_at,
                outcome="cancelled",
            )
            await send_json(
                {
                    "id": frame_id,
                    "type": "error",
                    "code": "cancelled",
                    "message": "exec cancelled",
                    "duration_ms": _duration_ms(started_at),
                }
            )
            raise
        except FileNotFoundError as exc:
            await _finish_exec_process(process, stream_tasks, terminate=True)
            _log_exec_finished(
                backend,
                frame_id,
                process,
                started_at,
                outcome="runtime_unavailable",
            )
            await send_json(
                {
                    "id": frame_id,
                    "type": "error",
                    "code": "runtime_unavailable",
                    "message": str(exc),
                }
            )
        except Exception as exc:
            await _finish_exec_process(process, stream_tasks, terminate=True)
            logger.warning(
                "agent_channel.exec_failed",
                backend_id=backend.id,
                backend=backend.name,
                container=backend.container,
                frame_id=frame_id,
                process_pid=process.pid if process is not None else None,
                error=str(exc),
            )
            if not exec_finished_logged:
                _log_exec_finished(
                    backend,
                    frame_id,
                    process,
                    started_at,
                    outcome="failed",
                )
            await send_json(
                {
                    "id": frame_id,
                    "type": "error",
                    "code": "exec_failed",
                    "message": str(exc),
                }
            )


async def _send_frame_error(
    frame_id: str,
    error: AgentChannelFrameError,
    send_json: SendJson,
) -> None:
    await send_json(
        {
            "id": frame_id,
            "type": "error",
            "code": error.code,
            "message": error.message,
        }
    )


def _log_exec_finished(
    backend: ResolvedBackend,
    frame_id: str,
    process: asyncio.subprocess.Process | None,
    started_at: float,
    *,
    outcome: str,
    exit_code: int | None = None,
) -> None:
    logger.info(
        "agent_channel.exec_finished",
        backend_id=backend.id,
        backend=backend.name,
        container=backend.container,
        frame_id=frame_id,
        process_pid=process.pid if process is not None else None,
        exit_code=(
            exit_code
            if exit_code is not None
            else (process.returncode if process is not None else None)
        ),
        outcome=outcome,
        duration_ms=_duration_ms(started_at),
    )


async def _finish_exec_process(
    process: asyncio.subprocess.Process | None,
    stream_tasks: list[asyncio.Task[None]],
    *,
    terminate: bool = False,
) -> None:
    if terminate and process is not None and process.returncode is None:
        await _terminate_process(process)
    if stream_tasks:
        await asyncio.gather(*stream_tasks, return_exceptions=True)


async def _stream_reader(
    reader: asyncio.StreamReader,
    frame_id: str,
    stream: str,
    send_json: SendJson,
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await reader.read(MAX_OUTPUT_CHUNK_BYTES)
        if not chunk:
            tail = decoder.decode(b"", final=True)
            if tail:
                await send_json({"id": frame_id, "type": stream, "data": tail})
            return
        await send_json(
            {
                "id": frame_id,
                "type": stream,
                "data": decoder.decode(chunk),
            }
        )


def _parse_frame(raw_frame: str) -> dict[str, Any]:
    try:
        frame = json.loads(raw_frame)
    except json.JSONDecodeError:
        raise AgentChannelFrameError(
            "invalid_json", "frame must be valid JSON"
        ) from None
    if not isinstance(frame, dict):
        raise AgentChannelFrameError("invalid_frame", "frame must be a JSON object")
    return frame


def _frame_id(frame: dict[str, Any]) -> str:
    frame_id = str(frame.get("id") or "").strip()
    if not frame_id or len(frame_id) > 80:
        raise AgentChannelFrameError(
            "invalid_id", "frame id is required and must be 80 characters or fewer"
        )
    return frame_id


def _exec_command(
    frame: dict[str, Any],
    container: str,
    *,
    timeout_sec: int = DEFAULT_EXEC_TIMEOUT_SEC,
) -> list[str]:
    has_argv = "argv" in frame
    has_shell = "shell" in frame
    if has_argv == has_shell:
        raise AgentChannelFrameError(
            "invalid_exec", "exec must include exactly one of argv or shell"
        )
    if has_shell:
        shell = str(frame.get("shell") or "")
        if not shell.strip():
            raise AgentChannelFrameError(
                "invalid_shell", "shell command cannot be blank"
            )
        guest_timeout = max(1, timeout_sec - 1)
        return [
            "podman",
            "exec",
            "-i",
            container,
            "timeout",
            "--signal=TERM",
            "--kill-after=1s",
            f"{guest_timeout}s",
            "bash",
            "-lc",
            shell,
        ]
    argv = frame.get("argv")
    if not isinstance(argv, list) or not argv:
        raise AgentChannelFrameError("invalid_argv", "argv must be a non-empty list")
    values = [str(item) for item in argv]
    if any(not value or "\x00" in value for value in values):
        raise AgentChannelFrameError(
            "invalid_argv", "argv values must be non-empty strings"
        )
    guest_timeout = max(1, timeout_sec - 1)
    return [
        "podman",
        "exec",
        "-i",
        container,
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        f"{guest_timeout}s",
        *values,
    ]


def _shell_value(frame: dict[str, Any], *, default: str) -> str:
    shell = str(frame.get("shell") or default).strip()
    if not shell.startswith("/"):
        raise AgentChannelFrameError(
            "invalid_shell", "pty shell must be an absolute path"
        )
    return shell


def _bounded_int(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def _require_matching_pty(frame: dict[str, Any], session: PtySession) -> None:
    frame_id = _frame_id(frame)
    if frame_id != session.frame_id:
        raise AgentChannelFrameError(
            "pty_id_mismatch", "pty frame id does not match the open pty"
        )


def _set_pty_size(fd: int, cols: int, rows: int) -> None:
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


async def _wait_for_fd(
    fd: int, *, writable: bool, timeout: float | None = None
) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    register = loop.add_writer if writable else loop.add_reader
    remove = loop.remove_writer if writable else loop.remove_reader
    # Cancellation removes the readiness callback; no blocked OS read survives the task.
    register(fd, mark_ready)
    try:
        if timeout is None:
            await ready
        else:
            await asyncio.wait_for(ready, timeout=timeout)
    finally:
        remove(fd)


async def _write_nonblocking(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            await _wait_for_fd(fd, writable=True)
            continue
        remaining = remaining[written:]


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        try:
            if process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def agent_channel_client_example(host: str) -> str:
    endpoint = f"wss://{host}{AGENT_CHANNEL_PATH}"
    frame = json.dumps(
        {
            "id": "1",
            "type": "exec",
            "output": "<output>",
            "argv": ["python3", "--version"],
        },
        separators=(",", ":"),
    )
    return (
        "websocat "
        "-H 'Authorization: Bearer <admin-access-key>' "
        f"{shlex.quote(endpoint)} "
        f"<<< {shlex.quote(frame)}"
    )
