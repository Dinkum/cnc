from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.cli.common import (
    CommandResult,
    bind_command,
    load_backend_async,
    load_shield_status_payload,
)
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    fix = subparsers.add_parser("fix")
    fix.add_argument("backend")
    fix.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        fix,
        handler=_run_fix_command,
        formatter=format_fix_text,
        needs_settings=True,
    )


def format_fix_text(payload: dict[str, object]) -> str:
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    reconcile = (
        payload.get("reconcile") if isinstance(payload.get("reconcile"), dict) else {}
    )
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    before_issues = (
        before.get("issues") if isinstance(before.get("issues"), list) else []
    )
    after_issues = after.get("issues") if isinstance(after.get("issues"), list) else []
    cleanup_actions = (
        cleanup.get("actions") if isinstance(cleanup.get("actions"), list) else []
    )
    cleanup_errors = (
        cleanup.get("errors") if isinstance(cleanup.get("errors"), list) else []
    )
    lines = [
        f"backend: {payload.get('backend')}",
        f"changed: {'yes' if payload.get('changed') else 'no'}",
        f"ok: {'yes' if payload.get('ok') else 'no'}",
        f"before_diagnosis: {before.get('diagnosis') or '-'}",
        f"after_diagnosis: {after.get('diagnosis') or '-'}",
        f"before_issues: {', '.join(str(item) for item in before_issues) if before_issues else 'none'}",
        f"after_issues: {', '.join(str(item) for item in after_issues) if after_issues else 'none'}",
        f"cleanup_changed: {'yes' if cleanup.get('changed') else 'no'}",
        f"cleanup_actions: {', '.join(str(item) for item in cleanup_actions) if cleanup_actions else 'none'}",
        f"cleanup_errors: {', '.join(str(item) for item in cleanup_errors) if cleanup_errors else 'none'}",
        f"reconcile_created: {'yes' if reconcile.get('created') else 'no'}",
        f"reconcile_recreated: {'yes' if reconcile.get('recreated') else 'no'}",
        f"reconcile_bootstrapped: {'yes' if reconcile.get('bootstrapped') else 'no'}",
    ]
    if failure:
        lines.extend(
            [
                f"failure_phase: {failure.get('phase') or failure.get('failed_phase') or '-'}",
                f"failure_error: {failure.get('error') or '-'}",
            ]
        )

    shield_status = (
        payload.get("shield_status")
        if isinstance(payload.get("shield_status"), dict)
        else {}
    )
    if shield_status:
        lines.append("shield_status:")
        lines.append(
            f"  server_enabled: {'yes' if shield_status.get('server_enabled') else 'no'}"
        )
        lines.append(
            f"  backend_exists: {'yes' if shield_status.get('backend_exists') else 'no'}"
        )
        lines.append(
            f"  backend_enabled: {'yes' if shield_status.get('backend_enabled') else 'no'}"
        )
        lines.append(
            f"  service_active: {'yes' if shield_status.get('service_active') else 'no'}"
        )
        lines.append(f"  output_state: {shield_status.get('output_state') or '-'}")
        lines.append(f"  ready: {'yes' if shield_status.get('ready') else 'no'}")

    return "\n".join(lines)


async def _load_enabled_app_backends_async() -> list:
    from app.database import SessionLocal
    from app.models.entities import Backend

    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(Backend)
                    .options(selectinload(Backend.inputs))
                    .where(Backend.kind == "app", Backend.enabled.is_(True))
                    .order_by(Backend.id.asc())
                )
            ).scalars()
        )


async def fix_app_async(
    backend_name: str,
    settings: Settings,
) -> CommandResult:
    from app.services.app_fix import fix_app_backend

    backend = await load_backend_async(backend_name)
    if backend is None:
        return 1, None, f"backend not found: {backend_name}"
    if str(backend.kind or "").lower() != "app":
        return 1, None, f"backend is not an app output: {backend_name}"
    if not backend.enabled:
        return 1, None, f"backend is disabled: {backend_name}"

    enabled_app_backends = await _load_enabled_app_backends_async()
    payload = await fix_app_backend(
        backend,
        settings,
        enabled_app_backends=enabled_app_backends,
    )
    payload["shield_status"] = await load_shield_status_payload(settings)
    return 0 if payload.get("ok") else 2, payload, None


async def _run_fix_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await fix_app_async(args.backend, settings)
