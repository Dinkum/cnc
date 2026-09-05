from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from inspect import signature
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.logger import get_logger
from app.models.entities import (
    ApplyRun,
    Backend,
    HostApplyState,
    Input,
    Operation,
    UpdateRun,
)
from app.services.app_diagnostics import (
    AppDiagnosticsSnapshot,
    collect_app_backend_diagnostics,
    collect_app_diagnostics_snapshot,
)
from app.services.app_network_isolation import collect_app_network_isolation_canary
from app.services.commands import CommandError, run_command_checked_async
from app.services.container_runtime import read_container_stats
from app.services.control_events import emit_control_event
from app.services.netdata_constants import NETDATA_SYSTEMD_SERVICE
from app.services.resource_profile import (
    ResourceProfile,
    backend_resource_profile,
    build_resource_profile,
)
from app.services.renderers import container_name
from app.services.shield_runtime import SHIELD_CONTAINER_NAME, SHIELD_CONTAINER_SERVICE
from app.services.tailscale_urls import infer_tailnet_dns_name, tailscale_service_url
from app.services.time_utils import utc_isoformat
from app.services.update_service import (
    current_app_version,
    get_cached_update_version_info,
    reconcile_update_run,
)


STATUS_KEYS = [
    "ActiveState",
    "SubState",
    "ActiveEnterTimestamp",
    "MemoryCurrent",
    "MemoryPeak",
    "CPUUsageNSec",
]

logger = get_logger("status_service")
_status_cache_lock: asyncio.Lock | None = None
_status_cache_lock_loop: asyncio.AbstractEventLoop | None = None
_status_cache_payload: dict[str, Any] | None = None
_status_cache_expires_at: float = 0.0
_status_cache_generation: int = 0
_status_cache_prefill_task: asyncio.Task[None] | None = None
_status_cache_prefill_loop: asyncio.AbstractEventLoop | None = None
_status_cache_prefill_pending: tuple[Settings, bool, float] | None = None
_status_cache_prefill_reschedule_attached: bool = False
_cpu_usage_samples: dict[str, tuple[float, int]] = {}
_network_usage_samples: dict[str, tuple[float, int, int]] = {}
_host_cpu_sample: tuple[float, int, int] | None = None
_host_network_sample: tuple[float, int, int] | None = None
_network_isolation_cache_payload: dict[str, Any] | None = None
_network_isolation_cache_signature: tuple[tuple[str, int], ...] | None = None
_network_isolation_cache_expires_at: float = 0.0
_network_isolation_event_signature: str | None = None
NETWORK_ISOLATION_STATUS_CACHE_TTL_SEC = 300
_SINGLE_OUTPUT_INPUT_KINDS = {"shield", "tailnet_path", "tailnet_service"}


def dashboard_overview_counts(
    backends: list[Backend], inputs: list[Input]
) -> dict[str, int]:
    routes_total = 0
    routes_active = 0
    for item in inputs:
        attached_backends = list(item.backends)
        if not attached_backends:
            routes_total += 1
            continue
        enabled_backends = [backend for backend in attached_backends if backend.enabled]
        enabled_kind_count = len({backend.kind for backend in enabled_backends})
        enabled_static_count = sum(
            1 for backend in enabled_backends if backend.kind == "static"
        )
        routes_total += len(attached_backends)
        for backend in attached_backends:
            conflict = (
                enabled_kind_count > 1
                or (
                    str(item.kind or "domain") in _SINGLE_OUTPUT_INPUT_KINDS
                    and len(enabled_backends) > 1
                )
                or (backend.kind == "static" and enabled_static_count > 1)
            )
            if item.enabled and backend.enabled and not conflict:
                routes_active += 1
    return {
        "inputs_total": len(inputs),
        "inputs_enabled": sum(1 for item in inputs if item.enabled),
        "backends_total": len(backends),
        "backends_enabled": sum(1 for backend in backends if backend.enabled),
        "app_backends_enabled": sum(
            1 for backend in backends if backend.kind == "app" and backend.enabled
        ),
        "routes_total": routes_total,
        "routes_active": routes_active,
    }


def _get_status_cache_lock() -> asyncio.Lock:
    global _status_cache_lock, _status_cache_lock_loop
    loop = asyncio.get_running_loop()
    if _status_cache_lock is None or _status_cache_lock_loop is not loop:
        _status_cache_lock = asyncio.Lock()
        _status_cache_lock_loop = loop
    return _status_cache_lock


