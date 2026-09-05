from __future__ import annotations

import argparse

from app.cli.common import bind_command
from app.cli.restore import (
    format_restore_create_text,
    format_restore_verify_text,
    restore_create_async,
    restore_verify_app_async,
)
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = subparsers.add_parser("backup")
    backup_subparsers = backup.add_subparsers(dest="backup_command", required=True)

    verify = backup_subparsers.add_parser("verify")
    verify.add_argument("backend")
    verify.add_argument("--backup-id", type=int, default=None)
    verify.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        verify, handler=_run_verify_command, formatter=format_restore_verify_text
    )

    import_parser = backup_subparsers.add_parser("import")
    import_parser.add_argument("bundle_path")
    import_parser.add_argument("--backend-name", dest="backend_name", default=None)
    import_parser.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        import_parser,
        handler=_run_import_command,
        formatter=format_restore_create_text,
        needs_settings=True,
    )


async def _run_verify_command(args: argparse.Namespace, settings: Settings | None):
    return await restore_verify_app_async(args.backend, args.backup_id)


async def _run_import_command(args: argparse.Namespace, settings: Settings | None):
    assert settings is not None
    return await restore_create_async(args.bundle_path, args.backend_name, settings)
