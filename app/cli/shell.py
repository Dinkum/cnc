from __future__ import annotations

import argparse
import subprocess
import sys

from app.cli.common import CommandResult, bind_command, load_backend_async


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    shell = subparsers.add_parser("shell")
    shell.add_argument("backend")
    bind_command(shell, handler=_run_shell_command)

    exec_parser = subparsers.add_parser("exec")
    register_exec(exec_parser)


def register_exec(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("backend", metavar="OUTPUT")
    parser.add_argument(
        "--bg",
        action="store_true",
        help="Submit a supervised background command; return its operation ID",
    )
    parser.add_argument(
        "--request-key",
        help="Reuse this key when retrying the same background submission",
    )
    parser.add_argument(
        "--timeout", type=int, help="Optional background execution deadline in seconds"
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("exec_args", nargs=argparse.REMAINDER, metavar="-- COMMAND ...")
    bind_command(
        parser,
        handler=_run_exec_command,
        formatter=_format_exec_text,
        needs_settings=True,
    )


def _format_exec_text(payload: dict) -> str:
    return f"operation: {payload['operation_id']}\nstatus: {payload['status']}\nrequest_key: {payload['request_key']}"


def _exec_options(args: argparse.Namespace) -> list[str]:
    values = list(args.exec_args)
    # REMAINDER preserves arbitrary application flags. CNC options after OUTPUT
    # are parsed only before an explicit --, never from the guest command itself.
    if "--" in values:
        boundary = values.index("--")
        options, values = values[:boundary], values[boundary + 1 :]
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--bg", action="store_true", default=argparse.SUPPRESS)
        parser.add_argument("--request-key", default=argparse.SUPPRESS)
        parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS)
        parser.add_argument(
            "--json", action="store_true", dest="json_output", default=argparse.SUPPRESS
        )
        parsed, unknown = parser.parse_known_args(options)
        if unknown:
            # Existing `exec OUTPUT command -- flags` remains a raw command.
            if options and not options[0].startswith("--"):
                return list(args.exec_args)
            raise ValueError(
                "unknown exec option; place application arguments after --"
            )
        vars(args).update(vars(parsed))
    elif values and values[0] in {"--bg", "--request-key", "--timeout", "--json"}:
        raise ValueError("separate CNC execution options from the command with --")
    return values


async def resolve_shell_app_async(
    backend_name: str,
) -> tuple[int, str | None, str | None]:
    from app.services.renderers import container_name

    backend = await load_backend_async(backend_name)
    if backend is None:
        return 1, None, f"backend not found: {backend_name}"
    if str(backend.kind or "").strip().lower() != "app":
        return 1, None, f"shell only supports app backends: {backend_name}"
    return 0, container_name(backend.name), None


def shell_command(container: str) -> list[str]:
    command = ["podman", "exec"]
    if sys.stdin.isatty():
        command.append("-i")
    if sys.stdout.isatty():
        command.append("-t")
    command.extend([container, "/bin/bash"])
    return command


def run_shell(container: str) -> int:
    completed = subprocess.run(shell_command(container), check=False)
    return int(completed.returncode)


def normalize_exec_args(command_args: list[str]) -> list[str]:
    if command_args and command_args[0] == "--":
        return command_args[1:]
    return command_args


def exec_command(container: str, command_args: list[str]) -> list[str]:
    command = ["podman", "exec"]
    if sys.stdin.isatty():
        command.append("-i")
    if sys.stdout.isatty():
        command.append("-t")
    command.append(container)
    command.extend(command_args)
    return command


def run_exec(container: str, command_args: list[str]) -> int:
    completed = subprocess.run(exec_command(container, command_args), check=False)
    return int(completed.returncode)


async def _run_shell_command(args: argparse.Namespace, settings) -> int:
    exit_code, container, error = await resolve_shell_app_async(args.backend)
    if error is not None:
        print(error, file=sys.stderr)
        return exit_code
    assert container is not None
    return run_shell(container)


async def _run_exec_command(args: argparse.Namespace, settings) -> CommandResult | int:
    command_args = _exec_options(args)
    if not command_args:
        return 1, None, "exec requires a command after --"
    if getattr(args, "bg", False):
        from app.services.command_jobs import submit_job

        assert settings is not None
        frame = {"argv": command_args}
        if getattr(args, "timeout", None) is not None:
            frame["timeout_sec"] = args.timeout
        payload = await submit_job(
            settings,
            args.backend,
            frame,
            request_key=getattr(args, "request_key", None),
        )
        return 0, payload, None
    if (
        getattr(args, "json_output", False)
        or getattr(args, "request_key", None)
        or getattr(args, "timeout", None) is not None
    ):
        raise ValueError(
            "--json, --request-key and --timeout require --bg; foreground exec preserves command streams"
        )
    exit_code, container, error = await resolve_shell_app_async(args.backend)
    if error is not None:
        print(error, file=sys.stderr)
        return exit_code
    assert container is not None
    return run_exec(container, command_args)
