from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from app.cli.common import (
    CommandResult,
    bind_command,
    load_backend_async,
    load_shield_status_payload,
)
from app.config import Settings


LEGACY_LOG_ENTRY_HEADER_RE = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<level>[A-Z]+)\s+(?P<app_id>\S+)\s+(?P<message>.*)$"
)
BLOCK_LOG_ENTRY_HEADER_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})\] ----- "
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d{3}) \| "
    r"(?P<level>[A-Z]+)\s+\| "
    r"(?P<app_id>\S+) \| "
    r"(?P<category>.+?) -----$"
)
ADMIN_LOG_TAIL_MAX_BYTES = 1_048_576


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    logs = subparsers.add_parser("logs")
    logs_subparsers = logs.add_subparsers(dest="logs_command", required=True)

    logs_app = logs_subparsers.add_parser("app")
    logs_app.add_argument("backend")
    logs_app.add_argument("--lines", type=int, default=200)
    logs_app.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        logs_app,
        handler=_run_logs_app_command,
        formatter=format_logs_text,
        needs_settings=True,
    )

    logs_admin = logs_subparsers.add_parser("admin")
    logs_admin.add_argument("--lines", type=int, default=200)
    logs_admin.add_argument("--errors", action="store_true", dest="errors_only")
    logs_admin.add_argument("--component", default=None)
    logs_admin.add_argument("--request-id", dest="request_id", default=None)
    logs_admin.add_argument("--error-code", dest="error_code", default=None)
    logs_admin.add_argument("--error-inst", dest="error_inst", default=None)
    logs_admin.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        logs_admin,
        handler=_run_logs_admin_command,
        formatter=format_admin_logs_text,
        needs_settings=True,
    )


def format_logs_text(payload: dict[str, object]) -> str:
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    source_errors = (
        payload.get("source_errors")
        if isinstance(payload.get("source_errors"), list)
        else []
    )
    collection_status = (
        "partial"
        if payload.get("partial")
        else ("complete" if payload.get("ok", True) else "failed")
    )
    lines = [
        f"backend: {payload.get('backend')}",
        f"container: {payload.get('container')}",
        f"collection_status: {collection_status}",
        f"issues: {', '.join(str(item) for item in issues) if issues else 'none'}",
        f"bootstrap_status: {payload.get('bootstrap_status') or '-'}",
        f"sources_available: {'yes' if payload.get('sources_available') else 'no'}",
        f"requested_lines: {payload.get('requested_lines')}",
    ]
    if sources:
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_name = source.get("source") or "unknown"
            output = str(source.get("output") or "").strip()
            lines.append("")
            lines.append(f"== {source_name} ==")
            lines.append(output or "(empty)")
    else:
        lines.append("")
        lines.append("(no log sources available)")

    if source_errors:
        lines.append("")
        lines.append("source_errors:")
        for error in source_errors:
            if not isinstance(error, dict):
                continue
            source = error.get("source") or "unknown"
            returncode = error.get("returncode")
            detail = error.get("stderr") or error.get("stdout") or "collection failed"
            lines.append(f"  {source} (exit {returncode}): {detail}")

    shield_status = (
        payload.get("shield_status")
        if isinstance(payload.get("shield_status"), dict)
        else {}
    )
    if shield_status:
        lines.append("")
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


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    target_lines = max(1, max_lines)
    chunks: list[bytes] = []
    newline_count = 0
    block_size = 8192
    bytes_read = 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while (
            position > 0
            and newline_count <= target_lines
            and bytes_read < ADMIN_LOG_TAIL_MAX_BYTES
        ):
            read_size = min(block_size, position, ADMIN_LOG_TAIL_MAX_BYTES - bytes_read)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            bytes_read += len(chunk)
            newline_count += chunk.count(b"\n")
    content = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()
    return content[-target_lines:]


