from __future__ import annotations

import argparse
import asyncio

from app.cli.apply import apply_async, format_apply_text
from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    host = subparsers.add_parser("host")
    host_subparsers = host.add_subparsers(dest="host_command", required=True)

    host_apply = host_subparsers.add_parser("apply")
    host_apply.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        host_apply,
        handler=_run_host_apply_command,
        formatter=format_apply_text,
        needs_settings=True,
    )

    host_reconcile = host_subparsers.add_parser("reconcile")
    host_reconcile.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        host_reconcile,
        handler=_run_reconcile_host_command,
        formatter=format_reconcile_host_text,
        needs_settings=True,
    )

    quadlet_cutover = host_subparsers.add_parser("quadlet-cutover-plan")
    quadlet_cutover.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        quadlet_cutover,
        handler=_run_quadlet_cutover_plan_command,
        formatter=format_quadlet_cutover_plan_text,
        needs_settings=True,
    )

    reconcile_host = subparsers.add_parser("reconcile-host")
    reconcile_host.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        reconcile_host,
        handler=_run_reconcile_host_command,
        formatter=format_reconcile_host_text,
        needs_settings=True,
    )


def format_reconcile_host_text(payload: dict[str, object]) -> str:
    changed_units = (
        payload.get("changed_units")
        if isinstance(payload.get("changed_units"), list)
        else []
    )
    changed_files = (
        payload.get("changed_files")
        if isinstance(payload.get("changed_files"), list)
        else []
    )
    timer_states = (
        payload.get("timer_states")
        if isinstance(payload.get("timer_states"), dict)
        else {}
    )
    lines = [
        "scope: host runtime assets only",
        f"changed_units: {', '.join(str(unit) for unit in changed_units) if changed_units else 'none'}",
        f"changed_files: {', '.join(str(path) for path in changed_files) if changed_files else 'none'}",
        f"daemon_reloaded: {'yes' if payload.get('daemon_reloaded') else 'no'}",
        f"admin_restart_required: {'yes' if payload.get('admin_restart_required') else 'no'}",
        f"timer_reconciled: {'yes' if payload.get('timer_reconciled') else 'no'}",
    ]
    if timer_states:
        lines.append("timer_states:")
        for timer_name in sorted(str(name) for name in timer_states.keys()):
            state = (
                timer_states.get(timer_name)
                if isinstance(timer_states.get(timer_name), dict)
                else {}
            )
            lines.append(
                "  "
                + f"{timer_name}: enabled={'yes' if state.get('enabled') else 'no'} "
                + f"active={'yes' if state.get('active') else 'no'}"
            )
    return "\n".join(lines)


def format_quadlet_cutover_plan_text(payload: dict[str, object]) -> str:
    outputs = payload.get("outputs") if isinstance(payload.get("outputs"), list) else []
    commands = (
        payload.get("commands") if isinstance(payload.get("commands"), list) else []
    )
    lines = [
        "scope: app runtime ownership",
        f"needs_cutover: {payload.get('needs_cutover_count') or 0}",
    ]
    if outputs:
        lines.append("outputs:")
        for item in outputs:
            if not isinstance(item, dict):
                continue
            lines.append(
                "  "
                + f"{item.get('backend')}: status={item.get('status')} "
                + f"owner={item.get('runtime_owner')} health={item.get('diagnosis')}"
            )
    else:
        lines.append("outputs: none")
    if commands:
        lines.append("commands:")
        for command in commands:
            lines.append(f"  {command}")
    else:
        lines.append("commands: none")
    return "\n".join(lines)


def _quadlet_cutover_output_status(diagnostics: dict[str, object]) -> str:
    maintenance_issues = (
        diagnostics.get("maintenance_issues")
        if isinstance(diagnostics.get("maintenance_issues"), list)
        else []
    )
    if "legacy_direct_podman_runtime_owner" in maintenance_issues:
        return "cutover_ready" if diagnostics.get("ok") else "cutover_blocked"
    runtime_owner = str(diagnostics.get("runtime_owner") or "").strip()
    if runtime_owner == "quadlet":
        return "quadlet"
    if runtime_owner == "missing":
        return "missing"
    if runtime_owner.startswith("legacy"):
        return "legacy_review"
    return runtime_owner or "unknown"


async def reconcile_host_async(settings: Settings) -> CommandResult:
    from app.services.runtime_assets import reconcile_managed_systemd_assets
    from app.services.operations import host_mutation_operation

    try:
        async with host_mutation_operation(
            settings, kind="reconcile_host", phase="runtime_assets"
        ) as operation:
            payload = await asyncio.to_thread(
                reconcile_managed_systemd_assets, settings
            )
            await operation.complete("success", phase="completed", details=payload)
    except Exception as exc:
        return 1, None, str(exc)
    return 0, payload, None


async def quadlet_cutover_plan_async(settings: Settings) -> CommandResult:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import SessionLocal
    from app.models.entities import Backend
    from app.services.app_diagnostics import (
        collect_app_backend_diagnostics,
        collect_app_diagnostics_snapshot,
    )

    async with SessionLocal() as session:
        backends = list(
            (
                await session.execute(
                    select(Backend)
                    .options(selectinload(Backend.inputs))
                    .where(Backend.kind == "app", Backend.enabled.is_(True))
                    .order_by(Backend.id.asc())
                )
            ).scalars()
        )

    snapshot = await asyncio.to_thread(collect_app_diagnostics_snapshot, settings)
    outputs: list[dict[str, object]] = []
    commands: list[str] = []
    for backend in backends:
        diagnostics = await asyncio.to_thread(
            collect_app_backend_diagnostics,
            backend,
            settings,
            snapshot=snapshot,
        )
        status = _quadlet_cutover_output_status(diagnostics)
        output = {
            "backend": backend.name,
            "status": status,
            "runtime_owner": diagnostics.get("runtime_owner") or "unknown",
            "runtime_owner_label": diagnostics.get("runtime_owner_label") or "",
            "diagnosis": diagnostics.get("diagnosis") or "unknown",
            "ok": bool(diagnostics.get("ok")),
            "maintenance_issues": diagnostics.get("maintenance_issues") or [],
        }
        outputs.append(output)
        if status == "cutover_ready":
            commands.append(f"cnc-admin app migrate-runtime {backend.name}")

    payload = {
        "outputs": outputs,
        "needs_cutover_count": sum(
            1 for item in outputs if item.get("status") == "cutover_ready"
        ),
        "blocked_count": sum(
            1 for item in outputs if item.get("status") == "cutover_blocked"
        ),
        "commands": commands,
    }
    return 0 if not payload["blocked_count"] else 2, payload, None


async def _run_reconcile_host_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await reconcile_host_async(settings)


async def _run_quadlet_cutover_plan_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await quadlet_cutover_plan_async(settings)


async def _run_host_apply_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await apply_async(settings)
