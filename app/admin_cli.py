from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


DEFAULT_ENV_FILE = Path("/etc/cnc.env")


def _build_parser(command_name: str | None = None) -> argparse.ArgumentParser:
    from app.cli import register_subparsers

    parser = argparse.ArgumentParser(prog="cnc-admin")
    register_subparsers(parser, command_names=[command_name] if command_name else None)
    return parser


def _load_env_defaults(path: Path = DEFAULT_ENV_FILE) -> None:
    from app.cli.common import load_env_defaults

    load_env_defaults(path)


def _resolve_settings() -> Settings:
    from app.cli.common import resolve_settings

    return resolve_settings()


def _resolve_llm_help_host(settings: Settings) -> str:
    from app.cli.common import resolve_llm_help_host

    return resolve_llm_help_host(settings)


def _configure_cli_logging(settings: Settings) -> None:
    from app.logger import configure_logging, resolve_log_version

    console_logging = str(os.environ.get("CNC_CLI_LOG_CONSOLE", "1")).strip().lower()
    console_enabled = console_logging not in {"0", "false", "no", "off"}
    configure_logging(
        str(settings.log_path),
        max_bytes=int(getattr(settings, "log_max_bytes", 10 * 1024 * 1024)),
        backup_count=int(getattr(settings, "log_backup_count", 5)),
        compress_rotated=bool(getattr(settings, "log_compress_rotated", True)),
        console=console_enabled,
        app_id=str(getattr(settings, "log_app_id", "cnc.admin")),
        env=str(getattr(settings, "log_env", "local")),
        version=resolve_log_version(str(getattr(settings, "log_version", ""))),
    )


def _emit_payload(
    payload: dict[str, object],
    *,
    json_output: bool,
    formatter,
) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(formatter(payload))


def _finish_command(
    exit_code: int,
    payload: dict[str, object] | None,
    error: str | None,
    *,
    json_output: bool,
    formatter,
) -> int:
    if error is not None:
        print(error, file=sys.stderr)
        return exit_code
    assert payload is not None
    _emit_payload(payload, json_output=json_output, formatter=formatter)
    return exit_code


def _command_name_from_argv(argv: list[str]) -> str | None:
    if not argv:
        return None
    first = str(argv[0]).strip()
    if not first or first.startswith("-"):
        return None
    return first


def _run_handler(args: argparse.Namespace) -> int:
    handler = getattr(args, "_cli_handler", None)
    if handler is None:
        raise ValueError("unsupported command")

    if bool(getattr(args, "_cli_needs_env", True)):
        _load_env_defaults()

    settings: Settings | None = None
    if bool(getattr(args, "_cli_needs_settings", False)):
        settings = _resolve_settings()
        if hasattr(settings, "log_path"):
            _configure_cli_logging(settings)

    result = handler(args, settings)
    if inspect.isawaitable(result):
        result = asyncio.run(result)

    if isinstance(result, tuple) and len(result) == 3:
        formatter = getattr(args, "_cli_formatter", None)
        if formatter is None:
            raise ValueError("missing formatter for payload command")
        return _finish_command(
            result[0],
            result[1],
            result[2],
            json_output=bool(getattr(args, "json_output", False)),
            formatter=formatter,
        )

    return int(result)


def main(argv: list[str] | None = None) -> int:
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    command_name = _command_name_from_argv(resolved_argv)
    if command_name is None:
        parser = _build_parser()
    else:
        from app.cli import is_known_command

        parser = _build_parser(command_name if is_known_command(command_name) else None)
    args = parser.parse_args(resolved_argv)

    try:
        return _run_handler(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
