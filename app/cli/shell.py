from __future__ import annotations

import argparse
import subprocess
import sys

from app.cli.common import bind_command, load_backend_async


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    shell = subparsers.add_parser("shell")
    shell.add_argument("backend")
    bind_command(shell, handler=_run_shell_command)

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("backend")
    exec_parser.add_argument("exec_args", nargs=argparse.REMAINDER)
    bind_command(exec_parser, handler=_run_exec_command)


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


async def _run_exec_command(args: argparse.Namespace, settings) -> int:
    command_args = normalize_exec_args(list(args.exec_args))
    if not command_args:
        print("exec requires a command after --", file=sys.stderr)
        return 1
    exit_code, container, error = await resolve_shell_app_async(args.backend)
    if error is not None:
        print(error, file=sys.stderr)
        return exit_code
    assert container is not None
    return run_exec(container, command_args)
