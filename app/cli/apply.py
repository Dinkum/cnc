from __future__ import annotations

import argparse
import json

from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    apply = subparsers.add_parser("apply")
    apply.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        apply,
        handler=_run_apply_command,
        formatter=format_apply_text,
        needs_settings=True,
    )


def format_apply_text(payload: dict[str, object]) -> str:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    lines = [
        "scope: full converge (routing + publication + guests)",
        f"status: {payload.get('status') or '-'}",
        f"message: {payload.get('message') or '-'}",
        f"run_id: {payload.get('run_id') or '-'}",
        f"created_at: {payload.get('created_at') or '-'}",
    ]
    if isinstance(details, dict):
        if details.get("phase") or details.get("failed_phase"):
            lines.append(
                f"phase: {details.get('phase') or details.get('failed_phase')}"
            )
        if details.get("error"):
            lines.append(f"error: {details.get('error')}")
        if details.get("failure_mode"):
            lines.append(f"failure_mode: {details.get('failure_mode')}")
        if "manual_review_required" in details:
            lines.append(
                f"manual_review_required: {'yes' if details.get('manual_review_required') else 'no'}"
            )
        if details.get("operator_action"):
            lines.append(f"operator_action: {details.get('operator_action')}")
        nginx_files = (
            details.get("nginx_files")
            if isinstance(details.get("nginx_files"), list)
            else []
        )
        if nginx_files:
            lines.append(f"nginx_files: {', '.join(str(item) for item in nginx_files)}")
        running = (
            details.get("app_containers_running")
            if isinstance(details.get("app_containers_running"), list)
            else []
        )
        if running:
            lines.append(
                f"app_containers_running: {', '.join(str(item) for item in running)}"
            )
        completed_phases = (
            details.get("completed_phases")
            if isinstance(details.get("completed_phases"), list)
            else []
        )
        if completed_phases:
            lines.append(
                f"completed_phases: {', '.join(str(item) for item in completed_phases)}"
            )
        live_mutation_phases = (
            details.get("live_mutation_phases")
            if isinstance(details.get("live_mutation_phases"), list)
            else []
        )
        if live_mutation_phases:
            lines.append(
                f"live_mutation_phases: {', '.join(str(item) for item in live_mutation_phases)}"
            )
        nginx_rollback = (
            details.get("nginx_rollback")
            if isinstance(details.get("nginx_rollback"), dict)
            else {}
        )
        if nginx_rollback:
            mode = str(nginx_rollback.get("mode") or "-")
            status = str(nginx_rollback.get("status") or "-")
            lines.append(f"nginx_rollback: mode={mode} status={status}")
    return "\n".join(lines)


async def apply_async(settings: Settings) -> CommandResult:
    from app.database import SessionLocal
    from app.services.apply_service import run_apply

    async with SessionLocal() as session:
        response = await run_apply(session, settings)
    payload = json.loads(response.model_dump_json())
    return (0 if response.status == "success" else 2), payload, None


async def _run_apply_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await apply_async(settings)
