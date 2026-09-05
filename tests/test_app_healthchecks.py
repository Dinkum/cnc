from app.models.entities import Backend
from app.services.app_healthchecks import probe_backend_health
from app.services.commands import CommandResult


def _backend(*, healthcheck_mode: str) -> Backend:
    return Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        healthcheck_mode=healthcheck_mode,
        env_json="{}",
        volumes_json="[]",
    )


def test_disabled_healthcheck_is_unmonitored_without_running_a_probe() -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], _timeout: int) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    result = probe_backend_health(
        _backend(healthcheck_mode="none"),
        host="127.0.0.1",
        port=12001,
        timeout_sec=5,
        command_runner=fake_run,
    )

    assert result.checked is False
    assert result.status == "unmonitored"
    assert commands == []


def test_enabled_healthcheck_rejects_an_invalid_port_without_running_a_probe() -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], _timeout: int) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    result = probe_backend_health(
        _backend(healthcheck_mode="tcp"),
        host="127.0.0.1",
        port=0,
        timeout_sec=5,
        command_runner=fake_run,
    )

    assert result.checked is True
    assert result.status == "unhealthy"
    assert result.error == "invalid healthcheck port: 0"
    assert commands == []
