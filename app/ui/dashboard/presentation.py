"""Dashboard cards and settings presentation from supplied data."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.models.entities import Backend, Input
from app.security import get_csrf_token
from app.services.app_containers import configured_app_dns_servers
from app.services.backend_metric_history import (
    DEFAULT_TIMEFRAME_KEY,
    TIMEFRAME_SPECS,
)
from app.services.operation_runtime import (
    create_backend_progress_steps,
    input_progress_pipelines,
    operation_progress_pipelines,
    output_save_progress_steps,
)
from app.services.renderers import container_name
from app.services.resource_profile import ResourceProfile
from app.services.sandbox_profiles import app_sandbox_profiles
from app.ui.forms import (
    DEFAULT_UI_APP_SANDBOX_PROFILE,
    DOMAIN_INPUT_HINT,
    STATIC_ROOT_HINT,
    TAILNET_PATH_HINT,
)
from app.ui.outputs.presentation import build_output_details
from app.ui.read_models import (
    DashboardRows,
    DashboardScope,
)
from app.ui.resources import format_cpu_limit, resource_size_matrix_rows
from app.ui.routing import (
    build_backend_input_maps,
    build_input_attach_options,
    build_input_details,
    build_shield_status,
    pending_input_count,
    routing_counts,
)
from app.ui.settings import (
    _access_key_settings_summary,
    _netdata_settings_summary,
    _notification_settings_summary,
)
from app.ui.view_models import (
    _change_state,
    _format_timestamp,
    _save_job_summary,
    _tone_for_value,
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


def _utc_hour_for_local_display(hour: int) -> str:
    now = datetime.now(UTC)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def build_service_cards(
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


def build_nginx_card(nginx_status: dict[str, object]) -> tuple[str, dict[str, object]]:
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


def build_runtime_cards(
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
                    format_cpu_limit(
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


def dashboard_settings_context(
    settings: Settings,
    *,
    scope: DashboardScope,
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
        "resource_size_matrix": resource_size_matrix_rows(
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


def dashboard_input_context(
    *,
    scope: DashboardScope,
    inputs: list[Input],
    last_applied_at: datetime | None,
    settings: Settings,
) -> dict[str, object]:
    if not scope.needs_inputs:
        return {
            "input_details": {},
            "routing_rows": [],
            "pending_inputs": pending_input_count(inputs, last_applied_at),
        }
    input_details, routing_rows, pending_inputs = build_input_details(
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


def dashboard_output_context(
    *,
    scope: DashboardScope,
    backends: list[Backend],
    service_map: dict[str, dict[str, object]],
    backend_input_ids: dict[int, list[int]],
    backend_input_labels: dict[int, list[str]],
    last_applied_at: datetime | None,
    resource_profile: ResourceProfile,
) -> dict[str, object]:
    if scope.needs_outputs:
        output_details, pending_backends = build_output_details(
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


def dashboard_overview(
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


def dashboard_latest_save_job(
    scope: DashboardScope, status: dict[str, object]
) -> dict[str, object] | None:
    if not scope.includes("home"):
        return None
    return _save_job_summary(
        status.get("last_apply"),
        title="Most recent save",
        include_detail_dump=False,
    )


def build_dashboard_context(
    settings: Settings,
    *,
    scope: DashboardScope,
    rows: DashboardRows,
    status: dict[str, object],
    cluster_nodes: list[dict[str, str]],
    resource_profile: ResourceProfile,
    current_version: str,
    asset_version: str,
) -> dict[str, object]:
    backends = rows.backends
    inputs = rows.inputs
    last_applied_at = rows.last_applied_at

    backend_map = {backend.id: backend.name for backend in backends}
    enabled_backends = [backend for backend in backends if backend.enabled]
    enabled_inputs = [item for item in inputs if item.enabled]
    app_backends = [backend for backend in backends if backend.kind == "app"]
    enabled_app_backends = [
        backend for backend in enabled_backends if backend.kind == "app"
    ]
    static_backends = [backend for backend in backends if backend.kind == "static"]
    host_cpu_count = status.get("resource_profile", {}).get("host_cpu_count")
    settings_context = dashboard_settings_context(
        settings,
        scope=scope,
        resource_profile=resource_profile,
        host_cpu_count=host_cpu_count,
    )

    service_rows = status.get("services", [])
    service_map, services_ok = build_service_cards(service_rows)
    nginx_status = status.get("nginx") or {}
    _nginx_active_state, nginx_card = build_nginx_card(nginx_status)
    runtime_cards, _runtime_cpu_host_values, _runtime_memory_current_values = (
        build_runtime_cards(service_rows, enabled_app_backends)
        if scope.needs_settings
        else ([], [], [])
    )

    backend_input_ids, backend_input_labels = (
        build_backend_input_maps(inputs) if scope.needs_outputs else ({}, {})
    )
    input_attach_options = (
        build_input_attach_options(inputs) if scope.needs_outputs else []
    )
    shield_status = build_shield_status(backends, inputs, settings, service_map)
    input_attach_available_count = sum(
        1 for item in input_attach_options if item["attachable"]
    )
    route_total, route_active = (
        routing_counts(inputs) if scope.includes("home", "routing") else (0, 0)
    )

    input_context = dashboard_input_context(
        scope=scope,
        inputs=inputs,
        last_applied_at=last_applied_at,
        settings=settings,
    )
    output_context = dashboard_output_context(
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
    overview = dashboard_overview(
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
    latest_save_job = dashboard_latest_save_job(scope, status)
    visible_backends = backends if scope.needs_visible_entities else []
    visible_inputs = inputs if scope.needs_visible_entities else []

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
            or current_version
        ),
        "asset_version": asset_version,
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
        "beta_hardening": settings.beta_hardening,
        "multi_node_enabled": settings.multi_node_enabled,
        "cluster_nodes": cluster_nodes,
    }
