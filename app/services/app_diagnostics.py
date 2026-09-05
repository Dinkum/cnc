from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models.entities import Backend
from app.services.app_containers import (
    configured_app_dns_servers,
    container_ipv4_address,
    container_state_payload,
    inspect_container,
    inspect_network,
    network_uses_bridge_dns,
    read_saved_spec,
)
from app.services.app_healthchecks import (
    HEALTHCHECK_MODE_NONE,
    HEALTHCHECK_STATUS_UNMONITORED,
    display_backend_healthcheck_mode,
    probe_backend_health,
    resolve_backend_healthcheck,
)
from app.services.bootstrap_state import (
    bootstrap_lock_is_held,
    bootstrap_state_age_seconds,
    read_failure_artifact,
    read_bootstrap_state,
)
from app.services.cgroup_diagnostics import collect_container_cgroup_diagnostics
from app.services.commands import run_command
from app.services.container_runtime import (
    container_exists,
    inspect_result_reports_missing,
    list_external_containers,
    list_mounted_containers,
)
from app.services.guest_exec import (
    EXEC_CIRCUIT_OPEN_RETURN_CODE,
    EXEC_GUARD_BUSY_RETURN_CODE,
    EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
    read_guest_exec_circuit,
    run_backend_guest_command,
)
from app.services.renderers import container_name, network_name, safe_slug
from app.services.sandbox_profiles import app_sandbox_rootfs_path


@dataclass(frozen=True)
class AppDiagnosticsSnapshot:
    external_containers: list[dict[str, Any]]
    mounted_containers: list[dict[str, Any]]


def collect_app_diagnostics_snapshot(settings: Settings) -> AppDiagnosticsSnapshot:
    return AppDiagnosticsSnapshot(
        external_containers=_load_external_containers(settings),
        mounted_containers=_load_mounted_containers(settings),
    )


def _load_external_containers(settings: Settings) -> list[dict[str, Any]]:
    return list_external_containers(settings.command_timeout_status_sec)


def _container_name_registered(container: str, settings: Settings) -> bool:
    return container_exists(container, settings.command_timeout_status_sec)


def _load_mounted_containers(settings: Settings) -> list[dict[str, Any]]:
    return list_mounted_containers(settings.command_timeout_status_sec)


def _entry_names(entry: dict[str, Any]) -> list[str]:
    names = entry.get("Names")
    if isinstance(names, list):
        return [value for value in names if isinstance(value, str)]
    if isinstance(names, str):
        return [names]
    name = entry.get("Name")
    if isinstance(name, str):
        return [name]
    return []


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("Id") or entry.get("id") or entry.get("ContainerID")
    return value if isinstance(value, str) else ""


def _loopback_binding_entries(
    inspect_payload: dict[str, Any] | None, handoff_port: int
) -> list[dict[str, Any]]:
    if not isinstance(inspect_payload, dict):
        return []
    binding_key = f"{handoff_port}/tcp"
    host_config = inspect_payload.get("HostConfig")
    if isinstance(host_config, dict):
        port_bindings = host_config.get("PortBindings")
        if isinstance(port_bindings, dict):
            entries = port_bindings.get(binding_key)
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
    network_settings = inspect_payload.get("NetworkSettings")
    if isinstance(network_settings, dict):
        port_bindings = network_settings.get("Ports")
        if isinstance(port_bindings, dict):
            entries = port_bindings.get(binding_key)
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _loopback_publish_present(
    backend: Backend, inspect_payload: dict[str, Any] | None
) -> bool:
    if backend.port is None:
        return True
    for entry in _loopback_binding_entries(inspect_payload, backend.handoff_port):
        host_port = str(entry.get("HostPort") or "").strip()
        host_ip = str(entry.get("HostIp") or "").strip()
        if host_port != str(backend.port):
            continue
        if host_ip in {"", "127.0.0.1", "::1", "localhost"}:
            return True
    return False


def _legacy_loopback_proxy_paths(
    settings: Settings, backend_name: str
) -> tuple[Path, Path]:
    slug = safe_slug(backend_name)
    service = settings.systemd_generated_dir / f"cnc-proxy-{slug}.service"
    socket = settings.systemd_generated_dir / f"cnc-proxy-{slug}.socket"
    return service, socket


