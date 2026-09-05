from __future__ import annotations

import argparse

from app.cli.common import bind_command
from app.cli.doctor import doctor_app_async, format_doctor_text
from app.cli.fix import fix_app_async, format_fix_text
from app.cli.logs import format_logs_text, logs_app_async
from app.cli.shell import _run_exec_command, _run_shell_command
from app.config import Settings
from app.logger import get_logger


logger = get_logger("cli.app")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    app = subparsers.add_parser("app")
    app_subparsers = app.add_subparsers(dest="app_command", required=True)

    doctor = app_subparsers.add_parser("doctor")
    doctor.add_argument("backend")
    doctor.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        doctor,
        handler=_run_doctor_command,
        formatter=format_doctor_text,
        needs_settings=True,
    )

    fix = app_subparsers.add_parser("fix")
    fix.add_argument("backend")
    fix.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        fix, handler=_run_fix_command, formatter=format_fix_text, needs_settings=True
    )

    repair = app_subparsers.add_parser("repair", help=argparse.SUPPRESS)
    repair.add_argument("backend")
    repair.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        repair, handler=_run_fix_command, formatter=format_fix_text, needs_settings=True
    )

    migrate_runtime = app_subparsers.add_parser("migrate-runtime")
    migrate_runtime.add_argument("backend")
    migrate_runtime.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        migrate_runtime,
        handler=_run_fix_command,
        formatter=format_fix_text,
        needs_settings=True,
    )

    reseed_rootfs = app_subparsers.add_parser("reseed-rootfs")
    reseed_rootfs.add_argument("backend")
    reseed_rootfs.add_argument(
        "--force",
        action="store_true",
        dest="confirm_reseed_rootfs",
    )
    reseed_rootfs.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        reseed_rootfs,
        handler=_run_reseed_rootfs_command,
        formatter=format_reseed_rootfs_text,
        needs_settings=True,
    )

    logs = app_subparsers.add_parser("logs")
    logs.add_argument("backend")
    logs.add_argument("--lines", type=int, default=200)
    logs.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        logs, handler=_run_logs_command, formatter=format_logs_text, needs_settings=True
    )

    shell = app_subparsers.add_parser("shell")
    shell.add_argument("backend")
    bind_command(shell, handler=_run_shell_command)

    exec_parser = app_subparsers.add_parser("exec")
    exec_parser.add_argument("backend")
    exec_parser.add_argument("exec_args", nargs=argparse.REMAINDER)
    bind_command(exec_parser, handler=_run_exec_command)


async def _run_doctor_command(args: argparse.Namespace, settings: Settings | None):
    assert settings is not None
    return await doctor_app_async(args.backend, settings)


async def _run_fix_command(args: argparse.Namespace, settings: Settings | None):
    assert settings is not None
    return await fix_app_async(args.backend, settings)


async def _run_logs_command(args: argparse.Namespace, settings: Settings | None):
    assert settings is not None
    return await logs_app_async(args.backend, args.lines, settings)


def format_reseed_rootfs_text(payload: dict[str, object]) -> str:
    lines = [
        f"backend: {payload.get('backend')}",
        f"changed: {'yes' if payload.get('changed') else 'no'}",
        f"seeded: {'yes' if payload.get('seeded') else 'no'}",
        f"rootfs: {payload.get('rootfs') or '-'}",
        f"backup_id: {payload.get('backup_id') or '-'}",
        f"backup_path: {payload.get('backup_path') or '-'}",
        f"quarantine_rootfs: {payload.get('quarantine_rootfs') or '-'}",
        f"seed_revision: {payload.get('seed_revision') or '-'}",
    ]
    preserved_steps = (
        payload.get("preserved_steps")
        if isinstance(payload.get("preserved_steps"), list)
        else []
    )
    provisioned_steps = (
        payload.get("provisioned_steps")
        if isinstance(payload.get("provisioned_steps"), list)
        else []
    )
    lines.append(
        f"preserved_steps: {', '.join(str(item) for item in preserved_steps) if preserved_steps else 'none'}"
    )
    lines.append(
        f"provisioned_steps: {', '.join(str(item) for item in provisioned_steps) if provisioned_steps else 'none'}"
    )
    return "\n".join(lines)


async def reseed_rootfs_app_async(backend_name: str, settings: Settings):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import SessionLocal
    from app.models.entities import Backend
    from app.services.app_runtime import reseed_app_backend_rootfs
    from app.services.backend_backup_service import create_backend_backup

    async with SessionLocal() as session:
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == backend_name)
                .limit(1)
            )
        ).scalar_one_or_none()
        if backend is None:
            return 1, None, f"backend not found: {backend_name}"
        if str(backend.kind or "").lower() != "app":
            return 1, None, f"backend is not an app output: {backend_name}"
        if not backend.enabled:
            return 1, None, f"backend is disabled: {backend_name}"

        logger.warning("app.reseed_rootfs.backup_requested", backend=backend.name)
        backup = await create_backend_backup(session, backend, settings)
        if backup.status != "success":
            logger.warning(
                "app.reseed_rootfs.backup_failed",
                backend=backend.name,
                backup_id=backup.id,
                error=backup.error or "unknown error",
            )
            return (
                1,
                None,
                f"backup failed before rootfs rebuild: {backup.error or 'unknown error'}",
            )
        backup_payload = {
            "backup_id": backup.id,
            "backup_path": backup.bundle_path,
            "backup_sha256": backup.bundle_sha256,
            "backup_size_bytes": backup.size_bytes,
        }
        logger.warning(
            "app.reseed_rootfs.backup_succeeded",
            backend=backend.name,
            backup_id=backup.id,
            backup_path=backup.bundle_path or "",
            backup_sha256=backup.bundle_sha256 or "",
            backup_size_bytes=backup.size_bytes or 0,
        )

        result = await reseed_app_backend_rootfs(
            backend,
            settings,
            backup_id=int(backup.id),
            backup_path=str(backup.bundle_path or ""),
        )
        payload = {
            "backend": backend.name,
            "changed": bool(result.get("seeded")),
            **backup_payload,
            **result,
        }
        return 0, payload, None


async def _run_reseed_rootfs_command(
    args: argparse.Namespace, settings: Settings | None
):
    assert settings is not None
    if not args.confirm_reseed_rootfs:
        return (
            2,
            None,
            "refusing to rebuild rootfs without --force",
        )
    return await reseed_rootfs_app_async(args.backend, settings)
