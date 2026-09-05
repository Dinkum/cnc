from __future__ import annotations

import argparse

from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    update = subparsers.add_parser("update")
    update_subparsers = update.add_subparsers(dest="update_command", required=True)
    check = update_subparsers.add_parser("check")
    check.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        check,
        handler=_run_update_check_command,
        formatter=format_update_check_text,
        needs_settings=True,
    )


def format_update_check_text(payload: dict[str, object]) -> str:
    lines = [
        f"status: {payload.get('status') or '-'}",
        f"current_version: {payload.get('current_version') or '-'}",
        f"available_version: {payload.get('available_version') or '-'}",
        f"has_update: {'yes' if payload.get('has_update') else 'no'}",
        f"repo: {payload.get('repo') or '-'}",
        f"ref: {payload.get('ref') or '-'}",
        f"checked_at: {payload.get('checked_at') or '-'}",
    ]
    if payload.get("error"):
        lines.append(f"error: {payload.get('error')}")
    return "\n".join(lines)


async def update_check_async(settings: Settings) -> CommandResult:
    from app.database import SessionLocal
    from app.services.update_service import refresh_update_version_info

    async with SessionLocal() as session:
        payload = await refresh_update_version_info(session, settings)
    return (1 if payload.get("error") else 0), payload, None


async def _run_update_check_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await update_check_async(settings)
