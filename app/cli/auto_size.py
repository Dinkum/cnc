from __future__ import annotations

import argparse

from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    auto_size = subparsers.add_parser("auto-size")
    auto_size_subparsers = auto_size.add_subparsers(
        dest="auto_size_command", required=True
    )
    auto_size_tick = auto_size_subparsers.add_parser("tick")
    auto_size_tick.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        auto_size_tick,
        handler=_run_auto_size_tick_command,
        formatter=format_auto_size_text,
        needs_settings=True,
    )


def format_auto_size_text(payload: dict[str, object]) -> str:
    changed = (
        payload.get("changed_backends")
        if isinstance(payload.get("changed_backends"), list)
        else []
    )
    apply_payload = (
        payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    )
    decisions = (
        payload.get("decisions") if isinstance(payload.get("decisions"), list) else []
    )
    lines = [
        f"bucket_start: {payload.get('bucket_start') or '-'}",
        f"sampled_backends: {payload.get('sampled_backends') or 0}",
        f"samples_written: {payload.get('samples_written') or 0}",
        f"samples_pruned: {payload.get('samples_pruned') or 0}",
        f"evaluated: {'yes' if payload.get('evaluated') else 'no'}",
        f"changed_backends: {', '.join(str(item) for item in changed) if changed else 'none'}",
    ]
    if apply_payload:
        lines.append(f"apply_status: {apply_payload.get('status') or '-'}")
        lines.append(f"apply_run_id: {apply_payload.get('run_id') or '-'}")
    if decisions:
        lines.append("")
        lines.append("== decisions ==")
        for item in decisions:
            if not isinstance(item, dict):
                continue
            lines.append(
                " - ".join(
                    [
                        str(item.get("backend") or "-"),
                        f"{item.get('previous_size') or '-'}->{item.get('next_size') or '-'}",
                        str(item.get("decision") or "-"),
                        str(item.get("reason") or "-"),
                    ]
                )
            )
    return "\n".join(lines)


async def auto_size_tick_async(settings: Settings) -> CommandResult:
    from app.database import SessionLocal
    from app.services.auto_size import run_auto_size_tick

    async with SessionLocal() as session:
        payload = await run_auto_size_tick(session, settings)
    apply_payload = (
        payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    )
    if apply_payload and apply_payload.get("status") not in {None, "success"}:
        return 2, payload, None
    return 0, payload, None


async def _run_auto_size_tick_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await auto_size_tick_async(settings)
