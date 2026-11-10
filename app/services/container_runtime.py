from __future__ import annotations

import json
from typing import Any

from app.services.commands import (
    CommandError,
    CommandResult,
    run_command,
    run_command_checked_async,
)
from app.services.observation import ABSENT, Observation, Present, Unknown


_INSPECT_MISSING_MARKERS = (
    "container not found",
    "container not known",
    "does not exist",
    "no such container",
    "no such network",
    "no such object",
    "network not found",
)


Inspection = Observation[dict[str, Any]]


def _parse_json_list(stdout: str) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or any(not isinstance(entry, dict) for entry in payload):
        return None
    return payload


def _command_detail(result: CommandResult) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[:500] if text else f"exit {result.returncode}"


def reports_missing(result: CommandResult) -> bool:
    if result.returncode != 125:
        return False
    output = "\n".join(part for part in (result.stderr, result.stdout) if part).lower()
    return any(marker in output for marker in _INSPECT_MISSING_MARKERS)


def classify_inspection(result: CommandResult) -> Inspection:
    if result.timed_out:
        return Unknown("timed_out", _command_detail(result))
    if not result.ok:
        if reports_missing(result):
            return ABSENT
        return Unknown("command_failed", _command_detail(result))
    payloads = _parse_json_list(result.stdout)
    if not payloads:
        return Unknown("malformed_payload", (result.stdout or "").strip()[:200])
    return Present(payloads[0])


def inspect_container(container: str, timeout_sec: int) -> Inspection:
    return classify_inspection(
        run_command(["podman", "inspect", container], timeout_sec=timeout_sec)
    )


def inspect_network(network: str, timeout_sec: int) -> Inspection:
    return classify_inspection(
        run_command(["podman", "network", "inspect", network], timeout_sec=timeout_sec)
    )


def classify_listing(result: CommandResult) -> Observation[list[dict[str, Any]]]:
    if result.timed_out:
        return Unknown("timed_out", _command_detail(result))
    if not result.ok:
        return Unknown("command_failed", _command_detail(result))
    entries = _parse_json_list(result.stdout or "[]")
    if entries is None:
        return Unknown("malformed_payload", (result.stdout or "").strip()[:200])
    return Present(entries)


def list_external_containers(timeout_sec: int) -> Observation[list[dict[str, Any]]]:
    return classify_listing(
        run_command(
            ["podman", "ps", "-a", "--external", "--format", "json"],
            timeout_sec=timeout_sec,
        )
    )


def list_mounted_containers(timeout_sec: int) -> Observation[list[dict[str, Any]]]:
    return classify_listing(
        run_command(["podman", "mount", "--format", "json"], timeout_sec=timeout_sec)
    )


def container_presence(container: str, timeout_sec: int) -> Observation[str]:
    # `podman container exists` exits 1 for a missing container and 125 for its own failures.
    result = run_command(
        ["podman", "container", "exists", container],
        timeout_sec=timeout_sec,
    )
    if result.timed_out:
        return Unknown("timed_out", _command_detail(result))
    if result.returncode == 0:
        return Present(container)
    if result.returncode == 1:
        return ABSENT
    return Unknown("command_failed", _command_detail(result))


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
