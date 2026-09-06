from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import redact_sensitive_text
from app.models.entities import (
    ApplyRun,
    Backend,
    BackendBackup,
    BackendHardeningRun,
    BackendResourceSample,
    ClusterNode,
    ControlEvent,
    HostApplyState,
    HostResourceSample,
    Input,
    Operation,
    UpdateCheck,
    UpdateRun,
)
from app.services.update_service import current_app_version


ERROR_INST_RE = re.compile(r"^[0-9A-HJKMNPQRSTVWXYZ]{8}$")
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|cookie|csrf|access[_-]?code|access[_-]?key|"
    r"shield[_-]?code|hash|private[_-]?key)",
    re.IGNORECASE,
)
MAX_TEXT_VALUE_CHARS = 4000
MAX_LOG_SCAN_BYTES = 2_000_000
MAX_LOG_LINES = 240
MAX_SUPPORT_LOG_LINES = 500
LOG_CONTEXT_LINES = 6


@dataclass(frozen=True)
class DebugBundle:
    filename: str
    content: bytes


async def build_error_debug_bundle(
    session: AsyncSession,
    settings: Settings,
    *,
    error_inst: str,
) -> DebugBundle:
    normalized_inst = normalize_error_inst(error_inst)
    state = await _collect_state(session, normalized_inst)
    report = _build_report(settings, normalized_inst, state)
    manifest = {
        "artifact": "cnc-error-debug-bundle",
        "created_at": report["time"],
        "version": report["version"],
        "error_code": report["error_code"],
        "error_name": report["error_name"],
        "error_inst": normalized_inst,
        "request_id": report["request_id"],
        "contents": [
            "manifest.json",
            "report.txt",
            "report.json",
            "logs/app.log",
            "logs/app.events.jsonl",
            "state/operations.json",
            "state/apply-runs.json",
            "state/control-events.json",
            "state/backends.json",
            "diagnostics/host.json",
            "redaction.json",
        ],
    }
    host = {
        "app_name": settings.app_name,
        "version": report["version"],
        "log_env": settings.log_env,
        "log_app_id": settings.log_app_id,
        "database": "configured" if settings.database_url else "unknown",
        "admin_host": settings.admin_host,
        "admin_port": settings.admin_port,
    }
    redaction = {
        "redacted_key_pattern": SECRET_KEY_RE.pattern,
        "max_text_value_chars": MAX_TEXT_VALUE_CHARS,
        "log_context_lines": LOG_CONTEXT_LINES,
        "max_log_lines_per_file": MAX_LOG_LINES,
    }
    log_needles = _log_needles(normalized_inst, report, state)
    app_log_path = Path(settings.log_path)
    events_log_path = _events_log_path(app_log_path)

    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as bundle:
        _write_json(bundle, "manifest.json", manifest)
        bundle.writestr("report.txt", _report_text(report))
        _write_json(bundle, "report.json", report)
        bundle.writestr(
            "logs/app.log",
            _correlated_log_snippet(app_log_path, needles=log_needles),
        )
        bundle.writestr(
            "logs/app.events.jsonl",
            _correlated_log_snippet(events_log_path, needles=log_needles),
        )
        _write_json(bundle, "state/operations.json", state["operations"])
        _write_json(bundle, "state/apply-runs.json", state["apply_runs"])
        _write_json(bundle, "state/control-events.json", state["control_events"])
        _write_json(bundle, "state/backends.json", state["backends"])
        _write_json(bundle, "diagnostics/host.json", _redact(host))
        _write_json(bundle, "redaction.json", redaction)

    return DebugBundle(
        filename=f"cnc-debug-{normalized_inst}.zip",
        content=output.getvalue(),
    )


