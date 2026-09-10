import os
import shlex
import subprocess

import pytest
from sqlalchemy import select


from app.config import Settings
from app.database import Base
from app.models.entities import Backend, CommandJob
from app.services import command_jobs as jobs
from app.services import command_job_runtime as runtime
from app.services.command_job_files import GUEST_JOB_ROOT, reservation_path
from app.services.commands import CommandResult
from app.services.guest_exec import BackendGuestExecGuardState, backend_guest_exec_guard


TOKEN = "a" * 32


@pytest.fixture
async def job_env(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}",
        app_control_dir=tmp_path / "control",
        app_sandbox_dir=tmp_path / "sandboxes",
        host_mutation_lock_path=tmp_path / "host.lock",
    )
    # Settings names the shared lock apply_lock_path in this checkout.
    monkeypatch.setattr(
        "app.services.operations._host_lock_path",
        lambda _settings: tmp_path / "host.lock",
    )
    async with jobs.job_session(settings) as session:
        async with session.bind.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session.add(Backend(name="demo", kind="app", port=8090, enabled=True))
        await session.commit()
    observed = {
        "state": "running",
        "container_id": "a" * 64,
        "started_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(runtime, "observe_runtime", lambda _output: dict(observed))
    calls = []

    def launch(*args):
        calls.append(args)
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
    return settings, calls, observed


def finish(settings, operation_id, value="success\texited\t0\n", stdout="done\n"):
    directory = next(
        (settings.app_sandbox_dir / "demo" / "rootfs" / GUEST_JOB_ROOT).glob(
            f"{operation_id}-*"
        )
    )
    (directory / "result").write_text(value)
    (directory / "stdout").write_text(stdout)


async def test_submission_is_durable_and_duplicate_key_does_not_execute_again(job_env):
    settings, calls, _ = job_env
    first = await jobs.submit_job(
        settings, "demo", {"argv": ["sleep", "2400"]}, request_key="build-1"
    )
    assert first["status"] == "running" and first["timeout_sec"] is None
    again = await jobs.submit_job(
        settings, "demo", {"argv": ["sleep", "2400"]}, request_key="build-1"
    )
    assert first["operation_id"] == again["operation_id"]
    assert len(calls) == 1
    assert calls[0][0] == "a" * 64  # launch pins the observed container incarnation
    with pytest.raises(jobs.CommandJobError, match="different command"):
        await jobs.submit_job(
            settings, "demo", {"argv": ["false"]}, request_key="build-1"
        )
    async with jobs.job_session(settings) as session:
        assert len((await session.execute(select(CommandJob))).scalars().all()) == 1


async def test_background_job_reserves_slot_and_result_releases_it(job_env):
    settings, calls, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    with backend_guest_exec_guard(settings, "demo") as state:
        assert state == BackendGuestExecGuardState.BUSY
    with pytest.raises(jobs.CommandJobError) as busy:
        await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    assert busy.value.code == "backend_busy"
    finish(settings, first["operation_id"])
    result = await jobs.get_job(settings, first["operation_id"])
    assert result["status"] == "success" and result["exit_code"] == 0
    assert not reservation_path(settings, "demo").exists()
    chunk = await jobs.job_logs(settings, first["operation_id"])
    assert chunk["data"] == "done\n" and chunk["eof"] and chunk["retained"]
    await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    assert len(calls) == 2


async def test_command_exit_124_is_not_a_guest_exec_timeout(job_env):
    settings, _, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["false"]})
    finish(settings, first["operation_id"], "exit-code\texited\t124\n")
    result = await jobs.get_job(settings, first["operation_id"])
    assert result["status"] == "failed" and result["exit_code"] == 124
    assert jobs.read_guest_exec_circuit(settings, "demo") is None


async def test_explicit_job_deadline_does_not_open_runtime_circuit(job_env):
    settings, calls, _ = job_env
    first = await jobs.submit_job(
        settings, "demo", {"argv": ["sleep", "10"], "timeout_sec": 3}
    )
    assert calls[0][3] == 3
    finish(settings, first["operation_id"], "timeout\tkilled\tTERM\n")
    result = await jobs.get_job(settings, first["operation_id"])
    assert result["status"] == "failed" and result["service_result"] == "timeout"
    assert jobs.read_guest_exec_circuit(settings, "demo") is None