def _legacy_loopback_proxy_present(settings: Settings, backend_name: str) -> bool:
    service, socket = _legacy_loopback_proxy_paths(settings, backend_name)
    return service.exists() or socket.exists()


def _legacy_reboot_bridge_present(settings: Settings, container: str) -> bool:
    return (
        settings.systemd_generated_dir / f"{container}-legacy-restart.service"
    ).exists()


def _container_is_quadlet_managed(inspect_payload: dict[str, Any] | None) -> bool:
    if not isinstance(inspect_payload, dict):
        return False
    config = inspect_payload.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        labels = inspect_payload.get("Labels")
    if not isinstance(labels, dict):
        return False
    return bool(labels.get("PODMAN_SYSTEMD_UNIT"))


def _runtime_ownership(
    *,
    backend: Backend,
    settings: Settings,
    inspect_payload: dict[str, Any] | None,
    container: str,
) -> tuple[str, str]:
    if backend.kind != "app":
        return "static", "static files"
    if inspect_payload is None:
        return "missing", "no running container"
    if _container_is_quadlet_managed(inspect_payload):
        return "quadlet", "Quadlet/systemd"
    if _legacy_reboot_bridge_present(settings, container):
        return "legacy_bridge", "legacy Podman + reboot bridge"
    return "legacy_direct", "legacy direct Podman"


def _classify_runtime_diagnosis(
    *,
    runtime_observation_available: bool = True,
    container_exists: bool,
    backend_exec_available: bool,
    backend_exec_status: str,
    guest_ready: bool,
    target_ip: str | None,
    healthcheck_enabled: bool,
    private_ok: bool,
    loopback_publish_present: bool,
    backend_port: int | None,
    loopback_ok: bool,
) -> tuple[str, str, str, str]:
    if not runtime_observation_available:
        return (
            "observation_unavailable",
            "unknown",
            "unknown",
            "sandbox_observation_unavailable",
        )
    sandbox_status = "ready" if container_exists else "missing"
    if not container_exists:
        return (
            sandbox_status,
            "missing",
            "unknown",
            "sandbox_missing",
        )

    if not backend_exec_available and backend_exec_status == "busy":
        return (
            sandbox_status,
            "observation_deferred",
            "unknown",
            "backend_observation_deferred",
        )

    if not backend_exec_available and backend_exec_status == "guard_unavailable":
        return (
            sandbox_status,
            "observation_unavailable",
            "unknown",
            "backend_observation_unavailable",
        )

    if not backend_exec_available:
        return (
            sandbox_status,
            "exec_unavailable",
            "unknown",
            "backend_exec_unavailable",
        )

    guest_status = "ready" if guest_ready else "init_broken"
    if not guest_ready:
        return (
            sandbox_status,
            guest_status,
            "unknown",
            "guest_init_broken",
        )

    if target_ip is None or (backend_port is not None and not loopback_publish_present):
        return (
            sandbox_status,
            guest_status,
            "publish_broken",
            "sandbox_publish_broken",
        )

    if not healthcheck_enabled:
        return (
            sandbox_status,
            guest_status,
            "unmonitored",
            "app_unmonitored",
        )

    if not private_ok or (backend_port is not None and not loopback_ok):
        return (
            sandbox_status,
            guest_status,
            "down",
            "app_service_down",
        )

    return (
        sandbox_status,
        guest_status,
        "healthy",
        "healthy",
    )


