from __future__ import annotations

import argparse

from app.cli.common import CommandResult, bind_command
from app.config import Settings
from app.cli.fix import fix_app_async, format_fix_text

format_repair_text = format_fix_text


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    repair = subparsers.add_parser("repair", help=argparse.SUPPRESS)
    repair_subparsers = repair.add_subparsers(dest="repair_command", required=True)

    repair_app = repair_subparsers.add_parser("app", help=argparse.SUPPRESS)
    repair_app.add_argument("backend")
    repair_app.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        repair_app,
        handler=_run_repair_app_command,
        formatter=format_fix_text,
        needs_settings=True,
    )


async def repair_app_async(
    backend_name: str,
    settings: Settings,
) -> CommandResult:
    return await fix_app_async(backend_name, settings)


async def _run_repair_app_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await repair_app_async(args.backend, settings)