def _parse_systemctl_show(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _tailscale_service_status(
    inputs: list[Input], settings: Settings
) -> dict[str, Any]:
    tailnet_dns_name = infer_tailnet_dns_name(settings)
    configured: list[dict[str, object]] = []
    for item in sorted(inputs, key=lambda entry: entry.id or 0):
        if str(item.kind or "domain").strip().lower() != "tailnet_service":
            continue
        service = str(item.hostname or "").strip().lower().rstrip(".")
        configured.append(
            {
                "input_id": item.id,
                "service": service,
                "enabled": bool(item.enabled),
                "url": tailscale_service_url(service, settings),
                "backend_names": [
                    backend.name
                    for backend in sorted(
                        item.backends, key=lambda backend: backend.name
                    )
                ],
            }
        )
    return {
        "configured": configured,
        "count": len(configured),
        "tailnet_dns_name": tailnet_dns_name,
        "prerequisites": {
            "tailnet_dns_name": "ok"
            if tailnet_dns_name
            else ("missing" if configured else "not_needed"),
            "service_policy": "external",
            "service_approval": "external",
        },
    }


def app_action_hints(
    backend: Backend, diagnostics: dict[str, Any]
) -> list[dict[str, str]]:
    issues = diagnostics.get("issues")
    issues = issues if isinstance(issues, list) else []
    observation_issues = diagnostics.get("observation_issues")
    observation_issues = (
        observation_issues if isinstance(observation_issues, list) else []
    )
    if not issues and not observation_issues:
        return []

    backend_name = backend.name
    hints: list[dict[str, str]] = []

    def add_hint(label: str, command: str, note: str) -> None:
        entry = {"label": label, "command": command, "note": note}
        if entry not in hints:
            hints.append(entry)

    issue_set = {str(issue) for issue in issues}
    if issue_set & {
        "bridge_dns_enabled",
        "container_missing",
        "legacy_loopback_proxy_present",
        "loopback_publish_missing",
        "missing_saved_spec",
        "network_missing",
        "network_orphan",
        "private_ip_missing",
        "storage_orphan",
        "mounted_storage_orphan",
        "stale_name_registration",
    }:
        add_hint(
            "fix",
            f"cnc-admin fix {backend_name}",
            "repair CNC-owned backend drift and restore the managed sandbox/publication path",
        )

    if issue_set & {"podman_exec_unavailable"}:
        add_hint(
            "fix",
            f"cnc-admin fix {backend_name}",
            "restart the managed backend container from the host side when the backend exec path is unavailable",
        )

    if issue_set & {"bootstrap_failed", "bootstrap_stuck", "private_unreachable"}:
        add_hint(
            "logs",
            f"cnc-admin app logs {backend_name}",
            "inspect the latest sandbox bootstrap and app-runtime failure context",
        )

    if issue_set & {"bootstrap_failed", "bootstrap_stuck"}:
        add_hint(
            "fix",
            f"cnc-admin fix {backend_name}",
            "clear failed sandbox bootstrap state and rerun backend reconciliation",
        )

    if issue_set & {"loopback_unreachable"}:
        add_hint(
            "fix",
            f"cnc-admin fix {backend_name}",
            "repair the sandbox path and restore loopback publication",
        )

    add_hint(
        "doctor",
        f"cnc-admin app doctor {backend_name}",
        "re-check runtime state after any fix or apply attempt",
    )
    return hints


async def _show_service(service: str, settings: Settings) -> dict[str, Any]:
    try:
        result = await run_command_checked_async(
            ["systemctl", "show", service, "--property", ",".join(STATUS_KEYS)],
            timeout_sec=settings.command_timeout_status_sec,
        )
        parsed = _parse_systemctl_show(result.stdout)
        return {"service": service, "ok": True, "data": parsed}
    except CommandError as exc:
        return {
            "service": service,
            "ok": False,
            "error": exc.result.stderr or exc.result.stdout,
        }


async def _show_shield_container(settings: Settings) -> dict[str, Any]:
    status = await _show_service(SHIELD_CONTAINER_SERVICE, settings)
    status["backend"] = "shield"
    status["metric_service"] = SHIELD_CONTAINER_NAME
    metrics: dict[str, float | int | None] = {
        "cpu_percent": None,
        "memory_percent": None,
        "cpu_percent_of_host": None,
        "cpu_entitlement_percent_of_host": None,
        "cpu_limit_percent_of_host": None,
        "memory_current_bytes": None,
        "memory_max_bytes": None,
        "network_rx_bytes": None,
        "network_tx_bytes": None,
        "network_rx_bps": None,
        "network_tx_bps": None,
        "network_total_bps": None,
    }
    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    service_active = str(data.get("ActiveState") or "").strip().lower() == "active"
    if status.get("ok") and service_active:
        try:
            await run_command_checked_async(
                [
                    "curl",
                    "-fsS",
                    "--max-time",
                    "2",
                    f"http://127.0.0.1:{settings.shield_port}/health",
                ],
                timeout_sec=min(settings.command_timeout_status_sec, 3),
            )
            data["HealthStatus"] = "healthy"
        except CommandError as exc:
            status["ok"] = False
            status["error"] = exc.result.stderr or exc.result.stdout
            data["HealthStatus"] = "unhealthy"
    else:
        status["ok"] = False
        data["HealthStatus"] = "unhealthy"
    if service_active:
        stats_result = await read_container_stats(
            SHIELD_CONTAINER_NAME,
            timeout_sec=settings.command_timeout_status_sec,
        )
        if stats_result.ok:
            metrics = _parse_podman_stats_metrics(
                stats_result.stdout, fallback_memory_max_bytes=None
            )
            metrics["cpu_percent"] = metrics.get("cpu_percent_of_host")
            metrics["cpu_limit_percent_of_host"] = None
    status["metrics"] = metrics
    return status


async def _show_netdata_service(settings: Settings) -> dict[str, Any]:
    status = await _show_service(NETDATA_SYSTEMD_SERVICE, settings)
    status["backend"] = "netdata"
    status["metric_service"] = NETDATA_SYSTEMD_SERVICE
    status["metrics"] = {
        "cpu_percent": None,
        "memory_percent": None,
        "cpu_percent_of_host": None,
        "cpu_entitlement_percent_of_host": None,
        "cpu_limit_percent_of_host": None,
        "memory_current_bytes": None,
        "memory_max_bytes": None,
        "network_rx_bytes": None,
        "network_tx_bytes": None,
        "network_rx_bps": None,
        "network_tx_bps": None,
        "network_total_bps": None,
    }
    return status


async def _show_app_container(
    backend: Backend,
    base_profile: ResourceProfile,
    settings: Settings,
    *,
    diagnostics_snapshot: AppDiagnosticsSnapshot | None = None,
) -> dict[str, Any]:
    diagnostics = await asyncio.to_thread(
        _collect_app_backend_diagnostics, backend, settings, diagnostics_snapshot
    )
    bootstrap = diagnostics.get("bootstrap") or {}
    data = dict(diagnostics.get("container_state") or {})
    data["BootstrapState"] = bootstrap.get("status") or "missing"
    data["BootstrapMarker"] = (
        "present" if bootstrap.get("marker_present") else "missing"
    )
    data["BootstrapLock"] = "held" if bootstrap.get("lock_active") else "idle"
    data["BootstrapStale"] = "yes" if bootstrap.get("stale") else "no"
    data["BootstrapStartedAt"] = (
        (bootstrap.get("state") or {}).get("started_at")
        if isinstance(bootstrap.get("state"), dict)
        else ""
    )
    data["BootstrapFinishedAt"] = (
        (bootstrap.get("state") or {}).get("finished_at")
        if isinstance(bootstrap.get("state"), dict)
        else ""
    )
    data["BootstrapFailurePhase"] = (
        (bootstrap.get("state") or {}).get("failure_phase")
        if isinstance(bootstrap.get("state"), dict)
        else ""
    )
    data["ReconcilePhase"] = (
        (bootstrap.get("state") or {}).get("reconcile_phase")
        if isinstance(bootstrap.get("state"), dict)
        else ""
    )
    data["ReconcilePhaseStatus"] = (
        (bootstrap.get("state") or {}).get("reconcile_phase_status")
        if isinstance(bootstrap.get("state"), dict)
        else ""
    )
    data["PrivateAddress"] = diagnostics.get("private_ip") or ""
    private_health_status = str(diagnostics.get("private_health_status") or "")
    data["PrivateReachable"] = (
        "unmonitored"
        if private_health_status == "unmonitored"
        else (
            "yes"
            if diagnostics.get("private_reachable") is True
            else ("no" if diagnostics.get("private_reachable") is False else "unknown")
        )
    )
    data["RuntimeDiagnosis"] = str(diagnostics.get("diagnosis") or "")
    data["RuntimeOwner"] = str(
        diagnostics.get("runtime_owner_label") or diagnostics.get("runtime_owner") or ""
    )
    data["SandboxStatus"] = str(diagnostics.get("sandbox_status") or "")
    data["GuestStatus"] = str(diagnostics.get("guest_status") or "")
    data["AppHandoffStatus"] = str(diagnostics.get("app_handoff_status") or "")
    data["BackendExecAvailable"] = (
        "yes" if diagnostics.get("backend_exec_available") else "no"
    )
    data["BackendExecStatus"] = str(diagnostics.get("backend_exec_status") or "")
    data["BackendExecError"] = str(diagnostics.get("backend_exec_error") or "")
    data["BackendExecCircuit"] = (
        "open" if diagnostics.get("backend_exec_circuit") else "closed"
    )
    data["ObservationIssues"] = (
        ", ".join(diagnostics.get("observation_issues") or []) or "-"
    )
    loopback_publish_present = diagnostics.get("loopback_publish_present")
    if isinstance(loopback_publish_present, bool):
        proxy_reachable = (
            bool(diagnostics.get("loopback_reachable")) and loopback_publish_present
        )
    else:
        proxy_reachable = bool(diagnostics.get("loopback_reachable"))
    loopback_health_status = str(diagnostics.get("loopback_health_status") or "")
    data["ProxyReachable"] = (
        "unmonitored"
        if loopback_health_status == "unmonitored"
        else (
            "yes"
            if proxy_reachable
            else ("no" if backend.port is not None else "unknown")
        )
    )
    data["DnsServers"] = ", ".join(diagnostics.get("dns_servers") or []) or "-"
    data["RuntimeIssues"] = ", ".join(diagnostics.get("issues") or []) or "-"
    action_hints = app_action_hints(backend, diagnostics)
    resource_profile = backend_resource_profile(backend, base_profile)
    memory_max_bytes = _parse_bytes(resource_profile.memory_max)
    container_id = (
        diagnostics.get("container_id")
        if isinstance(diagnostics.get("container_id"), str)
        else None
    )
    metrics: dict[str, float | int | None] = {
        "cpu_percent": None,
        "memory_percent": None,
        "cpu_percent_of_host": None,
        "cpu_entitlement_percent_of_host": _round_pct(
            resource_profile.cpu_entitlement_percent_of_host
        ),
        "cpu_limit_percent_of_host": _cpu_limit_percent_of_host(
            resource_profile.cpu_quota,
            resource_profile.host_cpu_count,
        ),
        "memory_current_bytes": None,
        "memory_max_bytes": memory_max_bytes,
        "network_rx_bytes": None,
        "network_tx_bytes": None,
        "network_rx_bps": None,
        "network_tx_bps": None,
        "network_total_bps": None,
    }
    cgroup = diagnostics.get("cgroup")
    cgroup_memory = cgroup.get("memory") if isinstance(cgroup, dict) else None
    if isinstance(cgroup_memory, dict):
        current = cgroup_memory.get("current")
        maximum = cgroup_memory.get("max")
        if isinstance(current, int):
            metrics["memory_current_bytes"] = current
        if isinstance(maximum, int):
            metrics["memory_max_bytes"] = maximum
        events = cgroup_memory.get("events")
        if isinstance(events, dict):
            data["CgroupMemoryEvents"] = ", ".join(
                f"{key}={value}" for key, value in sorted(events.items())
            )
    metric_service = container_name(backend.name)
    if diagnostics.get("container_exists"):
        stats_result = await read_container_stats(
            container_name(backend.name),
            timeout_sec=settings.command_timeout_status_sec,
        )
        if stats_result.ok:
            metrics = _parse_podman_stats_metrics(
                stats_result.stdout,
                fallback_memory_max_bytes=memory_max_bytes,
            )
            metrics["cpu_entitlement_percent_of_host"] = _round_pct(
                resource_profile.cpu_entitlement_percent_of_host
            )
            metrics["cpu_limit_percent_of_host"] = _cpu_limit_percent_of_host(
                resource_profile.cpu_quota,
                resource_profile.host_cpu_count,
            )
            metrics["cpu_percent"] = _derive_cpu_percent_of_entitlement(
                metrics.get("cpu_percent_of_host"),
                resource_profile.cpu_entitlement_percent_of_host,
            )
        elif container_id:
            scope_name = f"libpod-{container_id}.scope"
            scope_status = await _show_service(scope_name, settings)
            if scope_status.get("ok"):
                metric_service = scope_name
                scope_data = scope_status.get("data")
                if isinstance(scope_data, dict):
                    data.update(scope_data)
    if isinstance(cgroup_memory, dict):
        current = cgroup_memory.get("current")
        maximum = cgroup_memory.get("max")
        if isinstance(current, int):
            metrics["memory_current_bytes"] = current
        if isinstance(maximum, int):
            metrics["memory_max_bytes"] = maximum
        if (
            isinstance(metrics.get("memory_current_bytes"), int)
            and isinstance(metrics.get("memory_max_bytes"), int)
            and metrics["memory_max_bytes"] > 0
        ):
            metrics["memory_percent"] = round(
                metrics["memory_current_bytes"] / metrics["memory_max_bytes"] * 100,
                1,
            )
    if diagnostics.get("ok"):
        return {
            "backend": backend.name,
            "service": container_name(backend.name),
            "ok": True,
            "data": data,
            "metrics": metrics,
            "metric_service": metric_service,
            "action_hints": action_hints,
            "diagnostics": diagnostics,
        }
    return {
        "backend": backend.name,
        "service": container_name(backend.name),
        "ok": False,
        "data": data,
        "metrics": metrics,
        "metric_service": metric_service,
        "action_hints": action_hints,
        "diagnostics": diagnostics,
        "error": "; ".join(diagnostics.get("issues") or [])
        or str(diagnostics.get("backend_exec_error") or "")
        or str(diagnostics.get("inspect_error") or "container not ready"),
    }


def invalidate_status_cache(
    settings: Settings | None = None, *, prefill: bool = False
) -> None:
    global \
        _network_isolation_event_signature, \
        _network_isolation_cache_expires_at, \
        _network_isolation_cache_signature, \
        _network_isolation_cache_payload
    global _status_cache_generation, _status_cache_payload, _status_cache_expires_at
    _status_cache_generation += 1
    _status_cache_payload = None
    _status_cache_expires_at = 0.0
    _network_isolation_cache_payload = None
    _network_isolation_cache_signature = None
    _network_isolation_cache_expires_at = 0.0
    _network_isolation_event_signature = None
    if prefill and settings is not None:
        schedule_status_cache_prefill(settings, force_refresh=True)


def _collect_app_backend_diagnostics(
    backend: Backend,
    settings: Settings,
    diagnostics_snapshot: AppDiagnosticsSnapshot | None,
) -> dict[str, Any]:
    parameters = signature(collect_app_backend_diagnostics).parameters
    if "snapshot" in parameters:
        return collect_app_backend_diagnostics(
            backend, settings, snapshot=diagnostics_snapshot
        )
    return collect_app_backend_diagnostics(backend, settings)


def peek_cached_status() -> dict[str, Any] | None:
    return _status_cache_payload


async def warm_status_cache(settings: Settings, *, force_refresh: bool = False) -> None:
    try:
        from app.database import SessionLocal

        async with SessionLocal() as session:
            await collect_status(session, settings, force_refresh=force_refresh)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "status.cache_prefill.failed", error=str(exc), error_type=type(exc).__name__
        )