def collect_app_backend_diagnostics(
    backend: Backend,
    settings: Settings,
    *,
    snapshot: AppDiagnosticsSnapshot | None = None,
) -> dict[str, Any]:
    container = container_name(backend.name)
    network = network_name(backend.name)
    inspect_result, inspect_payload = inspect_container(
        container, settings.command_timeout_status_sec
    )
    network_result, network_payload = inspect_network(
        network, settings.command_timeout_status_sec
    )
    container_observation_available = bool(
        inspect_payload is not None or inspect_result_reports_missing(inspect_result)
    )
    network_observation_available = bool(
        network_payload is not None or inspect_result_reports_missing(network_result)
    )
    runtime_observation_available = (
        container_observation_available and network_observation_available
    )
    container_missing = container_observation_available and inspect_payload is None
    network_missing = network_observation_available and network_payload is None
    saved_spec = read_saved_spec(settings, backend.name)
    bootstrap_state = read_bootstrap_state(settings, backend.name)
    failure_artifact = read_failure_artifact(settings, backend.name)
    bootstrap_lock_active = bootstrap_lock_is_held(settings, backend.name)
    external_containers = (
        snapshot.external_containers
        if snapshot is not None
        else _load_external_containers(settings)
    )
    mounted_containers = (
        snapshot.mounted_containers
        if snapshot is not None
        else _load_mounted_containers(settings)
    )
    external_entries = [
        entry for entry in external_containers if container in _entry_names(entry)
    ]
    mounted_entries_by_id = {
        _entry_id(entry): entry for entry in mounted_containers if _entry_id(entry)
    }
    mounted_external_entries = [
        mounted_entries_by_id[_entry_id(entry)]
        for entry in external_entries
        if _entry_id(entry) in mounted_entries_by_id
    ]
    state_payload = container_state_payload(container, inspect_payload)
    name_registered = (
        True
        if inspect_payload is not None
        else (
            _container_name_registered(container, settings)
            if container_observation_available
            else False
        )
    )
    target_ip = container_ipv4_address(backend, inspect_payload)
    dns_servers = []
    if isinstance(saved_spec, dict):
        raw_dns_servers = saved_spec.get("dns_servers")
        if isinstance(raw_dns_servers, list):
            dns_servers = [entry for entry in raw_dns_servers if isinstance(entry, str)]
    if not dns_servers:
        dns_servers = configured_app_dns_servers(settings)

    guest_system_state = "missing"
    guest_multi_user_target = "missing"
    guest_ready = False
    guest_ready_error = ""
    backend_exec_available = False
    backend_exec_status = "not_checked"
    backend_exec_error = ""
    if inspect_payload is not None:
        guest_state_result = run_backend_guest_command(
            backend.name,
            settings,
            [
                "/bin/sh",
                "-c",
                (
                    "state=$(systemctl is-system-running 2>&1 || true); "
                    "target=$(systemctl is-active multi-user.target 2>&1 || true); "
                    'printf \'%s\\n%s\\n\' "$state" "$target"'
                ),
            ],
            timeout_sec=settings.command_timeout_status_sec,
            command_runner=run_command,
            source="status_probe",
        )
        backend_exec_available = guest_state_result.ok
        backend_exec_status = {
            EXEC_CIRCUIT_OPEN_RETURN_CODE: "circuit_open",
            EXEC_GUARD_BUSY_RETURN_CODE: "busy",
            EXEC_GUARD_UNAVAILABLE_RETURN_CODE: "guard_unavailable",
            124: "timeout",
        }.get(
            guest_state_result.returncode,
            "available" if backend_exec_available else "failed",
        )
        if not backend_exec_available:
            backend_exec_error = (
                guest_state_result.stderr
                or guest_state_result.stdout
                or f"exit {guest_state_result.returncode}"
            )
            guest_ready_error = backend_exec_error
        else:
            state_lines = guest_state_result.stdout.splitlines()
            guest_system_state = (
                state_lines[0].strip().lower() if state_lines else "unknown"
            )
            guest_multi_user_target = (
                state_lines[1].strip().lower() if len(state_lines) > 1 else "unknown"
            )
            guest_ready = (
                guest_multi_user_target == "active"
                and guest_system_state in {"running", "degraded"}
            )
            if not guest_ready:
                guest_ready_error = (
                    guest_state_result.stderr or guest_state_result.stdout
                )

    bootstrap_status = "missing"
    if guest_ready:
        bootstrap_status = "succeeded"
    elif isinstance(bootstrap_state, dict) and isinstance(
        bootstrap_state.get("status"), str
    ):
        bootstrap_status = str(bootstrap_state.get("status"))
    elif inspect_payload is not None:
        bootstrap_status = "pending"

    bootstrap_age_seconds = bootstrap_state_age_seconds(bootstrap_state)
    bootstrap_stale = (
        bootstrap_status == "running"
        and inspect_payload is not None
        and not guest_ready
        and bootstrap_age_seconds is not None
        and bootstrap_age_seconds >= settings.app_bootstrap_stale_after_sec
    )
    if bootstrap_stale:
        bootstrap_status = "stuck"

    healthcheck_plan = resolve_backend_healthcheck(backend)
    healthcheck_enabled = healthcheck_plan.effective_mode != HEALTHCHECK_MODE_NONE
    private_ok = False
    private_reachable: bool | None = None
    private_health_status = (
        "not_checked" if healthcheck_enabled else HEALTHCHECK_STATUS_UNMONITORED
    )
    private_error = ""
    if target_ip:
        private_probe = probe_backend_health(
            backend,
            host=target_ip,
            port=backend.handoff_port,
            timeout_sec=min(settings.command_timeout_status_sec, 5),
            command_runner=run_command,
        )
        private_ok = private_probe.ok
        private_reachable = private_probe.ok if private_probe.checked else None
        private_health_status = private_probe.status
        private_error = private_probe.error

    loopback_ok = False
    loopback_reachable: bool | None = None
    loopback_health_status = (
        "not_checked" if healthcheck_enabled else HEALTHCHECK_STATUS_UNMONITORED
    )
    loopback_error = ""
    if backend.port is not None:
        loopback_probe = probe_backend_health(
            backend,
            host="127.0.0.1",
            port=backend.port,
            timeout_sec=min(settings.command_timeout_status_sec, 5),
            command_runner=run_command,
        )
        loopback_ok = loopback_probe.ok
        loopback_reachable = loopback_probe.ok if loopback_probe.checked else None
        loopback_health_status = loopback_probe.status
        loopback_error = loopback_probe.error
    loopback_publish_present = _loopback_publish_present(backend, inspect_payload)
    legacy_loopback_proxy_present = _legacy_loopback_proxy_present(
        settings, backend.name
    )
    runtime_owner, runtime_owner_label = _runtime_ownership(
        backend=backend,
        settings=settings,
        inspect_payload=inspect_payload,
        container=container,
    )

    issues: list[str] = []
    observation_issues: list[str] = []
    maintenance_issues: list[str] = []
    if not container_observation_available:
        observation_issues.append("container_inspect_unavailable")
    if not network_observation_available:
        observation_issues.append("network_inspect_unavailable")
    if container_missing and saved_spec is not None:
        issues.append("container_missing")
    if container_missing and external_entries:
        issues.append("storage_orphan")
    if container_missing and mounted_external_entries:
        issues.append("mounted_storage_orphan")
    if container_missing and name_registered:
        issues.append("stale_name_registration")
    if container_missing and network_payload is not None:
        issues.append("network_orphan")
    if network_uses_bridge_dns(network_payload):
        issues.append("bridge_dns_enabled")
    if inspect_payload is not None and saved_spec is None:
        issues.append("missing_saved_spec")
    if network_missing:
        issues.append("network_missing")
    if bootstrap_status == "failed":
        issues.append("bootstrap_failed")
    if bootstrap_status == "stuck":
        issues.append("bootstrap_stuck")
    if inspect_payload is not None and backend_exec_status == "busy":
        observation_issues.append("podman_exec_busy")
    elif inspect_payload is not None and backend_exec_status == "guard_unavailable":
        observation_issues.append("podman_exec_guard_unavailable")
    elif inspect_payload is not None and not backend_exec_available:
        issues.append("podman_exec_unavailable")
    if inspect_payload is not None and backend_exec_available and not guest_ready:
        issues.append("guest_system_unready")
    if inspect_payload is not None and target_ip is None:
        issues.append("private_ip_missing")
    if (
        inspect_payload is not None
        and target_ip is not None
        and private_reachable is False
    ):
        issues.append("private_unreachable")
    if (
        inspect_payload is not None
        and backend.port is not None
        and not loopback_publish_present
    ):
        issues.append("loopback_publish_missing")
    if (
        inspect_payload is not None
        and backend.port is not None
        and not loopback_publish_present
        and legacy_loopback_proxy_present
    ):
        issues.append("legacy_loopback_proxy_present")
    if (
        inspect_payload is not None
        and backend.port is not None
        and loopback_reachable is False
    ):
        issues.append("loopback_unreachable")
    if runtime_owner == "legacy_bridge":
        maintenance_issues.append("legacy_direct_podman_runtime_owner")

    sandbox_status, guest_status, app_handoff_status, diagnosis = (
        _classify_runtime_diagnosis(
            runtime_observation_available=runtime_observation_available,
            container_exists=inspect_payload is not None,
            backend_exec_available=backend_exec_available,
            backend_exec_status=backend_exec_status,
            guest_ready=guest_ready,
            target_ip=target_ip,
            healthcheck_enabled=healthcheck_enabled,
            private_ok=private_ok,
            loopback_publish_present=loopback_publish_present,
            backend_port=backend.port,
            loopback_ok=loopback_ok,
        )
    )
    app_handoff_error = ""
    if app_handoff_status == "publish_broken":
        if target_ip is None:
            app_handoff_error = "missing private publish target"
        elif backend.port is not None and not loopback_publish_present:
            app_handoff_error = "missing loopback publish binding"
    elif app_handoff_status == "down":
        app_handoff_error = private_error or loopback_error

    return {
        "backend": backend.name,
        "kind": backend.kind,
        "container": container,
        "container_exists": inspect_payload is not None,
        "container_observation_available": container_observation_available,
        "container_id": inspect_payload.get("Id")
        if isinstance(inspect_payload, dict)
        else None,
        "container_name_registered": name_registered,
        "container_state": state_payload,
        "cgroup": collect_container_cgroup_diagnostics(
            inspect_payload,
            backend_name=backend.name,
            timeout_sec=settings.command_timeout_status_sec,
        ),
        "saved_spec_present": saved_spec is not None,
        "saved_spec": saved_spec,
        "network": network,
        "network_exists": network_payload is not None,
        "network_observation_available": network_observation_available,
        "healthcheck_mode": display_backend_healthcheck_mode(backend),
        "healthcheck_effective_mode": healthcheck_plan.effective_mode,
        "healthcheck_enabled": healthcheck_enabled,
        "healthcheck_path": healthcheck_plan.path,
        "healthcheck_host_header": healthcheck_plan.host_header,
        "sandbox_profile": backend.sandbox_profile,
        "guest_rootfs": str(app_sandbox_rootfs_path(settings, backend.name)),
        "private_ip": target_ip,
        "private_reachable": private_reachable,
        "private_health_status": private_health_status,
        "private_error": private_error,
        "loopback_port": backend.port,
        "loopback_reachable": loopback_reachable,
        "loopback_health_status": loopback_health_status,
        "loopback_error": loopback_error,
        "loopback_publish_present": loopback_publish_present,
        "legacy_loopback_proxy_present": legacy_loopback_proxy_present,
        "runtime_owner": runtime_owner,
        "runtime_owner_label": runtime_owner_label,
        "backend_exec_available": backend_exec_available,
        "backend_exec_status": backend_exec_status,
        "backend_exec_error": backend_exec_error,
        "backend_exec_circuit": read_guest_exec_circuit(settings, backend.name),
        "observation_deferred": (
            not runtime_observation_available
            or backend_exec_status in {"busy", "guard_unavailable"}
        ),
        "observation_issues": observation_issues,
        "sandbox_status": sandbox_status,
        "guest_status": guest_status,
        "app_handoff_status": app_handoff_status,
        "diagnosis": diagnosis,
        "app_handoff_error": app_handoff_error,
        "bootstrap": {
            "status": bootstrap_status,
            "state": bootstrap_state,
            "lock_active": bootstrap_lock_active,
            "age_seconds": bootstrap_age_seconds,
            "stale": bootstrap_stale,
            "guest_ready": guest_ready,
            "guest_system_state": guest_system_state,
            "guest_multi_user_target": guest_multi_user_target,
            "guest_ready_error": guest_ready_error,
        },
        "dns_servers": dns_servers,
        "external_containers": external_entries,
        "mounted_external_containers": mounted_external_entries,
        "failure_artifact": failure_artifact,
        "issues": issues,
        "maintenance_issues": maintenance_issues,
        "ok": not issues
        and inspect_payload is not None
        and backend_exec_available
        and network_payload is not None
        and bootstrap_status == "succeeded"
        and guest_ready
        and target_ip is not None
        and (not healthcheck_enabled or private_ok)
        and (not healthcheck_enabled or backend.port is None or loopback_ok),
        "inspect_error": (inspect_result.stderr or inspect_result.stdout)
        if not inspect_result.ok
        else "",
    }
