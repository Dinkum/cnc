import argparse
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import admin_cli
from app.cli import shell
from app.services import command_jobs


def parse(*argv):
    parser = argparse.ArgumentParser()
    shell.register_exec(parser)
    return parser.parse_args(argv)


def test_background_options_after_output():
    args = parse(
        "demo",
        "--bg",
        "--request-key",
        "build-1",
        "--timeout",
        "2400",
        "--json",
        "--",
        "npm",
        "run",
        "build",
        "--json",
    )
    assert shell._exec_options(args) == ["npm", "run", "build", "--json"]
    assert args.bg and args.json_output
    assert args.timeout == 2400
    assert args.request_key == "build-1"


@pytest.mark.parametrize(
    "command",
    [
        ["npm", "run", "build", "--", "--json"],
        ["echo", "--json"],
        ["sh", "-c", "echo ok", "--", "--bg"],
    ],
)
def test_foreground_command_flags_are_preserved(command):
    args = parse("demo", *command)
    assert shell._exec_options(args) == command
    assert not args.bg and not args.json_output


def test_delimiter_keeps_child_json_out_of_options():
    args = parse("demo", "--", "echo", "--json")
    assert shell._exec_options(args) == ["echo", "--json"]
    assert not args.json_output


@pytest.mark.asyncio
async def test_background_submission_forwards_execution_options(monkeypatch):
    submit = AsyncMock(return_value={"operation_id": 42, "status": "running"})
    monkeypatch.setattr(command_jobs, "submit_job", submit)
    settings = SimpleNamespace()
    args = parse(
        "demo",
        "--bg",
        "--request-key",
        "build-1",
        "--timeout",
        "2400",
        "--",
        "npm",
        "run",
        "build",
    )
    assert await shell._run_exec_command(args, settings) == (
        0,
        {"operation_id": 42, "status": "running"},
        None,
    )
    submit.assert_awaited_once_with(
        settings,
        "demo",
        {"argv": ["npm", "run", "build"], "timeout_sec": 2400},
        request_key="build-1",
    )


@pytest.mark.asyncio
async def test_foreground_preserves_return_code_and_arguments(monkeypatch):
    monkeypatch.setattr(
        shell,
        "resolve_shell_app_async",
        AsyncMock(return_value=(0, "cnc-app-demo", None)),
    )
    run = Mock(return_value=17)
    monkeypatch.setattr(shell, "run_exec", run)
    args = parse("demo", "--", "tool", "--json")
    assert await shell._run_exec_command(args, SimpleNamespace()) == 17
    run.assert_called_once_with("cnc-app-demo", ["tool", "--json"])


def test_json_parser_failure_has_structured_error(capsys):
    assert admin_cli.main(["operation", "show", "not-a-number", "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["code"] == "invalid_arguments"
    assert captured.err == ""


def test_json_command_job_error_preserves_code(monkeypatch, capsys):
    monkeypatch.setattr(
        admin_cli,
        "_run_handler",
        Mock(
            side_effect=command_jobs.CommandJobError("backend_busy", "output busy", 42)
        ),
    )
    assert admin_cli.main(["operation", "cancel", "42", "--json"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": {"code": "backend_busy", "message": "output busy", "operation_id": 42},
    }


def test_child_json_does_not_change_failure_format(monkeypatch, capsys):
    monkeypatch.setattr(
        admin_cli, "_run_handler", Mock(side_effect=ValueError("bad command"))
    )
    with pytest.raises(SystemExit):
        admin_cli.main(["exec", "demo", "--", "tool", "--json"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad command" in captured.err