async def test_submission_timeout_keeps_reservation_and_does_not_retry(
    job_env, monkeypatch
):
    settings, _, _ = job_env
    monkeypatch.setattr(
        runtime, "launch_job", lambda *_args: CommandResult([], 124, "", "timeout")
    )
    result = await jobs.submit_job(
        settings, "demo", {"argv": ["true"]}, request_key="unknown-start"
    )
    assert result["phase"] == "submission_unknown"
    assert jobs.read_guest_exec_circuit(settings, "demo") is not None
    assert reservation_path(settings, "demo").exists()
    again = await jobs.submit_job(
        settings, "demo", {"argv": ["true"]}, request_key="unknown-start"
    )
    assert again["operation_id"] == result["operation_id"]


async def test_runtime_replacement_is_interrupted_but_unknown_observation_is_not(
    job_env,
):
    settings, _, observed = job_env
    result = await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    observed["state"] = "unknown"
    assert (await jobs.get_job(settings, result["operation_id"]))["status"] == "running"
    assert reservation_path(settings, "demo").exists()
    observed.update(state="running", started_at="2026-01-02T00:00:00Z")
    result = await jobs.get_job(settings, result["operation_id"])
    assert result["phase"] == "interrupted" and result["status"] == "failed"


async def test_control_plane_restart_does_not_fail_guest_supervised_job(job_env):
    from app.services.operations import fail_interrupted_operations

    settings, _, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    await fail_interrupted_operations(settings)
    result = await jobs.get_job(settings, first["operation_id"])
    assert result["status"] == "running"


async def test_failed_cancel_can_be_retried_and_only_completion_confirms_it(
    job_env, monkeypatch
):
    settings, _, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["sleep", "100"]})
    stops = []

    def stop(*args):
        stops.append(args)
        return CommandResult([], 76 if len(stops) == 1 else 0, "", "")

    monkeypatch.setattr(runtime, "stop_job", stop)
    result = await jobs.cancel_job(settings, first["operation_id"])
    assert result["status"] == "running" and result["cancel_requested"]
    assert "cancel_error" in result
    await jobs.cancel_job(settings, first["operation_id"])
    assert len(stops) == 2 and stops[-1][-2] == "a" * 64
    finish(settings, first["operation_id"], "signal\tkilled\tTERM\n")
    assert (await jobs.get_job(settings, first["operation_id"]))[
        "status"
    ] == "cancelled"


async def test_completion_wins_race_with_cancel_request(job_env, monkeypatch):
    settings, _, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["true"]})

    def stop(*_args):
        finish(settings, first["operation_id"])
        return CommandResult([], 0, "", "")

    monkeypatch.setattr(runtime, "stop_job", stop)
    assert (await jobs.cancel_job(settings, first["operation_id"]))[
        "status"
    ] == "success"


async def test_absent_unit_releases_unknown_submission_without_claiming_success(
    job_env, monkeypatch
):
    settings, _, _ = job_env
    first = await jobs.submit_job(settings, "demo", {"argv": ["true"]})
    monkeypatch.setattr(
        runtime,
        "stop_job",
        lambda *_args: CommandResult([], 0, "CNC_COMMAND_ABSENT", ""),
    )
    result = await jobs.cancel_job(settings, first["operation_id"])
    assert result["status"] == "failed" and result["phase"] == "outcome_unknown"
    assert not reservation_path(settings, "demo").exists()


@pytest.mark.parametrize(
    "frame",
    [
        {"argv": []},
        {"argv": ["true"], "shell": "true"},
        {"shell": "\0"},
        {"argv": ["true"], "timeout_sec": True},
        {"argv": ["true"], "timeout_sec": 0},
    ],
)
async def test_invalid_command_never_starts(job_env, frame):
    settings, calls, _ = job_env
    with pytest.raises(jobs.CommandJobError):
        await jobs.submit_job(settings, "demo", frame)
    assert not calls


