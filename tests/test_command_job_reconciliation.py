import asyncio
import json

import pytest
import pytest_asyncio


from app.config import Settings
from app.database import Base
from app.models.entities import CommandJob, Operation
from app.services import command_jobs as jobs
from app.services.command_job_files import GUEST_JOB_ROOT, reserve_job, reservation_path


TOKEN = "a" * 32


@pytest_asyncio.fixture
async def job(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        app_control_dir=tmp_path / "control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    async with jobs.job_session(settings) as session:
        async with session.bind.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        operation = Operation(
            kind="output_exec",
            status="running",
            phase="executing",
            actor="operator",
            details_json="{}",
        )
        session.add(operation)
        await session.flush()
        session.add(
            CommandJob(
                execution_token=TOKEN,
                operation_id=operation.id,
                request_key="test",
                request_hash="a" * 64,
                output_name="demo",
                container_id="container-a",
                container_started_at="start-a",
                cancel_requested=False,
            )
        )
        await session.commit()
        operation_id = operation.id
    directory = (
        settings.app_sandbox_dir
        / "demo"
        / "rootfs"
        / GUEST_JOB_ROOT
        / f"{operation_id}-{TOKEN}"
    )
    directory.mkdir(parents=True)
    reserve_job(settings, "demo", operation_id, execution_token=TOKEN)
    return settings, operation_id, directory


@pytest.mark.asyncio
async def test_delayed_observer_cannot_replace_recorded_completion(job, monkeypatch):
    settings, operation_id, directory = job
    started = asyncio.Event()
    resume = asyncio.Event()

    async def inspect(*args):
        started.set()
        await resume.wait()
        return {"state": "stopped"}

    monkeypatch.setattr(jobs, "_bounded_thread", inspect)
    delayed = asyncio.create_task(jobs.get_job(settings, operation_id))
    await started.wait()
    result_path = directory / "result"
    result_path.write_text("success\texited\t0\n")
    completed = await jobs.get_job(settings, operation_id)
    assert completed["status"] == "success"
    # Retention can remove a marker after another reader durably recorded it.
    result_path.rename(directory / "archived-result")
    resume.set()
    final = await delayed
    assert final["status"] == "success"
    assert final["phase"] == "completed"
    assert final["exit_code"] == 0


@pytest.mark.asyncio
async def test_completion_arriving_during_inspection_beats_stopped_runtime(
    job, monkeypatch
):
    settings, operation_id, directory = job

    async def inspect(*args):
        (directory / "result").write_text("success\texited\t0\n")
        return {"state": "stopped"}

    monkeypatch.setattr(jobs, "_bounded_thread", inspect)
    assert (await jobs.get_job(settings, operation_id))["status"] == "success"
    assert not reservation_path(settings, "demo").exists()


@pytest.mark.asyncio
async def test_cancel_requested_during_inspection_is_reflected_in_terminal_status(
    job, monkeypatch
):
    settings, operation_id, directory = job

    async def inspect(*args):
        async with jobs.job_session(settings) as session:
            command = await session.get(CommandJob, operation_id)
            command.cancel_requested = True
            await session.commit()
        (directory / "result").write_text("success\tkilled\tTERM\n")
        return {
            "state": "running",
            "container_id": "container-a",
            "started_at": "start-a",
        }

    monkeypatch.setattr(jobs, "_bounded_thread", inspect)
    result = await jobs.get_job(settings, operation_id)
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] is True


@pytest.mark.asyncio
async def test_unreadable_result_remains_unknown_without_guest_or_host_probe(
    job, monkeypatch
):
    settings, operation_id, directory = job
    (directory / "result").write_bytes(b"\xff")

    async def unexpected(*args):
        raise AssertionError("unreadable completion must remain unknown")

    monkeypatch.setattr(jobs, "_bounded_thread", unexpected)
    result = await jobs.get_job(settings, operation_id)
    assert result["status"] == "running"
    assert result["phase"] == "observation_unavailable"
    assert reservation_path(settings, "demo").exists()


@pytest.mark.asyncio
async def test_details_cannot_override_authoritative_state(job):
    settings, operation_id, directory = job
    async with jobs.job_session(settings) as session:
        operation = await session.get(Operation, operation_id)
        operation.status = "success"
        operation.details_json = json.dumps(
            {
                "status": "running",
                "operation_id": -1,
                "output": "other",
                "cancel_requested": True,
            }
        )
        await session.commit()
    result = await jobs.get_job(settings, operation_id)
    assert result["status"] == "success"
    assert result["operation_id"] == operation_id
    assert result["output"] == "demo"
    assert result["cancel_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("service_result", ["timeout", "oom-kill"])
async def test_independent_failure_is_not_reclassified_by_cancel_request(
    job, service_result
):
    settings, operation_id, directory = job
    async with jobs.job_session(settings) as session:
        command = await session.get(CommandJob, operation_id)
        command.cancel_requested = True
        await session.commit()
    (directory / "result").write_text(f"{service_result}\tkilled\tKILL\n")
    result = await jobs.get_job(settings, operation_id)
    assert result["status"] == "failed"
    assert result["service_result"] == service_result
    assert result["cancel_requested"] is True
