from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, CommandJob
from app.services import (
    agent_channel,
    command_job_runtime as runtime,
    command_jobs as jobs,
)
from app.services.command_job_files import GUEST_JOB_ROOT
from app.services.commands import CommandResult


class Socket:
    def __init__(self, *frames):
        self.frames = list(frames)
        self.sent = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        raise agent_channel.WebSocketDisconnect()

    async def send_text(self, payload):
        self.sent.append(json.loads(payload))


@pytest.fixture
async def channel_env(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}",
        app_control_dir=tmp_path / "control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    monkeypatch.setattr(
        "app.services.operations._host_lock_path", lambda _: tmp_path / "host.lock"
    )
    async with jobs.job_session(settings) as session:
        async with session.bind.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session.add_all(
            [
                Backend(name=name, kind="app", port=8090, enabled=True)
                for name in ("demo", "other")
            ]
        )
        await session.commit()
    monkeypatch.setattr(
        runtime,
        "observe_runtime",
        lambda _: {
            "state": "running",
            "container_id": "a" * 64,
            "started_at": "2026-01-01T00:00:00Z",
        },
    )
    launches = []

    def launch(*args):
        launches.append(args)
        directory = (
            settings.app_sandbox_dir
            / "demo"
            / "rootfs"
            / GUEST_JOB_ROOT
            / f"{args[1]}-{args[-1]}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stdout").write_text("")
        (directory / "stderr").write_text("")
        return CommandResult([], 0, "", "")

    monkeypatch.setattr(runtime, "launch_job", launch)
    return settings, lambda: jobs.job_session(settings), launches


async def exchange(env, *frames):
    settings, factory, _ = env
    socket = Socket(*frames)
    await agent_channel.run_agent_channel(socket, settings, factory)
    return socket.sent[1:]


@pytest.mark.asyncio
async def test_background_disconnect_reconnect_discovers_same_job(
    channel_env, monkeypatch
):
    cancel = AsyncMock()
    monkeypatch.setattr(jobs, "cancel_job", cancel)
    frame = {
        "id": "submit",
        "type": "exec",
        "output": "demo",
        "background": True,
        "argv": ["sleep", "2400"],
        "request_key": "build-1",
    }
    first = (await exchange(channel_env, frame))[0]
    assert first["type"] == "operation" and first["status"] == "running"
    operation_id = first["operation_id"]
    cancel.assert_not_awaited()
    settings, _, launches = channel_env
    async with jobs.job_session(settings) as session:
        assert await session.get(CommandJob, operation_id) is not None
    listing = (
        await exchange(
            channel_env, {"id": "list", "type": "operation.list", "output": "demo"}
        )
    )[0]
    assert listing["operations"][0]["operation_id"] == operation_id
    assert listing["operations"][0]["status"] == "running"
    again = (await exchange(channel_env, frame))[0]
    assert again["operation_id"] == operation_id
    assert len(launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,service",
    [("show", "get_job"), ("logs", "job_logs"), ("cancel", "cancel_job")],
)
async def test_other_output_rejected_before_job_service(
    channel_env, monkeypatch, action, service
):
    submitted = await jobs.submit_job(channel_env[0], "demo", {"argv": ["true"]})
    handler = AsyncMock()
    monkeypatch.setattr(jobs, service, handler)
    reply = (
        await exchange(
            channel_env,
            {
                "id": "inspect",
                "type": f"operation.{action}",
                "output": "other",
                "operation_id": submitted["operation_id"],
            },
        )
    )[0]
    assert reply["code"] == "operation_not_found"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", 1, None])
async def test_background_requires_boolean(channel_env, monkeypatch, value):
    submit = AsyncMock()
    monkeypatch.setattr(jobs, "submit_job", submit)
    reply = (
        await exchange(
            channel_env,
            {
                "id": "bad",
                "type": "exec",
                "output": "demo",
                "background": value,
                "argv": ["true"],
            },
        )
    )[0]
    assert reply["code"] == "invalid_background"
    submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "1", 0, -1])
async def test_operation_id_requires_positive_integer(channel_env, value):
    reply = (
        await exchange(
            channel_env,
            {
                "id": "bad",
                "type": "operation.show",
                "output": "demo",
                "operation_id": value,
            },
        )
    )[0]
    assert reply["code"] == "invalid_operation"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-1, True, "0"])
async def test_log_offset_validation(channel_env, monkeypatch, value):
    submitted = await jobs.submit_job(channel_env[0], "demo", {"argv": ["true"]})
    logs = AsyncMock()
    monkeypatch.setattr(jobs, "job_logs", logs)
    reply = (
        await exchange(
            channel_env,
            {
                "id": "bad",
                "type": "operation.logs",
                "output": "demo",
                "operation_id": submitted["operation_id"],
                "offset": value,
            },
        )
    )[0]
    assert reply["code"] == "invalid_offset"
    logs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 101, True, "20"])
async def test_list_limit_is_bounded(channel_env, value):
    reply = (
        await exchange(
            channel_env,
            {"id": "bad", "type": "operation.list", "output": "demo", "limit": value},
        )
    )[0]
    assert reply["code"] == "invalid_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list", "show", "logs", "cancel"])
async def test_disabled_output_is_revalidated_after_submission(
    channel_env, monkeypatch, action
):
    settings = channel_env[0]
    submitted = await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    async with jobs.job_session(settings) as session:
        backend = await session.get(Backend, 1)
        backend.enabled = False
        await session.commit()
    handler = AsyncMock()
    service = {
        "list": "list_jobs",
        "show": "get_job",
        "logs": "job_logs",
        "cancel": "cancel_job",
    }[action]
    monkeypatch.setattr(jobs, service, handler)
    reply = (
        await exchange(
            channel_env,
            {
                "id": "disabled",
                "type": f"operation.{action}",
                "output": "demo",
                "operation_id": submitted["operation_id"],
            },
        )
    )[0]
    assert reply["code"] == "backend_disabled"
    handler.assert_not_awaited()