async def build_support_debug_bundle(
    session: AsyncSession,
    settings: Settings,
) -> DebugBundle:
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    state = await _collect_support_state(session)
    report = _build_support_report(settings, created_at, state)
    manifest = {
        "artifact": "cnc-support-bundle",
        "created_at": created_at,
        "version": report["version"],
        "contents": [
            "manifest.json",
            "report.txt",
            "report.json",
            "logs/app.log",
            "logs/app.events.jsonl",
            "state/backends.json",
            "state/inputs.json",
            "state/operations.json",
            "state/apply-runs.json",
            "state/control-events.json",
            "state/update-runs.json",
            "state/update-checks.json",
            "state/backups.json",
            "state/cluster-nodes.json",
            "state/host-apply-state.json",
            "state/backend-hardening-runs.json",
            "state/backend-resource-samples.json",
            "state/host-resource-samples.json",
            "diagnostics/host.json",
            "redaction.json",
        ],
    }
    redaction = {
        "redacted_key_pattern": SECRET_KEY_RE.pattern,
        "max_text_value_chars": MAX_TEXT_VALUE_CHARS,
        "max_log_scan_bytes": MAX_LOG_SCAN_BYTES,
        "max_log_lines_per_file": MAX_SUPPORT_LOG_LINES,
    }
    app_log_path = Path(settings.log_path)
    events_log_path = _events_log_path(app_log_path)

    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED) as bundle:
        _write_json(bundle, "manifest.json", manifest)
        bundle.writestr("report.txt", _support_report_text(report))
        _write_json(bundle, "report.json", report)
        bundle.writestr("logs/app.log", _redacted_log_tail(app_log_path))
        bundle.writestr("logs/app.events.jsonl", _redacted_log_tail(events_log_path))
        _write_json(bundle, "state/backends.json", state["backends"])
        _write_json(bundle, "state/inputs.json", state["inputs"])
        _write_json(bundle, "state/operations.json", state["operations"])
        _write_json(bundle, "state/apply-runs.json", state["apply_runs"])
        _write_json(bundle, "state/control-events.json", state["control_events"])
        _write_json(bundle, "state/update-runs.json", state["update_runs"])
        _write_json(bundle, "state/update-checks.json", state["update_checks"])
        _write_json(bundle, "state/backups.json", state["backups"])
        _write_json(bundle, "state/cluster-nodes.json", state["cluster_nodes"])
        _write_json(bundle, "state/host-apply-state.json", state["host_apply_state"])
        _write_json(
            bundle,
            "state/backend-hardening-runs.json",
            state["backend_hardening_runs"],
        )
        _write_json(
            bundle,
            "state/backend-resource-samples.json",
            state["backend_resource_samples"],
        )
        _write_json(
            bundle,
            "state/host-resource-samples.json",
            state["host_resource_samples"],
        )
        _write_json(bundle, "diagnostics/host.json", _support_host_payload(settings))
        _write_json(bundle, "redaction.json", redaction)

    stamp = created_at.replace("-", "").replace(":", "").replace("Z", "Z")
    return DebugBundle(
        filename=f"cnc-support-{stamp}.zip",
        content=output.getvalue(),
    )


def normalize_error_inst(error_inst: str) -> str:
    normalized = str(error_inst or "").strip().upper()
    if not ERROR_INST_RE.fullmatch(normalized):
        raise ValueError("error_inst must be an 8 character Crockford instance code")
    return normalized