def schedule_status_cache_prefill(
    settings: Settings,
    *,
    force_refresh: bool = False,
    delay_seconds: float = 0.05,
) -> bool:
    global _status_cache_prefill_loop, _status_cache_prefill_task
    global _status_cache_prefill_pending, _status_cache_prefill_reschedule_attached
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    def _schedule_pending_after_current(_task: asyncio.Task[None]) -> None:
        global _status_cache_prefill_pending, _status_cache_prefill_reschedule_attached
        _status_cache_prefill_reschedule_attached = False
        pending = _status_cache_prefill_pending
        _status_cache_prefill_pending = None
        if pending is None or loop.is_closed():
            return
        pending_settings, pending_force_refresh, pending_delay_seconds = pending
        schedule_status_cache_prefill(
            pending_settings,
            force_refresh=pending_force_refresh,
            delay_seconds=pending_delay_seconds,
        )

    if (
        _status_cache_prefill_task is not None
        and _status_cache_prefill_loop is loop
        and not _status_cache_prefill_task.done()
    ):
        if force_refresh:
            _status_cache_prefill_pending = (settings, True, delay_seconds)
            if not _status_cache_prefill_reschedule_attached:
                _status_cache_prefill_reschedule_attached = True
                _status_cache_prefill_task.add_done_callback(
                    _schedule_pending_after_current
                )
        return False

    async def _prefill() -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await warm_status_cache(settings, force_refresh=force_refresh)

    _status_cache_prefill_loop = loop
    _status_cache_prefill_task = loop.create_task(_prefill())
    return True


