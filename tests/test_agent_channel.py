from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import access
from app.access import hash_access_key
from app.config import Settings
from app.database import Base
from app.models.entities import Backend
from app.services import agent_channel
from app.services.llm_help import (
    build_backend_llm_help_payload,
    render_backend_llm_help_text,
)


async def _make_session(db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _reader_with_payload(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


class FakeProcess:
    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
    ) -> None:
        self.stdout = _reader_with_payload(stdout)
        self.stderr = _reader_with_payload(stderr)
        self.returncode = None
        self.pid = 0
        self._returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_exec_command_accepts_argv_and_shell() -> None:
    assert agent_channel._exec_command(
        {"argv": ["python3", "--version"]},
        "cnc-app-web",
    ) == [
        "podman",
        "exec",
        "-i",
        "cnc-app-web",
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        "59s",
        "python3",
        "--version",
    ]
    assert agent_channel._exec_command(
        {"shell": "cd /app && pytest -q"},
        "cnc-app-web",
    ) == [
        "podman",
        "exec",
        "-i",
        "cnc-app-web",
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        "59s",
        "bash",
        "-lc",
        "cd /app && pytest -q",
    ]


def test_exec_command_rejects_ambiguous_command_mode() -> None:
    with pytest.raises(agent_channel.AgentChannelFrameError) as exc:
        agent_channel._exec_command(
            {"argv": ["python3"], "shell": "python3 --version"},
            "cnc-app-web",
        )
    assert exc.value.code == "invalid_exec"


@pytest.mark.asyncio
async def test_authenticate_agent_websocket_requires_bearer_access_key(
    monkeypatch,
) -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    events: list[tuple[str, dict[str, object]]] = []

    class RecordingLogger:
        def info(self, event: str, **context: object) -> None:
            events.append((event, context))

        def warning(self, event: str, **context: object) -> None:
            events.append((event, context))

    monkeypatch.setattr(agent_channel, "logger", RecordingLogger())
    websocket = SimpleNamespace(
        headers={"authorization": "Bearer correct horse battery staple"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    await agent_channel.authenticate_agent_websocket(websocket, settings)

    bad_websocket = SimpleNamespace(
        headers={"authorization": "Bearer wrong horse battery staple"},
        client=SimpleNamespace(host="127.0.0.2"),
    )
    with pytest.raises(agent_channel.AgentChannelAuthError):
        await agent_channel.authenticate_agent_websocket(bad_websocket, settings)

    assert events == [
        (
            "agent_channel.authenticated",
            {"client_host": "127.0.0.1"},
        ),
        (
            "agent_channel.auth_rejected",
            {"client_host": "127.0.0.2", "reason": "bearer_token_invalid"},
        ),
    ]
    assert "correct horse battery staple" not in str(events)


@pytest.mark.asyncio
async def test_agent_websocket_auth_backoff_skips_repeated_argon2_work(
    monkeypatch,
) -> None:
    settings = Settings(access_key_hash="configured")
    client_host = "100.64.0.231"
    attempt_key = agent_channel._agent_auth_attempt_key(client_host)
    access.record_access_attempt(attempt_key, success=True)
    verify_calls = 0
    events: list[tuple[str, dict[str, object]]] = []

    def fake_verify(_settings, _token) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return False

    class RecordingLogger:
        def info(self, event: str, **context: object) -> None:
            events.append((event, context))

        def warning(self, event: str, **context: object) -> None:
            events.append((event, context))

    monkeypatch.setattr(access, "verify_access_key", fake_verify)
    monkeypatch.setattr(agent_channel, "logger", RecordingLogger())
    websocket = SimpleNamespace(
        headers={"authorization": "Bearer invalid key material"},
        client=SimpleNamespace(host=client_host),
    )

    with pytest.raises(agent_channel.AgentChannelAuthError):
        await agent_channel.authenticate_agent_websocket(websocket, settings)
    with pytest.raises(agent_channel.AgentChannelAuthError):
        await agent_channel.authenticate_agent_websocket(websocket, settings)

    assert verify_calls == 1
    assert events[0][1]["reason"] == "bearer_token_invalid"
    assert events[1][1]["reason"] == "backoff"
    assert int(events[1][1]["retry_after_sec"]) >= 1
    access.record_access_attempt(attempt_key, success=True)


@pytest.mark.asyncio
async def test_agent_websocket_auth_bounds_parallel_argon2_work(monkeypatch) -> None:
    settings = Settings(access_key_hash="configured")
    active = 0
    maximum_active = 0
    counter_lock = threading.Lock()

    def fake_verify(_settings, _token) -> bool:
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return True

    monkeypatch.setattr(access, "verify_access_key", fake_verify)
    monkeypatch.setattr(
        access,
        "_ACCESS_VERIFY_SEMAPHORE",
        asyncio.BoundedSemaphore(access.ACCESS_MAX_CONCURRENT_VERIFICATIONS),
    )
    websockets = [
        SimpleNamespace(
            headers={"authorization": "Bearer valid key material"},
            client=SimpleNamespace(host=f"100.64.1.{index}"),
        )
        for index in range(1, 7)
    ]

    await asyncio.gather(
        *(
            agent_channel.authenticate_agent_websocket(websocket, settings)
            for websocket in websockets
        )
    )

    assert maximum_active == access.ACCESS_MAX_CONCURRENT_VERIFICATIONS


@pytest.mark.asyncio
async def test_resolve_backend_requires_enabled_app_output(tmp_path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    async with maker() as session:
        session.add_all(
            [
                Backend(name="web", kind="app", enabled=True),
                Backend(name="docs", kind="static", enabled=True),
                Backend(name="draft", kind="app", enabled=False),
            ]
        )
        await session.commit()

    resolved = await agent_channel._resolve_backend(maker, {"output": "web"})
    assert resolved.name == "web"
    assert resolved.container == "cnc-app-web"

    with pytest.raises(agent_channel.AgentChannelFrameError) as static_exc:
        await agent_channel._resolve_backend(maker, {"output": "docs"})
    assert static_exc.value.code == "backend_not_app"

    with pytest.raises(agent_channel.AgentChannelFrameError) as disabled_exc:
        await agent_channel._resolve_backend(maker, {"output": "draft"})
    assert disabled_exc.value.code == "backend_disabled"


@pytest.mark.asyncio
async def test_resolve_backend_reloads_authorization_state_for_each_frame(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    async with maker() as session:
        session.add(Backend(name="web", kind="app", enabled=True))
        await session.commit()

    assert (
        await agent_channel._resolve_backend(maker, {"output": "web"})
    ).name == "web"

    async with maker() as session:
        backend = (await session.execute(select(Backend))).scalar_one()
        backend.enabled = False
        await session.commit()

    with pytest.raises(agent_channel.AgentChannelFrameError) as disabled_exc:
        await agent_channel._resolve_backend(maker, {"output": "web"})
    assert disabled_exc.value.code == "backend_disabled"


@pytest.mark.asyncio
async def test_run_agent_channel_closes_failed_pty_candidate(monkeypatch) -> None:
    closed = 0

    class FailedPtySession:
        closed = False

        def __init__(self, **kwargs) -> None:
            self.frame_id = kwargs["frame_id"]

        async def start(self) -> None:
            raise agent_channel.AgentChannelFrameError(
                "backend_busy", "guest exec guard is busy"
            )

        async def close(self, **_kwargs) -> None:
            nonlocal closed
            closed += 1
            self.closed = True

    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps({"id": "pty-1", "type": "pty.open", "output": "web"})
            ]
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def receive_text(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            raise agent_channel.WebSocketDisconnect()

        async def send_text(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

    async def resolve_backend(*_args, **_kwargs):
        return agent_channel.ResolvedBackend(name="web", container="cnc-app-web")

    monkeypatch.setattr(agent_channel, "PtySession", FailedPtySession)
    monkeypatch.setattr(agent_channel, "_resolve_backend", resolve_backend)
    websocket = FakeWebSocket()

    def unused_session_factory():
        raise AssertionError("backend resolution is stubbed")

    await agent_channel.run_agent_channel(websocket, Settings(), unused_session_factory)

    assert closed == 1
    assert websocket.sent[-1]["code"] == "backend_busy"


@pytest.mark.asyncio
async def test_run_agent_channel_cancel_emits_one_terminal_exec_frame(
    monkeypatch,
) -> None:
    started = asyncio.Event()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.receive_count = 0
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def receive_text(self) -> str:
            self.receive_count += 1
            if self.receive_count == 1:
                return json.dumps(
                    {
                        "id": "exec-1",
                        "type": "exec",
                        "output": "web",
                        "argv": ["sleep", "10"],
                    }
                )
            if self.receive_count == 2:
                await started.wait()
                return json.dumps({"id": "exec-1", "type": "cancel"})
            await asyncio.sleep(0)
            raise agent_channel.WebSocketDisconnect()

        async def send_text(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

    async def resolve_backend(*_args, **_kwargs):
        return agent_channel.ResolvedBackend(name="web", container="cnc-app-web")

    async def run_exec(_frame, frame_id, _backend, send_json, **_kwargs) -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await send_json(
                {
                    "id": frame_id,
                    "type": "error",
                    "code": "cancelled",
                    "message": "exec cancelled",
                }
            )
            raise

    monkeypatch.setattr(agent_channel, "_resolve_backend", resolve_backend)
    monkeypatch.setattr(agent_channel, "_run_exec_frame", run_exec)
    websocket = FakeWebSocket()

    def unused_session_factory():
        raise AssertionError("backend resolution is stubbed")

    await agent_channel.run_agent_channel(websocket, Settings(), unused_session_factory)

    terminal_frames = [
        frame
        for frame in websocket.sent
        if frame.get("id") == "exec-1" and frame.get("type") in {"error", "cancelled"}
    ]
    assert terminal_frames == [
        {
            "id": "exec-1",
            "type": "error",
            "code": "cancelled",
            "message": "exec cancelled",
        }
    ]


@pytest.mark.asyncio
async def test_run_agent_channel_cancel_before_exec_starts_emits_terminal_frame(
    monkeypatch,
) -> None:
    started = False

    class FakeWebSocket:
        def __init__(self) -> None:
            self.frames = [
                json.dumps(
                    {
                        "id": "exec-1",
                        "type": "exec",
                        "output": "web",
                        "argv": ["sleep", "10"],
                    }
                ),
                json.dumps({"id": "exec-1", "type": "cancel"}),
            ]
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def receive_text(self) -> str:
            if self.frames:
                return self.frames.pop(0)
            raise agent_channel.WebSocketDisconnect()

        async def send_text(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

    async def resolve_backend(*_args, **_kwargs):
        return agent_channel.ResolvedBackend(name="web", container="cnc-app-web")

    async def run_exec(*_args, **_kwargs) -> None:
        nonlocal started
        started = True
        await asyncio.Future()

    monkeypatch.setattr(agent_channel, "_resolve_backend", resolve_backend)
    monkeypatch.setattr(agent_channel, "_run_exec_frame", run_exec)
    websocket = FakeWebSocket()

    def unused_session_factory():
        raise AssertionError("backend resolution is stubbed")

    await agent_channel.run_agent_channel(websocket, Settings(), unused_session_factory)

    terminal_frames = [
        frame
        for frame in websocket.sent
        if frame.get("id") == "exec-1" and frame.get("type") == "error"
    ]
    assert started is False
    assert terminal_frames == [
        {
            "id": "exec-1",
            "type": "error",
            "code": "cancelled",
            "message": "exec cancelled",
        }
    ]


def _pty_session(
    send_json,
    *,
    idle_timeout_sec: int = 60,
    settings: Settings | None = None,
) -> agent_channel.PtySession:
    return agent_channel.PtySession(
        frame_id="pty-1",
        backend=agent_channel.ResolvedBackend(name="web", container="cnc-app-web"),
        shell="/bin/bash",
        cols=120,
        rows=32,
        idle_timeout_sec=idle_timeout_sec,
        send_json=send_json,
        settings=settings or Settings(),
    )


@pytest.mark.asyncio
async def test_pty_idle_timeout_closes_descriptor_without_worker_thread() -> None:
    frames: list[dict[str, object]] = []

    async def send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    master_fd, slave_fd = os.openpty()
    session = _pty_session(send_json, idle_timeout_sec=1)
    session.master_fd = master_fd
    os.set_blocking(master_fd, False)
    session.last_activity = time.monotonic() - 2
    try:
        await session._pump_output()
    finally:
        os.close(slave_fd)

    assert frames[-1]["code"] == "pty_idle_timeout"
    assert session.closed is True
    assert session.master_fd is None


@pytest.mark.asyncio
async def test_pty_close_awaits_reader_waiter_and_process_cleanup() -> None:
    async def send_json(_payload: dict[str, object]) -> None:
        return None

    process = FakeProcess()
    session = _pty_session(send_json)
    master_fd, slave_fd = os.openpty()
    session.master_fd = master_fd
    session.process = process
    session.pump_task = asyncio.create_task(asyncio.Event().wait())
    session.wait_task = asyncio.create_task(asyncio.Event().wait())
    try:
        await session.close()
    finally:
        os.close(slave_fd)

    assert process.returncode == 0
    assert session.pump_task.done()
    assert session.wait_task.done()
    assert session.master_fd is None


@pytest.mark.asyncio
async def test_pty_start_send_failure_cleans_process_tasks_fds_and_guard(
    monkeypatch, tmp_path
) -> None:
    guard_events: list[str] = []

    @contextmanager
    def acquired_guard(*_args, **_kwargs):
        guard_events.append("acquired")
        try:
            yield agent_channel.BackendGuestExecGuardState.ACQUIRED
        finally:
            guard_events.append("released")

    class RunningProcess(FakeProcess):
        async def wait(self) -> int:
            await asyncio.Future()
            return 0

    process = RunningProcess()

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        return process

    async def fake_terminate(target) -> None:
        target.returncode = -15

    async def failing_send(_payload) -> None:
        raise RuntimeError("websocket closed")

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", acquired_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)
    monkeypatch.setattr(
        agent_channel.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(agent_channel, "_terminate_process", fake_terminate)
    session = _pty_session(
        failing_send,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    with pytest.raises(RuntimeError, match="websocket closed"):
        await session.start()

    assert process.returncode == -15
    assert session.closed is True
    assert session.master_fd is None
    assert session.pump_task is not None and session.pump_task.done()
    assert session.wait_task is not None and session.wait_task.done()
    assert guard_events == ["acquired", "released"]


@pytest.mark.asyncio
async def test_pty_wait_releases_fd_and_guard_when_exit_send_fails(tmp_path) -> None:
    guard_events: list[str] = []

    @contextmanager
    def acquired_guard():
        guard_events.append("acquired")
        try:
            yield agent_channel.BackendGuestExecGuardState.ACQUIRED
        finally:
            guard_events.append("released")

    async def failing_send(_payload) -> None:
        raise RuntimeError("send failed")

    session = _pty_session(failing_send)
    guard = acquired_guard()
    guard.__enter__()
    session.exec_guard = guard
    session.process = FakeProcess()
    master_fd, slave_fd = os.openpty()
    session.master_fd = master_fd
    try:
        with pytest.raises(RuntimeError, match="send failed"):
            await session._wait()
    finally:
        os.close(slave_fd)

    assert session.master_fd is None
    assert guard_events == ["acquired", "released"]


@pytest.mark.asyncio
async def test_pty_close_releases_fd_and_guard_when_termination_fails(
    monkeypatch,
) -> None:
    guard_events: list[str] = []

    @contextmanager
    def acquired_guard():
        guard_events.append("acquired")
        try:
            yield agent_channel.BackendGuestExecGuardState.ACQUIRED
        finally:
            guard_events.append("released")

    async def failing_terminate(_process) -> None:
        raise RuntimeError("termination failed")

    session = _pty_session(lambda _payload: asyncio.sleep(0))
    guard = acquired_guard()
    guard.__enter__()
    session.exec_guard = guard
    session.process = FakeProcess()
    master_fd, slave_fd = os.openpty()
    session.master_fd = master_fd
    monkeypatch.setattr(agent_channel, "_terminate_process", failing_terminate)
    try:
        with pytest.raises(RuntimeError, match="termination failed"):
            await session.close()
    finally:
        os.close(slave_fd)

    assert session.master_fd is None
    assert guard_events == ["acquired", "released"]


@pytest.mark.asyncio
async def test_pty_expire_releases_fd_and_guard_when_task_gather_fails(
    monkeypatch,
) -> None:
    guard_events: list[str] = []

    @contextmanager
    def acquired_guard():
        guard_events.append("acquired")
        try:
            yield agent_channel.BackendGuestExecGuardState.ACQUIRED
        finally:
            guard_events.append("released")

    original_gather = asyncio.gather

    async def failing_gather(*_args, **_kwargs):
        raise RuntimeError("gather failed")

    session = _pty_session(lambda _payload: asyncio.sleep(0))
    guard = acquired_guard()
    guard.__enter__()
    session.exec_guard = guard
    process = FakeProcess()
    process.returncode = 0
    session.process = process
    session.pump_task = asyncio.create_task(asyncio.Event().wait())
    master_fd, slave_fd = os.openpty()
    session.master_fd = master_fd
    monkeypatch.setattr(agent_channel.asyncio, "gather", failing_gather)
    try:
        with pytest.raises(RuntimeError, match="gather failed"):
            await session.expire()
    finally:
        os.close(slave_fd)
        await original_gather(session.pump_task, return_exceptions=True)

    assert session.master_fd is None
    assert guard_events == ["acquired", "released"]


@pytest.mark.asyncio
async def test_terminate_process_waits_after_forced_kill(monkeypatch) -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.pid = 0
            self.wait_calls = 0
            self.killed = False

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                await asyncio.Future()
            self.returncode = -9
            return -9

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    real_wait_for = asyncio.wait_for

    async def immediate_timeout(awaitable, *, timeout):
        task = asyncio.create_task(awaitable)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise TimeoutError

    monkeypatch.setattr(agent_channel.asyncio, "wait_for", immediate_timeout)
    process = StubbornProcess()
    await agent_channel._terminate_process(process)
    monkeypatch.setattr(agent_channel.asyncio, "wait_for", real_wait_for)

    assert process.killed is True
    assert process.wait_calls == 2
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_run_exec_frame_streams_output_and_exit(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level("INFO", logger="agent.channel")
    commands: list[list[str]] = []
    frames: list[dict[str, object]] = []

    async def fake_create_subprocess_exec(*command, **_kwargs):
        commands.append(list(command))
        return FakeProcess(stdout=b"Python 3.12.3\n", stderr=b"warn\n", returncode=0)

    async def fake_send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    monkeypatch.setattr(
        agent_channel.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    await agent_channel._run_exec_frame(
        {
            "id": "1",
            "type": "exec",
            "output": "web",
            "argv": ["python3", "--version"],
        },
        "1",
        agent_channel.ResolvedBackend(name="web", container="cnc-app-web", id=7),
        fake_send_json,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    assert commands == [
        [
            "podman",
            "exec",
            "-i",
            "cnc-app-web",
            "timeout",
            "--signal=TERM",
            "--kill-after=1s",
            "59s",
            "python3",
            "--version",
        ]
    ]
    assert frames[0]["type"] == "start"
    assert {"id": "1", "type": "stdout", "data": "Python 3.12.3\n"} in frames
    assert {"id": "1", "type": "stderr", "data": "warn\n"} in frames
    assert frames[-1]["type"] == "exit"
    assert frames[-1]["code"] == 0
    started_log = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "agent.channel.exec.started"
    )
    finished_log = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "agent.channel.exec.finished"
    )
    assert started_log.context["backend_id"] == 7
    assert started_log.context["frame_id"] == "1"
    assert started_log.context["mode"] == "argv"
    assert finished_log.context["exit_code"] == 0
    assert finished_log.context["outcome"] == "exited"
    assert "command" not in started_log.context
    assert "shell" not in started_log.context


@pytest.mark.asyncio
async def test_run_exec_frame_reports_cross_process_guard_contention(
    monkeypatch, tmp_path
) -> None:
    frames: list[dict[str, object]] = []

    async def send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    @contextmanager
    def busy_guard(*_args, **_kwargs):
        yield agent_channel.BackendGuestExecGuardState.BUSY

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", busy_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)

    await agent_channel._run_exec_frame(
        {"id": "busy", "type": "exec", "argv": ["true"]},
        "busy",
        agent_channel.ResolvedBackend(name="web", container="cnc-app-web"),
        send_json,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    assert frames == [
        {
            "id": "busy",
            "type": "error",
            "code": "backend_busy",
            "message": "another CNC-managed guest command is already running",
        }
    ]


@pytest.mark.asyncio
async def test_run_exec_frame_rechecks_circuit_while_guarded(
    monkeypatch, tmp_path
) -> None:
    frames: list[dict[str, object]] = []
    circuit_reads = 0

    async def send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    @contextmanager
    def acquired_guard(*_args, **_kwargs):
        yield agent_channel.BackendGuestExecGuardState.ACQUIRED

    def read_circuit(*_args):
        nonlocal circuit_reads
        circuit_reads += 1
        return None if circuit_reads == 1 else {"open": True}

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", acquired_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", read_circuit)

    await agent_channel._run_exec_frame(
        {"id": "circuit", "type": "exec", "argv": ["true"]},
        "circuit",
        agent_channel.ResolvedBackend(name="web", container="cnc-app-web"),
        send_json,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    assert circuit_reads == 2
    assert frames[-1]["code"] == "backend_exec_unavailable"


@pytest.mark.asyncio
async def test_run_exec_frame_opens_circuit_and_drains_streams_on_host_timeout(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level("WARNING", logger="agent.channel")
    frames: list[dict[str, object]] = []
    opened: list[dict[str, object]] = []
    process = FakeProcess(stdout=b"partial output\n")

    async def send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    async def wait_forever() -> int:
        await asyncio.Future()
        return 0

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        process.wait = wait_forever  # type: ignore[method-assign]
        return process

    async def immediate_timeout(awaitable, *, timeout):
        del timeout
        awaitable.close()
        raise TimeoutError

    async def fake_terminate(target) -> None:
        target.returncode = -9

    @contextmanager
    def acquired_guard(*_args, **_kwargs):
        yield agent_channel.BackendGuestExecGuardState.ACQUIRED

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", acquired_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)
    monkeypatch.setattr(
        agent_channel,
        "open_guest_exec_circuit",
        lambda *_args, **kwargs: opened.append(kwargs),
    )
    monkeypatch.setattr(
        agent_channel.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(agent_channel.asyncio, "wait_for", immediate_timeout)
    monkeypatch.setattr(agent_channel, "_terminate_process", fake_terminate)

    await agent_channel._run_exec_frame(
        {
            "id": "timeout",
            "type": "exec",
            "argv": ["sleep", "10"],
            "timeout_sec": 1,
        },
        "timeout",
        agent_channel.ResolvedBackend(name="web", container="cnc-app-web", id=7),
        send_json,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    assert opened == [
        {
            "timeout_sec": 1,
            "error": "agent exec timeout exceeded",
            "source": "agent_exec",
        }
    ]
    assert {"id": "timeout", "type": "stdout", "data": "partial output\n"} in frames
    assert frames[-1]["code"] == "timeout"
    timeout_log = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "agent.channel.exec.timeout"
    )
    assert timeout_log.context["backend_id"] == 7
    assert timeout_log.context["frame_id"] == "timeout"


@pytest.mark.asyncio
async def test_run_exec_frame_opens_circuit_on_guest_timeout(
    monkeypatch, tmp_path
) -> None:
    frames: list[dict[str, object]] = []
    opened: list[dict[str, object]] = []

    async def send_json(payload: dict[str, object]) -> None:
        frames.append(payload)

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        return FakeProcess(returncode=124)

    @contextmanager
    def acquired_guard(*_args, **_kwargs):
        yield agent_channel.BackendGuestExecGuardState.ACQUIRED

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", acquired_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)
    monkeypatch.setattr(
        agent_channel,
        "open_guest_exec_circuit",
        lambda *_args, **kwargs: opened.append(kwargs),
    )
    monkeypatch.setattr(
        agent_channel.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    await agent_channel._run_exec_frame(
        {"id": "guest-timeout", "type": "exec", "argv": ["sleep", "10"]},
        "guest-timeout",
        agent_channel.ResolvedBackend(name="web", container="cnc-app-web"),
        send_json,
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    assert opened == [
        {
            "timeout_sec": 60,
            "error": "guest command timeout exceeded",
            "source": "agent_exec",
        }
    ]
    assert frames[-1]["type"] == "exit"
    assert frames[-1]["code"] == 124


@pytest.mark.asyncio
async def test_pty_holds_guest_exec_guard_until_close(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level("INFO", logger="agent.channel")
    guard_events: list[str] = []

    @contextmanager
    def acquired_guard(*_args, **_kwargs):
        guard_events.append("acquired")
        try:
            yield agent_channel.BackendGuestExecGuardState.ACQUIRED
        finally:
            guard_events.append("released")

    class RunningProcess(FakeProcess):
        async def wait(self) -> int:
            await asyncio.Future()
            return 0

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        return RunningProcess()

    async def fake_terminate(process) -> None:
        process.returncode = -15

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", acquired_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)
    monkeypatch.setattr(
        agent_channel.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(agent_channel, "_terminate_process", fake_terminate)

    session = _pty_session(
        lambda _payload: asyncio.sleep(0),
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )
    session.backend.id = 7
    await session.start()

    assert guard_events == ["acquired"]

    await session.close()

    assert guard_events == ["acquired", "released"]
    lifecycle = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        in {"agent.channel.pty.opened", "agent.channel.pty.closed"}
    ]
    assert [record.event_name for record in lifecycle] == [
        "agent.channel.pty.opened",
        "agent.channel.pty.closed",
    ]
    assert lifecycle[0].context["backend_id"] == 7
    assert lifecycle[1].context["reason"] == "client_close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard_state", "error_code"),
    (
        (agent_channel.BackendGuestExecGuardState.BUSY, "backend_busy"),
        (
            agent_channel.BackendGuestExecGuardState.UNAVAILABLE,
            "backend_exec_unavailable",
        ),
    ),
)
async def test_pty_reports_guest_exec_guard_failure(
    monkeypatch, tmp_path, guard_state, error_code
) -> None:
    @contextmanager
    def failed_guard(*_args, **_kwargs):
        yield guard_state

    monkeypatch.setattr(agent_channel, "backend_guest_exec_guard", failed_guard)
    monkeypatch.setattr(agent_channel, "read_guest_exec_circuit", lambda *_args: None)
    session = _pty_session(
        lambda _payload: asyncio.sleep(0),
        settings=Settings(app_control_dir=tmp_path / "app-control"),
    )

    with pytest.raises(agent_channel.AgentChannelFrameError) as exc:
        await session.start()

    assert exc.value.code == error_code


def test_agent_channel_ready_frame_is_compact_json() -> None:
    frame = json.dumps(
        {
            "id": "1",
            "type": "exec",
            "output": "web",
            "argv": ["python3", "--version"],
        },
        separators=(",", ":"),
    )
    assert (
        frame
        == '{"id":"1","type":"exec","output":"web","argv":["python3","--version"]}'
    )


def test_backend_llm_help_includes_agent_channel_guidance() -> None:
    backend = Backend(
        name="web", kind="app", enabled=True, port=12000, handoff_port=3000
    )
    payload = build_backend_llm_help_payload(backend, host="scw.example.ts.net")

    assert payload["commands"]["agent_channel"].startswith("websocat ")
    assert (
        "wss://scw.example.ts.net/api/agent/channel"
        in payload["commands"]["agent_channel"]
    )
    assert payload["commands"]["agent_exec_frame"] == (
        '{"id":"1","type":"exec","output":"web","argv":["python3","--version"]}'
    )

    body = render_backend_llm_help_text(payload)
    assert "beta tenant-wide WebSocket channel" in body
    assert '"type":"pty.open"' in body


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["café 漢字 🙂".encode(), b"\xffbad\xe2\x82"])
async def test_command_stream_keeps_unicode_between_reads(data):
    class Reader:
        def __init__(self):
            self.chunks = iter([bytes([value]) for value in data] + [b""])

        async def read(self, _size):
            return next(self.chunks)

    frames = []

    async def send(frame):
        frames.append(frame)

    await agent_channel._stream_reader(Reader(), "stream", "stdout", send)
    assert "".join(frame["data"] for frame in frames) == data.decode(
        "utf-8", errors="replace"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["café 漢字 🙂".encode(), b"\xffbad\xe2\x82"])
async def test_pty_stream_keeps_unicode_between_reads(data, monkeypatch):
    chunks = iter([bytes([value]) for value in data] + [b""])
    frames = []

    async def ready(*_args, **_kwargs):
        return None

    async def send(frame):
        frames.append(frame)

    monkeypatch.setattr(
        agent_channel, "os", SimpleNamespace(read=lambda *_args: next(chunks))
    )
    monkeypatch.setattr(agent_channel, "_wait_for_fd", ready)
    pty = SimpleNamespace(
        closed=False,
        idle_timeout_sec=60,
        last_activity=time.monotonic(),
        master_fd=42,
        frame_id="pty",
        send_json=send,
    )
    await agent_channel.PtySession._pump_output(pty)
    assert "".join(frame["data"] for frame in frames) == data.decode(
        "utf-8", errors="replace"
    )
