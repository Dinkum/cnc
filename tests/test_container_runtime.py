import pytest

from app.services import container_runtime
from app.services.commands import CommandError, CommandResult


def test_inspect_container_returns_first_payload(monkeypatch) -> None:
    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        assert command == ["podman", "inspect", "cnc-app-web"]
        return CommandResult(command, 0, '[{"Id":"abc"},{"Id":"def"}]', "")

    monkeypatch.setattr(container_runtime, "run_command", fake_run)

    result, payload = container_runtime.inspect_container("cnc-app-web", timeout_sec=9)

    assert result.ok is True
    assert payload == {"Id": "abc"}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"],
                125,
                "",
                'Error: no such object: "cnc-app-web"',
            ),
            True,
        ),
        (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                125,
                "",
                (
                    "Error: unable to find network with name or ID "
                    "cnc-net-web: network not found"
                ),
            ),
            True,
        ),
        (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"],
                124,
                "",
                "timed out after 5s",
            ),
            False,
        ),
        (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"],
                125,
                "",
                "Error: database is locked",
            ),
            False,
        ),
    ],
)
def test_inspect_result_reports_only_definitive_missing_results(
    result: CommandResult, expected: bool
) -> None:
    assert container_runtime.inspect_result_reports_missing(result) is expected


def test_list_external_containers_ignores_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        container_runtime,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 0, "{not-json}", ""),
    )

    assert container_runtime.list_external_containers(timeout_sec=7) == []


def test_list_mounted_containers_returns_empty_on_command_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        container_runtime,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 125, "", "failed"),
    )

    assert container_runtime.list_mounted_containers(timeout_sec=7) == []


def test_container_exists_checks_podman_returncode(monkeypatch) -> None:
    monkeypatch.setattr(
        container_runtime,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 0, "", ""),
    )
    assert container_runtime.container_exists("cnc-app-web", timeout_sec=7) is True

    monkeypatch.setattr(
        container_runtime,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 1, "", "missing"),
    )
    assert container_runtime.container_exists("cnc-app-web", timeout_sec=7) is False


@pytest.mark.asyncio
async def test_read_container_stats_returns_error_result_on_command_error(
    monkeypatch,
) -> None:
    error_result = CommandResult(
        [
            "podman",
            "stats",
            "--no-stream",
            "--format",
            "{{.CPUPerc}}|{{.MemUsage}}",
            "cnc-app-web",
        ],
        125,
        "",
        "boom",
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise CommandError(error_result)

    monkeypatch.setattr(
        container_runtime, "run_command_checked_async", fake_run_checked
    )

    result = await container_runtime.read_container_stats("cnc-app-web", timeout_sec=7)

    assert result is error_result