async def cancel_status_cache_prefill() -> None:
    global _status_cache_prefill_loop, _status_cache_prefill_task
    global _status_cache_prefill_pending, _status_cache_prefill_reschedule_attached
    task = _status_cache_prefill_task
    _status_cache_prefill_task = None
    _status_cache_prefill_loop = None
    _status_cache_prefill_pending = None
    _status_cache_prefill_reschedule_attached = False
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


def _parse_int_value(raw: str | None) -> int | None:
    if raw is None:
        return None
    value = raw.strip()
    if value == "" or value in {"[not set]", "infinity"}:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_bytes(raw: str | None) -> int | None:
    if raw is None:
        return None
    normalized = raw.strip().replace(" ", "")
    if normalized == "":
        return None
    units = [
        ("TIB", 1024 * 1024 * 1024 * 1024),
        ("GIB", 1024 * 1024 * 1024),
        ("MIB", 1024 * 1024),
        ("KIB", 1024),
        ("TI", 1024 * 1024 * 1024 * 1024),
        ("GI", 1024 * 1024 * 1024),
        ("MI", 1024 * 1024),
        ("KI", 1024),
        ("TB", 1000 * 1000 * 1000 * 1000),
        ("GB", 1000 * 1000 * 1000),
        ("MB", 1000 * 1000),
        ("KB", 1000),
        ("T", 1024 * 1024 * 1024 * 1024),
        ("G", 1024 * 1024 * 1024),
        ("M", 1024 * 1024),
        ("K", 1024),
        ("B", 1),
    ]
    upper = normalized.upper()
    for suffix, multiplier in units:
        if not upper.endswith(suffix):
            continue
        number = normalized[: -len(suffix)]
        try:
            return int(float(number) * multiplier)
        except ValueError:
            return None
    return _parse_int_value(normalized)


def _parse_percent_number(raw: object) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    value = str(raw or "").strip()
    if not value or value == "--":
        return None
    if value.endswith("%"):
        value = value[:-1]
    try:
        return float(value)
    except ValueError:
        return None


def _cpu_limit_percent_of_host(
    raw_quota: str | None, host_cpu_count: int | None
) -> float | None:
    quota_percent = _parse_cpu_quota_percent(raw_quota)
    if quota_percent is None:
        return None
    if isinstance(host_cpu_count, int) and host_cpu_count > 0:
        return _round_nonnegative(quota_percent / host_cpu_count)
    return _round_nonnegative(quota_percent)


def _parse_podman_stats_json_payload(raw: str) -> dict[str, object] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else None
    return payload if isinstance(payload, dict) else None