def test_systemd_runner_preserves_argv_and_drains_bounded_streams(
    tmp_path, monkeypatch
):
    # Run the generated guest shell against a small systemd-run stand-in. This
    # checks actual shell quoting/FIFO behavior, not merely generated strings.
    monkeypatch.setattr(runtime, "GUEST_JOB_ROOT", str(tmp_path / "jobs").lstrip("/"))
    monkeypatch.setattr(runtime, "MAX_STREAM_BYTES", 16)
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = tools / "systemd-run"
    stub.write_text(
        '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
    )
    stub.chmod(0o700)
    import sys

    malicious = "literal $(touch NEVER_CREATED) ' \" % $$"
    code = "import sys; print(sys.argv[1]); print('e'*100,file=sys.stderr)"
    script = runtime.guest_launch_script(
        1, [sys.executable, "-c", code, malicious], None, execution_token=TOKEN
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    directory = tmp_path / "jobs" / f"1-{TOKEN}"
    assert (directory / "stdout").read_text() == malicious[:16]
    assert (directory / "stderr").read_text() == "e" * 16
    assert (directory / "stdout.truncated").stat().st_size == 1
    assert "RuntimeMaxSec" not in script
    assert "RuntimeMaxSec=120s" in runtime.guest_launch_script(
        2, ["true"], 120, execution_token=TOKEN
    )


@pytest.mark.parametrize(
    ("service_result", "exit_kind", "exit_status"),
    [
        ("success", "exited", "0"),
        ("exit-code", "exited", "124"),
        ("timeout", "killed", "TERM"),
        ("success", "killed", "TERM"),
    ],
)
def test_finalizer_uses_systemd_result_not_application_exit_code(
    tmp_path, monkeypatch, service_result, exit_kind, exit_status
):
    monkeypatch.setattr(runtime, "GUEST_JOB_ROOT", str(tmp_path / "jobs").lstrip("/"))
    script = runtime.guest_launch_script(1, ["true"], None, execution_token=TOKEN)
    setup, launch = script.rsplit("\nexec ", 1)
    subprocess.run(["/bin/bash", "-c", setup], check=True, capture_output=True)
    argv = shlex.split(launch)
    value = next(
        item.split("=", 2)[2]
        for item in argv
        if item.startswith("--property=ExecStopPost=")
    )
    subprocess.run(
        shlex.split(value),
        env={
            **os.environ,
            "SERVICE_RESULT": service_result,
            "EXIT_CODE": exit_kind,
            "EXIT_STATUS": exit_status,
        },
        check=True,
    )
    assert (tmp_path / "jobs" / f"1-{TOKEN}" / "result").read_text() == (
        f"{service_result}\t{exit_kind}\t{exit_status}\n"
    )
    assert "KillMode=control-group" in script and "TimeoutStopSec=5s" in script


async def test_reused_operation_id_does_not_accept_old_execution_result(job_env):
    from app.models.entities import Operation

    settings, calls, _ = job_env
    first = await jobs.submit_job(
        settings, "demo", {"argv": ["true"]}, request_key="old"
    )
    finish(settings, first["operation_id"], stdout="old output")
    assert (await jobs.get_job(settings, first["operation_id"]))["status"] == "success"
    old_token = calls[0][-1]
    async with jobs.job_session(settings) as session:
        operation = await session.get(Operation, first["operation_id"])
        await session.delete(operation)
        await session.commit()
    # SQLite can reuse a pruned integer primary key; guest rootfs data survives.
    second = await jobs.submit_job(
        settings, "demo", {"argv": ["sleep", "100"]}, request_key="new"
    )
    assert second["operation_id"] == first["operation_id"]
    assert calls[-1][-1] != old_token
    assert second["status"] == "running"
    assert (await jobs.job_logs(settings, second["operation_id"]))["data"] == ""
    assert len(calls) == 2


def test_new_execution_runs_beside_stale_integer_and_token_directories(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "GUEST_JOB_ROOT", str(tmp_path / "jobs").lstrip("/"))
    tools = tmp_path / "bin"
    tools.mkdir()
    stub = tools / "systemd-run"
    stub.write_text(
        '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
    )
    stub.chmod(0o700)
    for old_name in ["1", "1-" + "b" * 32]:
        directory = tmp_path / "jobs" / old_name
        directory.mkdir(parents=True)
        (directory / "result").write_text("success\texited\t0\n")
    script = runtime.guest_launch_script(
        1, ["printf", "fresh"], None, execution_token=TOKEN
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "jobs" / f"1-{TOKEN}" / "stdout").read_text() == "fresh"
    assert runtime.unit_name(1, TOKEN) != runtime.unit_name(1, "b" * 32)
