from __future__ import annotations

import argparse
from pathlib import Path

from app.cli.common import CommandResult, bind_command, load_backend_async
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    restore_verify = subparsers.add_parser("restore-verify")
    restore_verify_subparsers = restore_verify.add_subparsers(
        dest="restore_verify_command", required=True
    )

    restore_verify_app = restore_verify_subparsers.add_parser("app")
    restore_verify_app.add_argument("backend")
    restore_verify_app.add_argument("--backup-id", type=int, default=None)
    restore_verify_app.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        restore_verify_app,
        handler=_run_restore_verify_command,
        formatter=format_restore_verify_text,
        needs_env=True,
    )

    restore_create = subparsers.add_parser("restore-create")
    restore_create.add_argument("bundle_path")
    restore_create.add_argument("--backend-name", dest="backend_name", default=None)
    restore_create.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        restore_create,
        handler=_run_restore_create_command,
        formatter=format_restore_create_text,
        needs_settings=True,
    )


def format_restore_verify_text(payload: dict[str, object]) -> str:
    covered_paths = (
        payload.get("covered_paths")
        if isinstance(payload.get("covered_paths"), list)
        else []
    )
    risk_flags = (
        payload.get("risk_flags") if isinstance(payload.get("risk_flags"), list) else []
    )
    lines = [
        f"backend: {payload.get('backend')}",
        f"backup_id: {payload.get('backup_id')}",
        f"ok: {'yes' if payload.get('ok') else 'no'}",
        f"scope: {payload.get('scope') or '-'}",
        f"bundle_path: {payload.get('bundle_path') or '-'}",
        f"bundle_sha256: {payload.get('bundle_sha256') or '-'}",
        f"bundle_size_bytes: {payload.get('bundle_size_bytes') or '-'}",
        f"bundle_format_version: {payload.get('bundle_format_version') or '-'}",
        f"mount_entries_archived: {payload.get('mount_entries_archived') or 0}/{payload.get('mount_entries_total') or 0}",
        f"container_snapshot_included: {'yes' if payload.get('container_snapshot_included') else 'no'}",
        f"verification_status: {payload.get('verification_status') or '-'}",
        f"restore_readiness: {payload.get('restore_readiness_label') or payload.get('restore_readiness') or '-'}",
        f"covered_paths: {', '.join(str(item) for item in covered_paths) if covered_paths else 'none'}",
        f"risk_flags: {', '.join(str(item) for item in risk_flags) if risk_flags else 'none'}",
        f"risk_summary: {payload.get('risk_summary') or '-'}",
        f"notes: {payload.get('notes') or '-'}",
    ]
    return "\n".join(lines)


def format_restore_create_text(payload: dict[str, object]) -> str:
    restored_paths = (
        payload.get("restored_paths")
        if isinstance(payload.get("restored_paths"), list)
        else []
    )
    lines = [
        f"backend: {payload.get('backend')}",
        f"backup_id: {payload.get('backup_id')}",
        f"bundle_path: {payload.get('bundle_path') or '-'}",
        f"restored_paths: {len(restored_paths)}",
    ]
    if restored_paths:
        lines.append(f"paths: {', '.join(str(item) for item in restored_paths)}")
    return "\n".join(lines)


async def restore_verify_app_async(
    backend_name: str,
    backup_id: int | None,
) -> CommandResult:
    from app.database import SessionLocal
    from app.services.backend_backup_service import (
        get_backend_backup,
        latest_successful_backend_backup,
        verify_backend_backup,
    )

    backend = await load_backend_async(backend_name)
    if backend is None:
        return 1, None, f"backend not found: {backend_name}"
    async with SessionLocal() as session:
        if isinstance(backup_id, int):
            backup = await get_backend_backup(session, backend.id, backup_id)
        else:
            backup = await latest_successful_backend_backup(session, backend.id)
        if backup is None:
            if isinstance(backup_id, int):
                return 1, None, f"backup not found for this output: #{backup_id}"
            return 1, None, f"no successful backup available for {backend_name}"
        try:
            payload = await verify_backend_backup(session, backend, backup)
        except LookupError as exc:
            return 1, None, str(exc)
        except Exception as exc:
            return 2, None, str(exc)
    return 0, payload, None


async def restore_create_async(
    bundle_path: str,
    backend_name: str | None,
    settings: Settings,
) -> CommandResult:
    from app.database import SessionLocal
    from app.services.backend_backup_service import (
        restore_backend_bundle_as_new_backend,
    )

    source_path = Path(bundle_path)
    if not source_path.exists():
        return 1, None, f"bundle not found: {bundle_path}"

    async with SessionLocal() as session:
        try:
            result = await restore_backend_bundle_as_new_backend(
                session,
                source_path,
                original_name=source_path.name,
                settings=settings,
                backend_name=backend_name,
            )
        except LookupError as exc:
            return 1, None, str(exc)
        except Exception as exc:
            return 2, None, str(exc)

    backend = result.get("backend")
    backup = result.get("backup")
    return (
        0,
        {
            "backend": getattr(backend, "name", None),
            "backup_id": getattr(backup, "id", None),
            "bundle_path": result.get("bundle_path"),
            "restored_paths": result.get("restored_paths") or [],
            "ok": True,
        },
        None,
    )


async def _run_restore_verify_command(
    args: argparse.Namespace,
    settings: Settings | None,
) -> CommandResult:
    return await restore_verify_app_async(args.backend, args.backup_id)


async def _run_restore_create_command(
    args: argparse.Namespace,
    settings: Settings | None,
) -> CommandResult:
    assert settings is not None
    return await restore_create_async(args.bundle_path, args.backend_name, settings)