def _json_value(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _parse_podman_stats_json_metrics(
    payload: dict[str, object],
    *,
    fallback_memory_max_bytes: int | None,
) -> tuple[float | None, int | None, int | None, int | None, int | None] | None:
    cpu_percent = _parse_percent_number(
        _json_value(payload, "cpu_percent", "CPUPerc", "CPU", "cpu")
    )
    memory_current_bytes: int | None = None
    memory_max_bytes = fallback_memory_max_bytes
    memory_usage = _json_value(payload, "mem_usage", "MemUsage", "memory_usage")
    if memory_usage is not None:
        memory_parts = str(memory_usage).split("/", 1)
        memory_current_bytes = _parse_bytes(memory_parts[0].strip())
        if len(memory_parts) == 2:
            parsed_limit = _parse_bytes(memory_parts[1].strip())
            if parsed_limit is not None:
                memory_max_bytes = parsed_limit
    parsed_memory_current = _json_value(
        payload, "mem_usage_bytes", "MemUsageBytes", "memory_current_bytes"
    )
    if memory_current_bytes is None and parsed_memory_current is not None:
        memory_current_bytes = _parse_bytes(str(parsed_memory_current))
    parsed_memory_limit = _json_value(
        payload, "mem_limit", "MemLimit", "memory_max_bytes"
    )
    if parsed_memory_limit is not None:
        parsed_limit = _parse_bytes(str(parsed_memory_limit))
        if parsed_limit is not None:
            memory_max_bytes = parsed_limit

    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    netio = _json_value(payload, "netio", "NetIO")
    if netio is not None:
        network_parts = str(netio).split("/", 1)
        network_rx_bytes = _parse_bytes(network_parts[0].strip())
        if len(network_parts) == 2:
            network_tx_bytes = _parse_bytes(network_parts[1].strip())
    parsed_net_input = _json_value(payload, "net_input", "NetInput", "network_rx_bytes")
    parsed_net_output = _json_value(
        payload, "net_output", "NetOutput", "network_tx_bytes"
    )
    if network_rx_bytes is None and parsed_net_input is not None:
        network_rx_bytes = _parse_bytes(str(parsed_net_input))
    if network_tx_bytes is None and parsed_net_output is not None:
        network_tx_bytes = _parse_bytes(str(parsed_net_output))
    if (
        cpu_percent is None
        and memory_current_bytes is None
        and network_rx_bytes is None
        and network_tx_bytes is None
    ):
        return None
    return (
        cpu_percent,
        memory_current_bytes,
        memory_max_bytes,
        network_rx_bytes,
        network_tx_bytes,
    )


def _parse_podman_stats_metrics(
    stdout: str,
    *,
    fallback_memory_max_bytes: int | None,
) -> dict[str, float | int | None]:
    cpu_percent: float | None = None
    memory_current_bytes: int | None = None
    memory_max_bytes = fallback_memory_max_bytes
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    raw = (stdout or "").strip()
    if raw:
        json_payload = _parse_podman_stats_json_payload(raw)
        json_metrics = (
            _parse_podman_stats_json_metrics(
                json_payload, fallback_memory_max_bytes=fallback_memory_max_bytes
            )
            if json_payload is not None
            else None
        )
        if json_metrics is not None:
            (
                cpu_percent,
                memory_current_bytes,
                memory_max_bytes,
                network_rx_bytes,
                network_tx_bytes,
            ) = json_metrics
        else:
            parts = raw.split("|", 2)
            cpu_percent = _parse_percent_number(parts[0] if parts else None)
            if len(parts) >= 2:
                memory_parts = parts[1].split("/", 1)
                memory_current_bytes = _parse_bytes(memory_parts[0].strip())
                if len(memory_parts) == 2:
                    parsed_limit = _parse_bytes(memory_parts[1].strip())
                    if parsed_limit is not None:
                        memory_max_bytes = parsed_limit
            if len(parts) >= 3:
                network_parts = parts[2].split("/", 1)
                network_rx_bytes = _parse_bytes(network_parts[0].strip())
                if len(network_parts) == 2:
                    network_tx_bytes = _parse_bytes(network_parts[1].strip())

    memory_percent: float | None = None
    if (
        memory_current_bytes is not None
        and memory_max_bytes is not None
        and memory_max_bytes > 0
    ):
        memory_percent = (memory_current_bytes / memory_max_bytes) * 100.0

    return {
        "cpu_percent": _round_pct(cpu_percent),
        "memory_percent": _round_pct(memory_percent),
        "cpu_percent_of_host": _round_pct(cpu_percent),
        "memory_current_bytes": memory_current_bytes,
        "memory_max_bytes": memory_max_bytes,
        "network_rx_bytes": network_rx_bytes,
        "network_tx_bytes": network_tx_bytes,
        "network_rx_bps": None,
        "network_tx_bps": None,
        "network_total_bps": None,
    }


def _parse_cpu_quota_percent(raw: str | None) -> float | None:
    if raw is None:
        return None
    value = raw.strip()
    if value.endswith("%"):
        value = value[:-1]
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def _derive_cpu_percent_of_entitlement(
    cpu_percent_of_host: Any,
    cpu_entitlement_percent_of_host: float | None,
) -> float | None:
    if not isinstance(cpu_percent_of_host, (int, float)):
        return None
    if (
        not isinstance(cpu_entitlement_percent_of_host, (int, float))
        or cpu_entitlement_percent_of_host <= 0
    ):
        return None
    return _round_nonnegative(
        (float(cpu_percent_of_host) / float(cpu_entitlement_percent_of_host)) * 100.0
    )


def _round_pct(raw: float | None) -> float | None:
    if raw is None:
        return None
    return round(max(0.0, min(100.0, raw)), 1)


def _round_nonnegative(raw: float | None) -> float | None:
    if raw is None:
        return None
    return round(max(0.0, raw), 1)


def _host_network_interface_in_scope(name: str) -> bool:
    if not name or name == "lo":
        return False
    virtual_prefixes = (
        "br-",
        "cni",
        "docker",
        "podman",
        "tap",
        "veth",
        "virbr",
    )
    return not name.startswith(virtual_prefixes)


def _read_linux_cpu_totals() -> tuple[int, int] | None:
    stat_path = Path("/proc/stat")
    if not stat_path.exists():
        return None
    try:
        first_line = stat_path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = first_line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def _read_linux_memory() -> tuple[int, int] | None:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None
    values: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            parts = raw_value.strip().split()
            if not parts:
                continue
            values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0:
        return None
    used = max(0, total - available)
    return total, used


def _read_linux_network_bytes() -> tuple[int, int] | None:
    netdev_path = Path("/proc/net/dev")
    if not netdev_path.exists():
        return None
    total_rx = 0
    total_tx = 0
    try:
        for line in netdev_path.read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            interface, payload = line.split(":", 1)
            name = interface.strip()
            if not _host_network_interface_in_scope(name):
                continue
            fields = payload.split()
            if len(fields) < 16:
                continue
            total_rx += int(fields[0])
            total_tx += int(fields[8])
    except (OSError, ValueError):
        return None
    return total_rx, total_tx


def _collect_host_metrics(resource_profile: Any) -> dict[str, Any]:
    global _host_cpu_sample, _host_network_sample

    now = time.monotonic()
    cpu_percent: float | None = None
    loadavg_1m: float | None = None
    cpu_count = resource_profile.host_cpu_count
    cpu_totals = _read_linux_cpu_totals()
    if cpu_totals is not None:
        total, idle = cpu_totals
        if _host_cpu_sample is not None:
            previous_ts, previous_total, previous_idle = _host_cpu_sample
            elapsed_total = total - previous_total
            elapsed_idle = idle - previous_idle
            if elapsed_total > 0 and now > previous_ts:
                cpu_percent = ((elapsed_total - elapsed_idle) / elapsed_total) * 100.0
        _host_cpu_sample = (now, total, idle)
    try:
        loadavg_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        loadavg_1m = None
    if (
        cpu_percent is None
        and loadavg_1m is not None
        and isinstance(cpu_count, int)
        and cpu_count > 0
    ):
        cpu_percent = (loadavg_1m / cpu_count) * 100.0

    memory_total_bytes = resource_profile.host_memory_bytes
    memory_used_bytes: int | None = None
    memory_percent: float | None = None
    memory = _read_linux_memory()
    if memory is not None:
        memory_total_bytes, memory_used_bytes = memory
    if (
        isinstance(memory_total_bytes, int)
        and memory_total_bytes > 0
        and isinstance(memory_used_bytes, int)
    ):
        memory_percent = (memory_used_bytes / memory_total_bytes) * 100.0

    disk_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_percent: float | None = None
    try:
        disk_usage = shutil.disk_usage("/")
        disk_total_bytes = int(disk_usage.total)
        disk_used_bytes = int(disk_usage.used)
    except OSError:
        disk_total_bytes = None
        disk_used_bytes = None
    if (
        isinstance(disk_total_bytes, int)
        and disk_total_bytes > 0
        and isinstance(disk_used_bytes, int)
    ):
        disk_percent = (disk_used_bytes / disk_total_bytes) * 100.0

    network_rx_bps: float | None = None
    network_tx_bps: float | None = None
    network_total_bps: float | None = None
    network_bytes = _read_linux_network_bytes()
    had_previous_network_sample = _host_network_sample is not None
    if network_bytes is not None:
        rx_bytes, tx_bytes = network_bytes
        if _host_network_sample is not None:
            previous_ts, previous_rx, previous_tx = _host_network_sample
            elapsed = now - previous_ts
            rx_delta = rx_bytes - previous_rx
            tx_delta = tx_bytes - previous_tx
            if elapsed > 0 and rx_delta >= 0 and tx_delta >= 0:
                network_rx_bps = rx_delta / elapsed
                network_tx_bps = tx_delta / elapsed
                network_total_bps = network_rx_bps + network_tx_bps
        _host_network_sample = (now, rx_bytes, tx_bytes)
    if network_total_bps is None and not had_previous_network_sample:
        network_rx_bps = 0.0
        network_tx_bps = 0.0
        network_total_bps = 0.0

    return {
        "cpu_percent": _round_pct(cpu_percent),
        "cpu_count": cpu_count,
        "loadavg_1m": round(loadavg_1m, 2) if loadavg_1m is not None else None,
        "memory_percent": _round_pct(memory_percent),
        "memory_used_bytes": memory_used_bytes,
        "memory_total_bytes": memory_total_bytes,
        "disk_percent": _round_pct(disk_percent),
        "disk_used_bytes": disk_used_bytes,
        "disk_total_bytes": disk_total_bytes,
        "network_rx_bps": round(network_rx_bps, 1)
        if network_rx_bps is not None
        else None,
        "network_tx_bps": round(network_tx_bps, 1)
        if network_tx_bps is not None
        else None,
        "network_total_bps": round(network_total_bps, 1)
        if network_total_bps is not None
        else None,
        "network_scale_bps": 12_500_000,
    }


async def collect_status(
    session: AsyncSession,
    settings: Settings,
    cache_ttl_seconds: int | None = None,
    force_refresh: bool = False,
    allow_stale: bool = False,
) -> dict[str, Any]:
    global _status_cache_payload, _status_cache_expires_at
    ttl_seconds = (
        settings.status_cache_ttl_sec
        if cache_ttl_seconds is None
        else cache_ttl_seconds
    )
    now = time.monotonic()

    if (
        not force_refresh
        and _status_cache_payload is not None
        and now < _status_cache_expires_at
    ):
        return _status_cache_payload
    if allow_stale and not force_refresh and _status_cache_payload is not None:
        return _status_cache_payload

    async with _get_status_cache_lock():
        now = time.monotonic()
        if (
            not force_refresh
            and _status_cache_payload is not None
            and now < _status_cache_expires_at
        ):
            return _status_cache_payload
        if allow_stale and not force_refresh and _status_cache_payload is not None:
            return _status_cache_payload

        collection_generation = _status_cache_generation
        payload = await _collect_status_uncached(
            session, settings, force_refresh=force_refresh
        )
        if collection_generation == _status_cache_generation:
            _status_cache_payload = payload
            _status_cache_expires_at = time.monotonic() + max(1, ttl_seconds)
        return payload


async def _collect_status_uncached(
    session: AsyncSession,
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    global _cpu_usage_samples, _network_usage_samples
    backends = (
        (await session.execute(select(Backend).options(selectinload(Backend.inputs))))
        .scalars()
        .all()
    )
    inputs = (
        (
            await session.execute(
                select(Input)
                .options(selectinload(Input.backends))
                .order_by(Input.id.asc())
            )
        )
        .scalars()
        .all()
    )
    enabled_app_backends = [
        backend for backend in backends if backend.kind == "app" and backend.enabled
    ]
    resource_profile = build_resource_profile(settings, enabled_app_backends)
    host_metrics = _collect_host_metrics(resource_profile)

    diagnostics_snapshot = None
    diagnostics_snapshot_error = ""
    if enabled_app_backends:
        try:
            diagnostics_snapshot = await asyncio.to_thread(
                collect_app_diagnostics_snapshot, settings
            )
        except Exception as exc:
            diagnostics_snapshot_error = str(exc)
            logger.exception(
                "status.diagnostics_snapshot.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
    diagnostics_semaphore = asyncio.Semaphore(4)

    async def _show_bounded_app_container(backend: Backend) -> dict[str, Any]:
        async with diagnostics_semaphore:
            try:
                return await _show_app_container(
                    backend,
                    resource_profile,
                    settings,
                    diagnostics_snapshot=diagnostics_snapshot,
                )
            except Exception as exc:
                logger.exception(
                    "status.backend.failed",
                    backend=backend.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return {
                    "backend": backend.name,
                    "service": container_name(backend.name),
                    "ok": False,
                    "data": {
                        "RuntimeDiagnosis": "diagnostics_failed",
                        "RuntimeIssues": "diagnostics_failed",
                    },
                    "metrics": {
                        "cpu_percent": None,
                        "memory_percent": None,
                        "cpu_percent_of_host": None,
                        "cpu_entitlement_percent_of_host": None,
                        "cpu_limit_percent_of_host": None,
                        "memory_current_bytes": None,
                        "memory_max_bytes": None,
                        "network_rx_bytes": None,
                        "network_tx_bytes": None,
                        "network_rx_bps": None,
                        "network_tx_bps": None,
                        "network_total_bps": None,
                    },
                    "metric_service": container_name(backend.name),
                    "action_hints": [],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }

    app_statuses = await asyncio.gather(
        *[_show_bounded_app_container(backend) for backend in enabled_app_backends]
    )
    service_statuses = list(app_statuses)
    if settings.shield_enabled or any(backend.kind == "shield" for backend in backends):
        service_statuses.append(await _show_shield_container(settings))
    if settings.netdata_enabled:
        service_statuses.append(await _show_netdata_service(settings))
    unique_services: list[str] = []
    for service_status in service_statuses:
        metric_service = service_status.get("metric_service") or service_status.get(
            "service"
        )
        if isinstance(metric_service, str) and metric_service not in unique_services:
            unique_services.append(metric_service)
    now = time.monotonic()
    active_samples = {
        svc: sample
        for svc, sample in _cpu_usage_samples.items()
        if svc in unique_services
    }
    active_network_samples = {
        svc: sample
        for svc, sample in _network_usage_samples.items()
        if svc in unique_services
    }
    for service_status in service_statuses:
        service = service_status.get("metric_service") or service_status.get("service")
        if not isinstance(service, str):
            continue
        existing_metrics = (
            service_status.get("metrics")
            if isinstance(service_status.get("metrics"), dict)
            else {}
        )
        memory_max_bytes = existing_metrics.get("memory_max_bytes")
        cpu_entitlement_percent = existing_metrics.get(
            "cpu_entitlement_percent_of_host"
        )
        if not service_status.get("ok"):
            service_status["metrics"] = service_status.get("metrics") or {
                "cpu_percent": None,
                "memory_percent": None,
                "cpu_percent_of_host": None,
                "cpu_entitlement_percent_of_host": _round_pct(cpu_entitlement_percent)
                if isinstance(cpu_entitlement_percent, (int, float))
                else None,
                "cpu_limit_percent_of_host": existing_metrics.get(
                    "cpu_limit_percent_of_host"
                ),
                "memory_current_bytes": None,
                "memory_max_bytes": memory_max_bytes,
                "network_rx_bytes": None,
                "network_tx_bytes": None,
                "network_rx_bps": None,
                "network_tx_bps": None,
                "network_total_bps": None,
            }
            continue
        if isinstance(service_status.get("metrics"), dict):
            rx_bytes = existing_metrics.get("network_rx_bytes")
            tx_bytes = existing_metrics.get("network_tx_bytes")
            if isinstance(rx_bytes, int) and isinstance(tx_bytes, int):
                previous_network = active_network_samples.get(service)
                if previous_network is not None:
                    previous_ts, previous_rx_bytes, previous_tx_bytes = previous_network
                    elapsed = now - previous_ts
                    rx_delta = rx_bytes - previous_rx_bytes
                    tx_delta = tx_bytes - previous_tx_bytes
                    if elapsed > 0 and rx_delta >= 0 and tx_delta >= 0:
                        rx_bps = rx_delta / elapsed
                        tx_bps = tx_delta / elapsed
                        existing_metrics["network_rx_bps"] = _round_nonnegative(rx_bps)
                        existing_metrics["network_tx_bps"] = _round_nonnegative(tx_bps)
                        existing_metrics["network_total_bps"] = _round_nonnegative(
                            rx_bps + tx_bps
                        )
                active_network_samples[service] = (now, rx_bytes, tx_bytes)
            if any(
                existing_metrics.get(key) is not None
                for key in ("cpu_percent", "memory_percent", "memory_current_bytes")
            ):
                continue

        data = service_status.get("data") or {}
        current_cpu_nsec = _parse_int_value(data.get("CPUUsageNSec"))
        memory_current_bytes = _parse_int_value(data.get("MemoryCurrent"))
        cpu_percent_of_host: float | None = None
        cpu_percent: float | None = None

        previous = active_samples.get(service)
        if current_cpu_nsec is not None and previous is not None:
            previous_ts, previous_cpu_nsec = previous
            elapsed = now - previous_ts
            if elapsed > 0:
                delta_cpu_nsec = max(0, current_cpu_nsec - previous_cpu_nsec)
                cpu_percent_of_one_core = (
                    (delta_cpu_nsec / 1_000_000_000) / elapsed * 100.0
                )
                host_cpu_count = getattr(resource_profile, "host_cpu_count", None)
                cpu_percent_of_host = (
                    cpu_percent_of_one_core / host_cpu_count
                    if isinstance(host_cpu_count, int) and host_cpu_count > 0
                    else cpu_percent_of_one_core
                )
                cpu_percent = _derive_cpu_percent_of_entitlement(
                    cpu_percent_of_host,
                    float(cpu_entitlement_percent)
                    if isinstance(cpu_entitlement_percent, (int, float))
                    else None,
                )

        if current_cpu_nsec is not None:
            active_samples[service] = (now, current_cpu_nsec)

        memory_percent: float | None = None
        if (
            memory_current_bytes is not None
            and memory_max_bytes is not None
            and memory_max_bytes > 0
        ):
            memory_percent = (memory_current_bytes / memory_max_bytes) * 100.0

        service_status["metrics"] = {
            "cpu_percent": _round_pct(cpu_percent),
            "memory_percent": _round_pct(memory_percent),
            "cpu_percent_of_host": _round_pct(cpu_percent_of_host),
            "cpu_entitlement_percent_of_host": _round_pct(cpu_entitlement_percent)
            if isinstance(cpu_entitlement_percent, (int, float))
            else None,
            "cpu_limit_percent_of_host": existing_metrics.get(
                "cpu_limit_percent_of_host"
            ),
            "memory_current_bytes": memory_current_bytes,
            "memory_max_bytes": memory_max_bytes,
            "network_rx_bytes": None,
            "network_tx_bytes": None,
            "network_rx_bps": None,
            "network_tx_bps": None,
            "network_total_bps": None,
        }

    _cpu_usage_samples = active_samples
    _network_usage_samples = active_network_samples

    nginx_status = await _show_service("nginx.service", settings)
    network_isolation = await _network_isolation_status(
        enabled_app_backends, settings, force_refresh=force_refresh
    )
    if diagnostics_snapshot_error:
        network_isolation.setdefault("warnings", []).append(
            {
                "component": "app_diagnostics_snapshot",
                "error": diagnostics_snapshot_error,
            }
        )
    last_apply = (
        await session.execute(select(ApplyRun).order_by(ApplyRun.id.desc()).limit(1))
    ).scalar_one_or_none()
    host_apply_state = await session.get(HostApplyState, 1)
    last_update = (
        await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()).limit(1))
    ).scalar_one_or_none()
    last_update = await reconcile_update_run(
        session, settings, last_update, notification_mode="background"
    )

    last_apply_payload = None
    profile_drift_payload = {
        "changed": False,
        "current": resource_profile.as_dict(),
        "last_applied": None,
    }
    if last_apply is not None:
        try:
            details = json.loads(last_apply.details_json or "{}")
        except json.JSONDecodeError:
            details = {"raw": last_apply.details_json}
        operation = (
            await session.get(Operation, last_apply.operation_id)
            if last_apply.operation_id
            else None
        )
        duration_seconds = None
        if (
            operation is not None
            and operation.started_at is not None
            and operation.finished_at is not None
        ):
            duration_seconds = max(
                0.0, (operation.finished_at - operation.started_at).total_seconds()
            )
        last_apply_payload = {
            "id": last_apply.id,
            "operation_id": last_apply.operation_id,
            "status": last_apply.status,
            "message": last_apply.message,
            "config_revision": last_apply.config_revision,
            "desired_state_hash": last_apply.desired_state_hash,
            "details": details,
            "created_at": utc_isoformat(last_apply.created_at),
            "started_at": utc_isoformat(operation.started_at) if operation else None,
            "finished_at": utc_isoformat(operation.finished_at) if operation else None,
            "duration_seconds": duration_seconds,
        }
        try:
            stored_profile = json.loads(last_apply.resource_profile_json or "null")
        except json.JSONDecodeError:
            stored_profile = None
        last_profile = (
            stored_profile
            if isinstance(stored_profile, dict)
            else details.get("resource_profile")
            if isinstance(details, dict)
            else None
        )
        if isinstance(last_profile, dict):
            profile_drift_payload["last_applied"] = last_profile
            drift_keys = (
                "mode",
                "memory_high",
                "memory_max",
                "cpu_quota",
                "app_memory_budget_bytes",
                "app_cpu_budget_percent",
                "size_counts",
            )
            profile_drift_payload["changed"] = any(
                (last_profile.get(key) or "")
                != (profile_drift_payload["current"].get(key) or "")
                for key in drift_keys
            )

    apply_state_payload = None
    if host_apply_state is not None:
        current_hash = last_apply.desired_state_hash if last_apply is not None else None
        last_hash = host_apply_state.last_applied_state_hash
        apply_state_payload = {
            "last_applied_state_hash": last_hash,
            "last_successful_operation_id": host_apply_state.last_successful_operation_id,
            "last_successful_apply_run_id": host_apply_state.last_successful_apply_run_id,
            "last_applied_at": utc_isoformat(host_apply_state.last_applied_at),
            "latest_apply_desired_state_hash": current_hash,
            "latest_apply_differs_from_last_applied": bool(
                current_hash and last_hash and current_hash != last_hash
            ),
        }

    last_update_payload = None
    if last_update is not None:
        try:
            details = json.loads(last_update.details_json or "{}")
        except json.JSONDecodeError:
            details = {"raw": last_update.details_json}
        last_update_payload = {
            "status": last_update.status,
            "message": last_update.message,
            "details": details,
            "created_at": utc_isoformat(last_update.created_at),
        }
    current_version = current_app_version()
    update_version = await get_cached_update_version_info(
        session, settings, current_version=current_version
    )

    return {
        "nginx": nginx_status,
        "services": service_statuses,
        "last_apply": last_apply_payload,
        "apply_state": apply_state_payload,
        "last_update": last_update_payload,
        "update_version": update_version,
        "resource_profile": resource_profile.as_dict(),
        "resource_profile_drift": profile_drift_payload,
        "host_metrics": host_metrics,
        "dashboard_overview": dashboard_overview_counts(backends, inputs),
        "app_network_isolation": network_isolation,
        "tailscale_services": _tailscale_service_status(inputs, settings),
    }


def _network_isolation_empty_status(
    enabled_app_backends: list[Backend],
) -> dict[str, Any]:
    return {
        "checked": True,
        "backend_count": len(enabled_app_backends),
        "pair_count": 0,
        "ok": True,
        "leaks": [],
        "unknown": [],
        "checks": [],
    }


def _network_isolation_cache_key(
    backends: list[Backend],
) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (str(backend.name), int(backend.handoff_port or 0))
            for backend in backends
            if backend.kind == "app" and backend.enabled
        )
    )


