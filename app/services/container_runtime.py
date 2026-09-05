from __future__ import annotations

import json
from typing import Any

from app.services.commands import (
    CommandError,
    CommandResult,
    run_command,
    run_command_checked_async,
)


_INSPECT_MISSING_MARKERS = (
    "container not found",
    "container not known",
    "does not exist",
    "no such container",
    "no such network",
    "no such object",
    "network not found",
)


def _parse_json_list(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def inspect_container(
    container: str, timeout_sec: int
) -> tuple[CommandResult, dict[str, Any] | None]:
    result = run_command(["podman", "inspect", container], timeout_sec=timeout_sec)
    payloads = _parse_json_list(result.stdout)
    return result, (payloads[0] if payloads else None)


def inspect_network(
    network: str, timeout_sec: int
) -> tuple[CommandResult, dict[str, Any] | None]:
    result = run_command(
        ["podman", "network", "inspect", network], timeout_sec=timeout_sec
    )
    payloads = _parse_json_list(result.stdout)
    return result, (payloads[0] if payloads else None)


def inspect_result_reports_missing(result: CommandResult) -> bool:
    """Return true only when Podman definitively says the inspected object is absent."""
    if result.returncode != 125:
        return False
    output = "\n".join(part for part in (result.stderr, result.stdout) if part).lower()
    return any(marker in output for marker in _INSPECT_MISSING_MARKERS)


def list_external_containers(timeout_sec: int) -> list[dict[str, Any]]:
    result = run_command(
        ["podman", "ps", "-a", "--external", "--format", "json"],
        timeout_sec=timeout_sec,
    )
    if not result.ok:
        return []
    return _parse_json_list(result.stdout)


def list_mounted_containers(timeout_sec: int) -> list[dict[str, Any]]:
    result = run_command(
        ["podman", "mount", "--format", "json"],
        timeout_sec=timeout_sec,
    )
    if not result.ok:
        return []
    return _parse_json_list(result.stdout)


def container_exists(container: str, timeout_sec: int) -> bool:
    result = run_command(
        ["podman", "container", "exists", container],
        timeout_sec=timeout_sec,
    )
    return result.returncode == 0


async def read_container_stats(container: str, timeout_sec: int) -> CommandResult:
    json_command = ["podman", "stats", "--no-stream", "--format", "json", container]
    try:
        return await run_command_checked_async(json_command, timeout_sec=timeout_sec)
    except CommandError as json_exc:
        try:
            return await run_command_checked_async(
                [
                    "podman",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}",
                    container,
                ],
                timeout_sec=timeout_sec,
            )
        except CommandError as legacy_exc:
            return (
                legacy_exc.result
                if legacy_exc.result.returncode != 0
                else json_exc.result
            )
