from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.models.entities import Backend
from app.services.bootstrap_state import read_bootstrap_state, read_failure_artifact
from app.services.commands import run_command
from app.services.renderers import container_name


def _clip_text(value: str, max_chars: int = 240) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}...[truncated]"


def _format_runtime_events(events: list[dict[str, Any]], limit: int) -> str:
    rendered: list[str] = []
    for entry in events[-limit:]:
        timestamp = str(entry.get("timestamp") or "-")
        kind = str(entry.get("kind") or "event")
        name = str(entry.get("name") or "-")
        status = str(entry.get("status") or "-")
        line = f"{timestamp} {kind}.{name} {status}"
        details = entry.get("details")
        if isinstance(details, dict) and details:
            line = f"{line} {_clip_text(json.dumps(details, sort_keys=True))}"
        rendered.append(line)
    return "\n".join(rendered)


def collect_app_backend_logs(
    backend: Backend,
    settings: Settings,
    *,
    lines: int = 200,
) -> dict[str, Any]:
    sources: list[dict[str, str]] = []
    source_errors: list[dict[str, object]] = []
    tail_lines = max(1, min(int(lines), 1000))
    container = container_name(backend.name)

    result = run_command(
        ["podman", "logs", "--tail", str(tail_lines), container],
        timeout_sec=settings.command_timeout_status_sec,
    )
    if result.stdout:
        sources.append({"source": "container-stdout", "output": result.stdout})
    if result.ok and result.stderr:
        sources.append({"source": "container-stderr", "output": result.stderr})
    if not result.ok:
        source_errors.append(
            {
                "source": "container",
                "returncode": result.returncode,
                "stderr": _clip_text(result.stderr, 2000),
                "stdout": _clip_text(result.stdout, 2000),
            }
        )

    state = read_bootstrap_state(settings, backend.name) or {}
    excerpt = state.get("last_log_excerpt") if isinstance(state, dict) else None
    events = (
        state.get("events")
        if isinstance(state, dict) and isinstance(state.get("events"), list)
        else []
    )
    if isinstance(excerpt, str) and excerpt.strip():
        sources.append({"source": "bootstrap-state", "output": excerpt})
    if events:
        structured_events = [entry for entry in events if isinstance(entry, dict)]
        if structured_events:
            sources.append(
                {
                    "source": "runtime-events",
                    "output": _format_runtime_events(structured_events, tail_lines),
                }
            )

    failure_artifact = read_failure_artifact(settings, backend.name) or {}
    if isinstance(failure_artifact, dict) and failure_artifact:
        details = failure_artifact.get("details")
        runtime_diagnostics = failure_artifact.get("runtime_diagnostics")
        artifact_lines = [
            f"phase: {failure_artifact.get('phase') or '-'}",
            f"status: {failure_artifact.get('status') or '-'}",
        ]
        if isinstance(details, dict):
            for key in ("error", "failed_phase", "stderr", "stdout"):
                value = details.get(key)
                if value:
                    artifact_lines.append(f"{key}: {_clip_text(str(value), 4000)}")
        if isinstance(runtime_diagnostics, dict):
            issues = runtime_diagnostics.get("issues")
            if isinstance(issues, list) and issues:
                artifact_lines.append(
                    f"runtime_issues: {', '.join(str(item) for item in issues)}"
                )
        recent_events = failure_artifact.get("recent_events")
        if isinstance(recent_events, list) and recent_events:
            artifact_lines.append(f"recent_events: {len(recent_events)}")
        sources.append(
            {"source": "failure-artifact", "output": "\n".join(artifact_lines)}
        )

    issues: list[str] = []
    if state.get("status") == "failed":
        issues.append("bootstrap_failed")
    runtime_diagnostics = failure_artifact.get("runtime_diagnostics")
    if isinstance(runtime_diagnostics, dict):
        runtime_issues = runtime_diagnostics.get("issues")
        if isinstance(runtime_issues, list):
            issues.extend(str(item) for item in runtime_issues if item)
    if source_errors:
        issues.append("container_logs_unavailable")

    return {
        "backend": backend.name,
        "container": container,
        "issues": list(dict.fromkeys(issues)),
        "bootstrap_status": state.get("status"),
        "requested_lines": tail_lines,
        "sources_available": bool(sources),
        "sources": sources,
        "source_errors": source_errors,
        "partial": bool(sources and source_errors),
        "ok": not source_errors,
    }