def _network_isolation_event_signature_from_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "checked": payload.get("checked"),
            "ok": payload.get("ok"),
            "leaks": payload.get("leaks")
            if isinstance(payload.get("leaks"), list)
            else [],
            "unknown": payload.get("unknown")
            if isinstance(payload.get("unknown"), list)
            else [],
            "error": payload.get("error") or "",
            "error_type": payload.get("error_type") or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _network_isolation_related_backends(
    backends: list[Backend], payload: dict[str, Any]
) -> list[str]:
    related: set[str] = set()
    for collection_key in ("leaks", "unknown"):
        rows = payload.get(collection_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("backend", "source", "target"):
                value = str(row.get(key) or "").strip()
                if value:
                    related.add(value)
    if related:
        return sorted(related)
    return sorted(
        str(backend.name)
        for backend in backends
        if backend.kind == "app" and backend.enabled
    )


async def _maybe_emit_network_isolation_event(
    backends: list[Backend],
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    global _network_isolation_event_signature
    if payload.get("ok") is True:
        _network_isolation_event_signature = None
        return
    if not payload:
        return

    signature = _network_isolation_event_signature_from_payload(payload)
    if signature == _network_isolation_event_signature:
        return
    _network_isolation_event_signature = signature

    leaks = payload.get("leaks") if isinstance(payload.get("leaks"), list) else []
    unknown = payload.get("unknown") if isinstance(payload.get("unknown"), list) else []
    status = "warning" if leaks else "unknown"
    related_backends = _network_isolation_related_backends(backends, payload)
    details = {
        "status": status,
        "checked": bool(payload.get("checked")),
        "backend_count": payload.get("backend_count"),
        "pair_count": payload.get("pair_count"),
        "leaks": leaks,
        "unknown": unknown,
        "error": payload.get("error") or "",
        "error_type": payload.get("error_type") or "",
    }
    logger.warning(
        "status.network_isolation.drift",
        status=status,
        backend_count=payload.get("backend_count"),
        pair_count=payload.get("pair_count"),
        leaks=len(leaks),
        unknown=len(unknown),
        related_backends=related_backends,
        error=payload.get("error") or "",
    )
    try:
        await emit_control_event(
            settings,
            kind="output_isolation_drift",
            source="status",
            summary="Output isolation drift",
            severity="warn",
            scope="host",
            affects_all=not related_backends,
            related_backends=related_backends,
            subevents=[
                {"label": "status", "value": status},
                {"label": "leaks", "value": str(len(leaks))},
                {"label": "unknown", "value": str(len(unknown))},
            ],
            details=details,
            notify=False,
        )
    except Exception as exc:
        logger.warning(
            "status.network_isolation.event_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def _network_isolation_status(
    backends: list[Backend],
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    global \
        _network_isolation_cache_expires_at, \
        _network_isolation_cache_signature, \
        _network_isolation_cache_payload
    if len(backends) <= 1:
        return _network_isolation_empty_status(backends)
    cache_key = _network_isolation_cache_key(backends)
    now = time.monotonic()
    if (
        not force_refresh
        and _network_isolation_cache_payload is not None
        and _network_isolation_cache_signature == cache_key
        and now < _network_isolation_cache_expires_at
    ):
        payload = dict(_network_isolation_cache_payload)
        payload["cache"] = "hit"
        payload["cache_ttl_seconds"] = max(
            0, round(_network_isolation_cache_expires_at - now, 1)
        )
        return payload

    try:
        payload = await asyncio.to_thread(
            collect_app_network_isolation_canary, backends, settings
        )
    except Exception as exc:
        logger.exception(
            "status.network_isolation.failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        payload = {
            "checked": False,
            "backend_count": len(backends),
            "pair_count": 0,
            "ok": False,
            "leaks": [],
            "unknown": [],
            "checks": [],
            "cache": "error",
            "cache_ttl_seconds": 0,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        await _maybe_emit_network_isolation_event(backends, settings, payload)
        return payload
    _network_isolation_cache_payload = dict(payload)
    _network_isolation_cache_signature = cache_key
    _network_isolation_cache_expires_at = (
        time.monotonic() + NETWORK_ISOLATION_STATUS_CACHE_TTL_SEC
    )
    payload["cache"] = "refresh" if force_refresh else "miss"
    payload["cache_ttl_seconds"] = NETWORK_ISOLATION_STATUS_CACHE_TTL_SEC
    await _maybe_emit_network_isolation_event(backends, settings, payload)
    return payload
