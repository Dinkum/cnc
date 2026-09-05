from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlencode

import shutil

from fastapi import (
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload

from app.config import Settings
from app.logger import get_logger
from app.models.entities import ApplyRun, Backend, ClusterNode, ControlEvent, Input
from app.security import get_csrf_token, secure_cookie_required
from app.static_delivery import stylesheet_asset_version
from app.services.app_containers import configured_app_dns_servers
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.app_healthchecks import (
    display_backend_healthcheck_mode,
    resolve_backend_healthcheck,
)
from app.services.backend_metric_history import (
    DEFAULT_METRIC_KEY,
    DEFAULT_TIMEFRAME_KEY,
    METRIC_SPECS,
    TIMEFRAME_SPECS,
)
from app.services.control_events import list_recent_output_events
from app.services.cluster_nodes import (
    LatencySummary,
    cluster_latency_summaries,
    current_node_version_label,
    list_cluster_nodes,
)
from app.services.error_reporting import ErrorCode
from app.services.inter_app_interfaces import (
    INTERFACE_DIRECTION_BIDIRECTIONAL,
    INTERFACE_DIRECTION_IN,
    INTERFACE_STATUS_ACCEPTED,
    INTERFACE_STATUS_PENDING,
    INTERFACE_STATUS_REJECTED,
    clean_interface_direction,
    clean_interface_status,
    inbound_inter_app_interfaces,
    read_inter_app_interfaces,
)
from app.services.placement_config import (
    LOCAL_NODE_UID,
    PLACEMENT_MODE_FAILOVER,
    PLACEMENT_MODE_LIVE,
    clean_node_uid,
    read_backend_placement,
)
from app.services.replica_readiness import (
    ReplicaReadinessStatus,
    backend_replica_readiness_statuses,
)
from app.services.llm_help import (
    build_backend_operator_commands,
    build_backend_llm_help_payload,
)
from app.services.netdata_constants import NETDATA_PORT
from app.services.operation_runtime import (
    create_backend_progress_steps,
    input_progress_pipelines,
    operation_progress_pipelines,
    output_save_progress_steps,
)
from app.services.operations import (
    HostMutationBlocker,
    active_host_mutation_blocker,
)
from app.services.port_preflight import is_loopback_port_free
from app.services.resource_profile import (
    RESOURCE_SIZES,
    ResourceProfile,
    backend_resource_profile,
    build_resource_profile,
)
from app.services.renderers import (
    SHIELD_PORT,
    container_name,
    network_name,
)
from app.services.sandbox_profiles import app_sandbox_dir, app_sandbox_profiles
from app.services.shield_runtime import SHIELD_CONTAINER_NAME, SHIELD_CONTAINER_SERVICE
from app.services.status_service import (
    collect_status,
    dashboard_overview_counts,
    peek_cached_status,
)
from app.services.tailscale_urls import (
    configured_tailnet_admin_host,
    infer_tailnet_dns_name,
    tailscale_service_url,
)
from app.services.update_service import (
    current_app_version,
)
from app.services.validators import (
    ValidationError,
    ensure_backend_name,
    ensure_input_kind,
    parse_volumes_json,
)
from app.ui.errors import (
    ensure_context_flash_error_code as _ensure_context_flash_error_code,
    ensure_operator_coded_error as _ensure_operator_coded_error,
    operator_coded_error as _operator_coded_error,
)
from app.ui.forms import (
    DEFAULT_UI_APP_SANDBOX_PROFILE,
)
from app.ui.settings import (
    _access_key_settings_summary,
    _netdata_settings_summary,
    _notification_settings_summary,
)
from app.ui.read_models import (
    DashboardRows as _DashboardRows,
    DashboardScope as _DashboardScope,
    dashboard_status_fallback,
)
from app.ui.view_models import (
    _backup_summary,
    _change_state,
    _format_bytes,
    _format_timestamp,
    _save_apply_feedback,
    _save_job_summary,
    _timestamp_data_value,
    _tone_for_value,
)


templates = Jinja2Templates(directory="app/templates")
logger = get_logger("ui")
DOMAIN_INPUT_HINT = "Use a lowercase hostname like api.example.com. Letters, digits, hyphens, and dots only."
TAILNET_PATH_HINT = "Use a private path like /app1 or /docs/api. Each segment must stay lowercase and hyphen-safe."
STATIC_ROOT_HINT = "Use an absolute host path like /srv/site or /var/www/docs. Static roots cannot contain spaces."
VOLUME_BOUNDARY_HINT = (
    "Use app-owned data paths only. Volumes cannot point at CNC-managed host paths, host runtime paths, "
    "or CNC control paths inside the container."
)


def _network_isolation_home_status(status: dict[str, object]) -> dict[str, str]:
    payload = status.get("app_network_isolation")
    if not isinstance(payload, dict) or payload.get("checked") is False:
        return {"label": "unknown", "tone": "warn"}
    if payload.get("ok") is True:
        return {"label": "OK", "tone": "success"}
    leaks = payload.get("leaks") if isinstance(payload.get("leaks"), list) else []
    if leaks:
        return {"label": "warning", "tone": "warn"}
    return {"label": "unknown", "tone": "warn"}


FLASH_SUCCESS_COOKIE = "cnc_flash_success"
FLASH_ERROR_COOKIE = "cnc_flash_error"
VALID_DASHBOARD_TABS = {"home", "inputs", "routing", "outputs", "settings"}
UI_RUNTIME_SIGNAL_TIMEOUT_SEC = 0.9
UI_RUNTIME_SIGNAL_CACHE_TTL_SEC = 15.0
FLASH_COOKIE_VALUE_LIMIT = 380
UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024
HOST_MUTATION_BLOCKER_LABELS = {
    "apply_host": "A host apply",
    "backup_backend": "A backup",
    "backend_ssh_key": "SSH key provisioning",
    "clone_backend": "An output clone",
    "cloudflare_sync": "Cloudflare sync",
    "create_backend": "Output create",
    "delete_backend": "Output delete",
    "host_mutation": "A host change",
    "repair_backend": "Output repair",
    "restore_backend": "Output restore",
    "setup_backend_replica": "Replica setup",
    "ui.backend.create": "Output create",
    "ui.backend.delete": "Output delete",
    "ui.input.create": "Input create",
    "ui.input.delete": "Input delete",
    "ui.input.update": "Input save",
    "update_control_plane": "A CNC update",
}


class RuntimeDiagnosticsPending(RuntimeError):
    pass


def _copy_upload_file_with_limit(source, destination: Path, *, max_bytes: int) -> int:
    copied = 0
    with destination.open("wb") as handle:
        while True:
            chunk = source.read(UPLOAD_COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise ValueError(
                    f"backup upload exceeds {_format_bytes(max_bytes)} limit"
                )
            handle.write(chunk)
    return copied


_RUNTIME_DIAGNOSTICS_CACHE: dict[
    tuple[int | None, str], tuple[float, dict[str, object]]
] = {}
_RUNTIME_DIAGNOSTICS_TASKS: dict[
    tuple[int | None, str], asyncio.Task[dict[str, object]]
] = {}
UI_RUNTIME_SIGNAL_CACHE_MAX_ENTRIES = 256


def _cache_completed_runtime_diagnostics_task(
    cache_key: tuple[int | None, str],
    backend_name: str,
    task: asyncio.Task[dict[str, object]],
) -> None:
    if _RUNTIME_DIAGNOSTICS_TASKS.get(cache_key) is not task:
        return
    _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
    try:
        payload = task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(
            "ui.output_runtime_signals.failed",
            backend=backend_name,
            error=str(exc),
        )
        return
    _RUNTIME_DIAGNOSTICS_CACHE[cache_key] = (time.monotonic(), dict(payload))
    _prune_runtime_diagnostics_cache()


def _prune_runtime_diagnostics_cache(now: float | None = None) -> None:
    cutoff = time.monotonic() if now is None else now
    expired = [
        key
        for key, (cached_at, _payload) in _RUNTIME_DIAGNOSTICS_CACHE.items()
        if cutoff - cached_at > UI_RUNTIME_SIGNAL_CACHE_TTL_SEC
    ]
    for key in expired:
        _RUNTIME_DIAGNOSTICS_CACHE.pop(key, None)
    overflow = len(_RUNTIME_DIAGNOSTICS_CACHE) - UI_RUNTIME_SIGNAL_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(
            _RUNTIME_DIAGNOSTICS_CACHE,
            key=lambda key: _RUNTIME_DIAGNOSTICS_CACHE[key][0],
        )[:overflow]
        for key in oldest_keys:
            _RUNTIME_DIAGNOSTICS_CACHE.pop(key, None)


def _render_dashboard_template(
    request: Request,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    context.setdefault("app_version", _app_version())
    context.setdefault("asset_version", _app_version())
    context.setdefault("create_output_progress_steps", create_backend_progress_steps())
    context.setdefault("input_progress_pipelines", input_progress_pipelines())
    context.setdefault("operation_progress_pipelines", operation_progress_pipelines())
    context.setdefault("output_save_progress_steps", output_save_progress_steps())
    return templates.TemplateResponse(
        request, "index.html", context, status_code=status_code
    )


def _render_output_template(
    request: Request,
    settings: Settings,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    context["request"] = request
    backend = context.get("selected_backend")
    if isinstance(backend, Backend) and str(backend.kind or "").lower() == "app":
        context["operator_commands"] = _backend_operator_commands_for_page(
            backend,
            settings=settings,
            host=_backend_ssh_destination(settings, request, resolve_dns=False),
            llm_help_url=_output_llm_help_url(request, backend.id),
        )
    else:
        context["operator_commands"] = []
    context.setdefault("app_version", _app_version())
    context.setdefault("asset_version", _stylesheet_asset_version(settings))
    context.setdefault("operation_progress_pipelines", operation_progress_pipelines())
    return templates.TemplateResponse(
        request, "output_detail.html", context, status_code=status_code
    )


def _render_access_template(
    request: Request,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    return templates.TemplateResponse(
        request, "access.html", context, status_code=status_code
    )


def _dashboard_tab(value: object, default: str = "home") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_DASHBOARD_TABS else default


def _sanitize_flash_cookie_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char for char in str(value)
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > FLASH_COOKIE_VALUE_LIMIT:
        return f"{cleaned[: FLASH_COOKIE_VALUE_LIMIT - 3].rstrip()}..."
    return cleaned


def _request_prefers_json(request: Request) -> bool:
    accept = str(request.headers.get("accept") or "").lower()
    requested_with = str(request.headers.get("x-requested-with") or "").strip().lower()
    return "application/json" in accept or requested_with == "fetch"


def _form_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _ui_request_mode(request: Request) -> str:
    return "async" if _request_prefers_json(request) else "page"


def _json_save_feedback(
    response, *, success_message: str, failure_prefix: str, **extra: object
) -> JSONResponse:
    success_flash, error_flash = _save_apply_feedback(
        response,
        success_message=success_message,
        failure_prefix=failure_prefix,
    )
    payload = {
        "flash_success": success_flash,
        "flash_error": error_flash,
        "apply_status": response.status,
        "run_id": response.run_id,
        **extra,
    }
    return JSONResponse(payload, status_code=200)


def _backend_update_changed_keys(backend: Backend, payload: object) -> set[str]:
    values = payload.model_dump(exclude_unset=True)
    changed: set[str] = set()
    for key, value in values.items():
        if key == "kind" and value == backend.kind:
            continue
        if getattr(backend, key) != value:
            changed.add(key)
    return changed


def _host_mutation_blocked_message(action: str, blocker: HostMutationBlocker) -> str:
    label = HOST_MUTATION_BLOCKER_LABELS.get(
        blocker.kind, blocker.kind.replace("_", " ").strip().title()
    )
    state = blocker.status.replace("_", " ").strip() or "running"
    phase = blocker.phase.replace("_", " ").strip()
    suffix = f" ({phase})" if phase and phase != state else ""
    return _operator_coded_error(
        f"{label} is already {state}{suffix}. {action} can't start until it finishes. Try again later.",
        ErrorCode.UI_ACTION_UNAVAILABLE,
    )


async def _host_mutation_preflight_message(
    settings: Settings, action: str
) -> str | None:
    blocker = await active_host_mutation_blocker(settings)
    return _host_mutation_blocked_message(action, blocker) if blocker else None


def _dashboard_tab_url(
    request: Request, active_tab: str, *, defer_status: bool = False
) -> str:
    try:
        base = str(request.url_for("dashboard"))
    except (AttributeError, RuntimeError):
        base = "/"
    params = {"tab": _dashboard_tab(active_tab, "home")}
    if defer_status:
        params["defer_status"] = "1"
    return f"{base}?{urlencode(params)}"


def _set_flash_cookies(
    response: Response,
    request: Request,
    *,
    flash_success: str | None = None,
    flash_error: str | None = None,
) -> None:
    secure_cookie = secure_cookie_required(request)
    response.delete_cookie(FLASH_SUCCESS_COOKIE, path="/", secure=secure_cookie)
    response.delete_cookie(FLASH_ERROR_COOKIE, path="/", secure=secure_cookie)
    safe_success = _sanitize_flash_cookie_value(flash_success)
    safe_error = _sanitize_flash_cookie_value(
        _ensure_operator_coded_error(flash_error) if flash_error else None
    )
    if safe_success:
        response.set_cookie(
            FLASH_SUCCESS_COOKIE,
            safe_success,
            max_age=60,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )
    if safe_error:
        response.set_cookie(
            FLASH_ERROR_COOKIE,
            safe_error,
            max_age=60,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )


def _dashboard_redirect(
    request: Request,
    *,
    active_tab: str,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    target = _dashboard_tab_url(request, active_tab)
    response = RedirectResponse(url=target, status_code=status_code)
    _set_flash_cookies(
        response, request, flash_success=flash_success, flash_error=flash_error
    )
    return response


async def _dashboard_refresh_response(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    active_tab: str,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    resolved_tab = _dashboard_tab(active_tab, "home")
    context = await _dashboard_context(
        session, settings, prefer_cached_status=True, active_tab=resolved_tab
    )
    context["request"] = request
    context["active_tab"] = resolved_tab
    context["flash_success"] = flash_success
    context["flash_error"] = flash_error
    return _render_dashboard_template(request, context, status_code=status_code)


def _output_redirect(
    request: Request,
    *,
    backend_id: int,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    target = f"/outputs/{backend_id}"
    response = RedirectResponse(url=target, status_code=status_code)
    _set_flash_cookies(
        response, request, flash_success=flash_success, flash_error=flash_error
    )
    return response


def _app_version() -> str:
    return current_app_version()


def _stylesheet_asset_version(settings: Settings) -> str:
    return stylesheet_asset_version(settings.static_files_dir)


def _format_node_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return (
                f"{amount:.0f} {unit}"
                if unit == "B"
                else f"{amount:.1f} {unit}".replace(".0 ", " ")
            )
        amount /= 1024
    return f"{amount:.0f} B"


def _node_details_payload(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _node_tailnet_url(
    node: ClusterNode, details: dict[str, object], settings: Settings
) -> str:
    for key in ("confirm", "register"):
        section = details.get(key)
        if not isinstance(section, dict):
            continue
        tailnet_url = str(section.get("tailnet_url") or "").strip()
        if tailnet_url.startswith("https://"):
            return tailnet_url

    tailnet_dns_name = infer_tailnet_dns_name(settings)
    node_host = str(node.name or "").strip().lower().rstrip(".")
    if node_host.endswith(".ts.net"):
        return f"https://{node_host}"
    if tailnet_dns_name and node_host and "." not in node_host:
        return f"https://{node_host}.{tailnet_dns_name}"
    return ""


def _node_version_label(raw_version: str | None, settings: Settings) -> str:
    version = str(raw_version or "").strip()
    if not version or version == "bootstrap":
        return "unknown"
    if "(" in version and ")" in version:
        return version
    normalized = (
        version if version == "dev" or version.startswith("v") else f"v{version}"
    )
    current = current_node_version_label(settings)
    if current.startswith(normalized) and "(" in current:
        return current
    return normalized


def _format_latency_value(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_latency_summary(
    summary: LatencySummary | None, latest_latency_ms: float | None
) -> str:
    if summary is not None:
        return (
            "1d avg / p95 / p99: "
            f"{_format_latency_value(summary.avg_ms)} / "
            f"{_format_latency_value(summary.p95_ms)} / "
            f"{_format_latency_value(summary.p99_ms)} ms"
        )
    if latest_latency_ms is not None:
        return f"latest: {_format_latency_value(latest_latency_ms)} ms"
    return "pending"


def _local_node_summary(settings: Settings) -> dict[str, str]:
    host_name = socket.gethostname() or "this-server"
    cpu_count = os.cpu_count()
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        physical_pages = int(os.sysconf("SC_PHYS_PAGES"))
        ram = _format_node_bytes(page_size * physical_pages)
    except (AttributeError, OSError, ValueError):
        ram = "unknown"
    try:
        disk = _format_node_bytes(shutil.disk_usage("/").total)
    except OSError:
        disk = "unknown"
    return {
        "id": "local",
        "name": host_name,
        "label": "the server (leader)",
        "role": "leader",
        "state": "healthy",
        "state_tone": "healthy",
        "ram": ram,
        "cpu": str(cpu_count) if cpu_count else "unknown",
        "disk": disk,
        "tailnet_ip": "this server",
        "tailnet_url": "",
        "latency": "local",
        "version": current_node_version_label(settings),
        "last_seen": "now",
        "removable": "false",
    }


def _format_relative_time(value: datetime) -> str:
    observed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    seconds = max(
        0, int((datetime.now(UTC) - observed.astimezone(UTC)).total_seconds())
    )
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _cluster_node_state_tone(state: object) -> str:
    normalized = str(state or "").strip().lower()
    if normalized == "healthy":
        return "healthy"
    if normalized in {"degraded", "offline", "error", "failed"}:
        return "degraded"
    if normalized in {"removed", "inactive"}:
        return "inactive"
    return "pending"


async def _cluster_nodes(
    session: AsyncSession, settings: Settings
) -> list[dict[str, str]]:
    rows = [_local_node_summary(settings)]
    nodes = await list_cluster_nodes(session)
    latency_summaries = await cluster_latency_summaries(
        session,
        [node.node_uid for node in nodes],
        since=datetime.now(UTC) - timedelta(days=1),
    )
    for node in nodes:
        details = _node_details_payload(node.details_json)
        last_seen = "never"
        if node.last_seen_at is not None:
            last_seen = _format_relative_time(node.last_seen_at)
        latency = _format_latency_summary(
            latency_summaries.get(node.node_uid), node.latency_ms
        )
        rows.append(
            {
                "id": node.node_uid,
                "name": node.name,
                "label": "Follower",
                "role": node.role,
                "state": node.state,
                "state_tone": _cluster_node_state_tone(node.state),
                "ram": _format_node_bytes(node.ram_bytes),
                "cpu": str(node.cpu_count) if node.cpu_count else "unknown",
                "disk": _format_node_bytes(node.disk_bytes),
                "tailnet_ip": node.tailnet_ip or node.wireguard_ip,
                "tailnet_url": _node_tailnet_url(node, details, settings),
                "latency": latency,
                "version": _node_version_label(node.version, settings),
                "last_seen": last_seen,
                "removable": "true",
            }
        )
    return rows


async def _cluster_nodes_for_context(
    session: AsyncSession, settings: Settings
) -> list[dict[str, str]]:
    if not settings.multi_node_enabled:
        return []
    return await _cluster_nodes(session, settings)


def _placement_node_status(
    *,
    mode: str,
    enabled: bool,
    node_uid: str,
    selected: bool,
    active_node_uid: str,
) -> str:
    if not enabled or not selected:
        return "off"
    if mode == PLACEMENT_MODE_LIVE:
        return "active"
    return "active" if node_uid == active_node_uid else "standby"


def _placement_node_display_role(node_uid: str) -> str:
    return "primary" if node_uid == LOCAL_NODE_UID else "follower"


def _inter_app_interface_targets(
    backend: Backend, all_backends: list[Backend]
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for candidate in all_backends:
        if candidate.id == backend.id or candidate.kind != "app":
            continue
        targets.append({"id": candidate.id, "name": candidate.name})
    return targets


def _interface_status_label(status: str) -> str:
    if status == INTERFACE_STATUS_ACCEPTED:
        return "confirmed"
    if status == INTERFACE_STATUS_REJECTED:
        return "rejected"
    return "pending"


def _interface_path_view(
    *,
    current_backend: str,
    other_backend: str,
    direction: str,
    inbound: bool,
) -> dict[str, str]:
    normalized = clean_interface_direction(direction)
    if normalized == INTERFACE_DIRECTION_BIDIRECTIONAL:
        return {
            "left": other_backend if inbound else current_backend,
            "right": current_backend if inbound else other_backend,
            "direction_label": "BIDIRECTIONAL <->",
        }
    if inbound:
        if normalized == INTERFACE_DIRECTION_IN:
            return {
                "left": current_backend,
                "right": other_backend,
                "direction_label": "OUTBOUND ->",
            }
        return {
            "left": other_backend,
            "right": current_backend,
            "direction_label": "INBOUND ->",
        }
    if normalized == INTERFACE_DIRECTION_IN:
        return {
            "left": other_backend,
            "right": current_backend,
            "direction_label": "INBOUND ->",
        }
    return {
        "left": current_backend,
        "right": other_backend,
        "direction_label": "OUTBOUND ->",
    }


def _build_output_placement_card(
    backend: Backend,
    cluster_nodes: list[dict[str, str]],
    all_backends: list[Backend],
    readiness_statuses: dict[str, ReplicaReadinessStatus] | None = None,
) -> dict[str, object]:
    placement = read_backend_placement(backend)
    active_node_uid = clean_node_uid(placement.active_node_uid)
    selected_node_uids = set(placement.selected_node_uids)
    readiness_statuses = readiness_statuses or {}
    rows: list[dict[str, object]] = []
    for node in cluster_nodes:
        node_uid = clean_node_uid(node.get("id", ""))
        selected = placement.enabled and node_uid in selected_node_uids
        readiness = readiness_statuses.get(node_uid)
        readiness_ready = (
            True
            if node_uid == LOCAL_NODE_UID
            else bool(readiness is not None and readiness.ready)
        )
        unavailable_reason = (
            ""
            if readiness_ready
            else (
                readiness.reason
                if readiness is not None
                else "replica readiness is missing"
            )
        )
        display_role = _placement_node_display_role(node_uid)
        rows.append(
            {
                "id": node_uid,
                "name": node.get("name") or node_uid,
                "tailnet_ip": node.get("tailnet_ip") or "unknown",
                "display_role": display_role,
                "setup_ready": readiness_ready,
                "unavailable_reason": unavailable_reason,
                "selected": selected,
                "active": selected and node_uid == active_node_uid,
                "status": _placement_node_status(
                    mode=placement.mode,
                    enabled=placement.enabled,
                    node_uid=node_uid,
                    selected=selected,
                    active_node_uid=active_node_uid,
                ),
            }
        )
    if not rows:
        rows.append(
            {
                "id": LOCAL_NODE_UID,
                "name": "leader",
                "tailnet_ip": "this server",
                "display_role": "primary",
                "setup_ready": True,
                "unavailable_reason": "",
                "selected": False,
                "active": False,
                "status": "off",
            }
        )
    interface_targets = _inter_app_interface_targets(backend, all_backends)
    targets_by_id = {
        int(target["id"]): str(target["name"]) for target in interface_targets
    }
    interfaces = [
        {
            "name": item.name,
            "port": "auto",
            "target_backend_id": item.target_backend_id,
            "target_backend_name": targets_by_id[item.target_backend_id],
            "status": clean_interface_status(item.status),
            "direction": clean_interface_direction(item.direction),
            **_interface_path_view(
                current_backend=backend.name,
                other_backend=targets_by_id[item.target_backend_id],
                direction=item.direction,
                inbound=False,
            ),
            "status_label": _interface_status_label(
                clean_interface_status(item.status)
            ),
        }
        for item in read_inter_app_interfaces(backend)
        if item.target_backend_id in targets_by_id
    ]
    inbound_interfaces = [
        {
            "name": item.name,
            "source_backend_id": item.source_backend_id,
            "source_backend_name": item.source_backend_name,
            "status": clean_interface_status(item.status),
            "direction": clean_interface_direction(item.direction),
            **_interface_path_view(
                current_backend=backend.name,
                other_backend=item.source_backend_name,
                direction=item.direction,
                inbound=True,
            ),
        }
        for item in inbound_inter_app_interfaces(backend, all_backends)
        if clean_interface_status(item.status) != INTERFACE_STATUS_REJECTED
    ]
    return {
        "enabled": placement.enabled,
        "mode": placement.mode if placement.enabled else PLACEMENT_MODE_FAILOVER,
        "active_node_uid": active_node_uid,
        "selected_node_uids": list(placement.selected_node_uids),
        "interface_targets": interface_targets,
        "interfaces": interfaces,
        "inbound_interfaces": inbound_interfaces,
        "interface_statuses": [
            {"value": INTERFACE_STATUS_PENDING, "label": "pending"},
            {"value": INTERFACE_STATUS_ACCEPTED, "label": "confirm"},
            {"value": INTERFACE_STATUS_REJECTED, "label": "reject"},
        ],
        "nodes": rows,
    }


def _operator_validation_error(exc: ValidationError) -> str:
    message = str(exc)
    if message.startswith("hostname"):
        message = f"{message}. {DOMAIN_INPUT_HINT}"
    elif message.startswith("tailnet path") or message.startswith(
        "invalid tailnet path"
    ):
        message = f"{message}. {TAILNET_PATH_HINT}"
    elif message.startswith("tailnet service") or message.startswith(
        "invalid tailnet service"
    ):
        message = f"{message}. Use a Tailscale service name like app-dev."
    elif message.startswith("static_root"):
        message = f"{message}. {STATIC_ROOT_HINT}"
    elif message.startswith("volume source path") or message.startswith(
        "volume target path"
    ):
        message = f"{message}. {VOLUME_BOUNDARY_HINT}"
    return _operator_coded_error(message, ErrorCode.VALIDATION_FAILED)


def _event_tone(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "success":
        return "success"
    if normalized == "warn":
        return "warn"
    if normalized == "error":
        return "error"
    return ""


_EVENT_VISIBLE_KEY_EXCLUDE = {
    "affects_all",
    "backend",
    "backend_name",
    "category",
    "kind",
    "message",
    "scope",
    "source",
    "status",
    "summary",
}


def _event_details(event: ControlEvent) -> dict[str, object]:
    try:
        details = json.loads(event.details_json or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    return details


def _event_visible_pairs(
    event: ControlEvent, details: dict[str, object], *, limit: int = 5
) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_pair(label: object, value: object) -> None:
        if len(pairs) >= limit:
            return
        normalized_label = str(label or "").strip()
        normalized_key = normalized_label.lower().replace("-", "_").replace(" ", "_")
        if (
            not normalized_label
            or normalized_key in _EVENT_VISIBLE_KEY_EXCLUDE
            or normalized_key in seen
        ):
            return
        if normalized_key == "path" or normalized_key.endswith("_path"):
            return
        if normalized_key.endswith("_id") and normalized_key[:-3] in seen:
            return
        if isinstance(value, (dict, list, tuple, set)):
            return
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return
        if normalized_key.endswith("size_bytes"):
            try:
                normalized_value = _format_bytes(int(normalized_value))
            except ValueError:
                pass
        seen.add(normalized_key)
        pairs.append(
            {"label": normalized_label.replace("_", " "), "value": normalized_value}
        )

    for subevent in event.subevents:
        add_pair(subevent.get("label"), subevent.get("value"))
    for label, value in details.items():
        add_pair(label, value)
    return pairs


def _event_detail_dump(event: ControlEvent, details: dict[str, object]) -> str:
    payload = {
        "kind": event.kind,
        "severity": event.severity,
        "scope": event.scope,
        "source": event.source,
        "backend": event.backend_name,
        "affects_all": bool(event.affects_all),
        "related_backends": event.related_backends,
        "summary": event.summary,
        "key_values": event.subevents,
        "details": details,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _event_rows(events: list[ControlEvent]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for event in events:
        details = _event_details(event)
        rows.append(
            {
                "summary": event.summary,
                "created_at": event.created_at.strftime("%b %d %H:%M")
                if event.created_at
                else "-",
                "created_at_raw": _timestamp_data_value(event.created_at),
                "tone": _event_tone(event.severity),
                "severity": event.severity or "info",
                "pairs": _event_visible_pairs(event, details),
                "details": _event_detail_dump(event, details),
            }
        )
    return rows


def _planned_backup_coverage_summary(backend: Backend, settings: Settings) -> str:
    paths: list[str] = []
    if backend.kind == "static" and backend.static_root:
        paths.append(str(backend.static_root))
    if backend.kind == "app":
        paths.append(str(app_sandbox_dir(settings, backend.name)))
        try:
            volumes = parse_volumes_json(backend.volumes_json)
        except ValidationError:
            volumes = []
        for volume in volumes:
            source, _, _target = volume.partition(":")
            if source.startswith("/"):
                paths.append(source)
    return _summarize_paths(paths) or "metadata only"


def _summarize_paths(paths: list[str], *, limit: int = 2) -> str:
    cleaned = list(dict.fromkeys(path for path in paths if path))
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return f"{', '.join(cleaned[:limit])} +{len(cleaned) - limit} more"


def _utc_hour_for_local_display(hour: int) -> str:
    now = datetime.now(UTC)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _output_load_meter(
    percent: float | None,
    *,
    enabled: bool,
    kind: str,
    service_state: str | None = None,
) -> dict[str, object]:
    if not enabled:
        return {
            "label": "OFF",
            "percent": 0.0,
            "tone": "inactive",
            "available": False,
            "compact": False,
        }
    if kind not in {"app", "shield"}:
        return {
            "label": "-",
            "percent": 0.0,
            "tone": "inactive",
            "available": False,
            "compact": True,
        }
    if isinstance(percent, (int, float)):
        normalized = round(max(0.0, min(100.0, float(percent))), 1)
        tone = (
            "critical"
            if normalized >= 90
            else ("warn" if normalized >= 75 else "active")
        )
        return {
            "label": f"{normalized:.1f}%",
            "percent": normalized,
            "tone": tone,
            "available": True,
            "compact": False,
        }
    state = str(service_state or "").strip().lower()
    if state in {"active", "activating", "reloading"}:
        return {
            "label": "collecting",
            "percent": 0.0,
            "tone": "queued",
            "available": False,
            "compact": False,
        }
    return {
        "label": "",
        "percent": 0.0,
        "tone": "inactive",
        "available": False,
        "compact": True,
    }


def _output_runtime_badge(
    *,
    enabled: bool,
    kind: str,
    runtime_diagnostics: dict[str, object] | None,
    unit_data: dict[str, object],
    unit_ok: bool,
) -> dict[str, str]:
    if not enabled:
        return {"value": "not enabled", "tone": "inactive"}
    if kind == "static":
        return {"value": "healthy", "tone": "success"}
    if kind == "shield":
        active_state = str(unit_data.get("ActiveState") or "").strip().lower()
        if not active_state:
            return {"value": "unknown", "tone": "queued"}
        if active_state != "active" or not unit_ok:
            return {"value": "unhealthy", "tone": "error"}
        return {"value": "healthy", "tone": "success"}
    if runtime_diagnostics is not None:
        diagnosis = str(runtime_diagnostics.get("diagnosis") or "").strip().lower()
        if diagnosis == "healthy":
            return {"value": "healthy", "tone": "success"}
        if diagnosis == "app_unmonitored":
            return {"value": "unmonitored", "tone": "warn"}
        if diagnosis in {
            "backend_observation_deferred",
            "backend_observation_unavailable",
        }:
            return {"value": "unknown", "tone": "queued"}
        if diagnosis:
            return {"value": "unhealthy", "tone": "error"}
    active_state = str(unit_data.get("ActiveState") or "").strip().lower()
    if active_state == "active":
        return {"value": "healthy", "tone": "success"}
    if active_state and active_state != "active" and not unit_ok:
        return {"value": "unhealthy", "tone": "error"}
    return {"value": "unhealthy", "tone": "error"}


def _is_tailscale_host(hostname: str) -> bool:
    normalized = hostname.strip().rstrip(".").lower()
    if not normalized:
        return False
    if normalized.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _ssh_destination_for_request(request: Request) -> str:
    hostname = (request.url.hostname or "").strip()
    if not hostname:
        return "SERVER_IP"
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        resolved: list[str] = []
        try:
            addrinfos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return "SERVER_IP"
        for _family, _socktype, _proto, _canonname, sockaddr in addrinfos:
            host = sockaddr[0] if sockaddr else ""
            if not host:
                continue
            try:
                parsed = ipaddress.ip_address(host)
            except ValueError:
                continue
            if parsed.version == 4:
                return str(parsed)
            resolved.append(str(parsed))
        return resolved[0] if resolved else "SERVER_IP"


def _page_ssh_destination(request: Request) -> str:
    # Keep output first paint free of synchronous DNS lookups.
    host = (request.url.hostname or "").strip()
    return host or "SERVER_IP"


def _backend_ssh_destination(
    settings: Settings,
    request: Request,
    *,
    resolve_dns: bool,
) -> str:
    hostname = (request.url.hostname or "").strip()
    if hostname and _is_tailscale_host(hostname):
        return hostname
    tailnet_host = configured_tailnet_admin_host(settings)
    if tailnet_host:
        return tailnet_host
    configured = str(settings.ssh_advertise_host or "").strip()
    if configured:
        return configured
    if not hostname:
        return "SERVER_IP"
    return (
        _ssh_destination_for_request(request)
        if resolve_dns
        else _page_ssh_destination(request)
    )


def _backend_operator_commands(
    backend: Backend, request: Request
) -> list[dict[str, str]]:
    if backend.kind == "app" and backend.enabled is False:
        return []
    host = _ssh_destination_for_request(request)
    payload = build_backend_llm_help_payload(
        backend,
        host=host,
        llm_help_url=_output_llm_help_url(request, backend.id),
    )
    return build_backend_operator_commands(payload)


def _backend_operator_commands_for_page(
    backend: Backend,
    *,
    settings: Settings,
    host: str,
    llm_help_url: str,
) -> list[dict[str, str]]:
    if backend.kind == "app" and backend.enabled is False:
        return []
    payload = build_backend_llm_help_payload(
        backend,
        host=host,
        llm_help_url=llm_help_url,
    )
    commands = build_backend_operator_commands(payload)
    loaded_inputs = backend.__dict__.get("inputs")
    if isinstance(loaded_inputs, list):
        for item in sorted(
            loaded_inputs,
            key=lambda entry: (str(entry.kind or ""), str(entry.hostname or "")),
        ):
            if str(item.kind or "domain").strip().lower() != "tailnet_service":
                continue
            url = tailscale_service_url(str(item.hostname or ""), settings)
            if not url:
                continue
            commands.insert(
                0,
                {
                    "label": "tailscale url",
                    "command": url,
                    "note": "Root-path tailnet URL for this output.",
                },
            )
    return commands


def _host_llm_help_url(request: Request) -> str:
    try:
        return str(request.url_for("host_llm_help"))
    except RuntimeError:
        host = request.headers.get("host") or request.url.netloc or "SERVER_IP"
        scheme = request.url.scheme or "http"
        return f"{scheme}://{host}/llm.txt"


def _output_llm_help_url(request: Request, backend_id: int | None) -> str:
    if isinstance(backend_id, int):
        try:
            return str(request.url_for("output_llm_help", backend_id=backend_id))
        except RuntimeError:
            pass
    host = request.headers.get("host") or request.url.netloc or "SERVER_IP"
    scheme = request.url.scheme or "http"
    backend_segment = str(backend_id) if isinstance(backend_id, int) else "BACKEND_ID"
    return f"{scheme}://{host}/outputs/{backend_segment}/llm.txt"


def _parse_memory_limit_bytes(raw: object) -> int | None:
    if not isinstance(raw, str):
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
    try:
        return int(normalized)
    except ValueError:
        return None


def _format_memory_limit(value: object) -> str:
    parsed = _parse_memory_limit_bytes(value)
    if parsed is None:
        return str(value or "-")
    return _format_bytes(parsed)


def _parse_percent_value(raw: object) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().rstrip("%")
    if normalized == "":
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _format_cpu_limit(
    raw_quota: object,
    *,
    host_cpu_count: object | None = None,
    entitlement_percent_of_host: object | None = None,
) -> str:
    cores = None
    quota_percent = _parse_percent_value(raw_quota)
    if quota_percent is not None:
        cores = quota_percent / 100.0
    host_count = (
        int(host_cpu_count)
        if isinstance(host_cpu_count, int) and host_cpu_count > 0
        else None
    )
    host_percent = (
        quota_percent / host_count
        if quota_percent is not None and host_count is not None
        else None
    )
    if host_percent is None and isinstance(entitlement_percent_of_host, (int, float)):
        host_percent = float(entitlement_percent_of_host)
    if host_percent is None and quota_percent is not None:
        host_percent = quota_percent
    if host_percent is not None:
        return f"{host_percent:.0f}% of total CPU"
    if quota_percent is not None:
        return f"{quota_percent:.0f}% quota"
    if cores is None:
        return str(raw_quota or "-")
    return str(raw_quota or "-")


def _format_rate(value_bps: object) -> str:
    if not isinstance(value_bps, (int, float)):
        return "-"
    bits_per_sec = max(0.0, float(value_bps) * 8.0)
    if bits_per_sec < 1000:
        return f"{bits_per_sec:.0f} bps"
    for unit in ["Kbps", "Mbps", "Gbps"]:
        bits_per_sec /= 1000.0
        if bits_per_sec < 1000 or unit == "Gbps":
            return f"{bits_per_sec:.1f} {unit}"
    return f"{bits_per_sec:.1f} Gbps"


def _humanize_diagnostic_code(value: str | None) -> str:
    normalized = str(value or "").strip().replace("_", " ")
    return normalized or "unknown"


def _configured_health_endpoint_label(
    *,
    healthcheck_mode: str,
    healthcheck_path: str,
    healthcheck_host_header: str,
) -> str:
    mode = str(healthcheck_mode or "http").strip().lower()
    if mode == "auto":
        mode = "http"
    path = str(healthcheck_path or "-").strip() or "-"
    host = str(healthcheck_host_header or "-").strip() or "-"
    if mode == "http":
        if host != "-":
            return f"http {path} @ {host}"
        return f"http {path}"
    if mode == "tcp":
        return "tcp internal app port"
    if mode == "none":
        return "none"
    if host != "-":
        return f"http {path} @ {host}"
    return f"http {path}"


def _runtime_health_view(
    *,
    backend: Backend,
    healthcheck_mode: str,
    healthcheck_path: str,
    healthcheck_host_header: str,
    runtime_diagnostics: dict[str, object] | None,
) -> dict[str, object]:
    health_target = _configured_health_endpoint_label(
        healthcheck_mode=healthcheck_mode,
        healthcheck_path=healthcheck_path,
        healthcheck_host_header=healthcheck_host_header,
    )
    if not backend.enabled:
        return {
            "label": "NOT ENABLED",
            "tone": "inactive",
            "summary": "NOT ENABLED",
            "detail": "output is disabled",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "- / -",
            "health_target": health_target,
        }
    if backend.kind == "shield":
        diagnosis = str((runtime_diagnostics or {}).get("diagnosis") or "")
        if diagnosis != "healthy":
            return {
                "label": "unknown" if not diagnosis else "UNHEALTHY",
                "tone": "queued" if not diagnosis else "error",
                "summary": "unknown" if not diagnosis else "UNHEALTHY",
                "detail": "shield runtime health not loaded"
                if not diagnosis
                else "shield health endpoint unreachable",
                "alert": None,
                "sandbox_status": "-",
                "guest_status": "-",
                "app_handoff_status": "-",
                "runtime_state": "shield runtime",
                "health_target": health_target,
            }
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "shield access gate runtime",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "shield runtime",
            "health_target": health_target,
        }
    if backend.kind != "app":
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "static output",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "-",
            "health_target": health_target,
        }
    if runtime_diagnostics is None:
        return {
            "label": "unknown",
            "tone": "queued",
            "summary": "unknown",
            "detail": "live runtime diagnosis not loaded",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "- / -",
            "health_target": health_target,
        }

    diagnostics = runtime_diagnostics
    diagnosis = str(diagnostics.get("diagnosis") or "").strip().lower()
    sandbox_status = str(diagnostics.get("sandbox_status") or "-").strip() or "-"
    guest_status = str(diagnostics.get("guest_status") or "-").strip() or "-"
    app_handoff_status = (
        str(diagnostics.get("app_handoff_status") or "-").strip() or "-"
    )
    container_state = diagnostics.get("container_state")
    runtime_state = f"guest {guest_status} · handoff {app_handoff_status}"
    if isinstance(container_state, dict):
        active_state = str(container_state.get("ActiveState") or "-").strip() or "-"
        sub_state = str(container_state.get("SubState") or "-").strip() or "-"
        runtime_state = (
            f"guest {guest_status} · handoff {app_handoff_status} · "
            f"service {active_state} / {sub_state}"
        )
    primary_error = ""
    for key in (
        "app_handoff_error",
        "loopback_error",
        "private_error",
        "inspect_error",
    ):
        value = str(diagnostics.get(key) or "").strip()
        if value:
            primary_error = value.splitlines()[0].strip()
            break

    if diagnosis == "healthy":
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "configured health endpoint reachable",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    if diagnosis == "app_unmonitored":
        return {
            "label": "UNMONITORED",
            "tone": "warn",
            "summary": "UNMONITORED",
            "detail": "app health check is disabled",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    if diagnosis in {
        "backend_observation_deferred",
        "backend_observation_unavailable",
    }:
        return {
            "label": "UNKNOWN",
            "tone": "warn",
            "summary": "UNKNOWN",
            "detail": "guest observation is temporarily unavailable",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    summary = "unhealthy"
    detail = "runtime state unknown"
    title = "Output unhealthy"
    if diagnosis == "app_service_down":
        summary = "unhealthy (configured health endpoint)"
        detail = "configured health endpoint unreachable"
    elif diagnosis == "sandbox_missing":
        summary = "down"
        detail = "guest missing"
        title = "Guest missing"
    elif diagnosis == "guest_init_broken":
        summary = "guest init broken"
        detail = "guest shell is up but systemd is not ready"
        title = "Guest init broken"
    elif diagnosis == "sandbox_publish_broken":
        summary = "publish broken"
        detail = "guest is up but CNC publish handoff is broken"
        title = "Publish handoff broken"
    elif diagnosis:
        summary = _humanize_diagnostic_code(diagnosis)
        detail = _humanize_diagnostic_code(diagnosis)

    alert_rows: list[tuple[str, str]] = [("status", summary)]
    if health_target != "none":
        alert_rows.append(("health check", health_target))
    if primary_error:
        alert_rows.append(("error", primary_error))
    issue_list = diagnostics.get("issues")
    if isinstance(issue_list, list):
        issue_label = ", ".join(
            _humanize_diagnostic_code(item) for item in issue_list if item
        )
        if issue_label:
            alert_rows.append(("signals", issue_label))

    return {
        "label": "UNHEALTHY",
        "tone": "error",
        "summary": "UNHEALTHY",
        "detail": detail,
        "alert": {
            "title": title,
            "summary": detail,
            "rows": alert_rows,
            "tone": "error",
            "pill": "unhealthy",
        },
        "sandbox_status": sandbox_status,
        "guest_status": guest_status,
        "app_handoff_status": app_handoff_status,
        "runtime_state": runtime_state,
        "health_target": health_target,
    }


def _backend_runtime_service_name(
    backend: Backend | str, *, kind: str | None = None
) -> str:
    backend_name = backend.name if isinstance(backend, Backend) else str(backend or "")
    backend_kind = (backend.kind if isinstance(backend, Backend) else kind) or ""
    if str(backend_kind).strip().lower() == "shield":
        return SHIELD_CONTAINER_SERVICE
    return container_name(backend_name)


def _runtime_diagnostics_from_status_payload(
    payload: dict[str, object] | None,
    backend_name: str,
    *,
    kind: str = "app",
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    expected_service = _backend_runtime_service_name(backend_name, kind=kind)
    for service in services:
        if not isinstance(service, dict):
            continue
        if str(service.get("service") or "").strip() != expected_service:
            continue
        data = service.get("data")
        if not isinstance(data, dict):
            return None
        issues_raw = str(data.get("RuntimeIssues") or "").strip()
        issues = [
            item.strip()
            for item in issues_raw.split(",")
            if item.strip() and item.strip() != "-"
        ]
        diagnostics: dict[str, object] = {
            "backend": backend_name,
            "diagnosis": str(data.get("RuntimeDiagnosis") or "").strip().lower(),
            "sandbox_status": str(data.get("SandboxStatus") or "-").strip() or "-",
            "guest_status": str(data.get("GuestStatus") or "-").strip() or "-",
            "app_handoff_status": str(data.get("AppHandoffStatus") or "-").strip()
            or "-",
            "private_ip": str(data.get("PrivateAddress") or "").strip(),
            "private_reachable": str(data.get("PrivateReachable") or "").strip().lower()
            == "yes",
            "loopback_reachable": str(data.get("ProxyReachable") or "").strip().lower()
            == "yes",
            "issues": issues,
            "container_state": {
                "ActiveState": str(data.get("ActiveState") or "").strip(),
                "SubState": str(data.get("SubState") or "").strip(),
            },
        }
        error_text = str(service.get("error") or "").strip()
        if error_text:
            diagnostics["app_handoff_error"] = error_text
        return diagnostics
    return None


async def _collect_output_runtime_diagnostics_for_ui(
    backend: Backend,
    settings: Settings,
) -> dict[str, object] | None:
    if backend.kind != "app" or not backend.enabled:
        return None
    cache_key = (backend.id, backend.name)
    now = time.monotonic()
    _prune_runtime_diagnostics_cache(now)
    cached = _RUNTIME_DIAGNOSTICS_CACHE.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if now - cached_at <= UI_RUNTIME_SIGNAL_CACHE_TTL_SEC:
            return dict(payload)

    task = _RUNTIME_DIAGNOSTICS_TASKS.get(cache_key)
    if task is not None and task.done():
        _cache_completed_runtime_diagnostics_task(cache_key, backend.name, task)
        cached_after_task = _RUNTIME_DIAGNOSTICS_CACHE.get(cache_key)
        return dict(cached_after_task[1]) if cached_after_task is not None else None
    if task is None:
        task = asyncio.create_task(
            asyncio.to_thread(collect_app_backend_diagnostics, backend, settings)
        )
        _RUNTIME_DIAGNOSTICS_TASKS[cache_key] = task
        task.add_done_callback(
            lambda completed_task, key=cache_key, name=backend.name: (
                _cache_completed_runtime_diagnostics_task(key, name, completed_task)
            )
        )
    try:
        payload = await asyncio.wait_for(
            asyncio.shield(task), timeout=UI_RUNTIME_SIGNAL_TIMEOUT_SEC
        )
        _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
        _RUNTIME_DIAGNOSTICS_CACHE[cache_key] = (time.monotonic(), dict(payload))
        _prune_runtime_diagnostics_cache()
        return dict(payload)
    except TimeoutError:
        logger.warning(
            "ui.output_runtime_signals.timeout",
            backend=backend.name,
            timeout_sec=UI_RUNTIME_SIGNAL_TIMEOUT_SEC,
        )
        raise RuntimeDiagnosticsPending from None
    except Exception as exc:
        _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
        logger.warning(
            "ui.output_runtime_signals.failed",
            backend=backend.name,
            error=str(exc),
        )
        return None


def _service_metrics_from_status_payload(
    payload: dict[str, object] | None,
    backend: Backend,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    expected_service = _backend_runtime_service_name(backend)
    for service in services:
        if not isinstance(service, dict):
            continue
        if str(service.get("service") or "").strip() != expected_service:
            continue
        metrics = service.get("metrics")
        if isinstance(metrics, dict):
            return metrics
        return None
    return None


def _memory_soft_limit_percent_for_profile(
    backend: Backend, base_profile: ResourceProfile
) -> float | None:
    if str(backend.kind or "").lower() != "app":
        return None
    backend_profile = backend_resource_profile(backend, base_profile)
    soft_bytes = _parse_memory_limit_bytes(backend_profile.memory_high)
    hard_bytes = _parse_memory_limit_bytes(backend_profile.memory_max)
    if soft_bytes is None or hard_bytes is None or hard_bytes <= 0:
        return None
    return round(max(0.0, min(100.0, soft_bytes * 100.0 / hard_bytes)), 2)


def _memory_limit_bytes_for_profile(
    backend: Backend, base_profile: ResourceProfile
) -> dict[str, int | None]:
    if str(backend.kind or "").lower() != "app":
        return {"soft": None, "hard": None}
    backend_profile = backend_resource_profile(backend, base_profile)
    return {
        "soft": _parse_memory_limit_bytes(backend_profile.memory_high),
        "hard": _parse_memory_limit_bytes(backend_profile.memory_max),
    }


def _format_metric_data_size(value: object) -> str:
    amount = float(value) if isinstance(value, (int, float)) else None
    if amount is None or amount < 0:
        return "n/a"
    if amount >= 1024**3:
        return f"{amount / 1024**3:.1f} GB"
    if amount >= 1024**2:
        return f"{amount / 1024**2:.0f} MB"
    if amount >= 1024:
        return f"{amount / 1024:.0f} KB"
    return f"{amount:.0f} B"


def _resource_size_matrix_rows(
    base_profile: ResourceProfile,
    *,
    host_cpu_count: object | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for size in RESOURCE_SIZES:
        profile = backend_resource_profile(
            Backend(
                name=f"resource-{size}",
                kind="app",
                resource_mode="auto",
                resource_size=size,
            ),
            base_profile,
        )
        rows.append(
            {
                "size": size,
                "memory_target": _format_memory_limit(profile.memory_high),
                "memory_cap": _format_memory_limit(profile.memory_max),
                "cpu_limit": _format_cpu_limit(
                    profile.cpu_quota,
                    host_cpu_count=host_cpu_count,
                    entitlement_percent_of_host=profile.cpu_entitlement_percent_of_host,
                ),
            }
        )
    return rows


def _output_signal_cards(detail: dict[str, object]) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    top_health = str(detail.get("runtime_health_label") or "").strip().upper()
    if top_health not in {"HEALTHY", "NOT ENABLED", "UNHEALTHY"}:
        return cards

    status_checks: list[dict[str, object]] = []
    diagnostics = detail.get("runtime_diagnostics")
    if top_health != "NOT ENABLED" and isinstance(diagnostics, dict):
        diagnosis = str(diagnostics.get("diagnosis") or "").strip().lower()
        health_target = str(detail.get("configured_health_target") or "").strip()
        if (
            health_target
            and health_target != "-"
            and health_target != "none"
            and diagnosis in {"healthy", "app_service_down"}
        ):
            status_checks.append(
                {"label": "health check", "ok": diagnosis == "healthy"}
            )

        route_checks: list[bool] = []
        loopback_publish_present = diagnostics.get("loopback_publish_present")
        loopback_reachable = diagnostics.get("loopback_reachable")
        if isinstance(loopback_publish_present, bool):
            route_checks.append(loopback_publish_present)
        if isinstance(loopback_reachable, bool):
            route_checks.append(loopback_reachable)
        if route_checks:
            status_checks.append({"label": "CNC route", "ok": all(route_checks)})

        container_state = diagnostics.get("container_state")
        container_ok: bool | None = None
        if isinstance(container_state, dict):
            active_state = str(container_state.get("ActiveState") or "").strip().lower()
            if active_state:
                container_ok = active_state == "active"
        if container_ok is None and isinstance(
            diagnostics.get("container_exists"), bool
        ):
            container_ok = bool(diagnostics.get("container_exists"))
        if container_ok is not None:
            status_checks.append({"label": "container", "ok": container_ok})

    cards.append(
        {
            "label": "status",
            "value": top_health,
            "checks": status_checks,
            "note": "Top-level output health.",
        }
    )
    cards.extend(
        [
            {
                "label": "target",
                "value": str(detail.get("target", "-")),
                "note": "The operator-facing destination for this output.",
            },
            {
                "label": str(detail.get("exposure_label", "runtime target")),
                "value": str(detail.get("exposure_value", "-")),
                "note": "How CNC publishes this backend onto loopback or nginx.",
            },
            {
                "label": str(detail.get("runtime_label", "runtime")),
                "value": str(detail.get("service_unit", "-")),
                "note": "Guest baseline CNC seeds for this output.",
            },
        ]
    )
    if str(detail.get("kind") or "") != "shield":
        cards.append(
            {
                "label": "health check",
                "value": str(detail.get("configured_health_target", "-")),
                "note": "Configured probe used to decide whether this backend is healthy.",
            }
        )
    return cards


def _input_value(item: Input) -> str:
    return str(item.hostname or "")


def _output_info_rows(
    backend: Backend, detail: dict[str, object]
) -> list[tuple[object, ...]]:
    timestamp_rows = [
        (
            "created",
            detail.get("created_at", "-"),
            {"raw": detail.get("created_at_raw", "")},
        ),
        (
            "updated",
            detail.get("updated_at", "-"),
            {"raw": detail.get("updated_at_raw", "")},
        ),
    ]
    if backend.kind == "shield":
        return [
            ("kind", backend.kind),
            ("runtime", detail.get("service_unit", "-")),
            ("loopback publish", detail.get("exposure_value", "-")),
            ("public access", detail.get("target", "-")),
            *timestamp_rows,
        ]
    return [
        ("kind", backend.kind),
        ("placement", _backend_placement_label(backend)),
        ("resource size", detail.get("resource_size_label", "-")),
        ("handoff port", backend.port or "-"),
        ("internal app port", backend.handoff_port),
        ("memory target", _format_memory_limit(detail.get("resource_memory_high"))),
        ("memory cap", _format_memory_limit(detail.get("resource_memory_max"))),
        (
            "cpu limit",
            _format_cpu_limit(
                detail.get("resource_cpu_quota", "-"),
                host_cpu_count=detail.get("resource_host_cpu_count"),
                entitlement_percent_of_host=detail.get(
                    "resource_cpu_entitlement_percent"
                ),
            ),
        ),
        *timestamp_rows,
    ]


def _backend_placement_label(backend: Backend) -> str:
    node_uid = str(getattr(backend, "placement_node_uid", "") or "").strip()
    return "leader" if not node_uid or node_uid == "local" else node_uid


def _input_kind(item: Input) -> str:
    return ensure_input_kind(str(item.kind or "domain"))


def _input_kind_label(kind: str) -> str:
    if kind == "domain":
        return "domain"
    if kind == "shield":
        return "shield"
    if kind == "tailnet_service":
        return "tailscale subdomain"
    return "tailnet path"


def _tailnet_service_route_label(input_value: str, settings: Settings) -> str:
    return (
        tailscale_service_url(input_value, settings)
        or f"tailscale subdomain {input_value}"
    )


def _debug_kv_dump(rows: list[tuple[str, object]]) -> str:
    lines: list[str] = []
    for key, value in rows:
        try:
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, indent=2, sort_keys=True)
            else:
                rendered = str(value)
        except (TypeError, ValueError):
            rendered = repr(value)
        if "\n" in rendered:
            lines.append(f"{key}:")
            lines.extend(rendered.splitlines())
            continue
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


async def _next_clone_backend_name(session: AsyncSession, source_name: str) -> str:
    counter = 1
    while True:
        prefix = "clone-"
        suffix = "" if counter == 1 else f"-{counter}"
        reserved_length = len(prefix) + len(suffix)
        max_base_length = 63 - reserved_length
        trimmed = source_name[:max_base_length].rstrip("-") or "backend"
        candidate = ensure_backend_name(f"{prefix}{trimmed}{suffix}")
        existing = (
            await session.execute(
                select(Backend.id).where(Backend.name == candidate).limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            return candidate
        counter += 1


async def _next_clone_backend_port(
    session: AsyncSession, source_port: int | None, settings: Settings
) -> int | None:
    existing_ports = {
        port
        for port in (
            await session.execute(
                select(Backend.port).where(
                    Backend.kind == "app", Backend.port.is_not(None)
                )
            )
        ).scalars()
        if isinstance(port, int)
    }
    existing_ports.add(NETDATA_PORT)
    candidate = (
        source_port + 1
        if isinstance(source_port, int) and source_port > 0
        else settings.port_range_start
    )
    candidate = max(candidate, settings.port_range_start)
    while candidate <= settings.port_range_end and (
        candidate in existing_ports or not is_loopback_port_free(candidate)
    ):
        candidate += 1
    if candidate > settings.port_range_end:
        return None
    return candidate


def _build_service_cards(
    service_rows: list[dict[str, object]],
) -> tuple[dict[str, dict[str, object]], int]:
    service_map = {
        service.get("service"): service
        for service in service_rows
        if isinstance(service, dict) and service.get("service")
    }
    services_ok = 0
    for service in service_rows:
        data = service.get("data") or {}
        if service.get("ok") and data.get("ActiveState") == "active":
            services_ok += 1
    return service_map, services_ok


def _build_nginx_card(nginx_status: dict[str, object]) -> tuple[str, dict[str, object]]:
    nginx_data = nginx_status.get("data") or {}
    nginx_active_state = str(
        nginx_data.get("ActiveState")
        or ("error" if not nginx_status.get("ok") else "unknown")
    )
    return nginx_active_state, {
        "tone": _tone_for_value(
            nginx_active_state,
            success_values={"active"},
            queued_values={"activating", "reloading"},
            error_values={"failed", "inactive", "deactivating"},
            ok=bool(nginx_status.get("ok")),
        ),
        "active_state": nginx_active_state,
        "sub_state": str(nginx_data.get("SubState") or "-"),
        "since": nginx_data.get("ActiveEnterTimestamp") or None,
        "error": nginx_status.get("error") or None,
    }


def _build_runtime_cards(
    service_rows: list[dict[str, object]],
    enabled_app_backends: list[Backend],
) -> tuple[list[dict[str, object]], list[float], list[int]]:
    runtime_backends_by_service = {
        container_name(backend.name): backend for backend in enabled_app_backends
    }
    runtime_cards: list[dict[str, object]] = []
    runtime_cpu_host_values: list[float] = []
    runtime_memory_current_values: list[int] = []
    for service in service_rows:
        service_name_raw = str(service.get("service") or "")
        backend = runtime_backends_by_service.get(service_name_raw)
        if backend is None:
            continue
        data = service.get("data") or {}
        metrics = service.get("metrics") or {}
        cpu_host_percent = metrics.get("cpu_percent_of_host")
        memory_current_bytes = metrics.get("memory_current_bytes")
        if isinstance(cpu_host_percent, (int, float)):
            runtime_cpu_host_values.append(float(cpu_host_percent))
        if isinstance(memory_current_bytes, int):
            runtime_memory_current_values.append(memory_current_bytes)
        active_state = str(
            data.get("ActiveState") or ("error" if not service.get("ok") else "unknown")
        )
        sub_state = str(data.get("SubState") or "-")
        tone = _tone_for_value(
            active_state,
            success_values={"active"},
            queued_values={"activating", "reloading"},
            error_values={"failed", "inactive", "deactivating"},
            ok=bool(service.get("ok")),
        )
        target = (
            f"127.0.0.1:{backend.port}" if backend.port else "port assigned on save"
        )
        bootstrap_state = str(data.get("BootstrapState") or "unknown")
        private_reachable = str(data.get("PrivateReachable") or "unknown")
        proxy_reachable = str(data.get("ProxyReachable") or "unknown")
        private_address = str(data.get("PrivateAddress") or "-")
        bootstrap_tone = _tone_for_value(
            bootstrap_state,
            success_values={"yes", "active", "succeeded"},
            queued_values={"pending", "activating", "reloading", "queued", "running"},
            error_values={"no", "error", "failed", "inactive", "missing"},
        )
        private_tone = _tone_for_value(
            private_reachable,
            success_values={"yes", "active", "succeeded"},
            queued_values={"pending", "activating", "reloading", "queued", "running"},
            error_values={"no", "error", "failed", "inactive", "missing"},
        )
        loopback_tone = _tone_for_value(
            proxy_reachable,
            success_values={"yes", "active", "succeeded"},
            queued_values={"pending", "activating", "reloading", "queued", "running"},
            error_values={"no", "error", "failed", "inactive", "missing"},
        )

        def _check_value(raw_value: str, *, good_label: str, bad_label: str) -> str:
            return (
                good_label
                if raw_value.lower() in {"yes", "active", "succeeded"}
                else bad_label
            )

        checks = [
            {
                "label": "bootstrap",
                "value": _check_value(
                    bootstrap_state, good_label="ready", bad_label=bootstrap_state
                ),
                "tone": bootstrap_tone,
            },
            {
                "label": "container",
                "value": _check_value(
                    private_reachable, good_label="reachable", bad_label="not reachable"
                ),
                "tone": private_tone,
            },
            {
                "label": "loopback",
                "value": _check_value(
                    proxy_reachable, good_label="reachable", bad_label="not reachable"
                ),
                "tone": loopback_tone,
            },
        ]
        facts = [
            {"label": "target", "value": target},
            {
                "label": "container",
                "value": f"{private_address}:{backend.handoff_port}",
            },
        ]
        if sub_state and sub_state != "running":
            facts.append({"label": "state", "value": sub_state})
        if tone != "success":
            dns_servers = str(data.get("DnsServers") or "").strip()
            if dns_servers:
                facts.append({"label": "dns", "value": dns_servers})
        runtime_cards.append(
            {
                "name": backend.name,
                "kind": backend.kind,
                "unit": service_name_raw,
                "target": target,
                "active_state": active_state,
                "sub_state": sub_state,
                "tone": tone,
                "checks": checks,
                "facts": facts,
                "action_hints": service.get("action_hints") or [],
                "error": service.get("error") or None,
            }
        )
    return runtime_cards, runtime_cpu_host_values, runtime_memory_current_values


def _build_backend_input_maps(
    inputs: list[Input],
) -> tuple[dict[int, list[int]], dict[int, list[str]]]:
    backend_input_ids: dict[int, list[int]] = {}
    backend_input_labels: dict[int, list[str]] = {}
    for item in inputs:
        label = f"{_input_kind_label(_input_kind(item))}: {_input_value(item)}"
        for backend in sorted(item.backends, key=lambda backend: backend.id):
            backend_input_ids.setdefault(backend.id, []).append(item.id)
            backend_input_labels.setdefault(backend.id, []).append(label)
    return backend_input_ids, backend_input_labels


def _build_input_attach_options(
    inputs: list[Input],
    *,
    current_backend_id: int | None = None,
) -> list[dict[str, object]]:
    options: list[dict[str, object]] = []
    for item in inputs:
        input_kind = _input_kind(item)
        input_value = _input_value(item)
        attached_to_current = current_backend_id is not None and any(
            backend.id == current_backend_id for backend in item.backends
        )
        enabled_backends = [
            backend
            for backend in item.backends
            if backend.enabled and backend.id != current_backend_id
        ]
        unavailable_reason = ""
        if (
            input_kind in {"shield", "tailnet_path", "tailnet_service"}
            and enabled_backends
        ):
            names = ", ".join(backend.name for backend in enabled_backends[:2])
            if len(enabled_backends) > 2:
                names += f" +{len(enabled_backends) - 2}"
            unavailable_reason = f"already attached to {names}"
        elif input_kind == "domain":
            static_backends = [
                backend for backend in enabled_backends if backend.kind == "static"
            ]
            if static_backends:
                names = ", ".join(backend.name for backend in static_backends[:2])
                if len(static_backends) > 2:
                    names += f" +{len(static_backends) - 2}"
                unavailable_reason = f"static route already attached to {names}"
        options.append(
            {
                "id": item.id,
                "value": input_value,
                "kind": input_kind,
                "kind_label": _input_kind_label(input_kind),
                "attachable": not unavailable_reason,
                "attached": attached_to_current,
                "unavailable_reason": unavailable_reason,
            }
        )
    return options


def _build_attached_input_attach_options(
    inputs: list[Input],
) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "value": _input_value(item),
            "kind": _input_kind(item),
            "kind_label": _input_kind_label(_input_kind(item)),
            "attachable": True,
            "attached": True,
            "unavailable_reason": "",
        }
        for item in sorted(inputs, key=lambda item: item.id)
    ]


def _shield_status(
    backends: list[Backend],
    inputs: list[Input],
    settings: Settings,
    service_map: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    shield_backend = next(
        (backend for backend in backends if backend.kind == "shield"), None
    )
    shield_inputs = [
        item
        for item in inputs
        if _input_kind(item) == "shield"
        and any(backend.kind == "shield" for backend in item.backends)
    ]
    enabled_shield_inputs = [
        item
        for item in shield_inputs
        if item.enabled
        and any(
            backend.kind == "shield" and backend.enabled for backend in item.backends
        )
    ]
    server_enabled = bool(settings.shield_enabled)
    shield_service = (service_map or {}).get("cnc-shield.service") or {}
    shield_service_data = (
        shield_service.get("data")
        if isinstance(shield_service.get("data"), dict)
        else {}
    )
    shield_service_active = (
        bool(shield_service.get("ok"))
        and str(shield_service_data.get("ActiveState") or "").strip().lower()
        == "active"
    )
    output_state = (
        "healthy"
        if shield_backend is not None
        and shield_backend.enabled
        and shield_service_active
        else ("missing" if shield_backend is None else "unhealthy")
    )
    config_ready = server_enabled and bool(enabled_shield_inputs)
    ready = config_ready and output_state == "healthy"
    return {
        "server_enabled": server_enabled,
        "backend_exists": shield_backend is not None,
        "backend_id": shield_backend.id if shield_backend else None,
        "backend_enabled": bool(shield_backend.enabled) if shield_backend else False,
        "output_state": output_state,
        "output_healthy": output_state == "healthy",
        "input_exists": bool(shield_inputs),
        "input_values": [_input_value(item) for item in shield_inputs],
        "config_ready": config_ready,
        "ready": ready,
    }


_SINGLE_OUTPUT_INPUT_KINDS = {"shield", "tailnet_path", "tailnet_service"}
_INPUT_MULTI_OUTPUT_TARGETS = {
    "shield": "shield inputs can mount only one output",
    "tailnet_path": "tailnet paths can mount only one output",
    "tailnet_service": "tailnet services can mount only one output",
}


def _route_label(input_kind: str, input_value: str, settings: Settings) -> str:
    if input_kind == "tailnet_path":
        return f"tailnet {input_value}"
    if input_kind == "tailnet_service":
        return _tailnet_service_route_label(input_value, settings)
    return input_value


def _backend_route_target(
    backend: Backend,
    *,
    input_kind: str,
    input_value: str,
    settings: Settings,
) -> str:
    if backend.kind == "app":
        destination = (
            f"http://127.0.0.1:{backend.port}"
            if backend.port
            else "port assigned on save"
        )
        return (
            f"{_route_label(input_kind, input_value, settings)} -> {destination}"
            if input_kind in {"tailnet_path", "tailnet_service"}
            else destination
        )
    if backend.kind == "shield":
        return f"{input_value} -> Shield access gate"
    target = backend.static_root or "-"
    return (
        f"{_route_label(input_kind, input_value, settings)} -> {target}"
        if input_kind in {"tailnet_path", "tailnet_service"}
        else target
    )


def _route_row_state(
    *,
    input_enabled: bool,
    backend: Backend,
    input_kind: str,
    enabled_backend_count: int,
    enabled_kind_count: int,
    enabled_static_backend_count: int,
) -> str:
    if not input_enabled or not backend.enabled:
        return "inactive"
    if (
        enabled_kind_count > 1
        or (input_kind in _SINGLE_OUTPUT_INPUT_KINDS and enabled_backend_count > 1)
        or (backend.kind == "static" and enabled_static_backend_count > 1)
    ):
        return "error"
    return "active"


def _input_route_view(
    item: Input,
    *,
    input_kind: str,
    input_value: str,
    settings: Settings,
    include_routing_rows: bool,
) -> tuple[list[Backend], list[str], str, str, str, str, list[dict[str, str | bool]]]:
    attached_backends = sorted(item.backends, key=lambda backend: backend.id)
    enabled_backends = [backend for backend in attached_backends if backend.enabled]
    attached_names = [backend.name for backend in attached_backends]

    backend_summary = "none"
    backend_kind = "-"
    route_state = "inactive"
    target = "no outputs attached"
    enabled_kind_count = 0
    enabled_static_backend_count = 0
    summary_conflict = False

    if attached_backends:
        attached_kinds = {backend.kind for backend in attached_backends}
        enabled_kind_count = len({backend.kind for backend in enabled_backends})
        enabled_static_backend_count = sum(
            1 for backend in enabled_backends if backend.kind == "static"
        )
        backend_summary = ", ".join(attached_names[:3]) + (
            f" +{len(attached_names) - 3}" if len(attached_names) > 3 else ""
        )
        backend_kind = (
            attached_backends[0].kind if len(attached_kinds) == 1 else "mixed"
        )
        summary_conflict = (
            len(attached_kinds) > 1
            or (input_kind in _SINGLE_OUTPUT_INPUT_KINDS and len(enabled_backends) > 1)
            or (backend_kind == "static" and len(enabled_backends) > 1)
        )
        route_state = (
            "inactive"
            if not enabled_backends or not item.enabled
            else ("error" if summary_conflict else "active")
        )
        if len(attached_kinds) > 1:
            target = "mixed output kinds are not routable"
        elif input_kind in _INPUT_MULTI_OUTPUT_TARGETS and len(enabled_backends) > 1:
            target = _INPUT_MULTI_OUTPUT_TARGETS[input_kind]
        elif backend_kind == "app":
            target = (
                "all attached outputs are disabled"
                if not enabled_backends
                else (
                    _backend_route_target(
                        enabled_backends[0],
                        input_kind=input_kind,
                        input_value=input_value,
                        settings=settings,
                    )
                    if input_kind in {"tailnet_path", "tailnet_service"}
                    else ", ".join(
                        _backend_route_target(
                            backend,
                            input_kind=input_kind,
                            input_value=input_value,
                            settings=settings,
                        )
                        for backend in enabled_backends
                    )
                )
            )
        else:
            target = _backend_route_target(
                enabled_backends[0] if enabled_backends else attached_backends[0],
                input_kind=input_kind,
                input_value=input_value,
                settings=settings,
            )

    routing_rows: list[dict[str, str | bool]] = []
    if include_routing_rows and not attached_backends:
        routing_rows.append(
            {
                "input_value": input_value,
                "input_kind": input_kind,
                "input_enabled": item.enabled,
                "backend_name": "none",
                "backend_enabled": False,
                "kind": "-",
                "target": target,
                "route_state": route_state,
            }
        )
    elif include_routing_rows:
        routing_rows.extend(
            {
                "input_value": input_value,
                "input_kind": input_kind,
                "input_enabled": item.enabled,
                "backend_name": backend.name,
                "backend_enabled": backend.enabled,
                "kind": backend.kind,
                "target": _backend_route_target(
                    backend,
                    input_kind=input_kind,
                    input_value=input_value,
                    settings=settings,
                ),
                "route_state": _route_row_state(
                    input_enabled=item.enabled,
                    backend=backend,
                    input_kind=input_kind,
                    enabled_backend_count=len(enabled_backends),
                    enabled_kind_count=enabled_kind_count,
                    enabled_static_backend_count=enabled_static_backend_count,
                ),
            }
            for backend in attached_backends
        )

    return (
        attached_backends,
        attached_names,
        backend_summary,
        backend_kind,
        route_state,
        target,
        routing_rows,
    )


def _build_input_details(
    *,
    inputs: list[Input],
    last_applied_at: datetime | None,
    settings: Settings,
    include_routing_rows: bool = True,
) -> tuple[dict[int, dict[str, object]], list[dict[str, str | bool]], int]:
    input_details: dict[int, dict[str, object]] = {}
    routing_rows: list[dict[str, str | bool]] = []
    pending_inputs = 0

    for item in inputs:
        change_state = _change_state(
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_applied_at=last_applied_at,
        )
        if change_state is not None:
            pending_inputs += 1
        input_kind = _input_kind(item)
        input_value = _input_value(item)
        route = _input_route_view(
            item,
            input_kind=input_kind,
            input_value=input_value,
            settings=settings,
            include_routing_rows=include_routing_rows,
        )
        (
            attached_backends,
            attached_names,
            backend_summary,
            backend_kind,
            route_state,
            target,
            route_rows,
        ) = route
        routing_rows.extend(route_rows)

        input_details[item.id] = {
            "input_id": item.id,
            "kind": input_kind,
            "kind_label": _input_kind_label(input_kind),
            "value": input_value,
            "enabled": item.enabled,
            "created_at": _format_timestamp(item.created_at) or "-",
            "created_at_raw": _timestamp_data_value(item.created_at),
            "updated_at": _format_timestamp(item.updated_at) or "-",
            "updated_at_raw": _timestamp_data_value(item.updated_at),
            "backend_name": backend_summary,
            "backend_kind": backend_kind,
            "target": target,
            "route_state": route_state,
            "input_state": "enabled" if item.enabled else "disabled",
            "shield_enabled": bool(getattr(item, "shield_enabled", False)),
            "shield_code_configured": bool(
                str(getattr(item, "shield_code_hash", "") or "").strip()
            ),
            "shield_access_code": str(getattr(item, "shield_access_code", "") or ""),
            "backend_ids": item.backend_ids,
            "backend_names": attached_names,
            "backend_tags": [
                {"id": backend.id, "name": backend.name}
                for backend in attached_backends
            ],
            "change_state": change_state,
        }

    return input_details, routing_rows, pending_inputs


def _pending_input_count(inputs: list[Input], last_applied_at: datetime | None) -> int:
    return sum(
        1
        for item in inputs
        if _change_state(
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_applied_at=last_applied_at,
        )
        is not None
    )


def _routing_counts(inputs: list[Input]) -> tuple[int, int]:
    counts = dashboard_overview_counts([], inputs)
    return counts["routes_total"], counts["routes_active"]


def _build_output_details(
    *,
    backends: list[Backend],
    service_map: dict[str, dict[str, object]],
    backend_input_ids: dict[int, list[int]],
    backend_input_labels: dict[int, list[str]],
    last_applied_at: datetime | None,
    base_profile: ResourceProfile,
) -> tuple[dict[int, dict[str, object]], int]:
    output_details: dict[int, dict[str, object]] = {}
    pending_backends = 0

    for backend in backends:
        attached_input_ids = backend_input_ids.get(backend.id, [])
        attached_input_labels = backend_input_labels.get(backend.id, [])
        detail = _build_output_detail(
            backend=backend,
            service_map=service_map,
            attached_input_ids=attached_input_ids,
            attached_input_labels=attached_input_labels,
            last_applied_at=last_applied_at,
            base_profile=base_profile,
        )
        output_details[backend.id] = detail
        if detail.get("change_state") is not None:
            pending_backends += 1

    return output_details, pending_backends


async def _enabled_runtime_backends_for_resource_profile(
    session: AsyncSession,
) -> list[Backend]:
    return list(
        (
            await session.execute(
                select(Backend)
                .options(load_only(Backend.id, Backend.kind, Backend.resource_size))
                .where(Backend.kind == "app", Backend.enabled.is_(True))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _build_output_detail(
    *,
    backend: Backend,
    service_map: dict[str, dict[str, object]],
    attached_input_ids: list[int],
    attached_input_labels: list[str],
    last_applied_at: datetime | None,
    base_profile: ResourceProfile,
    runtime_diagnostics: dict[str, object] | None = None,
) -> dict[str, object]:
    backend_profile = backend_resource_profile(backend, base_profile)
    change_state = _change_state(
        created_at=backend.created_at,
        updated_at=backend.updated_at,
        last_applied_at=last_applied_at,
    )
    output_target = "-"
    unit_name = "-"
    private_network = "-"
    container_runtime = "-"
    service_lookup_name = "-"
    runtime_label = "runtime"
    unit_state: dict[str, object] | None = None
    volume_view: list[str] | list[object] = []
    exposure_label = "runtime target"
    exposure_value = "-"
    cpu_percent = None
    memory_percent = None
    cpu_host_percent = None
    memory_current_bytes = None
    memory_max_bytes = None

    if backend.kind == "app":
        unit_name = backend.sandbox_profile or "-"
        container_runtime = container_name(backend.name)
        service_lookup_name = container_runtime
        private_network = network_name(backend.name)
        runtime_label = "guest profile"
        output_target = (
            f"http://127.0.0.1:{backend.port}"
            if backend.port
            else "port assigned on save"
        )
        exposure_label = "loopback publish"
        exposure_value = (
            f"127.0.0.1:{backend.port}:{backend.handoff_port}/tcp"
            if backend.port
            else f"127.0.0.1:AUTO:{backend.handoff_port}/tcp"
        )
        unit_state = service_map.get(service_lookup_name)
        metrics = unit_state.get("metrics") if isinstance(unit_state, dict) else None
        try:
            volume_view = parse_volumes_json(backend.volumes_json)
        except ValidationError as exc:
            volume_view = [f"error: {exc}", backend.volumes_json]
        cpu_percent = metrics.get("cpu_percent") if isinstance(metrics, dict) else None
        memory_percent = (
            metrics.get("memory_percent") if isinstance(metrics, dict) else None
        )
        cpu_host_percent = (
            metrics.get("cpu_percent_of_host") if isinstance(metrics, dict) else None
        )
        memory_current_bytes = (
            metrics.get("memory_current_bytes") if isinstance(metrics, dict) else None
        )
        memory_max_bytes = (
            metrics.get("memory_max_bytes") if isinstance(metrics, dict) else None
        )
    elif backend.kind == "shield":
        unit_name = SHIELD_CONTAINER_SERVICE
        container_runtime = SHIELD_CONTAINER_NAME
        service_lookup_name = SHIELD_CONTAINER_SERVICE
        runtime_label = "shield runtime"
        output_target = "protected app routes only"
        exposure_label = "loopback publish"
        exposure_value = f"127.0.0.1:{SHIELD_PORT}:1026/tcp"
        unit_state = service_map.get(service_lookup_name)
        metrics = unit_state.get("metrics") if isinstance(unit_state, dict) else None
        cpu_percent = metrics.get("cpu_percent") if isinstance(metrics, dict) else None
        memory_percent = (
            metrics.get("memory_percent") if isinstance(metrics, dict) else None
        )
        cpu_host_percent = (
            metrics.get("cpu_percent_of_host") if isinstance(metrics, dict) else None
        )
        memory_current_bytes = (
            metrics.get("memory_current_bytes") if isinstance(metrics, dict) else None
        )
        memory_max_bytes = (
            metrics.get("memory_max_bytes") if isinstance(metrics, dict) else None
        )
    else:
        output_target = backend.static_root or "-"

    unit_data = (
        unit_state.get("data")
        if isinstance(unit_state, dict) and isinstance(unit_state.get("data"), dict)
        else {}
    )
    unit_ok = bool(unit_state.get("ok")) if isinstance(unit_state, dict) else False
    if runtime_diagnostics is None and isinstance(unit_state, dict):
        cached_diagnostics = unit_state.get("diagnostics")
        if isinstance(cached_diagnostics, dict):
            runtime_diagnostics = cached_diagnostics
        elif backend.kind == "shield":
            runtime_diagnostics = {
                "diagnosis": "healthy"
                if unit_ok
                and str(unit_data.get("ActiveState") or "").strip().lower() == "active"
                else "shield_unhealthy"
            }
    healthcheck_plan = resolve_backend_healthcheck(backend)
    active_state = str(unit_data.get("ActiveState") or "")
    cpu_meter = _output_load_meter(
        cpu_percent if isinstance(cpu_percent, (int, float)) else None,
        enabled=backend.enabled,
        kind=backend.kind,
        service_state=active_state,
    )
    memory_meter = _output_load_meter(
        memory_percent if isinstance(memory_percent, (int, float)) else None,
        enabled=backend.enabled,
        kind=backend.kind,
        service_state=active_state,
    )
    runtime_health = _runtime_health_view(
        backend=backend,
        healthcheck_mode=display_backend_healthcheck_mode(backend),
        healthcheck_path=healthcheck_plan.path or "-",
        healthcheck_host_header=healthcheck_plan.host_header or "-",
        runtime_diagnostics=runtime_diagnostics,
    )
    runtime_badge = _output_runtime_badge(
        enabled=backend.enabled,
        kind=backend.kind,
        runtime_diagnostics=runtime_diagnostics,
        unit_data=unit_data,
        unit_ok=unit_ok,
    )
    runtime_owner_label = "static files"
    if backend.kind == "app":
        runtime_owner_label = str(
            (runtime_diagnostics or {}).get("runtime_owner_label")
            or unit_data.get("RuntimeOwner")
            or "unknown"
        )
    has_custom_resource_limits = bool(
        backend.memory_high_override
        or backend.memory_max_override
        or backend.cpu_quota_override
    )
    configured_resource_mode = backend.resource_mode or "auto"
    configured_resource_size = backend.resource_size or "small"
    resource_size_choice = (
        "custom" if has_custom_resource_limits else configured_resource_size
    )
    if configured_resource_mode == "auto" and not has_custom_resource_limits:
        resource_size_choice = "auto"
    resource_size_label = resource_size_choice if backend.kind == "app" else "-"
    if backend.kind == "app" and resource_size_choice == "auto":
        resource_size_label = (
            f"auto ({backend_profile.resource_size or configured_resource_size})"
        )

    return {
        "backend_id": backend.id,
        "name": backend.name,
        "kind": backend.kind,
        "enabled": backend.enabled,
        "placement_label": _backend_placement_label(backend),
        "sandbox_profile": backend.sandbox_profile or "-",
        "port": backend.port,
        "handoff_port": backend.handoff_port,
        "healthcheck_mode": display_backend_healthcheck_mode(backend),
        "healthcheck_path": healthcheck_plan.path or "-",
        "healthcheck_host_header": healthcheck_plan.host_header or "-",
        "configured_resource_mode": configured_resource_mode,
        "configured_resource_size": configured_resource_size,
        "resource_size_choice": resource_size_choice,
        "resource_size_label": resource_size_label,
        "memory_high_override": backend.memory_high_override or "",
        "memory_max_override": backend.memory_max_override or "",
        "cpu_quota_override": backend.cpu_quota_override or "",
        "resource_mode": backend_profile.mode,
        "resource_size": backend_profile.resource_size or "-",
        "resource_memory_high": backend_profile.memory_high,
        "resource_memory_max": backend_profile.memory_max,
        "resource_cpu_quota": backend_profile.cpu_quota,
        "resource_cpu_shares": backend_profile.cpu_shares,
        "resource_cpu_entitlement_percent": backend_profile.cpu_entitlement_percent_of_host,
        "resource_host_cpu_count": backend_profile.host_cpu_count,
        "target": output_target,
        "exposure_label": exposure_label,
        "exposure_value": exposure_value,
        "runtime_label": runtime_label,
        "service_unit": unit_name,
        "container_runtime": container_runtime,
        "private_network": private_network,
        "service_status": unit_state,
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "cpu_meter": cpu_meter,
        "memory_meter": memory_meter,
        "cpu_host_percent": cpu_host_percent,
        "memory_current_bytes": memory_current_bytes,
        "memory_max_bytes": memory_max_bytes,
        "volume_view": volume_view,
        "notes": backend.notes or "-",
        "created_at": _format_timestamp(backend.created_at) or "-",
        "created_at_raw": _timestamp_data_value(backend.created_at),
        "updated_at": _format_timestamp(backend.updated_at) or "-",
        "updated_at_raw": _timestamp_data_value(backend.updated_at),
        "input_ids": attached_input_ids,
        "input_names": attached_input_labels,
        "change_state": change_state,
        "shield_access_code": str(getattr(backend, "shield_access_code", "") or ""),
        "service_state": (
            f"{unit_data.get('ActiveState', '-')} / {unit_data.get('SubState', '-')}"
            if backend.kind in {"app", "shield"}
            else "-"
        ),
        "status_value": runtime_badge["value"],
        "status_tone": runtime_badge["tone"],
        "runtime_health_label": runtime_health["label"],
        "runtime_health_tone": runtime_health["tone"],
        "runtime_health_summary": runtime_health["summary"],
        "runtime_health_detail": runtime_health["detail"],
        "runtime_health_alert": runtime_health["alert"],
        "runtime_sandbox_status": runtime_health["sandbox_status"],
        "runtime_guest_status": runtime_health["guest_status"],
        "runtime_handoff_status": runtime_health["app_handoff_status"],
        "runtime_state_detail": runtime_health["runtime_state"],
        "runtime_owner_label": runtime_owner_label,
        "configured_health_target": runtime_health["health_target"],
        "runtime_diagnostics": runtime_diagnostics or {},
        "attached_inputs_summary": ", ".join(attached_input_labels)
        if attached_input_labels
        else "none",
        "debug_dump": _debug_kv_dump(
            [
                ("backend_id", backend.id),
                ("name", backend.name),
                ("kind", backend.kind),
                ("enabled", backend.enabled),
                ("port", backend.port or "-"),
                ("handoff_port", backend.handoff_port),
                ("sandbox_profile", backend.sandbox_profile or "-"),
                ("target", output_target),
                ("exposure_label", exposure_label),
                ("exposure_value", exposure_value),
                ("runtime_label", runtime_label),
                ("service_unit", unit_name),
                ("container_runtime", container_runtime),
                ("private_network", private_network),
                ("healthcheck_mode", display_backend_healthcheck_mode(backend)),
                ("healthcheck_path", healthcheck_plan.path or "-"),
                ("healthcheck_host_header", healthcheck_plan.host_header or "-"),
                ("runtime_health_summary", runtime_health["summary"]),
                ("runtime_health_detail", runtime_health["detail"]),
                ("runtime_sandbox_status", runtime_health["sandbox_status"]),
                ("runtime_guest_status", runtime_health["guest_status"]),
                ("runtime_handoff_status", runtime_health["app_handoff_status"]),
                ("runtime_diagnostics", runtime_diagnostics or {}),
                ("volumes", volume_view),
                ("notes", backend.notes or "-"),
                ("created_at", _format_timestamp(backend.created_at) or "-"),
                ("updated_at", _format_timestamp(backend.updated_at) or "-"),
                ("attached_inputs", attached_input_labels),
            ]
        ),
    }


_EMPTY_NOTIFICATION_SETTINGS = {
    "configured": False,
    "status_label": "",
    "app_token_masked": "",
    "user_key_masked": "",
    "delivery_label": "",
    "delivery_at": "",
    "delivery_error": "",
}

_EMPTY_ACCESS_KEY_SETTINGS = {
    "configured": False,
    "status_label": "",
    "ttl_label": "",
    "needs_rehash": False,
}

_DASHBOARD_SETTINGS_HELP = {
    "Bind": "Local admin listener address and port.",
    "Trusted hosts": "Hostnames accepted by the admin origin guard.",
    "Database": "SQLite database URL used by the admin service.",
    "Status cache TTL": "How long cached status can be reused before a fresh host poll.",
    "Managed env": "Env file CNC updates for persistent host settings.",
    "Nginx generated": "Directory where CNC writes managed nginx config.",
    "Apply backup dir": "Directory for rollback snapshots created during host apply.",
    "App control dir": "Directory for generated app runtime specs and control files.",
    "Sandbox dir": "Directory for managed app sandbox root filesystems.",
    "Systemd generated": "Directory where CNC writes generated systemd units.",
    "Tailscale serve state": "Saved desired Tailscale Serve path and service mappings.",
    "Host mutation lock": "Global lock used to serialize host-changing operations.",
    "Updater script": "Script used by the background updater unit.",
    "Cloudflare-only": "Whether public HTTP/S should only accept Cloudflare source IPs.",
    "Cloudflare IPs": "Current number of Cloudflare CIDR ranges in the allowlist.",
    "Port range": "Host loopback port range CNC can allocate to app outputs.",
    "Guest DNS": "DNS servers injected into app sandbox containers.",
    "Auto sizing": "Whether CNC computes app limits from the current host and size mix.",
    "Keep host memory free": "RAM percentage CNC reserves for the host.",
    "Keep host CPU free": "CPU percentage CNC reserves for the host.",
    "Minimum soft memory limit": "Smallest soft memory target CNC assigns automatically.",
    "Minimum CPU limit": "Smallest host CPU share CNC assigns automatically.",
    "CPU burst headroom": "Multiplier that lets an app borrow above entitlement before the hard CPU fuse.",
    "Nightly recompute time": "Browser-local time when autosize evaluates stored resource samples.",
    "Small app soft memory limit": "Computed soft memory target for a small app under the current host mix.",
    "Small app hard memory cap": "Computed hard memory cap for a small app under the current host mix.",
    "Small app CPU limit": "Computed host CPU share for a small app under the current host mix.",
    "Pushover": "Whether notification delivery is configured.",
    "Netdata": "Whether CNC exposes a managed Netdata dashboard on the tailnet.",
    "Netdata URL": "Tailnet service URL for the managed Netdata dashboard.",
    "Pushover app token": "Masked Pushover app token.",
    "Pushover user key": "Masked Pushover user key.",
    "Last delivery": "Most recent Pushover delivery attempt observed by CNC.",
    "Access key": "Whether admin access-key auth is enabled.",
    "Session TTL": "How long an admin browser session stays valid.",
}


def _dashboard_scope(active_tab: str | None) -> _DashboardScope:
    tab = _dashboard_tab(active_tab, "home") if active_tab is not None else None
    return _DashboardScope(tab)


async def _load_dashboard_rows(
    session: AsyncSession, *, scope: _DashboardScope
) -> _DashboardRows:
    backend_query = select(Backend).order_by(Backend.id.asc())
    input_query = select(Input).order_by(Input.id.asc())
    if scope.needs_visible_entities:
        backend_query = backend_query.options(selectinload(Backend.inputs))
    if scope.includes("home", "inputs", "routing", "outputs", "settings"):
        input_query = input_query.options(selectinload(Input.backends))
    backends = list((await session.execute(backend_query)).scalars().all())
    inputs = list((await session.execute(input_query)).scalars().all())
    last_successful_apply = (
        (
            await session.execute(
                select(ApplyRun)
                .where(ApplyRun.status == "success")
                .order_by(ApplyRun.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return _DashboardRows(
        backends=backends,
        inputs=inputs,
        last_applied_at=(
            last_successful_apply.created_at if last_successful_apply else None
        ),
    )


def _dashboard_status_fallback() -> dict[str, object]:
    return dashboard_status_fallback(_app_version())


async def _dashboard_status(
    session: AsyncSession,
    settings: Settings,
    *,
    scope: _DashboardScope,
    prefer_cached_status: bool,
    defer_status: bool = False,
) -> dict[str, object]:
    if defer_status:
        return peek_cached_status() or _dashboard_status_fallback()
    if scope.needs_status:
        if prefer_cached_status:
            if scope.active_tab == "home":
                return peek_cached_status() or _dashboard_status_fallback()
            return await collect_status(session, settings, allow_stale=True)
        return await collect_status(session, settings)
    return peek_cached_status() or _dashboard_status_fallback()


def _dashboard_settings_sections(
    settings: Settings,
    resource_profile: ResourceProfile,
    host_cpu_count: object | None,
    notification_settings: dict[str, object],
    netdata_settings: dict[str, object],
    access_key_settings: dict[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "title": "Admin",
            "rows": [
                ("Bind", f"{settings.admin_host}:{settings.admin_port}"),
                ("Trusted hosts", str(len(settings.trusted_host_list))),
                ("Database", settings.database_url),
                ("Status cache TTL", f"{settings.status_cache_ttl_sec}s"),
            ],
        },
        {
            "title": "Paths",
            "rows": [
                ("Managed env", str(settings.managed_env_file_path)),
                ("Nginx generated", str(settings.nginx_generated_dir)),
                ("Apply backup dir", str(settings.apply_backup_dir)),
                ("Backend backup dir", str(settings.backend_backup_dir)),
                ("Backend backup format", settings.backend_backup_archive_format),
                (
                    "Backend backup gzip level",
                    str(settings.backend_backup_gzip_compresslevel)
                    if settings.backend_backup_archive_format == "tar.gz"
                    else "off",
                ),
                (
                    "Backend backup retention",
                    (
                        f"keep latest {settings.backend_backup_retention_per_backend} successful backup(s) per output"
                        if settings.backend_backup_retention_per_backend > 0
                        else "keep all successful backups"
                    ),
                ),
                ("App control dir", str(settings.app_control_dir)),
                ("Tailscale serve state", str(settings.tailscale_serve_state_path)),
                ("Host mutation lock", str(settings.apply_lock_path)),
                ("Updater script", str(settings.updater_script_path)),
            ],
        },
        {
            "title": "Networking",
            "rows": [
                ("Cloudflare-only", "on" if settings.nginx_cloudflare_only else "off"),
                ("Cloudflare IPs", str(len(settings.cloudflare_ip_list))),
                (
                    "Port range",
                    f"{settings.port_range_start}-{settings.port_range_end}",
                ),
                ("Guest DNS", ", ".join(configured_app_dns_servers(settings))),
            ],
        },
        {
            "title": "Auto sizing",
            "rows": [
                ("Auto sizing", "on" if settings.auto_resource_limits else "off"),
                ("Keep host memory free", f"{settings.auto_memory_reserve_percent}%"),
                ("Keep host CPU free", f"{settings.auto_cpu_reserve_percent}%"),
                ("Minimum soft memory limit", f"{settings.auto_min_memory_high_mb}M"),
                ("Minimum CPU limit", f"{settings.auto_min_cpu_quota_percent}%"),
                ("CPU burst headroom", f"{settings.auto_cpu_burst_factor:g}x"),
                (
                    "Nightly recompute time",
                    f"{settings.auto_size_nightly_hour_utc:02d}:00",
                    {
                        "raw": _utc_hour_for_local_display(
                            settings.auto_size_nightly_hour_utc
                        ),
                        "format": "time",
                    },
                ),
                ("Small app soft memory limit", resource_profile.memory_high),
                ("Small app hard memory cap", resource_profile.memory_max),
                (
                    "Small app CPU limit",
                    _format_cpu_limit(
                        resource_profile.cpu_quota,
                        host_cpu_count=host_cpu_count,
                        entitlement_percent_of_host=resource_profile.cpu_entitlement_percent_of_host,
                    ),
                ),
            ],
        },
        {
            "title": "Notifications",
            "rows": [
                ("Pushover", str(notification_settings["status_label"])),
                ("Netdata", str(netdata_settings["status_label"])),
                (
                    "Netdata URL",
                    str(
                        netdata_settings.get("url")
                        or f"tailnet service netdata on port {settings.netdata_port}"
                    ),
                ),
                ("Pushover app token", str(notification_settings["app_token_masked"])),
                ("Pushover user key", str(notification_settings["user_key_masked"])),
                (
                    "Last delivery",
                    str(notification_settings.get("delivery_label") or "none"),
                ),
            ],
        },
        {
            "title": "Access",
            "rows": [
                ("Access key", str(access_key_settings["status_label"])),
                ("Session TTL", str(access_key_settings["ttl_label"])),
            ],
        },
    ]


def _empty_netdata_settings(settings: Settings) -> dict[str, object]:
    return {
        "enabled": False,
        "status_label": "",
        "url": "",
        "port": settings.netdata_port,
    }


def _dashboard_settings_context(
    settings: Settings,
    *,
    scope: _DashboardScope,
    resource_profile: ResourceProfile,
    host_cpu_count: object | None,
) -> dict[str, object]:
    if not scope.needs_settings:
        return {
            "resource_size_matrix": [],
            "notification_settings": dict(_EMPTY_NOTIFICATION_SETTINGS),
            "access_key_settings": dict(_EMPTY_ACCESS_KEY_SETTINGS),
            "netdata_settings": _empty_netdata_settings(settings),
            "settings_sections": [],
            "settings_help": {},
        }
    notification_settings = _notification_settings_summary(settings)
    access_key_settings = _access_key_settings_summary(settings)
    netdata_settings = _netdata_settings_summary(settings)
    return {
        "resource_size_matrix": _resource_size_matrix_rows(
            resource_profile, host_cpu_count=host_cpu_count
        ),
        "notification_settings": notification_settings,
        "access_key_settings": access_key_settings,
        "netdata_settings": netdata_settings,
        "settings_sections": _dashboard_settings_sections(
            settings,
            resource_profile,
            host_cpu_count,
            notification_settings,
            netdata_settings,
            access_key_settings,
        ),
        "settings_help": dict(_DASHBOARD_SETTINGS_HELP),
    }


def _dashboard_input_context(
    *,
    scope: _DashboardScope,
    inputs: list[Input],
    last_applied_at: datetime | None,
    settings: Settings,
) -> dict[str, object]:
    if not scope.needs_inputs:
        return {
            "input_details": {},
            "routing_rows": [],
            "pending_inputs": _pending_input_count(inputs, last_applied_at),
        }
    input_details, routing_rows, pending_inputs = _build_input_details(
        inputs=inputs,
        last_applied_at=last_applied_at,
        settings=settings,
        include_routing_rows=scope.needs_routing_rows,
    )
    return {
        "input_details": input_details,
        "routing_rows": routing_rows,
        "pending_inputs": pending_inputs,
    }


def _dashboard_output_context(
    *,
    scope: _DashboardScope,
    backends: list[Backend],
    service_map: dict[str, dict[str, object]],
    backend_input_ids: dict[int, list[int]],
    backend_input_labels: dict[int, list[str]],
    last_applied_at: datetime | None,
    resource_profile: ResourceProfile,
) -> dict[str, object]:
    if scope.needs_outputs:
        output_details, pending_backends = _build_output_details(
            backends=backends,
            service_map=service_map,
            backend_input_ids=backend_input_ids,
            backend_input_labels=backend_input_labels,
            last_applied_at=last_applied_at,
            base_profile=resource_profile,
        )
    else:
        output_details = {}
        pending_backends = sum(
            1
            for backend in backends
            if _change_state(
                created_at=backend.created_at,
                updated_at=backend.updated_at,
                last_applied_at=last_applied_at,
            )
            is not None
        )
    return {
        "output_details": output_details,
        "pending_backends": pending_backends,
        "outputs_unhealthy_count": sum(
            1
            for detail in output_details.values()
            if str(detail.get("status_tone") or "") == "error"
        ),
    }


def _dashboard_overview(
    *,
    backends: list[Backend],
    inputs: list[Input],
    enabled_backends: list[Backend],
    enabled_inputs: list[Input],
    app_backends: list[Backend],
    static_backends: list[Backend],
    service_rows: list[dict[str, object]],
    services_ok: bool,
    status: dict[str, object],
    resource_profile: ResourceProfile,
    route_total: int,
    route_active: int,
    pending_inputs: int,
    pending_backends: int,
    outputs_unhealthy_count: int,
) -> dict[str, object]:
    isolation_home_status = _network_isolation_home_status(status)
    return {
        "backends_total": len(backends),
        "backends_enabled": len(enabled_backends),
        "inputs_total": len(inputs),
        "inputs_enabled": len(enabled_inputs),
        "app_total": len(app_backends),
        "static_total": len(static_backends),
        "routes_total": route_total,
        "routes_active": route_active,
        "services_total": len(service_rows),
        "services_ok": services_ok,
        "nginx_state": (status.get("nginx", {}).get("data", {}) or {}).get(
            "ActiveState", "unknown"
        )
        if status.get("nginx", {}).get("ok")
        else "down",
        "resource_mode": resource_profile.mode,
        "resource_size": resource_profile.resource_size,
        "resource_memory_high": resource_profile.memory_high,
        "resource_memory_max": resource_profile.memory_max,
        "resource_cpu_quota": resource_profile.cpu_quota,
        "resource_reason": resource_profile.reason or "",
        "pending_inputs": pending_inputs,
        "pending_backends": pending_backends,
        "queued_changes": pending_inputs + pending_backends,
        "outputs_unhealthy": outputs_unhealthy_count,
        "isolation_status_label": isolation_home_status["label"],
        "isolation_status_tone": isolation_home_status["tone"],
    }


def _dashboard_latest_save_job(
    scope: _DashboardScope, status: dict[str, object]
) -> dict[str, object] | None:
    if not scope.includes("home"):
        return None
    return _save_job_summary(
        status.get("last_apply"),
        title="Most recent save",
        include_detail_dump=False,
    )


async def _dashboard_context(
    session: AsyncSession,
    settings: Settings,
    *,
    prefer_cached_status: bool = False,
    active_tab: str | None = None,
    defer_status: bool = False,
) -> dict:
    scope = _dashboard_scope(active_tab)
    rows = await _load_dashboard_rows(session, scope=scope)
    backends = rows.backends
    inputs = rows.inputs
    last_applied_at = rows.last_applied_at
    status = await _dashboard_status(
        session,
        settings,
        scope=scope,
        prefer_cached_status=prefer_cached_status,
        defer_status=defer_status,
    )
    backend_map = {backend.id: backend.name for backend in backends}

    enabled_backends = [backend for backend in backends if backend.enabled]
    enabled_inputs = [item for item in inputs if item.enabled]
    app_backends = [backend for backend in backends if backend.kind == "app"]
    enabled_app_backends = [
        backend for backend in enabled_backends if backend.kind == "app"
    ]
    static_backends = [backend for backend in backends if backend.kind == "static"]
    resource_profile = build_resource_profile(settings, enabled_app_backends)
    host_cpu_count = status.get("resource_profile", {}).get("host_cpu_count")
    settings_context = _dashboard_settings_context(
        settings,
        scope=scope,
        resource_profile=resource_profile,
        host_cpu_count=host_cpu_count,
    )

    service_rows = status.get("services", [])
    service_map, services_ok = _build_service_cards(service_rows)

    nginx_status = status.get("nginx") or {}
    _nginx_active_state, nginx_card = _build_nginx_card(nginx_status)
    runtime_cards, _runtime_cpu_host_values, _runtime_memory_current_values = (
        _build_runtime_cards(service_rows, enabled_app_backends)
        if scope.needs_settings
        else ([], [], [])
    )
    backend_input_ids, backend_input_labels = (
        _build_backend_input_maps(inputs) if scope.needs_outputs else ({}, {})
    )
    input_attach_options = (
        _build_input_attach_options(inputs) if scope.needs_outputs else []
    )
    shield_status = _shield_status(backends, inputs, settings, service_map)
    input_attach_available_count = sum(
        1 for item in input_attach_options if item["attachable"]
    )
    route_total, route_active = (
        _routing_counts(inputs) if scope.includes("home", "routing") else (0, 0)
    )
    input_context = _dashboard_input_context(
        scope=scope,
        inputs=inputs,
        last_applied_at=last_applied_at,
        settings=settings,
    )
    output_context = _dashboard_output_context(
        scope=scope,
        backends=backends,
        service_map=service_map,
        backend_input_ids=backend_input_ids,
        backend_input_labels=backend_input_labels,
        last_applied_at=last_applied_at,
        resource_profile=resource_profile,
    )
    input_details = input_context["input_details"]
    output_details = output_context["output_details"]
    pending_inputs = int(input_context["pending_inputs"])
    pending_backends = int(output_context["pending_backends"])
    overview = _dashboard_overview(
        backends=backends,
        inputs=inputs,
        enabled_backends=enabled_backends,
        enabled_inputs=enabled_inputs,
        app_backends=app_backends,
        static_backends=static_backends,
        service_rows=service_rows,
        services_ok=services_ok,
        status=status,
        resource_profile=resource_profile,
        route_total=route_total,
        route_active=route_active,
        pending_inputs=pending_inputs,
        pending_backends=pending_backends,
        outputs_unhealthy_count=int(output_context["outputs_unhealthy_count"]),
    )
    latest_save_job = _dashboard_latest_save_job(scope, status)
    visible_backends = backends if scope.needs_visible_entities else []
    visible_inputs = inputs if scope.needs_visible_entities else []
    cluster_nodes = (
        await _cluster_nodes_for_context(session, settings)
        if scope.needs_settings
        else []
    )
    return {
        "backends": visible_backends,
        "inputs": visible_inputs,
        "input_attach_options": input_attach_options,
        "input_attach_available_count": input_attach_available_count,
        "shield_status": shield_status,
        "backend_map": backend_map,
        "input_details": input_details,
        "output_details": output_details,
        "routing_rows": input_context["routing_rows"],
        "overview": overview,
        "settings_sections": settings_context["settings_sections"],
        "resource_size_matrix": settings_context["resource_size_matrix"],
        "settings_help": settings_context["settings_help"],
        "notification_settings": settings_context["notification_settings"],
        "access_key_settings": settings_context["access_key_settings"],
        "netdata_settings": settings_context["netdata_settings"],
        "nginx_card": nginx_card,
        "runtime_cards": runtime_cards,
        "status": status,
        "latest_save_job": latest_save_job,
        "active_tab": scope.active_tab,
        "app_version": str(
            (status.get("update_version") or {}).get("current_version")
            or _app_version()
        ),
        "asset_version": _stylesheet_asset_version(settings),
        "app_sandbox_profiles": app_sandbox_profiles(),
        "default_app_sandbox_profile": DEFAULT_UI_APP_SANDBOX_PROFILE,
        "create_output_progress_steps": create_backend_progress_steps(),
        "input_progress_pipelines": input_progress_pipelines(),
        "operation_progress_pipelines": operation_progress_pipelines(),
        "output_save_progress_steps": output_save_progress_steps(),
        "metric_history_default_timeframe": DEFAULT_TIMEFRAME_KEY,
        "metric_history_timeframes": [
            spec.__dict__ for spec in TIMEFRAME_SPECS.values()
        ],
        "domain_input_hint": DOMAIN_INPUT_HINT,
        "tailnet_path_hint": TAILNET_PATH_HINT,
        "static_root_hint": STATIC_ROOT_HINT,
        "last_applied_at": _format_timestamp(last_applied_at),
        "csrf_token": get_csrf_token(settings),
        "flash_error": None,
        "flash_success": None,
        "beta_routing": settings.beta_routing,
        "multi_node_enabled": settings.multi_node_enabled,
        "cluster_nodes": cluster_nodes,
    }


async def _cached_dashboard_context(
    session: AsyncSession,
    settings: Settings,
    *,
    active_tab: str,
) -> dict[str, object]:
    return await _dashboard_context(
        session,
        settings,
        prefer_cached_status=True,
        active_tab=active_tab,
        defer_status=True,
    )


async def _host_llm_help_context(
    session: AsyncSession,
    settings: Settings,
) -> dict[str, object]:
    """Build only the output state used by the host-level LLM help document."""
    rows = await _load_dashboard_rows(session, scope=_dashboard_scope("outputs"))
    status = peek_cached_status() or _dashboard_status_fallback()
    service_map, _services_ok = _build_service_cards(status.get("services", []))
    backend_input_ids, backend_input_labels = _build_backend_input_maps(rows.inputs)
    resource_profile = build_resource_profile(
        settings,
        [
            backend
            for backend in rows.backends
            if backend.kind == "app" and backend.enabled
        ],
    )
    output_details, _pending_backends = _build_output_details(
        backends=rows.backends,
        service_map=service_map,
        backend_input_ids=backend_input_ids,
        backend_input_labels=backend_input_labels,
        last_applied_at=rows.last_applied_at,
        base_profile=resource_profile,
    )
    return {"backends": rows.backends, "output_details": output_details}


async def _output_page_context(
    session: AsyncSession,
    settings: Settings,
    backend_id: int,
    *,
    prefer_cached_runtime: bool = False,
) -> dict[str, object]:
    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        raise LookupError("backend not found")
    last_successful_apply = (
        (
            await session.execute(
                select(ApplyRun)
                .where(ApplyRun.status == "success")
                .order_by(ApplyRun.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    last_applied_at = (
        last_successful_apply.created_at if last_successful_apply else None
    )
    base_profile = build_resource_profile(
        settings, await _enabled_runtime_backends_for_resource_profile(session)
    )
    attached_input_labels = [
        f"{_input_kind_label(_input_kind(item))}: {_input_value(item)}"
        for item in sorted(backend.inputs, key=lambda item: item.id)
    ]
    cached_status_payload = peek_cached_status() if prefer_cached_runtime else None
    runtime_diagnostics: dict[str, object] | None = None
    if backend.kind == "app":
        if prefer_cached_runtime:
            runtime_diagnostics = _runtime_diagnostics_from_status_payload(
                cached_status_payload,
                backend.name,
                kind=backend.kind,
            )
        if runtime_diagnostics is None and not prefer_cached_runtime:
            runtime_diagnostics = await asyncio.to_thread(
                collect_app_backend_diagnostics, backend, settings
            )
    detail = _build_output_detail(
        backend=backend,
        service_map={},
        attached_input_ids=backend.input_ids,
        attached_input_labels=attached_input_labels,
        last_applied_at=last_applied_at,
        base_profile=base_profile,
        runtime_diagnostics=runtime_diagnostics,
    )
    if (
        backend.kind == "app"
        and backend.enabled
        and prefer_cached_runtime
        and runtime_diagnostics is None
    ):
        detail["runtime_health_label"] = ""
        detail["runtime_health_tone"] = "inactive"
        detail["runtime_health_summary"] = ""
        detail["runtime_health_detail"] = ""
        detail["runtime_health_alert"] = None
        detail["runtime_state_detail"] = ""
    all_backends = list(
        (
            await session.execute(
                select(Backend).where(Backend.kind == "shield").limit(1)
            )
        )
        .scalars()
        .all()
    )
    shield_inputs = (
        list(
            (
                await session.execute(
                    select(Input)
                    .options(selectinload(Input.backends))
                    .where(Input.kind == "shield")
                    .order_by(Input.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if settings.shield_enabled or backend.kind == "shield"
        else []
    )
    total_input_count = int(
        (await session.execute(select(func.count(Input.id)))).scalar_one() or 0
    )
    input_attach_options = _build_attached_input_attach_options(list(backend.inputs))
    shield_status = _shield_status(all_backends, shield_inputs, settings)
    recent_events = await list_recent_output_events(
        session,
        backend_name=backend.name,
        since=backend.created_at,
        limit=12,
    )
    clone_defaults = {
        "name": await _next_clone_backend_name(session, backend.name),
        "port": await _next_clone_backend_port(session, backend.port, settings),
    }
    multi_node_app_enabled = settings.multi_node_enabled and backend.kind == "app"
    cluster_nodes = (
        await _cluster_nodes_for_context(session, settings)
        if multi_node_app_enabled
        else []
    )
    placement_backends = (
        list(
            (
                await session.execute(
                    select(Backend)
                    .where(Backend.kind == "app", Backend.id != backend.id)
                    .order_by(Backend.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if multi_node_app_enabled
        else []
    )
    placement_readiness = (
        await backend_replica_readiness_statuses(
            session,
            settings,
            backend=backend,
            node_uids=tuple(
                clean_node_uid(node.get("id", "")) for node in cluster_nodes
            ),
        )
        if multi_node_app_enabled
        else {}
    )
    return {
        "inputs": list(backend.inputs),
        "input_attach_options": input_attach_options,
        "shield_status": shield_status,
        "input_attach_visible_count": sum(
            1 for item in input_attach_options if not item["attached"]
        ),
        "input_attach_lazy_available": total_input_count > len(backend.inputs),
        "input_attach_options_url": (
            f"/api/backends/{backend.id}/input-attach-options"
        ),
        "selected_backend": backend,
        "selected_output_detail": detail,
        "selected_output_live_metrics": _service_metrics_from_status_payload(
            cached_status_payload, backend
        )
        or {},
        "metric_history_default_metric": DEFAULT_METRIC_KEY,
        "metric_history_default_timeframe": DEFAULT_TIMEFRAME_KEY,
        "metric_history_options": [spec.__dict__ for spec in METRIC_SPECS.values()],
        "metric_history_timeframes": [
            spec.__dict__ for spec in TIMEFRAME_SPECS.values()
        ],
        "output_signal_cards": _output_signal_cards(detail),
        "attached_input_rows": detail.get("input_names", []),
        "output_info_rows": _output_info_rows(backend, detail),
        "latest_backup": None,
        "backup_summary": _backup_summary(
            None,
            None,
            planned_coverage=_planned_backup_coverage_summary(backend, settings),
        ),
        "backup_history": [],
        "backup_signals_pending": True,
        "clone_defaults": clone_defaults,
        "runtime_alert": detail.get("runtime_health_alert"),
        "recent_event_rows": _event_rows(recent_events),
        "app_sandbox_profiles": app_sandbox_profiles(),
        "default_app_sandbox_profile": DEFAULT_UI_APP_SANDBOX_PROFILE,
        "static_root_hint": STATIC_ROOT_HINT,
        "csrf_token": get_csrf_token(settings),
        "operation_progress_pipelines": operation_progress_pipelines(),
        "output_save_progress_steps": output_save_progress_steps(),
        "flash_error": None,
        "flash_success": None,
        "beta_routing": settings.beta_routing,
        "multi_node_enabled": settings.multi_node_enabled,
        "multi_node_transfer_enabled": multi_node_app_enabled,
        "cluster_nodes": cluster_nodes,
        "multi_node_placement": _build_output_placement_card(
            backend, cluster_nodes, placement_backends, placement_readiness
        )
        if multi_node_app_enabled
        else None,
    }