def _parse_admin_log_entries(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current_lines: list[str] = []
    current_header: dict[str, str] | None = None

    for raw_line in lines:
        line = str(raw_line)
        block_header_match = BLOCK_LOG_ENTRY_HEADER_RE.match(line)
        legacy_header_match = LEGACY_LOG_ENTRY_HEADER_RE.match(line)
        if block_header_match or legacy_header_match:
            if current_lines and current_header is not None:
                entries.append(
                    {
                        "timestamp": current_header["timestamp"],
                        "level": current_header["level"],
                        "app_id": current_header["app_id"],
                        "first_line": current_lines[0],
                        "text": "\n".join(current_lines),
                        "category": current_header.get("category") or "",
                    }
                )
            if block_header_match:
                current_header = {
                    "timestamp": f"{block_header_match.group('date')}T{block_header_match.group('time')}Z",
                    "level": block_header_match.group("level"),
                    "app_id": block_header_match.group("app_id"),
                    "message": "",
                    "category": block_header_match.group("category").strip(),
                }
            else:
                current_header = legacy_header_match.groupdict()  # type: ignore[union-attr]
                current_header["category"] = ""
            current_lines = [line]
            continue
        if current_lines:
            current_lines.append(line)

    if current_lines and current_header is not None:
        entries.append(
            {
                "timestamp": current_header["timestamp"],
                "level": current_header["level"],
                "app_id": current_header["app_id"],
                "first_line": current_lines[0],
                "text": "\n".join(current_lines),
                "category": current_header.get("category") or "",
            }
        )
    return entries


def _entry_matches_filter(text: str, *, key: str, value: str | None) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return True
    return f"{key}={normalized}" in text or f"{key}: {normalized}" in text


def collect_admin_logs(
    settings: Settings,
    *,
    lines: int,
    errors_only: bool,
    component: str | None,
    request_id: str | None,
    error_code: str | None,
    error_inst: str | None,
) -> CommandResult:
    log_path = Path(settings.log_path)
    if not log_path.exists():
        return 1, None, f"log file not found: {log_path}"

    raw_lines = _tail_lines(log_path, lines)
    entries = _parse_admin_log_entries(raw_lines)
    filtered: list[dict[str, object]] = []
    for entry in entries:
        text = str(entry.get("text") or "")
        first_line = str(entry.get("first_line") or "")
        level = str(entry.get("level") or "")
        if errors_only and level not in {"ERROR", "CRITICAL"}:
            continue
        category = str(entry.get("category") or "")
        if component and category and category != component:
            continue
        if not category and not _entry_matches_filter(
            first_line, key="component", value=component
        ):
            continue
        if not _entry_matches_filter(text, key="request_id", value=request_id):
            continue
        if not _entry_matches_filter(text, key="error_code", value=error_code):
            continue
        if not _entry_matches_filter(text, key="error_inst", value=error_inst):
            continue
        filtered.append(entry)

    payload: dict[str, object] = {
        "app_id": str(getattr(settings, "log_app_id", "") or "cnc.admin"),
        "log_path": str(log_path),
        "scanned_lines": len(raw_lines),
        "requested_lines": max(1, lines),
        "errors_only": errors_only,
        "component": component,
        "request_id": request_id,
        "error_code": error_code,
        "error_inst": error_inst,
        "entry_count": len(filtered),
        "entries": filtered,
    }
    return 0, payload, None


def format_admin_logs_text(payload: dict[str, object]) -> str:
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    lines = [
        f"app_id: {payload.get('app_id') or '-'}",
        f"log_path: {payload.get('log_path') or '-'}",
        f"requested_lines: {payload.get('requested_lines') or 0}",
        f"scanned_lines: {payload.get('scanned_lines') or 0}",
        f"errors_only: {'yes' if payload.get('errors_only') else 'no'}",
        f"component: {payload.get('component') or '-'}",
        f"request_id: {payload.get('request_id') or '-'}",
        f"error_code: {payload.get('error_code') or '-'}",
        f"error_inst: {payload.get('error_inst') or '-'}",
        f"entry_count: {payload.get('entry_count') or 0}",
    ]
    if entries:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lines.append("")
            lines.append(
                f"== {entry.get('timestamp') or '-'} {entry.get('level') or '-'} =="
            )
            lines.append(str(entry.get("text") or "").strip() or "(empty)")
    else:
        lines.append("")
        lines.append("(no matching log entries)")

    shield_status = (
        payload.get("shield_status")
        if isinstance(payload.get("shield_status"), dict)
        else {}
    )
    if shield_status:
        lines.append("")
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


async def logs_app_async(
    backend_name: str,
    lines: int,
    settings: Settings,
) -> CommandResult:
    from app.services.app_logs import collect_app_backend_logs

    backend = await load_backend_async(backend_name)
    if backend is None:
        return 1, None, f"backend not found: {backend_name}"
    payload = await asyncio.to_thread(
        collect_app_backend_logs, backend, settings, lines=lines
    )
    payload["shield_status"] = await load_shield_status_payload(settings)
    return (0 if payload.get("ok") else 2), payload, None


async def _run_logs_app_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await logs_app_async(args.backend, args.lines, settings)


async def _run_logs_admin_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    status_code, payload, error = collect_admin_logs(
        settings,
        lines=args.lines,
        errors_only=bool(args.errors_only),
        component=args.component,
        request_id=args.request_id,
        error_code=args.error_code,
        error_inst=args.error_inst,
    )
    if payload is None or status_code != 0:
        return status_code, payload, error
    payload["shield_status"] = await load_shield_status_payload(settings)
    return 0, payload, error
