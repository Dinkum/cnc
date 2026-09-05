from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

from app.models.entities import ApplyRun, BackendBackup
from app.schemas.apply import ApplyResponse


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    hour = value.strftime("%I").lstrip("0") or "0"
    return f"{value:%b} {value.day}, {value:%Y}, {hour}:{value:%M} {value:%p}"


def _timestamp_data_value(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat()


def _timestamp_data_string(value: object) -> str:
    if isinstance(value, datetime):
        return _timestamp_data_value(value)
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return _timestamp_data_value(parsed)


def _format_timestamp_value(value: object) -> str | None:
    if isinstance(value, datetime):
        return _format_timestamp(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return _format_timestamp(parsed)


def _summarize_values(values: list[str], *, limit: int = 3) -> str:
    cleaned = [value for value in values if value]
    if not cleaned:
        return "-"
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return f"{', '.join(cleaned[:limit])} +{len(cleaned) - limit} more"


def _normalize_route_contracts(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        input_kind = str(item.get("input_kind") or "").strip()
        input_value = str(item.get("input_value") or "").strip()
        backend_names = sorted(
            str(value).strip()
            for value in item.get("backend_names") or []
            if str(value).strip()
        )
        normalized.append(
            {
                "route_key": f"{input_kind}:{input_value}",
                "input_kind": input_kind,
                "input_value": input_value,
                "backend_names": backend_names,
                "target": str(item.get("target") or "").strip(),
                "enabled": bool(item.get("enabled")),
            }
        )
    return normalized


def _normalize_backend_contracts(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        backend = str(item.get("backend") or "").strip()
        if not backend:
            continue
        normalized.append(
            {
                "backend": backend,
                "kind": str(item.get("kind") or "").strip(),
                "enabled": bool(item.get("enabled")),
                "port": item.get("port"),
                "handoff_port": item.get("handoff_port"),
                "sandbox_profile": str(item.get("sandbox_profile") or "").strip(),
                "healthcheck_path": str(item.get("healthcheck_path") or "").strip(),
            }
        )
    return normalized


def _change_state(
    *,
    created_at: datetime | None,
    updated_at: datetime | None,
    last_applied_at: datetime | None,
    now: datetime | None = None,
) -> str | None:
    latest = updated_at or created_at
    if latest is None:
        return None
    reference_now = now or (
        datetime.now(tz=created_at.tzinfo)
        if created_at and created_at.tzinfo
        else datetime.now()
    )
    new_cutoff = reference_now - timedelta(hours=24)
    is_new_change = last_applied_at is None or (
        created_at is not None and created_at > last_applied_at
    )
    if is_new_change and created_at is not None and created_at >= new_cutoff:
        return "new"
    if last_applied_at is None:
        return "unsynced"
    if latest > last_applied_at:
        return "unsynced"
    return None


def _format_bytes(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "-"
    if value == 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    unit = units[0]
    for current in units:
        unit = current
        if size < 1024 or current == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def _format_compact_decimal_bytes(value: object) -> str:
    if not isinstance(value, int) or value < 0:
        return "-"
    if value == 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit = units[0]
    for current in units:
        unit = current
        if size < 1000 or current == units[-1]:
            break
        size /= 1000
    if unit in {"B", "KB", "MB"}:
        return f"{int(round(size))}{unit}"
    return f"{size:.1f}{unit}"


def _tone_for_value(
    value: str | None,
    *,
    success_values: set[str],
    queued_values: set[str],
    error_values: set[str],
    ok: bool | None = None,
) -> str:
    normalized = (value or "").lower()
    if ok is False:
        return "error"
    if normalized in success_values:
        return "success"
    if normalized in queued_values:
        return "queued"
    if normalized in error_values:
        return "error"
    return "inactive"


def _backup_summary(
    backup: BackendBackup | None,
    coverage: dict[str, object] | None = None,
    planned_coverage: str | None = None,
) -> dict[str, object]:
    coverage = coverage or {}
    covered_paths_summary = str(coverage.get("covered_paths_summary") or "")
    if backup is None:
        return {
            "available": False,
            "rows": [
                ("covered paths", planned_coverage or "-"),
            ],
        }
    return {
        "available": True,
        "rows": [
            ("covered paths", covered_paths_summary or "-"),
        ],
    }


def _backup_status_tone(status: str | None) -> str:
    return _tone_for_value(
        status,
        success_values={"success"},
        queued_values={"running", "queued", "pending"},
        error_values={"error", "failed"},
    )


def _backup_history_rows(
    backups: list[BackendBackup],
    coverage_by_id: dict[int, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    coverage_by_id = coverage_by_id or {}
    rows: list[dict[str, object]] = []
    for backup in backups:
        coverage = coverage_by_id.get(backup.id, {})
        rows.append(
            {
                "id": backup.id,
                "status": backup.status or "-",
                "tone": _backup_status_tone(backup.status),
                "created_at": _format_timestamp(backup.created_at) or "-",
                "created_at_raw": _timestamp_data_value(backup.created_at),
                "covered_paths_summary": str(
                    coverage.get("covered_paths_summary") or "-"
                ),
                "size": _format_compact_decimal_bytes(backup.size_bytes),
                "bundle": Path(str(backup.bundle_path or "-")).name
                if backup.bundle_path
                else "-",
                "notes": backup.notes or backup.error or "-",
                "restorable": backup.status == "success",
            }
        )
    return rows


def _pending_backup_history_rows(
    backups: list[BackendBackup],
) -> list[dict[str, object]]:
    rows = _backup_history_rows(
        [backup for backup in backups if str(backup.status or "").lower() == "success"]
    )
    for row in rows:
        if row["status"] == "success" and row["bundle"] != "-":
            row["covered_paths_summary"] = "not recorded"
    return rows


def _debug_kv_dump(rows: list[tuple[str, object]]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in rows)


def _save_job_id_meta(run_id: object) -> dict[str, str]:
    return {
        "label": "apply id",
        "value": f"#{run_id}" if isinstance(run_id, int) else "pending",
    }


def _save_job_error_meta(details: dict[str, object]) -> dict[str, str] | None:
    error_code = str(details.get("error_code") or "").strip()
    error_inst = str(details.get("error_inst") or "").strip()
    if not error_code or not error_inst:
        return None
    return {"label": "error", "value": f"{error_code}-{error_inst}"}


def _save_job_status_label(
    status: str | None, details: dict[str, object] | None = None
) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "success":
        return "saved"
    if normalized == "error":
        return "failed"
    if normalized in {"queued", "pending"}:
        return "queued"
    if normalized == "running":
        return "running"
    return normalized or "unknown"


def _save_job_tone(status: str | None) -> str:
    return _tone_for_value(
        status,
        success_values={"success"},
        queued_values={"queued", "pending", "running"},
        error_values={"error", "failed"},
    )


def _save_job_counts(details: dict[str, object]) -> list[str]:
    counts: list[str] = []
    nginx_files = details.get("nginx_files")
    if isinstance(nginx_files, list):
        counts.append(f"{len(nginx_files)} nginx")
    route_contracts = details.get("route_contracts")
    if isinstance(route_contracts, list):
        counts.append(f"{len(route_contracts)} routes")
    tailscale_paths = details.get("tailscale_paths")
    if isinstance(tailscale_paths, list):
        counts.append(f"{len(tailscale_paths)} tailnet")
    tailscale_services = details.get("tailscale_services")
    if isinstance(tailscale_services, list):
        counts.append(f"{len(tailscale_services)} tailnet service")
    running = details.get("app_containers_running")
    if isinstance(running, list):
        counts.append(f"{len(running)} app runtime")
    ssh_backends = details.get("ssh_backends")
    if isinstance(ssh_backends, list):
        counts.append(f"{len(ssh_backends)} ssh")
    runtime_assets = details.get("runtime_assets")
    if isinstance(runtime_assets, dict):
        changed_units = runtime_assets.get("changed_units")
        if isinstance(changed_units, list) and changed_units:
            counts.append(f"{len(changed_units)} host assets")
    return counts


def _humanize_save_phase(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    return normalized.replace("_", " ")


def _capitalize_first(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def _format_save_failure_subject(value: str) -> str:
    acronyms = {
        "dns": "DNS",
        "http": "HTTP",
        "https": "HTTPS",
        "nginx": "NGINX",
        "ssh": "SSH",
    }
    words = [acronyms.get(word, word) for word in value.split(" ")]
    return _capitalize_first(" ".join(words))


def _save_job_primary_phase(details: dict[str, object]) -> str:
    failed_phase = str(details.get("failed_phase") or "").strip()
    phase = str(details.get("phase") or "").strip()
    if failed_phase:
        return failed_phase
    return phase


def _save_job_backend_name(details: dict[str, object]) -> str:
    backend = str(details.get("backend") or "").strip()
    if backend:
        return backend

    runtime_diagnostics = details.get("runtime_diagnostics")
    if isinstance(runtime_diagnostics, dict):
        backend = str(runtime_diagnostics.get("backend") or "").strip()
        if backend:
            return backend

    for key in ("container", "network"):
        value = str(details.get(key) or "").strip()
        if value.startswith("cnc-app-"):
            return value.removeprefix("cnc-app-")
        if value.startswith("cnc-net-"):
            return value.removeprefix("cnc-net-")

    command = details.get("command")
    command_text = (
        " ".join(str(item) for item in command)
        if isinstance(command, list)
        else str(command or "")
    )
    match = re.search(r"\bcnc-app-([a-z0-9-]+)\b", command_text)
    if match:
        return match.group(1)
    match = re.search(r"\bcnc-net-([a-z0-9-]+)\b", command_text)
    if match:
        return match.group(1)
    return ""


def _save_job_root_cause(details: dict[str, object]) -> str:
    phase = (
        str(details.get("phase") or details.get("failed_phase") or "").strip().lower()
    )
    findings = details.get("findings")
    if phase == "control_plane_self_audit" and isinstance(findings, list):
        for severity in ("blocking", "warning"):
            for item in findings:
                if not isinstance(item, dict):
                    continue
                if str(item.get("severity") or "").strip().lower() != severity:
                    continue
                message = str(item.get("message") or "").strip()
                if message:
                    return message
    if phase == "app_healthcheck" or details.get("http_status") is not None:
        status = details.get("http_status")
        url = str(details.get("url") or "").strip()
        host_header = str(details.get("host_header") or "").strip()
        error = str(details.get("error") or "").strip()
        if isinstance(status, int):
            cause = f"healthcheck returned HTTP {status}"
        elif error:
            cause = f"healthcheck: {error}"
        else:
            cause = "healthcheck failed"
        if url:
            cause = f"{cause} at {url}"
        if host_header:
            cause = f"{cause} with Host {host_header}"
        return cause

    stderr = str(details.get("stderr") or "").strip()
    if stderr:
        return stderr.splitlines()[0].strip()

    diagnostics = details.get("runtime_diagnostics")
    if isinstance(diagnostics, dict):
        for key in (
            "app_handoff_error",
            "loopback_error",
            "private_error",
            "inspect_error",
        ):
            value = str(diagnostics.get(key) or "").strip()
            if value:
                return value.splitlines()[0].strip()
        diagnosis = str(diagnostics.get("diagnosis") or "").strip()
        if diagnosis:
            return diagnosis.replace("_", " ")

    error = str(details.get("error") or "").strip()
    if error and error.lower() != "command failed":
        return error
    return error or "save failed"


def _save_job_command_label(details: dict[str, object]) -> str | None:
    command = details.get("command")
    if isinstance(command, list) and command:
        return " ".join(str(item) for item in command)
    if isinstance(command, str) and command.strip():
        return command.strip()
    return None


def _save_job_phase_rows(details: dict[str, object]) -> list[dict[str, str]]:
    phase_map = details.get("app_reconcile_phases")
    if not isinstance(phase_map, dict):
        return []
    rows: list[dict[str, str]] = []
    for backend, phases in sorted(phase_map.items()):
        if not isinstance(backend, str) or not isinstance(phases, list):
            continue
        phase_bits: list[str] = []
        tone = "inactive"
        for item in phases:
            if not isinstance(item, dict):
                continue
            phase_name = str(item.get("phase") or "").strip()
            phase_status = str(item.get("status") or "").strip().lower()
            if not phase_name:
                continue
            if phase_status == "failed":
                tone = "error"
            elif phase_status in {"running", "queued", "pending"} and tone != "error":
                tone = "queued"
            elif phase_status == "succeeded" and tone == "inactive":
                tone = "success"
            phase_bits.append(f"{phase_name} {phase_status or 'unknown'}")
        if not phase_bits:
            continue
        rows.append(
            {
                "backend": backend,
                "summary": " · ".join(phase_bits),
                "tone": tone,
            }
        )
    return rows


def _save_job_change_rows(details: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    route_contracts = _normalize_route_contracts(details.get("route_contracts"))
    if route_contracts:
        rows.append(
            {
                "label": "routes",
                "value": _summarize_values(
                    [
                        f"{item['input_kind']} {item['input_value']}"
                        for item in route_contracts
                    ],
                    limit=3,
                ),
                "tone": "success",
            }
        )
    backend_contracts = _normalize_backend_contracts(details.get("backend_contracts"))
    if backend_contracts:
        rows.append(
            {
                "label": "outputs",
                "value": _summarize_values(
                    [str(item["backend"]) for item in backend_contracts], limit=4
                ),
                "tone": "success",
            }
        )
    ssh_backends = details.get("ssh_backends")
    if isinstance(ssh_backends, list):
        rows.append(
            {
                "label": "backend ssh",
                "value": _summarize_values(
                    [str(item) for item in ssh_backends], limit=4
                ),
                "tone": "success",
            }
        )
    runtime_assets = details.get("runtime_assets")
    if isinstance(runtime_assets, dict):
        changed_units = runtime_assets.get("changed_units")
        if isinstance(changed_units, list) and changed_units:
            rows.append(
                {
                    "label": "host assets",
                    "value": _summarize_values(
                        [str(item) for item in changed_units], limit=3
                    ),
                    "tone": "warn",
                }
            )
        elif runtime_assets.get("timer_reconciled"):
            rows.append(
                {
                    "label": "host assets",
                    "value": "managed timers reconciled",
                    "tone": "warn",
                }
            )
    return rows


def _save_job_detail_dump(details: dict[str, object]) -> str | None:
    rows: list[tuple[str, object]] = []
    backend = _save_job_backend_name(details)
    if backend:
        rows.append(("backend", backend))

    phase = _save_job_primary_phase(details)
    if phase:
        rows.append(("failed phase", _humanize_save_phase(phase)))

    root_cause = _save_job_root_cause(details)
    if root_cause:
        rows.append(("root cause", root_cause))

    command = _save_job_command_label(details)
    if command:
        rows.append(("command", command))

    failure_mode = str(details.get("failure_mode") or "").strip()
    if failure_mode.lower() == "partial":
        rows.append(("host state", "partially updated before failure"))
    elif failure_mode.lower() == "clean":
        rows.append(("host state", "previous config still active"))

    counts = _save_job_counts(details)
    if counts:
        rows.append(("saved", ", ".join(counts)))
    for key in (
        "nginx_files",
        "tailscale_paths",
        "tailscale_services",
        "app_containers_running",
        "app_containers_bootstrapped",
    ):
        value = details.get(key)
        if value:
            rows.append((key, value))
    phase_rows = _save_job_phase_rows(details)
    if phase_rows:
        rows.append(
            (
                "app_reconcile",
                [f"{item['backend']}: {item['summary']}" for item in phase_rows],
            )
        )
    for key in ("completed_phases", "live_mutation_phases"):
        value = details.get(key)
        if value:
            rows.append((key, value))
    nginx_rollback = details.get("nginx_rollback")
    if isinstance(nginx_rollback, dict) and nginx_rollback:
        rows.append(("nginx_rollback", nginx_rollback))
    if not rows:
        return None
    return _debug_kv_dump(rows)


def _save_job_failure_copy(
    details: dict[str, object],
    *,
    message: str,
    phase: object,
) -> tuple[str, str]:
    backend = _save_job_backend_name(details)
    error = _save_job_root_cause(details)
    phase_label = _humanize_save_phase(phase)
    subject = " ".join(part for part in (backend, phase_label) if part).strip()
    headline = f"{_format_save_failure_subject(subject or 'save')} failed"
    if error:
        return headline, error

    failure_mode = str(details.get("failure_mode") or "").strip().lower()
    if failure_mode == "partial":
        return headline, "Some host changes may already be active."
    if failure_mode == "clean":
        return headline, "Previous config is still active."
    return headline, message or "No detailed reason was reported."


def _save_job_summary(
    source: object,
    *,
    title: str = "Latest save",
    include_detail_dump: bool = True,
) -> dict[str, object] | None:
    if isinstance(source, ApplyResponse):
        run_id = source.run_id
        status = source.status
        message = source.message
        details = source.details if isinstance(source.details, dict) else {}
        created_at = _format_timestamp(source.created_at)
        created_at_raw = _timestamp_data_value(source.created_at)
    elif isinstance(source, dict):
        run_id = source.get("id")
        status = source.get("status")
        message = str(source.get("message") or "")
        details = (
            source.get("details") if isinstance(source.get("details"), dict) else {}
        )
        created_at = _format_timestamp_value(source.get("created_at"))
        created_at_raw = _timestamp_data_string(source.get("created_at"))
    elif isinstance(source, ApplyRun):
        run_id = source.id
        status = source.status
        message = source.message
        details = source.details
        created_at = _format_timestamp(source.created_at)
        created_at_raw = _timestamp_data_value(source.created_at)
    else:
        return None

    tone = _save_job_tone(str(status or ""))
    status_label = _save_job_status_label(str(status or ""), details)
    phase = _save_job_primary_phase(details) or None
    if run_id is None and not status and not message and not details and not created_at:
        return None
    counts = _save_job_counts(details)
    change_rows = _save_job_change_rows(details)
    phase_rows = _save_job_phase_rows(details)
    detail_dump = _save_job_detail_dump(details) if include_detail_dump else None

    if str(status or "").lower() == "success":
        headline = "Host updated."
        note = (
            ", ".join(counts)
            if counts
            else (message or "Host state converged successfully.")
        )
    elif str(status or "").lower() == "error":
        headline, note = _save_job_failure_copy(details, message=message, phase=phase)
    else:
        headline = "Save state updated."
        note = message or "Save state updated."

    meta = []
    if str(status or "").strip().lower() == "error":
        error_meta = _save_job_error_meta(details)
        meta.insert(0, error_meta or _save_job_id_meta(run_id))
    if created_at:
        meta.append({"label": "time", "value": created_at, "raw": created_at_raw})
    if phase:
        meta.append({"label": "phase", "value": phase})

    return {
        "title": title,
        "tone": tone,
        "status_label": status_label,
        "headline": headline,
        "note": note,
        "meta": meta,
        "change_rows": change_rows,
        "phase_rows": phase_rows,
        "detail_dump": detail_dump,
    }


def _save_apply_feedback(
    response,
    *,
    success_message: str,
    failure_prefix: str,
) -> tuple[str | None, str | None]:
    if response.status == "success":
        return f"{success_message} Host updated.", None
    details = response.details if isinstance(response.details, dict) else {}
    phase = _save_job_primary_phase(details)
    backend = _save_job_backend_name(details)
    error = _save_job_root_cause(details) or str(response.message).strip()
    phase_label = _humanize_save_phase(phase)
    reason = " ".join(
        part
        for part in (
            backend,
            f"{phase_label} failed" if phase_label else "",
        )
        if part
    )
    if error:
        reason = f"{reason}: {error}" if reason else error
    error_ref = ""
    if details.get("error_code") and details.get("error_inst"):
        error_ref = f" (Error {details['error_code']}-{details['error_inst']})"
    if str(details.get("failure_mode") or "").strip().lower() == "partial":
        return None, (
            f"Save failed{error_ref}. Some host changes may already be active. "
            f"Reason: {reason}"
        )
    return (
        None,
        f"Save failed{error_ref}. Existing config is still active. Reason: {reason}",
    )
