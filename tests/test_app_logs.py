from app.config import Settings
from app.models.entities import Backend
from app.services import app_logs
from app.services.commands import CommandResult


def test_collect_app_backend_logs_collects_all_sources(monkeypatch, tmp_path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(
        app_logs,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "last_log_excerpt": "apt-get update failed",
            "events": [
                {
                    "timestamp": "2026-03-26T05:30:00Z",
                    "kind": "phase",
                    "name": "bootstrap",
                    "status": "running",
                },
                {
                    "timestamp": "2026-03-26T05:31:00Z",
                    "kind": "phase",
                    "name": "bootstrap",
                    "status": "failed",
                    "details": {
                        "failed_phase": "app_bootstrap",
                        "error": "apt-get update failed",
                    },
                },
            ],
        },
    )
    monkeypatch.setattr(
        app_logs, "read_failure_artifact", lambda *_args, **_kwargs: None
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["podman", "logs"]:
            return CommandResult(command, 0, "container output", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(app_logs, "run_command", fake_run)

    payload = app_logs.collect_app_backend_logs(backend, settings, lines=80)

    assert payload["backend"] == "web"
    assert payload["requested_lines"] == 80
    assert payload["sources_available"] is True
    assert payload["ok"] is True
    assert payload["sources"] == [
        {"source": "container-stdout", "output": "container output"},
        {"source": "bootstrap-state", "output": "apt-get update failed"},
        {
            "source": "runtime-events",
            "output": (
                "2026-03-26T05:30:00Z phase.bootstrap running\n"
                "2026-03-26T05:31:00Z phase.bootstrap failed "
                '{"error": "apt-get update failed", "failed_phase": "app_bootstrap"}'
            ),
        },
    ]
    assert ["podman", "logs", "--tail", "80", "cnc-app-web"] in commands
    assert payload["source_errors"] == []
    assert payload["partial"] is False


def test_collect_app_backend_logs_reports_failed_container_logs(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    monkeypatch.setattr(
        app_logs,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "failed"},
    )
    monkeypatch.setattr(
        app_logs, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_logs,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(
            command, 125, "partial stdout", "permission denied"
        ),
    )

    payload = app_logs.collect_app_backend_logs(backend, settings, lines=25)

    assert payload["sources"] == [
        {"source": "container-stdout", "output": "partial stdout"},
    ]
    assert payload["source_errors"] == [
        {
            "source": "container",
            "returncode": 125,
            "stderr": "permission denied",
            "stdout": "partial stdout",
        }
    ]
    assert payload["partial"] is True
    assert payload["ok"] is False
    assert payload["issues"] == ["bootstrap_failed", "container_logs_unavailable"]


def test_collect_app_backend_logs_handles_empty_sources(monkeypatch, tmp_path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_logs, "read_bootstrap_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_logs, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_logs,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 1, "", "not found"),
    )

    payload = app_logs.collect_app_backend_logs(backend, settings, lines=0)

    assert payload["requested_lines"] == 1
    assert payload["sources"] == []
    assert payload["sources_available"] is False
    assert payload["partial"] is False
    assert payload["ok"] is False


def test_collect_app_backend_logs_includes_failure_artifact(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_logs,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "failed"},
    )
    monkeypatch.setattr(
        app_logs,
        "read_failure_artifact",
        lambda *_args, **_kwargs: {
            "phase": "verify",
            "status": "failed",
            "details": {
                "error": "healthcheck failed",
                "failed_phase": "app_healthcheck",
            },
            "runtime_diagnostics": {"issues": ["private_unreachable"]},
            "recent_events": [{"name": "verify", "status": "failed"}],
        },
    )
    monkeypatch.setattr(
        app_logs,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 1, "", "not found"),
    )

    payload = app_logs.collect_app_backend_logs(backend, settings, lines=25)

    assert payload["sources_available"] is True
    assert payload["sources"] == [
        {
            "source": "failure-artifact",
            "output": (
                "phase: verify\n"
                "status: failed\n"
                "error: healthcheck failed\n"
                "failed_phase: app_healthcheck\n"
                "runtime_issues: private_unreachable\n"
                "recent_events: 1"
            ),
        },
    ]
    assert payload["issues"] == [
        "bootstrap_failed",
        "private_unreachable",
        "container_logs_unavailable",
    ]
