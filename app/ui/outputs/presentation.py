"""Output cards, resource badges, and detail presentation from supplied data."""

from __future__ import annotations

import json
from datetime import datetime

from app.config import Settings
from app.models.entities import Backend
from app.security import get_csrf_token
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
from app.services.operation_runtime import (
    operation_progress_pipelines,
    output_save_progress_steps,
)
from app.services.renderers import (
    SHIELD_PORT,
    container_name,
    network_name,
)
from app.services.resource_profile import (
    ResourceProfile,
    backend_resource_profile,
)
from app.services.sandbox_profiles import app_sandbox_dir, app_sandbox_profiles
from app.services.shield_runtime import SHIELD_CONTAINER_NAME, SHIELD_CONTAINER_SERVICE
from app.services.validators import (
    ValidationError,
    parse_volumes_json,
)
from app.ui.cluster import build_output_placement_card
from app.ui.forms import (
    DEFAULT_UI_APP_SANDBOX_PROFILE,
    STATIC_ROOT_HINT,
)
from app.ui.outputs.data import OutputPageData
from app.ui.outputs.events import event_rows
from app.ui.outputs.health import (
    runtime_health_view,
    service_metrics_from_status_payload,
)
from app.ui.resources import format_cpu_limit, format_memory_limit
from app.ui.routing import (
    build_attached_input_attach_options,
    build_shield_status,
    get_input_kind,
    get_input_value,
    input_kind_label,
)
from app.ui.view_models import (
    _backup_summary,
    _change_state,
    _format_timestamp,
    _timestamp_data_value,
)


def planned_backup_coverage_summary(backend: Backend, settings: Settings) -> str:
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
        if not active_state or active_state == "unknown":
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
    # A cold post-mutation cache has no observation, not a failed runtime.
    if not active_state or active_state == "unknown":
        return {"value": "unknown", "tone": "queued"}
    return {"value": "unhealthy", "tone": "error"}


def output_signal_cards(detail: dict[str, object]) -> list[dict[str, object]]:
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


def output_info_rows(
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
        ("memory target", format_memory_limit(detail.get("resource_memory_high"))),
        ("memory cap", format_memory_limit(detail.get("resource_memory_max"))),
        (
            "cpu limit",
            format_cpu_limit(
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


def build_output_details(
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
        detail = build_output_detail(
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


def build_output_detail(
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
    runtime_health = runtime_health_view(
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


def build_output_page_context(
    data: OutputPageData,
    settings: Settings,
    *,
    prefer_cached_runtime: bool = False,
) -> dict[str, object]:
    attached_input_labels = [
        f"{input_kind_label(get_input_kind(item))}: {get_input_value(item)}"
        for item in sorted(data.backend.inputs, key=lambda item: item.id)
    ]

    detail = build_output_detail(
        backend=data.backend,
        service_map={},
        attached_input_ids=data.backend.input_ids,
        attached_input_labels=attached_input_labels,
        last_applied_at=data.last_applied_at,
        base_profile=data.base_profile,
        runtime_diagnostics=data.runtime_diagnostics,
    )
    if (
        data.backend.kind == "app"
        and data.backend.enabled
        and prefer_cached_runtime
        and data.runtime_diagnostics is None
    ):
        detail["runtime_health_label"] = ""
        detail["runtime_health_tone"] = "inactive"
        detail["runtime_health_summary"] = ""
        detail["runtime_health_detail"] = ""
        detail["runtime_health_alert"] = None
        detail["runtime_state_detail"] = ""
    input_attach_options = build_attached_input_attach_options(
        list(data.backend.inputs)
    )
    shield_status = build_shield_status(
        data.shield_backends, data.shield_inputs, settings
    )

    return {
        "inputs": list(data.backend.inputs),
        "input_attach_options": input_attach_options,
        "shield_status": shield_status,
        "input_attach_visible_count": sum(
            1 for item in input_attach_options if not item["attached"]
        ),
        "input_attach_lazy_available": data.total_input_count
        > len(data.backend.inputs),
        "input_attach_options_url": (
            f"/api/backends/{data.backend.id}/input-attach-options"
        ),
        "selected_backend": data.backend,
        "selected_output_detail": detail,
        "selected_output_live_metrics": service_metrics_from_status_payload(
            data.cached_status_payload, data.backend
        )
        or {},
        "metric_history_default_metric": DEFAULT_METRIC_KEY,
        "metric_history_default_timeframe": DEFAULT_TIMEFRAME_KEY,
        "metric_history_options": [spec.__dict__ for spec in METRIC_SPECS.values()],
        "metric_history_timeframes": [
            spec.__dict__ for spec in TIMEFRAME_SPECS.values()
        ],
        "output_signal_cards": output_signal_cards(detail),
        "attached_input_rows": detail.get("input_names", []),
        "output_info_rows": output_info_rows(data.backend, detail),
        "latest_backup": None,
        "backup_summary": _backup_summary(
            None,
            None,
            planned_coverage=planned_backup_coverage_summary(data.backend, settings),
        ),
        "backup_history": [],
        "backup_signals_pending": True,
        "clone_defaults": data.clone_defaults,
        "runtime_alert": detail.get("runtime_health_alert"),
        "recent_event_rows": event_rows(data.recent_events),
        "app_sandbox_profiles": app_sandbox_profiles(),
        "default_app_sandbox_profile": DEFAULT_UI_APP_SANDBOX_PROFILE,
        "static_root_hint": STATIC_ROOT_HINT,
        "csrf_token": get_csrf_token(settings),
        "operation_progress_pipelines": operation_progress_pipelines(),
        "output_save_progress_steps": output_save_progress_steps(),
        "flash_error": None,
        "flash_success": None,
        "beta_routing": settings.beta_routing,
        "beta_hardening": settings.beta_hardening,
        "multi_node_enabled": settings.multi_node_enabled,
        "multi_node_transfer_enabled": data.multi_node_app_enabled,
        "cluster_nodes": data.cluster_nodes,
        "multi_node_placement": build_output_placement_card(
            data.backend,
            data.cluster_nodes,
            data.placement_backends,
            data.placement_readiness,
        )
        if data.multi_node_app_enabled
        else None,
    }
