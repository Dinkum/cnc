from pathlib import Path
import threading
import time

from app.config import Settings
from app.services.commands import CommandResult
from app.services import guest_exec


def _settings(tmp_path: Path) -> Settings:
    return Settings(app_control_dir=tmp_path / "app-control")


def test_timeout_opens_circuit_and_suppresses_followup_exec(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(command: list[str], timeout_sec: float) -> CommandResult:
        calls.append(command)
        return CommandResult(command, 124, "", f"timed out after {timeout_sec}s")

    first = guest_exec.run_backend_guest_command(
        "web", settings, ["true"], timeout_sec=5, command_runner=fake_runner
    )
    second = guest_exec.run_backend_guest_command(
        "web", settings, ["true"], timeout_sec=5, command_runner=fake_runner
    )

    assert first.returncode == 124
    assert second.returncode == guest_exec.EXEC_CIRCUIT_OPEN_RETURN_CODE
    assert "circuit open" in second.stderr
    assert len(calls) == 1
    assert guest_exec.read_guest_exec_circuit(settings, "web") is not None


def test_old_circuit_stays_latched_until_explicitly_cleared(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = settings.app_control_dir / "web" / "exec-circuit.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"opened_at": 10, "error": "timeout"}', encoding="utf-8")
    calls: list[list[str]] = []

    result = guest_exec.run_backend_guest_command(
        "web",
        settings,
        ["true"],
        timeout_sec=5,
        command_runner=lambda command, _timeout: (
            calls.append(command) or CommandResult(command, 0, "", "")
        ),
    )

    assert result.returncode == guest_exec.EXEC_CIRCUIT_OPEN_RETURN_CODE
    assert calls == []
    assert guest_exec.read_guest_exec_circuit(settings, "web") is not None

    guest_exec.clear_guest_exec_circuit(settings, "web", reason="runtime_started")

    recovered = guest_exec.run_backend_guest_command(
        "web",
        settings,
        ["true"],
        timeout_sec=5,
        command_runner=lambda command, _timeout: CommandResult(command, 0, "", ""),
    )
    assert recovered.ok is True


def test_clear_guest_exec_circuit_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    guest_exec.clear_guest_exec_circuit(settings, "web")

    assert guest_exec.read_guest_exec_circuit(settings, "web") is None


def test_circuit_transitions_emit_structured_logs(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []

    class RecordingLogger:
        def info(self, event: str, **context: object) -> None:
            events.append((event, context))

        def warning(self, event: str, **context: object) -> None:
            events.append((event, context))

    monkeypatch.setattr(guest_exec, "logger", RecordingLogger())

    guest_exec.open_guest_exec_circuit(
        settings,
        "web",
        timeout_sec=5,
        error="guest timeout",
        source="runtime_readiness",
    )
    guest_exec.clear_guest_exec_circuit(settings, "web", reason="quadlet_restarted")

    contexts = dict(events)
    assert contexts["guest_exec.circuit_opened"]["backend"] == "web"
    assert contexts["guest_exec.circuit_opened"]["source"] == "runtime_readiness"
    assert contexts["guest_exec.circuit_closed"]["reason"] == "quadlet_restarted"


def test_backend_guard_distinguishes_busy_and_waits_briefly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    acquired: list[guest_exec.BackendGuestExecGuardState] = []

    with guest_exec.backend_guest_exec_guard(settings, "web") as outer_state:
        assert outer_state == guest_exec.BackendGuestExecGuardState.ACQUIRED
        with guest_exec.backend_guest_exec_guard(settings, "web") as busy_state:
            assert busy_state == guest_exec.BackendGuestExecGuardState.BUSY

        started = threading.Event()

        def acquire_after_release() -> None:
            started.set()
            with guest_exec.backend_guest_exec_guard(
                settings, "web", wait_sec=0.5
            ) as state:
                acquired.append(state)

        thread = threading.Thread(target=acquire_after_release)
        thread.start()
        started.wait(timeout=1)
        time.sleep(0.05)

    thread.join(timeout=1)
    assert acquired == [guest_exec.BackendGuestExecGuardState.ACQUIRED]


def test_backend_guard_reports_unavailable(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def fail_enter(_self):
        raise OSError("read-only guard path")

    monkeypatch.setattr(guest_exec.FileLock, "__enter__", fail_enter)

    with guest_exec.backend_guest_exec_guard(settings, "web") as state:
        assert state == guest_exec.BackendGuestExecGuardState.UNAVAILABLE


def test_run_backend_guest_command_rechecks_circuit_inside_guard(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    circuit_reads = iter([None, {"open": True}])
    calls: list[list[str]] = []

    monkeypatch.setattr(
        guest_exec,
        "read_guest_exec_circuit",
        lambda *_args, **_kwargs: next(circuit_reads),
    )

    result = guest_exec.run_backend_guest_command(
        "web",
        settings,
        ["true"],
        timeout_sec=5,
        command_runner=lambda command, _timeout: (
            calls.append(command) or CommandResult(command, 0, "", "")
        ),
    )

    assert result.returncode == guest_exec.EXEC_CIRCUIT_OPEN_RETURN_CODE
    assert calls == []


def test_run_backend_guest_command_distinguishes_guard_outcomes(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)

    for guard_state, expected_code in (
        (
            guest_exec.BackendGuestExecGuardState.BUSY,
            guest_exec.EXEC_GUARD_BUSY_RETURN_CODE,
        ),
        (
            guest_exec.BackendGuestExecGuardState.UNAVAILABLE,
            guest_exec.EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
        ),
    ):
        monkeypatch.setattr(
            guest_exec,
            "backend_guest_exec_guard",
            _guard_with_state(guard_state),
        )
        result = guest_exec.run_backend_guest_command(
            "web", settings, ["true"], timeout_sec=5
        )
        assert result.returncode == expected_code


def _guard_with_state(state: guest_exec.BackendGuestExecGuardState):
    from contextlib import contextmanager

    @contextmanager
    def guard(*_args, **_kwargs):
        yield state

    return guard