async def _collect_state(
    session: AsyncSession,
    error_inst: str,
) -> dict[str, list[dict[str, Any]]]:
    like_pattern = f"%{error_inst}%"
    operations = (
        (
            await session.execute(
                select(Operation)
                .where(
                    or_(
                        Operation.details_json.like(like_pattern),
                        Operation.error.like(like_pattern),
                    )
                )
                .order_by(Operation.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    apply_runs = (
        (
            await session.execute(
                select(ApplyRun)
                .where(
                    or_(
                        ApplyRun.details_json.like(like_pattern),
                        ApplyRun.message.like(like_pattern),
                    )
                )
                .order_by(ApplyRun.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    control_events = (
        (
            await session.execute(
                select(ControlEvent)
                .where(
                    or_(
                        ControlEvent.details_json.like(like_pattern),
                        ControlEvent.subevents_json.like(like_pattern),
                        ControlEvent.summary.like(like_pattern),
                    )
                )
                .order_by(ControlEvent.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    backend_ids = {
        operation.backend_id
        for operation in operations
        if operation.backend_id is not None
    }
    backends: list[Backend] = []
    if backend_ids:
        backends = (
            (
                await session.execute(
                    select(Backend)
                    .where(Backend.id.in_(backend_ids))
                    .order_by(Backend.id.asc())
                )
            )
            .scalars()
            .all()
        )
    return {
        "operations": [_operation_payload(row) for row in operations],
        "apply_runs": [_apply_run_payload(row) for row in apply_runs],
        "control_events": [_control_event_payload(row) for row in control_events],
        "backends": [_backend_payload(row) for row in backends],
    }


def _operation_payload(operation: Operation) -> dict[str, Any]:
    return _redact(
        {
            "id": operation.id,
            "kind": operation.kind,
            "status": operation.status,
            "phase": operation.phase,
            "actor": operation.actor,
            "backend_id": operation.backend_id,
            "config_revision": operation.config_revision,
            "desired_state_hash": operation.desired_state_hash,
            "started_at": _iso(operation.started_at),
            "finished_at": _iso(operation.finished_at),
            "error": operation.error,
            "details": _json_object(operation.details_json),
        }
    )


def _apply_run_payload(run: ApplyRun) -> dict[str, Any]:
    return _redact(
        {
            "id": run.id,
            "operation_id": run.operation_id,
            "status": run.status,
            "message": run.message,
            "config_revision": run.config_revision,
            "desired_state_hash": run.desired_state_hash,
            "details": _json_object(run.details_json),
            "created_at": _iso(run.created_at),
        }
    )


def _control_event_payload(event: ControlEvent) -> dict[str, Any]:
    return _redact(
        {
            "id": event.id,
            "kind": event.kind,
            "scope": event.scope,
            "severity": event.severity,
            "source": event.source,
            "backend_name": event.backend_name,
            "affects_all": event.affects_all,
            "summary": event.summary,
            "details": _json_object(event.details_json),
            "subevents": _json_array(event.subevents_json),
            "created_at": _iso(event.created_at),
        }
    )


def _backend_payload(backend: Backend) -> dict[str, Any]:
    return _redact(
        {
            "id": backend.id,
            "name": backend.name,
            "kind": backend.kind,
            "port": backend.port,
            "enabled": backend.enabled,
            "resource_size": backend.resource_size,
            "healthcheck_path": backend.healthcheck_path,
            "healthcheck_mode": backend.healthcheck_mode,
            "sandbox_profile": backend.sandbox_profile,
            "hardening_config_json": backend.hardening_config_json or "{}",
            "created_at": _iso(backend.created_at),
        }
    )


async def _collect_support_state(session: AsyncSession) -> dict[str, list[Any]]:
    backends = (
        (await session.execute(select(Backend).order_by(Backend.id.asc())))
        .scalars()
        .all()
    )
    inputs = (
        (await session.execute(select(Input).order_by(Input.id.asc()))).scalars().all()
    )
    operations = (
        (
            await session.execute(
                select(Operation).order_by(Operation.id.desc()).limit(50)
            )
        )
        .scalars()
        .all()
    )
    apply_runs = (
        (await session.execute(select(ApplyRun).order_by(ApplyRun.id.desc()).limit(25)))
        .scalars()
        .all()
    )
    control_events = (
        (
            await session.execute(
                select(ControlEvent).order_by(ControlEvent.id.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    update_runs = (
        (
            await session.execute(
                select(UpdateRun).order_by(UpdateRun.id.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )
    update_checks = (
        (
            await session.execute(
                select(UpdateCheck).order_by(UpdateCheck.id.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )
    backups = (
        (
            await session.execute(
                select(BackendBackup).order_by(BackendBackup.id.desc()).limit(50)
            )
        )
        .scalars()
        .all()
    )
    cluster_nodes = (
        (
            await session.execute(
                select(ClusterNode).order_by(ClusterNode.id.asc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    host_apply_state = (
        (
            await session.execute(
                select(HostApplyState).order_by(HostApplyState.id.asc())
            )
        )
        .scalars()
        .all()
    )
    backend_hardening_runs = (
        (
            await session.execute(
                select(BackendHardeningRun)
                .order_by(BackendHardeningRun.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    backend_resource_samples = (
        (
            await session.execute(
                select(BackendResourceSample)
                .order_by(BackendResourceSample.bucket_start.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    host_resource_samples = (
        (
            await session.execute(
                select(HostResourceSample)
                .order_by(HostResourceSample.bucket_start.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return {
        "backends": [_backend_support_payload(row) for row in backends],
        "inputs": [_input_payload(row) for row in inputs],
        "operations": [_operation_payload(row) for row in operations],
        "apply_runs": [_apply_run_payload(row) for row in apply_runs],
        "control_events": [_control_event_payload(row) for row in control_events],
        "update_runs": [_update_run_payload(row) for row in update_runs],
        "update_checks": [_update_check_payload(row) for row in update_checks],
        "backups": [_backup_payload(row) for row in backups],
        "cluster_nodes": [_cluster_node_payload(row) for row in cluster_nodes],
        "host_apply_state": [
            _host_apply_state_payload(row) for row in host_apply_state
        ],
        "backend_hardening_runs": [
            _backend_hardening_run_payload(row) for row in backend_hardening_runs
        ],
        "backend_resource_samples": [
            _resource_sample_payload(row) for row in backend_resource_samples
        ],
        "host_resource_samples": [
            _resource_sample_payload(row) for row in host_resource_samples
        ],
    }


def _backend_support_payload(backend: Backend) -> dict[str, Any]:
    return _redact(
        {
            **_backend_payload(backend),
            "handoff_port": backend.handoff_port,
            "healthcheck_host_header": backend.healthcheck_host_header,
            "resource_mode": backend.resource_mode,
            "memory_high_override": backend.memory_high_override,
            "memory_max_override": backend.memory_max_override,
            "cpu_quota_override": backend.cpu_quota_override,
            "shield_enabled": backend.shield_enabled,
            "placement_node_uid": backend.placement_node_uid,
            "placement_mode": backend.placement_mode,
            "placement_active_node_uid": backend.placement_active_node_uid,
            "placement_node_uids": _json_array(backend.placement_node_uids_json),
            "inter_app_interfaces": _json_array(backend.inter_app_interfaces_json),
            "volumes": _json_array(backend.volumes_json),
            "updated_at": _iso(backend.updated_at),
        }
    )


def _input_payload(input_row: Input) -> dict[str, Any]:
    return _redact(
        {
            "id": input_row.id,
            "kind": input_row.kind,
            "hostname": input_row.hostname,
            "shield_enabled": input_row.shield_enabled,
            "enabled": input_row.enabled,
            "created_at": _iso(input_row.created_at),
            "updated_at": _iso(input_row.updated_at),
        }
    )


def _update_run_payload(run: UpdateRun) -> dict[str, Any]:
    return _redact(
        {
            "id": run.id,
            "status": run.status,
            "message": run.message,
            "details": _json_object(run.details_json),
            "created_at": _iso(run.created_at),
        }
    )


def _update_check_payload(check: UpdateCheck) -> dict[str, Any]:
    return _redact(
        {
            "id": check.id,
            "status": check.status,
            "current_version": check.current_version,
            "available_version": check.available_version,
            "has_update": check.has_update,
            "repo": check.repo,
            "ref": check.ref,
            "error": check.error,
            "details": _json_object(check.details_json),
            "checked_at": _iso(check.checked_at),
        }
    )


def _backup_payload(backup: BackendBackup) -> dict[str, Any]:
    return _redact(
        {
            "id": backup.id,
            "backend_id": backup.backend_id,
            "operation_id": backup.operation_id,
            "status": backup.status,
            "scope": backup.scope,
            "bundle_path": backup.bundle_path,
            "bundle_sha256": backup.bundle_sha256,
            "size_bytes": backup.size_bytes,
            "notes": backup.notes,
            "error": backup.error,
            "created_at": _iso(backup.created_at),
        }
    )


def _cluster_node_payload(node: ClusterNode) -> dict[str, Any]:
    return _redact(
        {
            "id": node.id,
            "node_uid": node.node_uid,
            "name": node.name,
            "role": node.role,
            "state": node.state,
            "wireguard_ip": node.wireguard_ip,
            "wireguard_public_key": node.wireguard_public_key,
            "public_endpoint": node.public_endpoint,
            "tailnet_ip": node.tailnet_ip,
            "ram_bytes": node.ram_bytes,
            "cpu_count": node.cpu_count,
            "disk_bytes": node.disk_bytes,
            "latency_ms": node.latency_ms,
            "version": node.version,
            "details": _json_object(node.details_json),
            "joined_at": _iso(node.joined_at),
            "last_seen_at": _iso(node.last_seen_at),
            "removed_at": _iso(node.removed_at),
        }
    )


def _host_apply_state_payload(state: HostApplyState) -> dict[str, Any]:
    return {
        "id": state.id,
        "last_applied_state_hash": state.last_applied_state_hash,
        "last_successful_operation_id": state.last_successful_operation_id,
        "last_successful_apply_run_id": state.last_successful_apply_run_id,
        "last_applied_at": _iso(state.last_applied_at),
    }


def _backend_hardening_run_payload(run: BackendHardeningRun) -> dict[str, Any]:
    return _redact(
        {
            "id": run.id,
            "backend_id": run.backend_id,
            "phase": run.phase,
            "status": run.status,
            "evidence_dir": run.evidence_dir,
            "ratings": _json_object(run.ratings_json),
            "details": _json_object(run.details_json),
            "error": run.error,
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
        }
    )


def _resource_sample_payload(sample: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for column in sample.__table__.columns:
        key = str(column.name)
        value = getattr(sample, key)
        payload[key] = _iso(value) if hasattr(value, "isoformat") else value
    return _redact(payload)


def _build_report(
    settings: Settings,
    error_inst: str,
    state: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    operation = _first_row(state, "operations")
    apply_run = _first_row(state, "apply_runs")
    backend = _first_row(state, "backends")
    details = _first_details(state)
    error_code = _first_string(details, "error_code", "code") or "-"
    error_name = _first_string(details, "error_name", "name") or "-"
    request_id = _first_string(details, "request_id", "req") or "-"
    operation_kind = str(operation.get("kind") or "").strip()
    phase = str(operation.get("phase") or "").strip()
    location = "/".join(part for part in (operation_kind, phase) if part) or "-"
    output = "-"
    if backend:
        output = f"{backend.get('name') or 'output'} (#{backend.get('id')})"
    summary = (
        _first_string(details, "flash_error", "message", "summary")
        or str(operation.get("error") or apply_run.get("message") or "").strip()
        or "No matching CNC state row was found for this instance code."
    )
    return {
        "error_code": error_code,
        "error_name": error_name,
        "error_inst": error_inst,
        "request_id": request_id,
        "summary": _clip(summary),
        "location": location,
        "version": current_app_version(),
        "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "output": output,
        "op": operation.get("id") or "-",
        "apply": apply_run.get("id") or "-",
        "diagnostics": f"cnc-admin support error {error_inst}",
        "log_env": settings.log_env,
    }


def _report_text(report: dict[str, Any]) -> str:
    keys = (
        "error_code",
        "error_name",
        "error_inst",
        "request_id",
        "summary",
        "location",
        "version",
        "time",
        "output",
        "op",
        "apply",
        "diagnostics",
    )
    return "\n".join(f"{key}: {report.get(key, '-')}" for key in keys) + "\n"


def _build_support_report(
    settings: Settings,
    created_at: str,
    state: dict[str, list[Any]],
) -> dict[str, Any]:
    failed_operations = [
        item for item in state["operations"] if item.get("status") == "failed"
    ]
    failed_apply_runs = [
        item for item in state["apply_runs"] if item.get("status") == "error"
    ]
    return {
        "artifact": "cnc-support-bundle",
        "version": current_app_version(),
        "time": created_at,
        "app_name": settings.app_name,
        "log_env": settings.log_env,
        "log_app_id": settings.log_app_id,
        "admin_host": settings.admin_host,
        "admin_port": settings.admin_port,
        "database": "configured" if settings.database_url else "unknown",
        "counts": {
            "backends": len(state["backends"]),
            "inputs": len(state["inputs"]),
            "recent_operations": len(state["operations"]),
            "recent_failed_operations": len(failed_operations),
            "recent_apply_runs": len(state["apply_runs"]),
            "recent_failed_apply_runs": len(failed_apply_runs),
            "recent_control_events": len(state["control_events"]),
            "cluster_nodes": len(state["cluster_nodes"]),
            "backups": len(state["backups"]),
        },
        "diagnostics": "cnc-admin support bundle",
    }


def _support_report_text(report: dict[str, Any]) -> str:
    lines = [
        f"artifact: {report.get('artifact', '-')}",
        f"version: {report.get('version', '-')}",
        f"time: {report.get('time', '-')}",
        f"app_name: {report.get('app_name', '-')}",
        f"log_env: {report.get('log_env', '-')}",
        f"log_app_id: {report.get('log_app_id', '-')}",
        f"admin: {report.get('admin_host', '-')}:{report.get('admin_port', '-')}",
        f"database: {report.get('database', '-')}",
        f"diagnostics: {report.get('diagnostics', '-')}",
        "",
        "counts:",
    ]
    counts = report.get("counts")
    if isinstance(counts, dict):
        lines.extend(f"  {key}: {value}" for key, value in counts.items())
    return "\n".join(lines) + "\n"


def _support_host_payload(settings: Settings) -> dict[str, Any]:
    return _redact(
        {
            "app_name": settings.app_name,
            "version": current_app_version(),
            "log_env": settings.log_env,
            "log_app_id": settings.log_app_id,
            "database": "configured" if settings.database_url else "unknown",
            "admin_host": settings.admin_host,
            "admin_port": settings.admin_port,
            "trusted_hosts": len(settings.trusted_host_list),
            "status_cache_ttl_sec": settings.status_cache_ttl_sec,
            "paths": {
                "log_path": str(settings.log_path),
                "managed_env": str(settings.managed_env_file_path),
                "nginx_generated": str(settings.nginx_generated_dir),
                "apply_backup_dir": str(settings.apply_backup_dir),
                "backend_backup_dir": str(settings.backend_backup_dir),
                "app_control_dir": str(settings.app_control_dir),
                "host_mutation_lock": str(settings.apply_lock_path),
                "updater_script": str(settings.updater_script_path),
            },
            "networking": {
                "cloudflare_only": settings.nginx_cloudflare_only,
                "cloudflare_ip_count": len(settings.cloudflare_ip_list),
                "port_range": [
                    settings.port_range_start,
                    settings.port_range_end,
                ],
            },
            "backup": {
                "format": settings.backend_backup_archive_format,
                "gzip_level": settings.backend_backup_gzip_compresslevel,
                "retention_per_backend": settings.backend_backup_retention_per_backend,
            },
            "auto_sizing": {
                "enabled": settings.auto_resource_limits,
                "memory_reserve_percent": settings.auto_memory_reserve_percent,
                "cpu_reserve_percent": settings.auto_cpu_reserve_percent,
                "min_memory_high_mb": settings.auto_min_memory_high_mb,
                "min_cpu_quota_percent": settings.auto_min_cpu_quota_percent,
                "cpu_burst_factor": settings.auto_cpu_burst_factor,
                "nightly_hour_utc": settings.auto_size_nightly_hour_utc,
            },
        }
    )


def _first_row(
    state: dict[str, list[dict[str, Any]]],
    key: str,
) -> dict[str, Any]:
    rows = state.get(key) or []
    return rows[0] if rows else {}


def _first_details(state: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    for collection in ("operations", "apply_runs", "control_events"):
        for row in state.get(collection) or []:
            details = row.get("details")
            if isinstance(details, dict):
                return details
    return {}


def _first_string(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _log_needles(
    error_inst: str,
    report: dict[str, Any],
    state: dict[str, list[dict[str, Any]]],
) -> list[str]:
    needles = {error_inst}
    for key in ("error_code", "error_name", "request_id"):
        value = str(report.get(key) or "").strip()
        if value and value != "-":
            needles.add(value)
    for row in state.get("operations") or []:
        value = row.get("id")
        if value:
            needles.add(f"operation_id: {value}")
            needles.add(f'"operation_id":{value}')
    for row in state.get("apply_runs") or []:
        value = row.get("id")
        if value:
            needles.add(f"apply_run_id: {value}")
            needles.add(f'"apply_run_id":{value}')
    return sorted(needles)


def _correlated_log_snippet(path: Path, *, needles: list[str]) -> str:
    if not path.exists() or not path.is_file():
        return f"# {path} not found\n"
    text = _read_tail(path)
    if not text:
        return f"# {path} is empty\n"
    lines = text.splitlines()
    matched_indexes = [
        index
        for index, line in enumerate(lines)
        if any(needle and needle in line for needle in needles)
    ]
    if not matched_indexes:
        return f"# no matching lines found in {path}\n"
    keep: set[int] = set()
    for index in matched_indexes:
        start = max(0, index - LOG_CONTEXT_LINES)
        end = min(len(lines), index + LOG_CONTEXT_LINES + 1)
        keep.update(range(start, end))
    ordered = sorted(keep)
    if len(ordered) > MAX_LOG_LINES:
        ordered = ordered[-MAX_LOG_LINES:]
    rendered: list[str] = []
    previous = None
    for index in ordered:
        if previous is not None and index > previous + 1:
            rendered.append("# ...")
        rendered.append(_redact_log_line(lines[index]))
        previous = index
    return "\n".join(rendered) + "\n"


def _redacted_log_tail(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return f"# {path} not found\n"
    text = _read_tail(path)
    if not text:
        return f"# {path} is empty\n"
    lines = text.splitlines()[-MAX_SUPPORT_LOG_LINES:]
    return "\n".join(_redact_log_line(line) for line in lines) + "\n"


def _read_tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > MAX_LOG_SCAN_BYTES:
                handle.seek(size - MAX_LOG_SCAN_BYTES)
                handle.readline()
            return handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"# could not read {path}: {exc}\n"


def _redact_log_line(line: str) -> str:
    pattern = re.compile(
        r"(?i)\b(token|secret|password|cookie|csrf|access[_-]?key|access[_-]?code)"
        r"([\"']?\s*[:=]\s*)([\"']?)([^,\"'\s}]+)([\"']?)"
    )

    def replace(match: re.Match[str]) -> str:
        closing_quote = match.group(5) if match.group(3) else ""
        return (
            f"{match.group(1)}{match.group(2)}{match.group(3)}[redacted]{closing_quote}"
        )

    # Consume the complete header value before generic key/value redaction.
    line = re.sub(
        r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer|basic)\s+[^\s,\"'}]+",
        r"\1[redacted]",
        line,
    )
    return pattern.sub(replace, redact_sensitive_text(line))


def _write_json(bundle: ZipFile, name: str, payload: Any) -> None:
    bundle.writestr(
        name,
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
    )


def _events_log_path(log_path: Path) -> Path:
    if log_path.suffix == ".log":
        return log_path.with_name(f"{log_path.stem}.events.jsonl")
    return log_path.with_name(f"{log_path.name}.events.jsonl")


def _json_object(raw_value: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw_value or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_array(raw_value: str | None) -> list[Any]:
    try:
        value = json.loads(raw_value or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _redact(value: Any, *, key: str = "") -> Any:
    if key and SECRET_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _clip(_redact_log_line(value))
    return value


def _clip(value: str) -> str:
    if len(value) <= MAX_TEXT_VALUE_CHARS:
        return value
    return value[:MAX_TEXT_VALUE_CHARS] + "... [clipped]"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
