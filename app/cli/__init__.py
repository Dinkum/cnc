from __future__ import annotations

import argparse
import importlib


COMMAND_MODULES: dict[str, str] = {
    "host": "app.cli.host",
    "app": "app.cli.app",
    "backup": "app.cli.backup",
    "fix": "app.cli.fix",
    "doctor": "app.cli.doctor",
    "repair": "app.cli.repair",
    "logs": "app.cli.logs",
    "llm-help": "app.cli.llm_help",
    "apply": "app.cli.apply",
    "reconcile-host": "app.cli.host",
    "auto-size": "app.cli.auto_size",
    "update": "app.cli.update",
    "cloudflare": "app.cli.cloudflare",
    "backend-alerts": "app.cli.alerts",
    "notifications": "app.cli.alerts",
    "restore-verify": "app.cli.restore",
    "restore-create": "app.cli.restore",
    "shell": "app.cli.shell",
    "exec": "app.cli.shell",
}
COMMAND_ORDER = [
    "host",
    "app",
    "backup",
    "fix",
    "doctor",
    "repair",
    "logs",
    "llm-help",
    "apply",
    "reconcile-host",
    "auto-size",
    "update",
    "cloudflare",
    "backend-alerts",
    "notifications",
    "restore-verify",
    "restore-create",
    "shell",
    "exec",
]


def register_subparsers(
    parser: argparse.ArgumentParser,
    *,
    command_names: list[str] | tuple[str, ...] | None = None,
) -> None:
    subparsers = parser.add_subparsers(dest="command", required=True)
    requested = _normalized_command_names(command_names)
    registered_modules: set[str] = set()
    for command_name in requested:
        module_name = COMMAND_MODULES[command_name]
        if module_name in registered_modules:
            continue
        importlib.import_module(module_name).register(subparsers)
        registered_modules.add(module_name)


def is_known_command(command_name: str | None) -> bool:
    normalized = str(command_name or "").strip()
    return normalized in COMMAND_MODULES


def _normalized_command_names(
    command_names: list[str] | tuple[str, ...] | None,
) -> list[str]:
    if not command_names:
        return list(COMMAND_ORDER)
    requested = {name for name in command_names if name in COMMAND_MODULES}
    return [name for name in COMMAND_ORDER if name in requested]
