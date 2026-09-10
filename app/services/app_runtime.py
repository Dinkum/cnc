from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import time
import uuid
from typing import Any

from app.services.hardening_policy import (
    hardening_podman_args,
    hardening_volumes,
    read_hardening_policy,
)
from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.app_containers import (
    APP_DEBUG_TOOLBELT_VERSION,
    app_debug_tool_commands,
    app_sandbox_profile_state_path,
    app_sandbox_rootfs_path,
    configured_app_dns_servers,
    container_ipv4_address,
    control_dir,
    network_uses_bridge_dns,
    read_saved_spec,
    runtime_spec,
    write_app_control_assets,
)
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.app_healthchecks import (
    display_backend_healthcheck_mode,
    probe_backend_health,
)
from app.services.app_memory_slice import APPS_SLICE_UNIT, reconcile_apps_slice
from app.services.app_network_isolation import reconcile_app_network_isolation
from app.services.app_quadlet import (
    AppQuadletWriteError,
    managed_quadlet_backend_names,
    managed_quadlet_interface_network_names,
    quadlet_container_path,
    quadlet_container_service_name,
    quadlet_interface_network_path,
    quadlet_interface_network_service_name,
    quadlet_network_path,
    quadlet_network_service_name,
    remove_interface_quadlet_asset,
    remove_app_quadlet_assets,
    write_app_quadlet_assets,
)
from app.services.apply_core import (
    AppRuntimeReconcileResult,
    ApplyFailed,
    DesiredState,
    clip_output,
)
from app.services.bootstrap_state import (
    BackendBootstrapLock,
    append_bootstrap_event,
    bootstrap_lock_is_held,
    clear_failure_artifact,
    read_bootstrap_state,
    write_bootstrap_state,
    write_failure_artifact,
)
from app.services.commands import CommandError
from app.services.guest_exec import (
    EXEC_CIRCUIT_OPEN_RETURN_CODE,
    EXEC_GUARD_BUSY_RETURN_CODE,
    EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
    clear_guest_exec_circuit,
    run_backend_guest_command,
)
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.resource_profile import ResourceProfile, backend_resource_profile
from app.services.renderers import container_name, network_name
from app.services.runtime_services import (
    AppRuntimeServices,
    default_app_runtime_services,
)
from app.services.safe_tar import safe_extract_tar
from app.services.guest_isolation import (
    guest_podman_args,
    guest_rootfs_argument,
    guest_volume_arguments,
    protect_sandbox_directory,
    preflight_guest_storage,
    validate_guest_hardening,
)
from app.services.sandbox_profiles import get_app_sandbox_profile
from app.services.systemd_memory import (
    SystemdMemoryPolicyError,
    converge_systemd_memory_policy,
)
from app.services.validators import ensure_backend_name, parse_volumes_json


logger = get_logger("apply.runtime")

_RUNTIME_GUEST_EXEC_GUARD_WAIT_SEC = 0.25
_TERMINAL_GUEST_EXEC_OUTCOMES = {
    "circuit_open",
    "guard_busy",
    "guard_unavailable",
    "timeout",
}


def preflight_app_storage(
    desired: DesiredState,
    selected_backend_names: set[str],
) -> dict[str, Any]:
    volumes_by_backend = {
        backend.name: hardening_volumes(
            read_hardening_policy(backend), parse_volumes_json(backend.volumes_json)
        )
        for backend in desired.known_app_backends
    }
    mounted_sources = [
        Path(volume.split(":", 1)[0])
        for volumes in volumes_by_backend.values()
        for volume in volumes
    ]
    blockers: list[dict[str, str]] = []
    repaired: list[str] = []
    for name in sorted(
        selected_backend_names & desired.runtime_graph.enabled_app_backend_names
    ):
        try:
            guest_volume_arguments(volumes_by_backend[name])
            for volume in volumes_by_backend[name]:
                repaired.extend(
                    preflight_guest_storage([volume], mounted_sources=mounted_sources)
                )
        except (ValueError, OSError) as exc:
            blockers.append({"backend": name, "reason": str(exc)})
    details = {"storage_repairs": sorted(set(repaired)), "storage_blockers": blockers}
    if repaired:
        logger.info("app.storage.repaired", **details)
    if blockers:
        summary = "; ".join(f"{item['backend']}: {item['reason']}" for item in blockers)
        raise ApplyFailed(
            summary,
            phase="storage_preflight",
            details={
                **details,
                "failed_backend": blockers[0]["backend"],
                "operator_message": blockers[0]["reason"]
                if len(blockers) == 1
                else summary,
            },
        )
    return details


async def apply_app_backends(
    desired: DesiredState,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
    selected_backend_names: set[str] | None = None,
    cleanup_deleted: bool = True,
    cleanup_deleted_filesystem: bool = True,
    reconcile_network_isolation: bool = True,
    operation_id: int | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    containers_created: list[str] = []
    containers_recreated: list[str] = []
    containers_bootstrapped: list[str] = []
    containers_running: list[str] = []
    containers_stopped: list[str] = []
    containers_removed: list[str] = []
    networks_removed: list[str] = []
    container_dns: dict[str, list[str]] = {}
    disabled_runtime_cleanup: list[dict[str, Any]] = []
    reconcilers: list[AppRuntimeReconciler] = []
    completed_runtime_backends: list[str] = []
    active_backend_name: str | None = None
    known_backends = {backend.name: backend for backend in desired.known_app_backends}
    desired_app_names = desired.runtime_graph.known_app_backend_names
    interface_links = tuple(desired.runtime_graph.inter_app_interfaces)
    desired_interface_network_names = {item.network for item in interface_links}
    selected_names = (
        set(selected_backend_names)
        if selected_backend_names is not None
        else set(desired_app_names)
    )

    apps_slice = await reconcile_apps_slice(
        desired.resource_profile,
        settings,
        services=services,
    )

    if cleanup_deleted:
        cleanup = await _cleanup_deleted_app_runtime(
            desired_app_names,
            settings,
            services=services,
            desired_interface_network_names=desired_interface_network_names,
            cleanup_filesystem=cleanup_deleted_filesystem,
        )
    else:
        cleanup = {
            "containers_removed": [],
            "networks_removed": [],
            "quadlet_files_removed": [],
            "legacy_reboot_bridges_removed": [],
            "filesystem_artifacts_removed": [],
        }
        interface_cleanup = await _cleanup_stale_interface_runtime(
            desired_interface_network_names,
            settings,
            services=services,
        )
        cleanup["networks_removed"].extend(interface_cleanup["networks_removed"])
        cleanup["quadlet_files_removed"].extend(
            interface_cleanup["quadlet_files_removed"]
        )
    containers_removed.extend(cleanup["containers_removed"])
    networks_removed.extend(cleanup["networks_removed"])
    quadlet_files_removed = list(cleanup["quadlet_files_removed"])
    legacy_reboot_bridges_removed = list(cleanup["legacy_reboot_bridges_removed"])
    filesystem_artifacts_removed = list(cleanup["filesystem_artifacts_removed"])

    try:
        for backend_name in sorted(
            desired.runtime_graph.enabled_app_backend_names & selected_names
        ):
            active_backend_name = backend_name
            backend = known_backends[backend_name]
            backend_interface_links = tuple(
                item
                for item in interface_links
                if item.source_backend == backend.name
                or item.target_backend == backend.name
            )
            reconciler = AppRuntimeReconciler(
                backend,
                settings,
                base_profile=desired.resource_profile,
                inter_app_interfaces=backend_interface_links,
                services=services,
                operation_id=operation_id,
            )
            reconcilers.append(reconciler)
            app_state = await reconciler.reconcile_for_publish()
            container = app_state.container
            containers_running.append(container)
            if app_state.created:
                containers_created.append(container)
            if app_state.recreated:
                containers_recreated.append(container)
            if app_state.bootstrapped:
                containers_bootstrapped.append(container)
            container_dns[container] = list(app_state.dns_servers or [])
            completed_runtime_backends.append(backend_name)
            active_backend_name = None
    except Exception as exc:
        try:
            cleanup_results = await _cleanup_failed_new_app_runtimes(
                reconcilers,
                settings,
                services=services,
            )
        except Exception as cleanup_exc:
            logger.warning("app.create.rollback_cleanup_failed", error=str(cleanup_exc))
        else:
            if cleanup_results:
                logger.warning("app.create.rollback_cleanup", results=cleanup_results)
        selected_runtime_backends = sorted(
            desired.runtime_graph.enabled_app_backend_names & selected_names
        )
        pending_runtime_backends = [
            name
            for name in selected_runtime_backends
            if name not in completed_runtime_backends and name != active_backend_name
        ]
        failure_details = {
            "selected_backends": selected_runtime_backends,
            "completed_backends": completed_runtime_backends,
            "failed_backend": active_backend_name,
            "pending_backends": pending_runtime_backends,
            "operation_id": operation_id,
        }
        if isinstance(exc, ApplyFailed):
            raise ApplyFailed(
                str(exc),
                phase=exc.phase,
                details={**exc.details, **failure_details},
                error_code=exc.error_code,
            ) from exc
        raise ApplyFailed(
            str(exc), phase="app_runtime", details=failure_details
        ) from exc

    disabled_app_names = (
        desired_app_names - desired.runtime_graph.enabled_app_backend_names
    ) & selected_names
    for backend_name in sorted(disabled_app_names):
        backend = known_backends[backend_name]
        cleanup_details = await _cleanup_disabled_app_backend(
            backend, settings, services=services
        )
        if cleanup_details["actions"]:
            disabled_runtime_cleanup.append(cleanup_details)
        if cleanup_details["container_stopped"]:
            containers_stopped.append(cleanup_details["container"])
        if cleanup_details["container_removed"]:
            containers_removed.append(cleanup_details["container"])
        if cleanup_details["network_removed"]:
            networks_removed.append(cleanup_details["network"])
        quadlet_files_removed.extend(cleanup_details["quadlet_files_removed"])

    phase_details: dict[str, list[dict[str, Any]]] = {}
    for reconciler in reconcilers:
        app_state = await reconciler.verify_steady_state()
        phase_details[app_state.backend] = list((app_state.phases or {}).values())

    app_healthcheck_failures = _app_healthcheck_failures(phase_details)
    if reconcile_network_isolation:
        isolation_details = await reconcile_app_network_isolation(
            desired.runtime_graph.known_app_backend_names,
            settings,
            services=services,
        )
    else:
        isolation_details = {
            "skipped": True,
            "reason": "network_isolation slice unchanged",
        }

    return {
        "apps_memory_slice": apps_slice,
        "app_containers": sorted(
            node.container for node in desired.runtime_graph.app_backends.values()
        ),
        "app_containers_created": sorted(containers_created),
        "app_containers_recreated": sorted(containers_recreated),
        "app_containers_bootstrapped": sorted(containers_bootstrapped),
        "app_containers_running": sorted(containers_running),
        "app_containers_stopped": sorted(containers_stopped),
        "app_containers_removed": sorted(containers_removed),
        "app_networks_removed": sorted(networks_removed),
        "app_quadlet_files_removed": sorted(quadlet_files_removed),
        "app_legacy_reboot_bridges_removed": sorted(legacy_reboot_bridges_removed),
        "app_filesystem_artifacts_removed": sorted(filesystem_artifacts_removed),
        "app_disabled_runtime_cleanup": sorted(
            disabled_runtime_cleanup, key=lambda item: str(item.get("backend") or "")
        ),
        "app_container_dns": {
            key: value for key, value in sorted(container_dns.items())
        },
        "app_reconcile_phases": {
            key: phase_details[key] for key in sorted(phase_details)
        },
        "app_healthcheck_status": "degraded" if app_healthcheck_failures else "ok",
        "app_healthcheck_failures": app_healthcheck_failures,
        "app_network_isolation": isolation_details,
        "app_control_dir": str(settings.app_control_dir),
    }


async def _cleanup_deleted_app_runtime(
    desired_app_names: set[str],
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
    desired_interface_network_names: set[str] | None = None,
    cleanup_filesystem: bool = True,
) -> dict[str, list[str]]:
    services = services or default_app_runtime_services()
    desired_interface_network_names = set(desired_interface_network_names or set())
    containers_removed: list[str] = []
    networks_removed: list[str] = []
    deleted_backend_names: set[str] = set()
    interface_cleanup = await _cleanup_stale_interface_runtime(
        desired_interface_network_names,
        settings,
        services=services,
    )

    for entry in _managed_podman_entries(
        services.run_command(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                "label=io.cnc.managed=true",
                "--format",
                "json",
            ],
            settings.command_timeout_status_sec,
        ).stdout
    ):
        backend_name = _entry_backend_label(entry)
        if not backend_name or backend_name in desired_app_names:
            continue
        deleted_backend_names.add(backend_name)
        target = _entry_primary_name(entry) or _entry_id(entry)
        if not target:
            continue
        await _stop_app_quadlet_backend(backend_name, settings, services=services)
        _remove_podman_container(target, settings, services=services)
        containers_removed.append(target)
        _clear_runtime_artifacts_for_recreate(settings, backend_name, clear_lock=True)

    for entry in _managed_podman_entries(
        services.run_command(
            [
                "podman",
                "network",
                "ls",
                "--filter",
                "label=io.cnc.managed=true",
                "--format",
                "json",
            ],
            settings.command_timeout_status_sec,
        ).stdout
    ):
        if _entry_label(entry, "io.cnc.kind") == "interface":
            continue
        backend_name = _entry_backend_label(entry)
        if not backend_name or backend_name in desired_app_names:
            continue
        deleted_backend_names.add(backend_name)
        target = _entry_primary_name(entry) or _entry_id(entry)
        if not target:
            continue
        _stop_app_quadlet_network(backend_name, settings, services=services)
        await services.run_command_checked_async(
            ["podman", "network", "rm", target],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        networks_removed.append(target)

    quadlet_files_removed: list[str] = []
    legacy_reboot_bridges_removed: list[str] = []
    deleted_backend_names.update(
        backend_name
        for backend_name in managed_quadlet_backend_names(settings)
        if backend_name not in desired_app_names
    )
    networks_removed.extend(interface_cleanup["networks_removed"])
    quadlet_files_removed.extend(interface_cleanup["quadlet_files_removed"])
    for backend_name in sorted(deleted_backend_names):
        quadlet_files_removed.extend(remove_app_quadlet_assets(settings, backend_name))
        bridge_path = _legacy_reboot_bridge_path(settings, container_name(backend_name))
        if bridge_path.exists():
            await services.run_command_checked_async(
                ["systemctl", "disable", bridge_path.name],
                timeout_sec=settings.command_timeout_apply_sec,
            )
            bridge_path.unlink()
            legacy_reboot_bridges_removed.append(str(bridge_path))
    if (
        containers_removed
        or networks_removed
        or quadlet_files_removed
        or legacy_reboot_bridges_removed
    ):
        await _systemctl_daemon_reload(settings, services=services)
    filesystem_artifacts_removed = (
        _cleanup_deleted_app_filesystem_artifacts(desired_app_names, settings)
        if cleanup_filesystem
        else []
    )

    return {
        "containers_removed": containers_removed,
        "networks_removed": networks_removed,
        "quadlet_files_removed": quadlet_files_removed,
        "legacy_reboot_bridges_removed": legacy_reboot_bridges_removed,
        "filesystem_artifacts_removed": filesystem_artifacts_removed,
    }


async def _cleanup_stale_interface_runtime(
    desired_interface_network_names: set[str],
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> dict[str, list[str]]:
    networks_removed: list[str] = []
    quadlet_files_removed: list[str] = []
    for entry in _managed_podman_entries(
        services.run_command(
            [
                "podman",
                "network",
                "ls",
                "--filter",
                "label=io.cnc.managed=true",
                "--format",
                "json",
            ],
            settings.command_timeout_status_sec,
        ).stdout
    ):
        if _entry_label(entry, "io.cnc.kind") != "interface":
            continue
        target = _entry_primary_name(entry) or _entry_id(entry)
        if not target or target in desired_interface_network_names:
            continue
        _stop_interface_quadlet_network(target, settings, services=services)
        await services.run_command_checked_async(
            ["podman", "network", "rm", target],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        networks_removed.append(target)

    for network in sorted(
        managed_quadlet_interface_network_names(settings)
        - desired_interface_network_names
    ):
        quadlet_files_removed.extend(remove_interface_quadlet_asset(settings, network))
    if networks_removed or quadlet_files_removed:
        await _systemctl_daemon_reload(settings, services=services)
    return {
        "networks_removed": networks_removed,
        "quadlet_files_removed": quadlet_files_removed,
    }


def _cleanup_deleted_app_filesystem_artifacts(
    desired_app_names: set[str],
    settings: Settings,
) -> list[str]:
    backend_names: set[str] = set()
    if settings.app_control_dir.exists():
        for path in settings.app_control_dir.iterdir():
            if (
                path.is_dir()
                and path.name not in desired_app_names
                and _control_dir_has_runtime_artifacts(path)
            ):
                backend_names.add(path.name)
    if settings.app_sandbox_dir.exists():
        for path in settings.app_sandbox_dir.iterdir():
            if (
                path.is_dir()
                and path.name not in desired_app_names
                and _sandbox_dir_has_runtime_artifacts(path)
            ):
                backend_names.add(path.name)

    return _remove_deleted_app_filesystem_artifacts(backend_names, settings)


async def remove_deleted_app_filesystem_artifacts(
    backend_names: set[str], settings: Settings
) -> list[str]:
    """Remove marked persistent state for exact, already-committed deletions."""
    return await asyncio.to_thread(
        _remove_deleted_app_filesystem_artifacts, set(backend_names), settings
    )


def _remove_deleted_app_filesystem_artifacts(
    backend_names: set[str], settings: Settings
) -> list[str]:
    removed: list[str] = []
    for backend_name in sorted(backend_names):
        ensure_backend_name(backend_name)
        control_path = control_dir(settings, backend_name)
        sandbox_path = app_sandbox_rootfs_path(settings, backend_name).parent
        marked = (
            control_path.is_dir() and _control_dir_has_runtime_artifacts(control_path)
        ) or (
            sandbox_path.is_dir() and _sandbox_dir_has_runtime_artifacts(sandbox_path)
        )
        if not marked:
            continue
        if control_path.exists():
            _remove_tree_or_path(control_path)
            removed.append(str(control_path))
        if sandbox_path.exists():
            _remove_tree_or_path(sandbox_path)
            removed.append(str(sandbox_path))
    return removed


def _control_dir_has_runtime_artifacts(path: Path) -> bool:
    return any(
        (path / filename).exists()
        for filename in (
            "spec.json",
            "bootstrap.json",
            "failure.json",
            "bootstrap.lock",
        )
    )


def _sandbox_dir_has_runtime_artifacts(path: Path) -> bool:
    return (path / "profile.json").exists() or (path / "rootfs").exists()


def _app_healthcheck_failures(
    phase_details: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for backend, phases in phase_details.items():
        for phase in phases:
            if phase.get("phase") != "verify":
                continue
            details = phase.get("details")
            if not isinstance(details, dict):
                continue
            healthchecks = details.get("healthchecks")
            if not isinstance(healthchecks, dict):
                continue
            for target, payload in sorted(healthchecks.items()):
                if not isinstance(payload, dict) or payload.get("ok") is True:
                    continue
                failures.append(
                    {
                        "backend": backend,
                        "target": target,
                        "url": payload.get("url") or "",
                        "error": payload.get("error") or "healthcheck failed",
                        "http_status": payload.get("http_status"),
                        "host_header": payload.get("host_header") or "",
                    }
                )
    return failures


def _managed_podman_entries(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def _entry_backend_label(entry: dict[str, Any]) -> str:
    return _entry_label(entry, "io.cnc.backend")


def _entry_label(entry: dict[str, Any], key: str) -> str:
    labels = entry.get("Labels")
    if isinstance(labels, dict):
        return str(labels.get(key) or "").strip()
    return ""


def _entry_primary_name(entry: dict[str, Any]) -> str:
    names = entry.get("Names")
    if isinstance(names, list):
        for name in names:
            if isinstance(name, str) and name:
                return name
    if isinstance(names, str) and names:
        return names
    name = entry.get("Name")
    return name if isinstance(name, str) else ""


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("Id") or entry.get("ID") or entry.get("NetworkID")
    return value if isinstance(value, str) else ""


async def _systemctl_daemon_reload(
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    await services.run_command_checked_async(
        ["systemctl", "daemon-reload"],
        timeout_sec=settings.command_timeout_apply_sec,
    )


def _legacy_reboot_bridge_service_name(container: str) -> str:
    return f"{container}-legacy-restart.service"


def _legacy_reboot_bridge_path(settings: Settings, container: str) -> Path:
    return settings.systemd_generated_dir / _legacy_reboot_bridge_service_name(
        container
    )


def _render_legacy_reboot_bridge(container: str) -> str:
    return "\n".join(
        [
            "# Managed by CNC. Do not edit by hand.",
            "",
            "[Unit]",
            f"Description=CNC legacy app container boot bridge {container}",
            "Wants=network-online.target podman.service",
            "After=network-online.target podman.service",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"ExecStart=/usr/bin/podman start {container}",
            f"ExecStop=/usr/bin/podman stop -t 10 {container}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _write_text_atomic(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(encoded)
    temp_path.chmod(0o644)
    temp_path.replace(path)
    return True


async def _reconcile_app_quadlet_assets(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
    services: AppRuntimeServices,
) -> dict[str, Any]:
    asset_paths = {
        quadlet_container_path(settings, backend.name),
        quadlet_network_path(settings, backend.name),
        *(
            quadlet_interface_network_path(settings, link.network)
            for link in inter_app_interfaces
        ),
    }
    previous_assets = {
        path: path.read_bytes() if path.exists() else None for path in asset_paths
    }
    try:
        details = write_app_quadlet_assets(
            backend,
            settings,
            base_profile=base_profile,
            inter_app_interfaces=inter_app_interfaces,
        )
    except AppQuadletWriteError as exc:
        raise ApplyFailed(
            "app Quadlet asset write failed",
            phase="app_create",
            details=exc.details,
        ) from exc
    try:
        if details["changed_files"]:
            await _systemctl_daemon_reload(settings, services=services)
        service_name = quadlet_container_service_name(backend.name)
        fragment_path = await _resolve_generated_service_fragment(
            service_name,
            settings,
            services=services,
        )
        await services.run_command_checked_async(
            [
                "systemd-analyze",
                "verify",
                str(settings.systemd_generated_dir / APPS_SLICE_UNIT),
                fragment_path,
            ],
            timeout_sec=settings.command_timeout_apply_sec,
        )
    except Exception as exc:
        for path, previous in previous_assets.items():
            if previous is None:
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(f".{path.name}.rollback")
            temporary.write_bytes(previous)
            temporary.chmod(0o644)
            temporary.replace(path)
        await _systemctl_daemon_reload(settings, services=services)
        logger.exception(
            "app.quadlet.systemd_verify_failed",
            backend=backend.name,
            service=quadlet_container_service_name(backend.name),
            error=str(exc),
        )
        raise ApplyFailed(
            "app systemd unit validation failed",
            phase="app_create",
            details={
                "backend": backend.name,
                "service": quadlet_container_service_name(backend.name),
                "changed_files": details["changed_files"],
                "error": str(exc),
            },
        ) from exc
    details["systemd_verified"] = True
    details["systemd_fragment_path"] = fragment_path
    return details


async def _resolve_generated_service_fragment(
    service_name: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> str:
    result = await services.run_command_checked_async(
        [
            "systemctl",
            "show",
            service_name,
            "--property=FragmentPath",
            "--value",
        ],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    fragment_path = str(result.stdout or "").strip()
    if not fragment_path.startswith("/") or Path(fragment_path).name != service_name:
        raise ApplyFailed(
            "generated app service fragment is unavailable",
            phase="app_create",
            details={
                "service": service_name,
                "fragment_path": fragment_path,
            },
        )
    return fragment_path


async def _start_app_quadlet_backend(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    await services.run_command_checked_async(
        ["systemctl", "start", quadlet_network_service_name(backend.name)],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    await services.run_command_checked_async(
        ["systemctl", "start", quadlet_container_service_name(backend.name)],
        timeout_sec=settings.command_timeout_apply_sec,
    )


async def _restart_app_quadlet_backend(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    await services.run_command_checked_async(
        ["systemctl", "restart", quadlet_container_service_name(backend.name)],
        timeout_sec=settings.command_timeout_apply_sec,
    )


async def _stop_app_quadlet_backend(
    backend_name: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    result = services.run_command(
        ["systemctl", "stop", quadlet_container_service_name(backend_name)],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if result.ok or "not loaded" in (result.stderr or "").lower():
        return
    raise CommandError(result)


def _stop_app_quadlet_network(
    backend_name: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    result = services.run_command(
        ["systemctl", "stop", quadlet_network_service_name(backend_name)],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if result.ok or "not loaded" in (result.stderr or "").lower():
        return
    raise CommandError(result)


def _stop_interface_quadlet_network(
    network: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    result = services.run_command(
        ["systemctl", "stop", quadlet_interface_network_service_name(network)],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if result.ok or "not loaded" in (result.stderr or "").lower():
        return
    raise CommandError(result)


def _remove_podman_container(
    container: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    result = services.run_command(
        ["podman", "rm", "-f", container],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    combined_output = "\n".join(
        part for part in (result.stderr, result.stdout) if part
    ).lower()
    if result.ok or "no such container" in combined_output:
        return
    raise CommandError(result)


def _remove_podman_network(
    network: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    result = services.run_command(
        ["podman", "network", "rm", network],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    combined_output = "\n".join(
        part for part in (result.stderr, result.stdout) if part
    ).lower()
    if result.ok or "no such network" in combined_output:
        return
    raise CommandError(result)


async def _cleanup_failed_new_app_runtimes(
    reconcilers: list["AppRuntimeReconciler"],
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> list[dict[str, Any]]:
    cleanup_results: list[dict[str, Any]] = []
    for reconciler in reconcilers:
        if not reconciler.remove_runtime_on_apply_failure:
            continue
        backend_name = reconciler.backend.name
        actions: list[str] = []
        errors: list[str] = []
        try:
            await _stop_app_quadlet_backend(backend_name, settings, services=services)
            actions.append(f"stopped {quadlet_container_service_name(backend_name)}")
        except Exception as exc:
            errors.append(f"container_stop_failed: {exc}")
        try:
            _remove_podman_container(reconciler.container, settings, services=services)
            actions.append(f"removed {reconciler.container}")
        except Exception as exc:
            errors.append(f"container_remove_failed: {exc}")
        try:
            _stop_app_quadlet_network(backend_name, settings, services=services)
            actions.append(f"stopped {quadlet_network_service_name(backend_name)}")
        except Exception as exc:
            errors.append(f"network_stop_failed: {exc}")
        try:
            _remove_podman_network(reconciler.network, settings, services=services)
            actions.append(f"removed {reconciler.network}")
        except Exception as exc:
            errors.append(f"network_remove_failed: {exc}")
        removed_files = remove_app_quadlet_assets(settings, backend_name)
        if removed_files:
            actions.extend(f"removed {path}" for path in removed_files)
        removed_artifacts = _clear_runtime_artifacts_for_recreate(
            settings, backend_name, clear_lock=True
        )
        if removed_artifacts:
            actions.extend(f"removed {name}" for name in removed_artifacts)
        sandbox_dir = app_sandbox_rootfs_path(settings, backend_name).parent
        try:
            if sandbox_dir.exists():
                _remove_tree_or_path(sandbox_dir)
                actions.append(f"removed {sandbox_dir}")
        except Exception as exc:
            errors.append(f"sandbox_remove_failed: {exc}")
        cleanup_results.append(
            {
                "backend": backend_name,
                "container": reconciler.container,
                "network": reconciler.network,
                "actions": actions,
                "errors": errors,
            }
        )
    if cleanup_results:
        await _systemctl_daemon_reload(settings, services=services)
    return cleanup_results


def build_app_container_create_command(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
) -> list[str]:
    if backend.port is None:
        raise ApplyFailed(
            f"app backend {backend.name} requires allocated port",
            phase="app_create",
        )
    sandbox_profile = get_app_sandbox_profile(backend.sandbox_profile)
    dns_servers = configured_app_dns_servers(settings)
    policy = read_hardening_policy(backend)
    volume_entries = guest_volume_arguments(
        hardening_volumes(policy, parse_volumes_json(backend.volumes_json))
    )
    resource_profile = backend_resource_profile(backend, base_profile)
    command = [
        "podman",
        "create",
        *hardening_podman_args(policy),
        *guest_podman_args(),
        "--name",
        container_name(backend.name),
        "--hostname",
        container_name(backend.name),
        "--network",
        network_name(backend.name),
        "--systemd",
        "always",
        "--publish",
        f"127.0.0.1:{backend.port}:{backend.handoff_port}",
        "--cpu-shares",
        str(resource_profile.cpu_shares or 1024),
        "--cpus",
        _cpu_quota_to_cpus(resource_profile.cpu_quota, resource_profile.host_cpu_count),
    ]
    for dns_server in dns_servers:
        command.extend(["--dns", dns_server])
    command.extend(
        [
            "--label",
            "io.cnc.managed=true",
            "--label",
            f"io.cnc.backend={backend.name}",
        ]
    )
    for volume in volume_entries:
        command.extend(["--volume", volume])
    command.extend(
        [
            "--rootfs",
            guest_rootfs_argument(app_sandbox_rootfs_path(settings, backend.name)),
            *sandbox_profile.init_command,
        ]
    )
    return command


def _guest_rootfs_ready(settings: Settings, backend: Backend) -> bool:
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    if not rootfs.is_dir():
        return False
    try:
        next(rootfs.iterdir())
    except (FileNotFoundError, StopIteration):
        return False
    return True


def _read_profile_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _expected_profile_state(backend: Backend) -> dict[str, Any]:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    return {
        "sandbox_profile": profile.profile_id,
        "seed_image": profile.seed_image,
        "init_command": list(profile.init_command),
        "seed_revision": profile.seed_revision,
    }


def _profile_state_matches(
    current: dict[str, Any] | None, expected: dict[str, Any]
) -> bool:
    if not isinstance(current, dict):
        return False
    return {
        "sandbox_profile": current.get("sandbox_profile"),
        "seed_image": current.get("seed_image"),
        "init_command": current.get("init_command"),
        "seed_revision": current.get("seed_revision"),
    } == expected


def _remove_tree_or_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


def _rootfs_quarantine_path(rootfs_dir: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = rootfs_dir.parent / f"{rootfs_dir.name}.quarantine-{timestamp}"
    candidate = base
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = rootfs_dir.parent / f"{base.name}-{suffix}"
    return candidate


def _preserve_guest_systemd_state(rootfs_dir: Path, preserved_root: Path) -> list[str]:
    systemd_dir = rootfs_dir / "etc/systemd/system"
    if not systemd_dir.exists():
        return []
    preserved_dir = preserved_root / "etc/systemd/system"
    preserved_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(systemd_dir, preserved_dir, symlinks=True)
    return ["preserved_guest_systemd_state"]


def _restore_guest_systemd_state(rootfs_dir: Path, preserved_root: Path) -> list[str]:
    preserved_dir = preserved_root / "etc/systemd/system"
    if not preserved_dir.exists():
        return []
    target_dir = rootfs_dir / "etc/systemd/system"
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    _remove_tree_or_path(target_dir)
    shutil.copytree(preserved_dir, target_dir, symlinks=True)
    return ["restored_guest_systemd_state"]


def _record_guest_seed_step(
    settings: Settings,
    backend: Backend,
    substep: str,
    status: str,
    **details: Any,
) -> None:
    payload = read_bootstrap_state(settings, backend.name) or {}
    step_details = {"substep": substep, **details}
    payload.setdefault("status", "running")
    payload["container_name"] = container_name(backend.name)
    payload["command"] = "guest rootfs seed"
    payload["dns_servers"] = configured_app_dns_servers(settings)
    payload["reconcile_phase"] = "create"
    payload["reconcile_phase_status"] = status
    payload["reconcile_details"] = step_details
    payload = append_bootstrap_event(
        payload,
        kind="seed",
        name=substep,
        status=status,
        details=step_details,
    )
    try:
        write_bootstrap_state(settings, backend.name, payload)
    except OSError as exc:
        logger.warning(
            "app.rootfs_seed.progress_write_failed",
            backend=backend.name,
            substep=substep,
            status=status,
            error=str(exc),
        )
    logger.info(
        "app.rootfs_seed.progress",
        backend=backend.name,
        substep=substep,
        status=status,
        details=step_details,
    )


def _record_app_reconcile_step(
    settings: Settings,
    backend: Backend,
    *,
    phase: str,
    substep: str,
    status: str,
    kind: str = "runtime",
    **details: Any,
) -> None:
    payload = read_bootstrap_state(settings, backend.name) or {}
    step_details = {"substep": substep, **details}
    payload.setdefault("status", "running")
    payload["container_name"] = container_name(backend.name)
    payload["reconcile_phase"] = phase
    payload["reconcile_phase_status"] = status
    payload["reconcile_details"] = step_details
    payload = append_bootstrap_event(
        payload,
        kind=kind,
        name=substep,
        status=status,
        details=step_details,
    )
    try:
        write_bootstrap_state(settings, backend.name, payload)
    except OSError as exc:
        logger.warning(
            "app.reconcile.progress_write_failed",
            backend=backend.name,
            phase=phase,
            substep=substep,
            status=status,
            error=str(exc),
        )


async def _provision_guest_rootfs(
    backend: Backend,
    settings: Settings,
    rootfs_dir: Path,
    *,
    services: AppRuntimeServices,
) -> list[str]:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    provisioned_steps: list[str] = []
    if not profile.provision_steps:
        _record_guest_seed_step(
            settings, backend, "profile_provision", "succeeded", skipped=True
        )
        return provisioned_steps
    for step in profile.provision_steps:
        command = [
            "podman",
            "run",
            "--rm",
            "--rootfs",
            str(rootfs_dir),
            "/bin/bash",
            "-lc",
            step.command,
        ]
        started_at = time.monotonic()
        _record_guest_seed_step(
            settings, backend, step.step_id, "running", label=step.label
        )
        await services.run_command_checked_async(
            command,
            timeout_sec=settings.command_timeout_apply_sec,
        )
        _record_guest_seed_step(
            settings,
            backend,
            step.step_id,
            "succeeded",
            label=step.label,
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
        provisioned_steps.append(step.step_id)
    return provisioned_steps


async def _self_test_guest_rootfs(
    backend: Backend,
    settings: Settings,
    rootfs_dir: Path,
    *,
    services: AppRuntimeServices,
) -> list[str]:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    self_test_steps: list[str] = []
    if not profile.self_test_command:
        _record_guest_seed_step(
            settings, backend, "profile_self_test", "succeeded", skipped=True
        )
        return self_test_steps
    command = [
        "podman",
        "run",
        "--rm",
        "--rootfs",
        str(rootfs_dir),
        *profile.self_test_command,
    ]
    started_at = time.monotonic()
    _record_guest_seed_step(settings, backend, "profile_self_test", "running")
    await services.run_command_checked_async(
        command,
        timeout_sec=settings.command_timeout_apply_sec,
    )
    _record_guest_seed_step(
        settings,
        backend,
        "profile_self_test",
        "succeeded",
        duration_seconds=round(time.monotonic() - started_at, 3),
    )
    self_test_steps.append("profile_self_test")
    return self_test_steps


async def _seed_persistent_guest_rootfs(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices,
    allow_reseed: bool = False,
) -> dict[str, Any]:
    expected_profile = _expected_profile_state(backend)
    sandbox_dir = app_sandbox_rootfs_path(settings, backend.name).parent
    protect_sandbox_directory(sandbox_dir)
    rootfs_dir = app_sandbox_rootfs_path(settings, backend.name)
    profile_state_path = app_sandbox_profile_state_path(settings, backend.name)
    current_profile = _read_profile_state(profile_state_path)
    rootfs_reseed_required = False
    if _guest_rootfs_ready(settings, backend):
        if _profile_state_matches(current_profile, expected_profile):
            _record_guest_seed_step(settings, backend, "rootfs_reuse", "succeeded")
            return {
                "seeded": False,
                "rootfs": str(rootfs_dir),
                "sandbox_dir": str(sandbox_dir),
                "seed_revision": expected_profile["seed_revision"],
                "provisioned_steps": [],
                "preserved_steps": [],
            }
        if not allow_reseed:
            _record_guest_seed_step(
                settings,
                backend,
                "rootfs_reseed_available",
                "succeeded",
                skipped=True,
                reason="explicit_reseed_required",
                current_profile=current_profile,
                expected_profile=expected_profile,
            )
            return {
                "seeded": False,
                "rootfs": str(rootfs_dir),
                "sandbox_dir": str(sandbox_dir),
                "seed_revision": current_profile.get("seed_revision")
                if isinstance(current_profile, dict)
                else None,
                "expected_seed_revision": expected_profile["seed_revision"],
                "profile_current": current_profile,
                "profile_expected": expected_profile,
                "reseed_required": True,
                "reseed_skipped": True,
                "reseed_skip_reason": "explicit_reseed_required",
                "provisioned_steps": [],
                "preserved_steps": [],
            }
        logger.warning(
            "app.rootfs_reseed.explicit_requested",
            backend=backend.name,
            rootfs=str(rootfs_dir),
            current_profile=current_profile,
            expected_profile=expected_profile,
        )
        rootfs_reseed_required = True

    protect_sandbox_directory(sandbox_dir)
    settings.app_sandbox_dir.mkdir(parents=True, exist_ok=True)
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    with tempfile.TemporaryDirectory(
        prefix=f"{backend.name}-guest-seed-", dir=str(settings.app_sandbox_dir)
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        preserved_root = temp_dir / "preserved"
        preserved_steps: list[str] = []
        if rootfs_reseed_required:
            preserved_steps = _preserve_guest_systemd_state(rootfs_dir, preserved_root)
        temp_container = f"{container_name(backend.name)}-seed-{temp_dir.name[-8:]}"
        export_tar = temp_dir / "guest-rootfs.tar"
        extracted_rootfs = temp_dir / "rootfs"
        extracted_rootfs.mkdir(parents=True, exist_ok=True)
        if rootfs_reseed_required:
            _record_app_reconcile_step(
                settings,
                backend,
                phase="create",
                substep="guest_services_preserve",
                status="succeeded",
                kind="seed",
                preserved_steps=preserved_steps,
            )
        _record_guest_seed_step(
            settings,
            backend,
            "seed_container_create",
            "running",
            seed_image=profile.seed_image,
        )
        await services.run_command_checked_async(
            [
                "podman",
                "create",
                "--name",
                temp_container,
                profile.seed_image,
                "/bin/true",
            ],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        _record_guest_seed_step(settings, backend, "seed_container_create", "succeeded")
        try:
            _record_guest_seed_step(settings, backend, "seed_image_export", "running")
            await services.run_command_checked_async(
                ["podman", "export", "-o", str(export_tar), temp_container],
                timeout_sec=settings.command_timeout_apply_sec,
            )
            _record_guest_seed_step(settings, backend, "seed_image_export", "succeeded")
        finally:
            _record_guest_seed_step(
                settings, backend, "seed_container_remove", "running"
            )
            await services.run_command_checked_async(
                ["podman", "rm", "-f", temp_container],
                timeout_sec=settings.command_timeout_apply_sec,
            )
            _record_guest_seed_step(
                settings, backend, "seed_container_remove", "succeeded"
            )
        _record_guest_seed_step(settings, backend, "seed_archive_extract", "running")
        with tarfile.open(export_tar, "r:*") as archive:
            safe_extract_tar(
                archive,
                extracted_rootfs,
                allow_relative_symlinks=True,
                guest_symlink_roots={PurePosixPath()},
                guest_metadata_roots={PurePosixPath()},
            )
        _record_guest_seed_step(settings, backend, "seed_archive_extract", "succeeded")
        (extracted_rootfs / "etc").mkdir(parents=True, exist_ok=True)
        machine_id_path = extracted_rootfs / "etc/machine-id"
        _record_app_reconcile_step(
            settings,
            backend,
            phase="create",
            substep="guest_identity_prepare",
            status="succeeded",
            kind="seed",
        )
        if not machine_id_path.exists():
            machine_id_path.write_text("", encoding="utf-8")
        preserved_steps.extend(
            _restore_guest_systemd_state(extracted_rootfs, preserved_root)
        )
        if preserved_steps:
            _record_app_reconcile_step(
                settings,
                backend,
                phase="create",
                substep="guest_services_restore",
                status="succeeded",
                kind="seed",
            )
        provisioned_steps: list[str] = []
        try:
            provisioned_steps = await _provision_guest_rootfs(
                backend,
                settings,
                extracted_rootfs,
                services=services,
            )
            provisioned_steps.extend(
                await _self_test_guest_rootfs(
                    backend,
                    settings,
                    extracted_rootfs,
                    services=services,
                )
            )
        except Exception:
            shutil.rmtree(extracted_rootfs, ignore_errors=True)
            raise

        had_existing_rootfs = rootfs_dir.exists()
        quarantine_rootfs: Path | None = None
        rollback_rootfs = temp_dir / "rollback-rootfs"
        _record_app_reconcile_step(
            settings,
            backend,
            phase="create",
            substep="rootfs_switch_prepare",
            status="running",
            kind="seed",
        )
        if had_existing_rootfs:
            if rootfs_reseed_required:
                quarantine_rootfs = _rootfs_quarantine_path(rootfs_dir)
                logger.warning(
                    "app.rootfs_reseed.quarantine_create",
                    backend=backend.name,
                    rootfs=str(rootfs_dir),
                    quarantine_rootfs=str(quarantine_rootfs),
                )
                shutil.move(str(rootfs_dir), str(quarantine_rootfs))
            else:
                shutil.move(str(rootfs_dir), str(rollback_rootfs))
        try:
            _record_guest_seed_step(settings, backend, "rootfs_activate", "running")
            shutil.move(str(extracted_rootfs), str(rootfs_dir))
        except Exception:
            if quarantine_rootfs is not None and quarantine_rootfs.exists():
                shutil.move(str(quarantine_rootfs), str(rootfs_dir))
                logger.warning(
                    "app.rootfs_reseed.quarantine_restored_after_failure",
                    backend=backend.name,
                    rootfs=str(rootfs_dir),
                    quarantine_rootfs=str(quarantine_rootfs),
                )
            elif had_existing_rootfs and rollback_rootfs.exists():
                shutil.move(str(rollback_rootfs), str(rootfs_dir))
            raise
        _record_guest_seed_step(settings, backend, "rootfs_activate", "succeeded")
        if quarantine_rootfs is None:
            shutil.rmtree(rollback_rootfs, ignore_errors=True)
        else:
            logger.warning(
                "app.rootfs_reseed.quarantine_retained",
                backend=backend.name,
                rootfs=str(rootfs_dir),
                quarantine_rootfs=str(quarantine_rootfs),
            )
    profile_state_path.write_text(
        json.dumps(expected_profile, indent=2, sort_keys=True), encoding="utf-8"
    )
    result = {
        "seeded": True,
        "rootfs": str(rootfs_dir),
        "sandbox_dir": str(sandbox_dir),
        "seed_image": profile.seed_image,
        "seed_revision": profile.seed_revision,
        "provisioned_steps": provisioned_steps,
        "preserved_steps": preserved_steps,
    }
    if quarantine_rootfs is not None:
        result["quarantine_rootfs"] = str(quarantine_rootfs)
    return result


async def reseed_app_backend_rootfs(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
    backup_id: int | None = None,
    backup_path: str | None = None,
) -> dict[str, Any]:
    if str(backend.kind or "").lower() != "app":
        raise ValueError(f"backend {backend.name} is not an app backend")
    if backup_id is None:
        raise ValueError("explicit rootfs reseed requires a successful backend backup")
    services = services or default_app_runtime_services()
    logger.warning(
        "app.rootfs_reseed.requested",
        backend=backend.name,
        backup_id=backup_id,
        backup_path=backup_path or "",
    )
    await _stop_app_quadlet_backend(backend.name, settings, services=services)
    try:
        rootfs_state = await _seed_persistent_guest_rootfs(
            backend,
            settings,
            services=services,
            allow_reseed=True,
        )
    except Exception:
        logger.exception("app.rootfs_reseed.failed", backend=backend.name)
        raise
    try:
        await _start_app_quadlet_backend(backend, settings, services=services)
    except Exception:
        logger.exception(
            "app.rootfs_reseed.start_failed_after_reseed",
            backend=backend.name,
            rootfs=rootfs_state.get("rootfs"),
            quarantine_rootfs=rootfs_state.get("quarantine_rootfs"),
            backup_id=backup_id,
            backup_path=backup_path or "",
        )
        raise
    logger.warning(
        "app.rootfs_reseed.succeeded",
        backend=backend.name,
        rootfs=rootfs_state.get("rootfs"),
        quarantine_rootfs=rootfs_state.get("quarantine_rootfs"),
        seed_revision=rootfs_state.get("seed_revision"),
        backup_id=backup_id,
        backup_path=backup_path or "",
    )
    return rootfs_state


async def apply_app_resource_limits(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
    services: AppRuntimeServices | None = None,
    operation_id: int | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    resource_profile = backend_resource_profile(backend, base_profile)
    await services.run_command_checked_async(
        [
            "podman",
            "update",
            "--cpu-shares",
            str(resource_profile.cpu_shares or 1024),
            "--cpus",
            _cpu_quota_to_cpus(
                resource_profile.cpu_quota, resource_profile.host_cpu_count
            ),
            container_name(backend.name),
        ],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    memory_policy = await apply_app_memory_limits(
        backend,
        settings,
        base_profile=base_profile,
        services=services,
        operation_id=operation_id,
    )
    return {
        "memory_high": resource_profile.memory_high,
        "memory_max": resource_profile.memory_max,
        "cpu_quota": resource_profile.cpu_quota,
        "cpu_shares": resource_profile.cpu_shares,
        "memory_policy": memory_policy,
    }


async def apply_app_memory_limits(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
    services: AppRuntimeServices | None = None,
    operation_id: int | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    resource_profile = backend_resource_profile(backend, base_profile)
    service = quadlet_container_service_name(backend.name)
    try:
        policy = await converge_systemd_memory_policy(
            service,
            services=services,
            timeout_sec=settings.command_timeout_apply_sec,
            memory_high=resource_profile.memory_high,
            memory_max=resource_profile.memory_max,
            require_apps_slice=True,
        )
    except SystemdMemoryPolicyError as exc:
        fields = {
            "app_id": "cnc.admin",
            "backend_id": backend.id,
            "backend": backend.name,
            "operation_id": operation_id,
            "service": service,
            "memory_high": resource_profile.memory_high,
            "memory_max": resource_profile.memory_max,
            "details": exc.details,
        }
        logger.error("app.memory.policy_failed", **fields)
        raise ApplyFailed(
            f"app backend {backend.name} memory policy did not converge",
            phase="app_runtime",
            details=fields,
        ) from exc
    log_policy = (
        logger.warning if policy["memory_max_lowered_below_usage"] else logger.info
    )
    log_policy(
        "app.memory.policy_converged",
        app_id="cnc.admin",
        backend_id=backend.id,
        backend=backend.name,
        operation_id=operation_id,
        service=service,
        control_group=policy["control_group"],
        memory_current_bytes=policy["memory_current_bytes"],
        memory_high_bytes=policy["memory_high_bytes"],
        memory_max_bytes=policy["memory_max_bytes"],
        changed=policy["changed"],
        changed_properties=policy["changed_properties"],
        memory_max_lowered_below_usage=policy["memory_max_lowered_below_usage"],
        verified=policy["verified"],
    )
    return policy


def _command_error_is_stale_runtime_state(exc: CommandError) -> bool:
    combined_output = "\n".join(
        part for part in (exc.result.stderr, exc.result.stdout) if part
    ).lower()
    return (
        "error opening /run/crun/" in combined_output
        and "/status: no such file or directory" in combined_output
    )


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


def _container_requires_quadlet_migration(
    current_spec: dict[str, Any],
    inspect_payload: dict[str, Any] | None,
    settings: Settings,
    container: str,
) -> bool:
    return (
        current_spec.get("runtime_owner") == "quadlet"
        and inspect_payload is not None
        and _legacy_reboot_bridge_path(settings, container).exists()
        and not _container_is_quadlet_managed(inspect_payload)
    )


def legacy_direct_podman_migration_backend_reasons(
    desired: DesiredState,
    settings: Settings,
    *,
    services: AppRuntimeServices,
    candidate_backend_names: set[str] | None = None,
) -> dict[str, str]:
    candidates = (
        set(candidate_backend_names)
        if candidate_backend_names is not None
        else set(desired.runtime_graph.enabled_app_backend_names)
    )
    reasons: dict[str, str] = {}
    for backend in sorted(desired.known_app_backends, key=lambda item: item.name):
        if (
            backend.name not in candidates
            or backend.name not in desired.runtime_graph.enabled_app_backend_names
        ):
            continue
        inter_app_interfaces = tuple(
            item
            for item in desired.runtime_graph.inter_app_interfaces
            if item.source_backend == backend.name
            or item.target_backend == backend.name
        )
        current_spec = runtime_spec(
            backend,
            settings,
            base_profile=desired.resource_profile,
            inter_app_interfaces=inter_app_interfaces,
        )
        if current_spec.get("runtime_owner") != "quadlet":
            continue
        bridge_path = _legacy_reboot_bridge_path(settings, container_name(backend.name))
        if not bridge_path.exists():
            continue
        _inspect_result, inspect_payload = services.inspect_container(
            container_name(backend.name),
            timeout_sec=settings.command_timeout_status_sec,
        )
        if _container_requires_quadlet_migration(
            current_spec, inspect_payload, settings, container_name(backend.name)
        ):
            reasons[backend.name] = "legacy_direct_podman_runtime_owner"
    return reasons


async def _remove_legacy_direct_podman_reboot_bridge(
    container: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> dict[str, Any]:
    service_name = _legacy_reboot_bridge_service_name(container)
    service_path = _legacy_reboot_bridge_path(settings, container)
    removed = False
    if service_path.exists():
        await services.run_command_checked_async(
            ["systemctl", "disable", service_name],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        service_path.unlink()
        await _systemctl_daemon_reload(settings, services=services)
        removed = True
    return {"changed": removed, "reason": "quadlet_managed" if removed else "no_bridge"}


async def _ensure_legacy_direct_podman_reboot_bridge(
    container: str,
    inspect_payload: dict[str, Any] | None,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> dict[str, Any]:
    if _container_is_quadlet_managed(inspect_payload):
        return await _remove_legacy_direct_podman_reboot_bridge(
            container, settings, services=services
        )
    service_name = _legacy_reboot_bridge_service_name(container)
    changed = _write_text_atomic(
        _legacy_reboot_bridge_path(settings, container),
        _render_legacy_reboot_bridge(container),
    )
    if changed:
        await _systemctl_daemon_reload(settings, services=services)
    await services.run_command_checked_async(
        ["systemctl", "enable", service_name],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    return {"changed": changed, "service": service_name}


class AppRuntimeReconciler:
    PHASES = ("prepare", "create", "bootstrap", "publish", "verify", "steady_state")

    def __init__(
        self,
        backend: Backend,
        settings: Settings,
        *,
        base_profile: ResourceProfile,
        inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
        services: AppRuntimeServices | None = None,
        operation_id: int | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.base_profile = base_profile
        self.inter_app_interfaces = inter_app_interfaces
        self.services = services or default_app_runtime_services()
        self.operation_id = operation_id
        self.container = container_name(backend.name)
        self.network = network_name(backend.name)
        self.current_spec = runtime_spec(
            backend,
            settings,
            base_profile=base_profile,
            inter_app_interfaces=inter_app_interfaces,
        )
        self.saved_spec = _normalize_saved_spec(
            read_saved_spec(settings, backend.name),
            self.current_spec,
        )
        self.inspect_result = None
        self.inspect_payload = None
        self.network_payload = None
        self.create_action = "reuse"
        self.container_started = False
        self.guest_rootfs_seeded = False
        self.remove_runtime_on_apply_failure = False
        self._isolation_rollback_files: dict[Path, bytes] = {}
        self._isolation_rollback_dir: Path | None = None
        self._isolation_migration_started = False
        self.result = AppRuntimeReconcileResult(
            backend=backend.name,
            container=self.container,
            dns_servers=list(self.current_spec.get("dns_servers", [])),
            phases={
                name: {"phase": name, "status": "pending", "details": {}}
                for name in self.PHASES
            },
        )

    async def _auto_repair_prepare_drift(
        self,
        *,
        reason: str,
        container_exists: bool,
        rebuild_required_keys: list[str] | None = None,
    ) -> dict[str, Any] | None:
        if bootstrap_lock_is_held(self.settings, self.backend.name):
            return None

        actions: list[str] = []
        if container_exists:
            if self._isolation_rollback_files:
                self._isolation_migration_started = True
            await _stop_app_quadlet_backend(
                self.backend.name, self.settings, services=self.services
            )
            remove_command = ["podman", "rm", "-f", self.container]
            _remove_podman_container(
                self.container, self.settings, services=self.services
            )
            actions.append(" ".join(remove_command))
            self.result.recreated = True

        removed_artifacts = _clear_runtime_artifacts_for_recreate(
            self.settings,
            self.backend.name,
            clear_lock=True,
        )
        actions.extend(f"removed {name}" for name in removed_artifacts)

        self.saved_spec = None
        self.inspect_result = None
        self.inspect_payload = None
        self.create_action = "create"

        details: dict[str, Any] = {
            "reason": reason,
            "actions": actions,
        }
        if rebuild_required_keys:
            details["rebuild_required_keys"] = rebuild_required_keys
        logger.info(
            "app.prepare.auto_repair",
            backend=self.backend.name,
            container=self.container,
            reason=reason,
            actions=actions,
        )
        return details

    def _update_runtime_state(
        self, phase: str, status: str, details: dict[str, Any]
    ) -> None:
        current_state = read_bootstrap_state(self.settings, self.backend.name) or {}
        payload = dict(current_state)
        payload.setdefault("status", "pending")
        payload["container_name"] = self.container
        payload["command"] = "guest init readiness"
        payload["dns_servers"] = list(self.current_spec.get("dns_servers", []))
        payload["reconcile_phase"] = phase
        payload["reconcile_phase_status"] = status
        payload["reconcile_details"] = details
        payload = append_bootstrap_event(
            payload,
            kind="phase",
            name=phase,
            status=status,
            details=details,
        )
        write_bootstrap_state(self.settings, self.backend.name, payload)

    def _record_phase(self, phase: str, status: str, **details: Any) -> None:
        assert self.result.phases is not None
        self.result.phases[phase] = {
            "phase": phase,
            "status": status,
            "details": details,
        }
        self._update_runtime_state(phase, status, details)

    async def _write_failure_artifact(
        self, phase: str, details: dict[str, Any]
    ) -> None:
        bootstrap_state = read_bootstrap_state(self.settings, self.backend.name) or {}
        artifact: dict[str, Any] = {
            "container": self.container,
            "network": self.network,
            "phase": phase,
            "status": "failed",
            "details": details,
            "dns_servers": list(self.current_spec.get("dns_servers", [])),
            "bootstrap_state": bootstrap_state,
            "recent_events": (
                bootstrap_state.get("events", [])[-10:]
                if isinstance(bootstrap_state.get("events"), list)
                else []
            ),
        }
        try:
            diagnostics = await asyncio.to_thread(
                collect_app_backend_diagnostics,
                self.backend,
                self.settings,
            )
        except Exception as exc:
            artifact["diagnostics_error"] = str(exc)
        else:
            artifact["runtime_diagnostics"] = diagnostics
        write_failure_artifact(self.settings, self.backend.name, artifact)

    async def _run_phase(self, phase: str, fn):
        bootstrap_snapshot = (
            _snapshot_bootstrap_state_file(self.settings, self.backend.name)
            if phase == "bootstrap"
            else None
        )
        self._record_phase(phase, "running")
        try:
            details = await fn()
        except Exception as exc:
            failure_details = (
                exc.as_details()
                if isinstance(exc, ApplyFailed)
                else {"error": str(exc)}
            )
            if "phase" in failure_details:
                failed_phase = failure_details["phase"]
                failure_details = {
                    key: value
                    for key, value in failure_details.items()
                    if key != "phase"
                }
                failure_details["failed_phase"] = failed_phase
            observation = failure_details.get("bootstrap_observation")
            observation_unavailable = (
                phase == "bootstrap"
                and isinstance(observation, dict)
                and observation.get("status") == "unavailable"
                and observation.get("reason") in {"guard_busy", "guard_unavailable"}
            )
            if observation_unavailable and bootstrap_snapshot is not None:
                assert self.result.phases is not None
                self.result.phases[phase] = {
                    "phase": phase,
                    "status": "failed",
                    "details": failure_details,
                }
                _restore_bootstrap_state_file(
                    self.settings,
                    self.backend.name,
                    bootstrap_snapshot,
                )
                raise
            self._record_phase(phase, "failed", **failure_details)
            await self._write_failure_artifact(phase, failure_details)
            raise
        self._record_phase(phase, "succeeded", **(details or {}))
        return details

    async def reconcile_for_publish(self) -> AppRuntimeReconcileResult:
        try:
            await self._run_phase("prepare", self._prepare_phase)
            await self._run_phase("create", self._create_phase)
            await self._run_phase("bootstrap", self._bootstrap_phase)
            await self._run_phase("publish", self._publish_phase)
        except Exception as exc:
            if self._isolation_migration_started:
                try:
                    await self._restore_previous_isolation_runtime()
                except Exception as rollback_exc:
                    raise ApplyFailed(
                        "Guest runtime migration failed and the previous runtime could not restart",
                        phase="app_create",
                        details={
                            "error": str(exc),
                            "rollback_error": str(rollback_exc),
                            "runtime_backup": str(self._isolation_rollback_dir),
                        },
                    ) from exc
            raise
        return self.result

    def _save_previous_isolation_runtime(self) -> None:
        unit = quadlet_container_path(self.settings, self.backend.name)
        if not unit.is_file():
            raise ApplyFailed(
                "Guest isolation migration requires an existing Quadlet for automatic rollback",
                phase="app_create",
            )
        backup = (
            self.settings.apply_backup_dir
            / "guest-runtime"
            / self.backend.name
            / uuid.uuid4().hex
        )
        backup.mkdir(parents=True, mode=0o700)
        paths = [unit, quadlet_network_path(self.settings, self.backend.name)]
        paths.extend(
            control_dir(self.settings, self.backend.name) / name
            for name in ("spec.json", "bootstrap.json")
        )
        for path in paths:
            if path.is_file():
                content = path.read_bytes()
                self._isolation_rollback_files[path] = content
                (backup / path.name).write_bytes(content)
        self._isolation_rollback_dir = backup

    async def _restore_previous_isolation_runtime(self) -> None:
        # Only runtime artifacts changed: idmapped storage needs no recursive
        # ownership conversion, so the previous Quadlet can reuse the same data.
        self.remove_runtime_on_apply_failure = False
        await _stop_app_quadlet_backend(
            self.backend.name, self.settings, services=self.services
        )
        _remove_podman_container(self.container, self.settings, services=self.services)
        for path, content in self._isolation_rollback_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        await _systemctl_daemon_reload(self.settings, services=self.services)
        await _start_app_quadlet_backend(
            self.backend, self.settings, services=self.services
        )
        self._isolation_migration_started = False
        logger.warning(
            "app.isolation.migration_rolled_back",
            backend=self.backend.name,
            runtime_backup=str(self._isolation_rollback_dir),
        )

    async def verify_steady_state(self) -> AppRuntimeReconcileResult:
        await self._run_phase("verify", self._verify_phase)
        await self._run_phase("steady_state", self._steady_state_phase)
        return self.result

    async def _prepare_phase(self) -> dict[str, Any]:
        policy = read_hardening_policy(self.backend)
        validate_guest_hardening(policy.model_dump())
        volumes = hardening_volumes(
            policy, parse_volumes_json(self.backend.volumes_json)
        )
        guest_volume_arguments(volumes)
        # Fail before stopping an existing guest if the host cannot prepare the
        # confined runtime. ExecStartPre repeats this after host reboot.
        await self.services.run_command_checked_async(
            [str(self.settings.guest_runtime_helper_path)]
            + [arg for volume in volumes for arg in ("--volume", volume)],
            timeout_sec=self.settings.command_timeout_apply_sec,
        )
        self.inspect_result, self.inspect_payload = self.services.inspect_container(
            self.container,
            timeout_sec=self.settings.command_timeout_status_sec,
        )
        _network_result, self.network_payload = self.services.inspect_network(
            self.network,
            timeout_sec=self.settings.command_timeout_status_sec,
        )
        container_exists = self.inspect_payload is not None
        if (
            container_exists
            and self.saved_spec is not None
            and self.saved_spec.get("guest_isolation_revision")
            != self.current_spec.get("guest_isolation_revision")
        ):
            self._save_previous_isolation_runtime()
        network_exists = self.network_payload is not None
        self.create_action = "reuse"
        auto_repair_details: dict[str, Any] | None = None

        if not container_exists:
            if self.saved_spec is not None:
                auto_repair_details = await self._auto_repair_prepare_drift(
                    reason="container_missing_saved_spec",
                    container_exists=False,
                )
                if auto_repair_details is None:
                    runtime_diagnostics = collect_app_backend_diagnostics(
                        self.backend, self.settings
                    )
                    raise ApplyFailed(
                        f"app backend {self.backend.name} container missing with saved spec present",
                        phase="app_create",
                        details={
                            "backend": self.backend.name,
                            "container": self.container,
                            "runtime_diagnostics": runtime_diagnostics,
                        },
                    )
            self.create_action = "create"
            self.remove_runtime_on_apply_failure = self.saved_spec is None
        elif self.saved_spec is None:
            auto_repair_details = await self._auto_repair_prepare_drift(
                reason="missing_saved_spec",
                container_exists=True,
            )
            if auto_repair_details is None:
                runtime_diagnostics = collect_app_backend_diagnostics(
                    self.backend, self.settings
                )
                raise ApplyFailed(
                    f"app backend {self.backend.name} is missing saved runtime spec",
                    phase="app_create",
                    details={
                        "backend": self.backend.name,
                        "container": self.container,
                        "stderr": clip_output(
                            self.inspect_result.stderr
                            if self.inspect_result is not None
                            else ""
                        ),
                        "runtime_diagnostics": runtime_diagnostics,
                    },
                )
            container_exists = False
        else:
            rebuild_required_keys = sorted(
                _rebuild_required_spec_keys(self.saved_spec, self.current_spec)
            )
            if rebuild_required_keys:
                auto_repair_details = await self._auto_repair_prepare_drift(
                    reason="rebuild_required_settings",
                    container_exists=True,
                    rebuild_required_keys=rebuild_required_keys,
                )
                if auto_repair_details is None:
                    raise ApplyFailed(
                        f"app backend {self.backend.name} changed rebuild-required settings",
                        phase="app_create",
                        details={
                            "backend": self.backend.name,
                            "container": self.container,
                            "rebuild_required_keys": rebuild_required_keys,
                            "saved_spec": self.saved_spec,
                            "current_spec": self.current_spec,
                        },
                    )
                container_exists = False
            elif _container_requires_quadlet_migration(
                self.current_spec,
                self.inspect_payload,
                self.settings,
                self.container,
            ):
                auto_repair_details = await self._auto_repair_prepare_drift(
                    reason="legacy_direct_podman_runtime_owner",
                    container_exists=True,
                )
                if auto_repair_details is None:
                    runtime_diagnostics = collect_app_backend_diagnostics(
                        self.backend, self.settings
                    )
                    raise ApplyFailed(
                        f"app backend {self.backend.name} requires Quadlet runtime migration",
                        phase="app_create",
                        details={
                            "backend": self.backend.name,
                            "container": self.container,
                            "runtime_diagnostics": runtime_diagnostics,
                        },
                    )
                container_exists = False
            else:
                self.guest_rootfs_seeded = False

        if container_exists and network_uses_bridge_dns(self.network_payload):
            raise ApplyFailed(
                f"app backend {self.backend.name} network requires explicit rebuild",
                phase="app_network",
                details={
                    "backend": self.backend.name,
                    "container": self.container,
                    "network": self.network,
                    "reason": "bridge_dns_enabled",
                },
            )

        return {
            "container_exists": container_exists,
            "network_exists": network_exists,
            "saved_spec_present": self.saved_spec is not None,
            "action": self.create_action,
            "auto_repair": auto_repair_details,
        }

    async def _create_phase(self) -> dict[str, Any]:
        auto_repair_details: dict[str, Any] | None = None
        quadlet_container_restarted = False
        legacy_reboot_bridge: dict[str, Any] = {
            "changed": False,
            "reason": "not_legacy_direct_podman",
        }
        resource_limits: dict[str, Any]
        rootfs_state = await _seed_persistent_guest_rootfs(
            self.backend,
            self.settings,
            services=self.services,
        )
        self.guest_rootfs_seeded = bool(rootfs_state.get("seeded"))
        write_profile_state = not bool(rootfs_state.get("reseed_skipped"))
        if self.create_action == "create":
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="app_control_assets_write",
                status="running",
            )
            write_app_control_assets(
                self.backend,
                self.settings,
                write_profile_state=write_profile_state,
                base_profile=self.base_profile,
                inter_app_interfaces=self.inter_app_interfaces,
            )
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="service_files_write",
                status="running",
            )
            quadlet_details = await _reconcile_app_quadlet_assets(
                self.backend,
                self.settings,
                base_profile=self.base_profile,
                inter_app_interfaces=self.inter_app_interfaces,
                services=self.services,
            )
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="app_service_start",
                status="running",
            )
            await _start_app_quadlet_backend(
                self.backend, self.settings, services=self.services
            )
            legacy_reboot_bridge = await _remove_legacy_direct_podman_reboot_bridge(
                self.container,
                self.settings,
                services=self.services,
            )
            self.result.created = True
            resource_limits = {
                "resource_mode": self.current_spec.get("resource_mode"),
                "resource_size": self.current_spec.get("resource_size"),
                "memory_high": self.current_spec.get("memory_high"),
                "memory_max": self.current_spec.get("memory_max"),
                "cpu_quota": self.current_spec.get("cpu_quota"),
                "cpu_shares": self.current_spec.get("cpu_shares"),
            }
        else:
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="app_control_assets_write",
                status="running",
            )
            write_app_control_assets(
                self.backend,
                self.settings,
                write_profile_state=write_profile_state,
                base_profile=self.base_profile,
                inter_app_interfaces=self.inter_app_interfaces,
            )
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="service_files_write",
                status="running",
            )
            quadlet_details = await _reconcile_app_quadlet_assets(
                self.backend,
                self.settings,
                base_profile=self.base_profile,
                inter_app_interfaces=self.inter_app_interfaces,
                services=self.services,
            )
            if quadlet_details["container_restart_required"]:
                _record_app_reconcile_step(
                    self.settings,
                    self.backend,
                    phase="create",
                    substep="app_service_restart",
                    status="running",
                )
                restart_fields = {
                    "app_id": "cnc.admin",
                    "backend_id": self.backend.id,
                    "backend": self.backend.name,
                    "operation_id": self.operation_id,
                    "container": self.container,
                    "service": quadlet_details["container_service"],
                    "reason": "container_policy_changed",
                    "changed_files": quadlet_details["changed_files"],
                }
                logger.info(
                    "app.quadlet.container_restart",
                    status="started",
                    **restart_fields,
                )
                try:
                    await _restart_app_quadlet_backend(
                        self.backend,
                        self.settings,
                        services=self.services,
                    )
                except Exception:
                    logger.exception(
                        "app.quadlet.container_restart",
                        status="failed",
                        **restart_fields,
                    )
                    raise
                logger.info(
                    "app.quadlet.container_restart",
                    status="succeeded",
                    **restart_fields,
                )
                self.container_started = True
                self.result.recreated = True
                quadlet_container_restarted = True
            legacy_reboot_bridge = await _ensure_legacy_direct_podman_reboot_bridge(
                self.container,
                self.inspect_payload,
                self.settings,
                services=self.services,
            )
            if quadlet_container_restarted:
                resource_limits = {
                    "resource_mode": self.current_spec.get("resource_mode"),
                    "resource_size": self.current_spec.get("resource_size"),
                    "memory_high": self.current_spec.get("memory_high"),
                    "memory_max": self.current_spec.get("memory_max"),
                    "cpu_quota": self.current_spec.get("cpu_quota"),
                    "cpu_shares": self.current_spec.get("cpu_shares"),
                }
            else:
                try:
                    _record_app_reconcile_step(
                        self.settings,
                        self.backend,
                        phase="create",
                        substep="resource_limits_apply",
                        status="running",
                    )
                    resource_limits = await apply_app_resource_limits(
                        self.backend,
                        self.settings,
                        base_profile=self.base_profile,
                        services=self.services,
                        operation_id=self.operation_id,
                    )
                except CommandError as exc:
                    if not _command_error_is_stale_runtime_state(exc):
                        raise
                    auto_repair_details = await self._auto_repair_prepare_drift(
                        reason="stale_runtime_state",
                        container_exists=True,
                    )
                    if auto_repair_details is None:
                        raise
                    _record_app_reconcile_step(
                        self.settings,
                        self.backend,
                        phase="create",
                        substep="app_control_assets_write",
                        status="running",
                    )
                    write_app_control_assets(
                        self.backend,
                        self.settings,
                        write_profile_state=write_profile_state,
                        base_profile=self.base_profile,
                        inter_app_interfaces=self.inter_app_interfaces,
                    )
                    _record_app_reconcile_step(
                        self.settings,
                        self.backend,
                        phase="create",
                        substep="service_files_write",
                        status="running",
                    )
                    quadlet_details = await _reconcile_app_quadlet_assets(
                        self.backend,
                        self.settings,
                        base_profile=self.base_profile,
                        inter_app_interfaces=self.inter_app_interfaces,
                        services=self.services,
                    )
                    _record_app_reconcile_step(
                        self.settings,
                        self.backend,
                        phase="create",
                        substep="app_service_start",
                        status="running",
                    )
                    await _start_app_quadlet_backend(
                        self.backend, self.settings, services=self.services
                    )
                    self.result.created = True
                    legacy_reboot_bridge = (
                        await _remove_legacy_direct_podman_reboot_bridge(
                            self.container,
                            self.settings,
                            services=self.services,
                        )
                    )
                    resource_limits = {
                        "resource_mode": self.current_spec.get("resource_mode"),
                        "resource_size": self.current_spec.get("resource_size"),
                        "memory_high": self.current_spec.get("memory_high"),
                        "memory_max": self.current_spec.get("memory_max"),
                        "cpu_quota": self.current_spec.get("cpu_quota"),
                        "cpu_shares": self.current_spec.get("cpu_shares"),
                    }

        if not _app_container_running(
            self.backend, self.settings, services=self.services
        ):
            _record_app_reconcile_step(
                self.settings,
                self.backend,
                phase="create",
                substep="app_service_start",
                status="running",
            )
            await _start_app_quadlet_backend(
                self.backend, self.settings, services=self.services
            )
            self.container_started = True

        if "memory_policy" not in resource_limits:
            resource_limits["memory_policy"] = await apply_app_memory_limits(
                self.backend,
                self.settings,
                base_profile=self.base_profile,
                services=self.services,
                operation_id=self.operation_id,
            )

        if self.container_started or self.result.created or self.result.recreated:
            clear_guest_exec_circuit(
                self.settings, self.backend.name, reason="runtime_started"
            )

        return {
            "action": self.create_action,
            "created": self.result.created,
            "recreated": self.result.recreated,
            "container_started": self.container_started,
            "guest_rootfs": rootfs_state,
            "resource_limits": resource_limits,
            "quadlet": quadlet_details,
            "quadlet_container_restarted": quadlet_container_restarted,
            "auto_repair": auto_repair_details,
            "legacy_reboot_bridge": legacy_reboot_bridge,
        }

    async def _bootstrap_phase(self) -> dict[str, Any]:
        readiness_deadline = time.monotonic() + self.settings.command_timeout_apply_sec
        initial_timeout = min(
            self.settings.command_timeout_status_sec,
            max(0.0, readiness_deadline - time.monotonic()),
        )
        guest_state = await asyncio.to_thread(
            _guest_systemd_state,
            self.backend,
            self.settings,
            services=self.services,
            timeout_sec=initial_timeout,
            guard_wait_sec=min(
                _RUNTIME_GUEST_EXEC_GUARD_WAIT_SEC,
                initial_timeout,
            ),
        )
        if not guest_state["ready"]:
            guest_state = await _bootstrap_app_backend(
                self.backend,
                self.settings,
                services=self.services,
                initial_guest_state=guest_state,
                readiness_deadline=readiness_deadline,
                stop_on_failure=bool(
                    self.container_started
                    or self.result.created
                    or self.result.recreated
                ),
            )
            self.result.bootstrapped = True
        elif (read_bootstrap_state(self.settings, self.backend.name) or {}).get(
            "status"
        ) != "succeeded":
            write_bootstrap_state(
                self.settings,
                self.backend.name,
                {
                    "status": "succeeded",
                    "container_name": self.container,
                    "command": "guest init readiness",
                    "failure_phase": None,
                    "last_log_excerpt": None,
                    "dns_servers": self.current_spec.get("dns_servers", []),
                },
            )
        if self.container_started or self.result.created or self.result.recreated:
            await asyncio.to_thread(
                _verify_guest_isolation,
                self.backend,
                self.settings,
                services=self.services,
            )
        return {
            "bootstrapped": self.result.bootstrapped,
            "bootstrap_status": (
                read_bootstrap_state(self.settings, self.backend.name) or {}
            ).get("status"),
            "guest_system_state": guest_state.get("system_state"),
            "guest_multi_user_target": guest_state.get("multi_user_target"),
            "debug_toolbelt_version": APP_DEBUG_TOOLBELT_VERSION,
            "debug_tools": app_debug_tool_commands(),
        }

    async def _publish_phase(self) -> dict[str, Any]:
        _, self.inspect_payload = self.services.inspect_container(
            self.container,
            timeout_sec=self.settings.command_timeout_status_sec,
        )
        target_ip = container_ipv4_address(self.backend, self.inspect_payload)
        if target_ip is None:
            runtime_diagnostics = collect_app_backend_diagnostics(
                self.backend, self.settings
            )
            raise ApplyFailed(
                f"app backend {self.backend.name} missing private network address",
                phase="app_network",
                details={
                    "backend": self.backend.name,
                    "container": self.container,
                    "network": self.network,
                    "runtime_diagnostics": runtime_diagnostics,
                },
            )
        self.result.target_ip = target_ip
        return {
            "target_ip": target_ip,
            "loopback_target": f"127.0.0.1:{self.backend.port}",
        }

    async def _verify_phase(self) -> dict[str, Any]:
        if self.result.target_ip is None:
            raise ApplyFailed(
                f"app backend {self.backend.name} publish target missing",
                phase="app_publish",
                details={"backend": self.backend.name, "container": self.container},
            )
        healthchecks: dict[str, dict[str, Any]] = {}
        if self.backend.port is not None:
            healthchecks["loopback"] = await _observe_app_health(
                self.backend,
                self.settings,
                host="127.0.0.1",
                port=self.backend.port,
                services=self.services,
            )
        else:
            healthchecks["private"] = await _observe_app_health(
                self.backend,
                self.settings,
                host=self.result.target_ip,
                port=self.backend.handoff_port,
                services=self.services,
            )
        return {
            "private_target": f"{self.result.target_ip}:{self.backend.handoff_port}",
            "loopback_target": f"127.0.0.1:{self.backend.port}"
            if self.backend.port is not None
            else "",
            "healthcheck_mode": display_backend_healthcheck_mode(self.backend),
            "healthcheck_status": _aggregate_healthcheck_status(healthchecks),
            "healthchecks": healthchecks,
        }

    async def _steady_state_phase(self) -> dict[str, Any]:
        state = read_bootstrap_state(self.settings, self.backend.name) or {}
        write_bootstrap_state(
            self.settings,
            self.backend.name,
            {
                **state,
                "status": "succeeded",
                "container_name": self.container,
                "command": "guest init readiness",
                "failure_phase": None,
                "last_log_excerpt": None,
                "dns_servers": self.current_spec.get("dns_servers", []),
            },
        )
        clear_failure_artifact(self.settings, self.backend.name)
        return {
            "container": self.container,
            "target_ip": self.result.target_ip,
            "dns_servers": list(self.current_spec.get("dns_servers", [])),
        }


def _cpu_quota_to_cpus(raw: str, host_cpu_count: int | None) -> str:
    normalized = str(raw or "").strip()
    if normalized.endswith("%"):
        normalized = normalized[:-1]
    quota_percent = float(normalized)
    host_cpus = (
        host_cpu_count if isinstance(host_cpu_count, int) and host_cpu_count > 0 else 1
    )
    cpus = (quota_percent / 100.0) * host_cpus
    return f"{cpus:.2f}".rstrip("0").rstrip(".")


def _app_container_running(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> bool:
    services = services or default_app_runtime_services()
    _, payload = services.inspect_container(
        container_name(backend.name),
        timeout_sec=settings.command_timeout_status_sec,
    )
    state = payload.get("State") if isinstance(payload, dict) else {}
    if not isinstance(state, dict):
        return False
    return bool(state.get("Running"))


def _verify_guest_isolation(
    backend: Backend, settings: Settings, *, services: AppRuntimeServices
) -> None:
    unit = f"cnc-runtime-check-{uuid.uuid4().hex}"
    script = (
        "set -eu; "
        f"systemd-run --quiet --wait --collect --unit={unit}-user "
        "-p Type=exec -p User=65534 -p NoNewPrivileges=yes -p RestrictSUIDSGID=yes "
        '/bin/sh -ec \'test "$(id -u)" = 65534; '
        'grep -q "^CapEff:[[:space:]]*0000000000000000$" /proc/self/status; '
        'grep -q "^NoNewPrivs:[[:space:]]*1$" /proc/self/status\'; '
        f"systemd-run --quiet --wait --collect --unit={unit}-filesystem "
        "-p Type=exec -p ProtectSystem=strict -p ProtectHome=yes "
        "-p PrivateTmp=yes -p PrivateDevices=yes "
        "/bin/sh -ec 'test \"$(id -u)\" = 0; test ! -w /etc'"
    )
    result = run_backend_guest_command(
        backend.name,
        settings,
        ["/bin/sh", "-ec", script],
        timeout_sec=min(30, settings.command_timeout_apply_sec),
        command_runner=services.run_command,
        wait_sec=1,
        source="runtime_isolation_check",
    )
    if not result.ok:
        raise ApplyFailed(
            "Ubuntu guest failed its service isolation check",
            phase="app_bootstrap",
            details={
                "backend": backend.name,
                "returncode": result.returncode,
                "error": clip_output(result.stderr or result.stdout),
            },
        )


def _guest_systemd_state(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
    timeout_sec: float | None = None,
    wait: bool = False,
    guard_wait_sec: float = 0,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    wait_arg = " --wait" if wait else ""
    # `is-system-running --wait` still fails immediately before the guest bus exists.
    # Keep that transient startup race inside the one already-bounded exec session.
    manager_wait = (
        "until systemctl show-environment >/dev/null 2>&1; do sleep 0.1; done; "
        if wait
        else ""
    )
    guest_command = [
        "/bin/sh",
        "-c",
        (
            f"{manager_wait}systemctl is-system-running{wait_arg}; "
            "systemctl is-active multi-user.target"
        ),
    ]
    effective_timeout = (
        settings.command_timeout_status_sec if timeout_sec is None else timeout_sec
    )
    system_state_result = run_backend_guest_command(
        backend.name,
        settings,
        guest_command,
        timeout_sec=effective_timeout,
        command_runner=services.run_command,
        wait_sec=guard_wait_sec,
        source="runtime_readiness",
    )
    output_lines = [
        line.strip().lower()
        for line in system_state_result.stdout.splitlines()
        if line.strip()
    ]
    system_state = output_lines[0] if output_lines else "unknown"
    multi_user_target = output_lines[-1] if len(output_lines) > 1 else "unknown"
    exec_outcome = {
        EXEC_CIRCUIT_OPEN_RETURN_CODE: "circuit_open",
        EXEC_GUARD_BUSY_RETURN_CODE: "guard_busy",
        EXEC_GUARD_UNAVAILABLE_RETURN_CODE: "guard_unavailable",
        124: "timeout",
    }.get(system_state_result.returncode, "completed")
    exec_timed_out = exec_outcome == "timeout"
    ready = (
        not exec_timed_out
        and system_state in {"running", "degraded"}
        and multi_user_target == "active"
    )
    error = ""
    if not ready:
        error = (
            system_state_result.stderr
            or system_state_result.stdout
            or "guest systemd not ready"
        )
    return {
        "ready": ready,
        "system_state": system_state,
        "multi_user_target": multi_user_target,
        "exec_timed_out": exec_timed_out,
        "exec_outcome": exec_outcome,
        "error": clip_output(error) if error else "",
    }


def _bootstrap_retry_details(backend: Backend, settings: Settings) -> dict[str, Any]:
    state = read_bootstrap_state(settings, backend.name)
    return {
        "backend": backend.name,
        "container": container_name(backend.name),
        "bootstrap_state": state,
        "bootstrap_lock_active": bootstrap_lock_is_held(settings, backend.name),
    }


def _snapshot_bootstrap_state_file(
    settings: Settings, backend_name: str
) -> tuple[bool, str]:
    path = control_dir(settings, backend_name) / "bootstrap.json"
    try:
        return True, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, ""


def _restore_bootstrap_state_file(
    settings: Settings,
    backend_name: str,
    snapshot: tuple[bool, str],
) -> None:
    path = control_dir(settings, backend_name) / "bootstrap.json"
    existed, content = snapshot
    if existed:
        _write_text_atomic(path, content)
    else:
        _remove_runtime_artifact(path)


def _runtime_spec_diff_keys(
    saved_spec: dict[str, Any], current_spec: dict[str, Any]
) -> set[str]:
    return {
        key
        for key in set(saved_spec) | set(current_spec)
        if saved_spec.get(key) != current_spec.get(key)
    }


def _normalize_saved_spec(
    saved_spec: dict[str, Any] | None,
    current_spec: dict[str, Any],
) -> dict[str, Any] | None:
    if saved_spec is None:
        return None
    # Older releases may have persisted a partial runtime spec. Missing keys are
    # schema drift, not a requested rebuild, so backfill them from current state.
    normalized = dict(saved_spec)
    # A missing policy means the old runtime had no reviewed hardening overrides.
    normalized.setdefault("hardening", {})
    if "guest_isolation_revision" in current_spec:
        normalized.setdefault("guest_isolation_revision", 0)
    for key, value in current_spec.items():
        normalized.setdefault(key, value)
    return normalized


def _rebuild_required_spec_keys(
    saved_spec: dict[str, Any], current_spec: dict[str, Any]
) -> set[str]:
    immutable_keys = {
        "guest_isolation_revision",
        "hardening",
        "runtime_owner",
        "network",
        "sandbox_profile",
        "seed_image",
        "guest_rootfs",
        "init_command",
        "port",
        "handoff_port",
        "dns_servers",
        "volumes",
        "inter_app_interfaces",
    }
    return _runtime_spec_diff_keys(saved_spec, current_spec) & immutable_keys


def _remove_runtime_artifact(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _clear_runtime_artifacts_for_recreate(
    settings: Settings,
    backend_name: str,
    *,
    clear_lock: bool,
) -> list[str]:
    backend_dir = control_dir(settings, backend_name)
    removed: list[str] = []
    for filename in ("spec.json", "bootstrap.json", "failure.json"):
        if _remove_runtime_artifact(backend_dir / filename):
            removed.append(filename)
    if clear_lock and _remove_runtime_artifact(backend_dir / "bootstrap.lock"):
        removed.append("bootstrap.lock")
    return removed


def _stop_failed_bootstrap_container(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    container = container_name(backend.name)
    command = ["systemctl", "stop", quadlet_container_service_name(backend.name)]
    result = services.run_command(
        command,
        timeout_sec=min(60, max(10, settings.command_timeout_apply_sec)),
    )
    details = {
        "action": "stopped" if result.ok else "stop_failed",
        "command": command,
    }
    if result.stdout:
        details["stdout"] = clip_output(result.stdout)
    if result.stderr:
        details["stderr"] = clip_output(result.stderr)
    details["container"] = container
    return details


async def _bootstrap_app_backend(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
    initial_guest_state: dict[str, Any] | None = None,
    readiness_deadline: float | None = None,
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    bootstrap_snapshot = _snapshot_bootstrap_state_file(settings, backend.name)
    readiness_deadline = readiness_deadline or (
        time.monotonic() + settings.command_timeout_apply_sec
    )
    dns_servers = configured_app_dns_servers(settings)
    container = container_name(backend.name)
    state_details = _bootstrap_retry_details(backend, settings)
    existing_state = state_details.get("bootstrap_state")
    if isinstance(existing_state, dict) and existing_state.get("status") == "running":
        if state_details.get("bootstrap_lock_active"):
            raise ApplyFailed(
                f"app backend {backend.name} bootstrap already running",
                phase="app_bootstrap",
                details=state_details,
            )
        write_bootstrap_state(
            settings,
            backend.name,
            {
                **existing_state,
                "status": "failed",
                "finished_at": existing_state.get("finished_at")
                or existing_state.get("updated_at"),
                "failure_phase": "bootstrap_stale",
                "last_log_excerpt": "stale running bootstrap state detected before retry",
            },
        )
        logger.warning(
            "app.bootstrap.stale_state",
            backend_id=backend.id,
            backend=backend.name,
            container=container,
        )
    _inspect_result, inspect_payload = services.inspect_container(
        container,
        timeout_sec=settings.command_timeout_status_sec,
    )
    container_id = (
        inspect_payload.get("Id") if isinstance(inspect_payload, dict) else None
    )
    logger.info(
        "app.bootstrap.start",
        backend_id=backend.id,
        backend=backend.name,
        container=container,
        dns_servers=dns_servers,
        command="guest init readiness",
    )
    try:
        try:
            with BackendBootstrapLock(settings, backend.name):
                write_bootstrap_state(
                    settings,
                    backend.name,
                    {
                        "status": "running",
                        "started_at": existing_state.get("started_at")
                        if isinstance(existing_state, dict)
                        and existing_state.get("status") == "pending"
                        else None,
                        "finished_at": None,
                        "container_name": container,
                        "container_id": container_id,
                        "command": "guest init readiness",
                        "failure_phase": None,
                        "last_log_excerpt": None,
                        "dns_servers": dns_servers,
                    },
                )
                _record_app_reconcile_step(
                    settings,
                    backend,
                    phase="bootstrap",
                    substep="guest_systemd_check",
                    status="running",
                    dns_servers=dns_servers,
                )
                guest_state = initial_guest_state
                if (
                    guest_state is not None
                    and guest_state.get("exec_outcome") in _TERMINAL_GUEST_EXEC_OUTCOMES
                ):
                    exec_outcome = str(guest_state.get("exec_outcome"))
                    exec_error = (
                        "timed out"
                        if exec_outcome == "timeout"
                        else exec_outcome.replace("_", " ")
                    )
                    raise ApplyFailed(
                        f"app backend {backend.name} guest exec {exec_error}",
                        phase="app_bootstrap",
                        details={
                            "backend": backend.name,
                            "container": container,
                            "guest_state": guest_state,
                        },
                    )
                if guest_state is None or not guest_state.get("ready"):
                    remaining = readiness_deadline - time.monotonic()
                    if remaining <= 0:
                        raise ApplyFailed(
                            f"app backend {backend.name} guest readiness timed out",
                            phase="app_bootstrap",
                            details={
                                "backend": backend.name,
                                "container": container,
                                "guest_state": guest_state or {},
                            },
                        )
                    _record_app_reconcile_step(
                        settings,
                        backend,
                        phase="bootstrap",
                        substep="guest_readiness_wait",
                        status="running",
                        dns_servers=dns_servers,
                    )
                    guest_state = await asyncio.to_thread(
                        _guest_systemd_state,
                        backend,
                        settings,
                        services=services,
                        timeout_sec=remaining,
                        wait=True,
                        guard_wait_sec=min(
                            _RUNTIME_GUEST_EXEC_GUARD_WAIT_SEC,
                            remaining,
                        ),
                    )
                if guest_state.get("exec_outcome") in _TERMINAL_GUEST_EXEC_OUTCOMES:
                    exec_outcome = str(guest_state.get("exec_outcome"))
                    exec_error = (
                        "timed out"
                        if exec_outcome == "timeout"
                        else exec_outcome.replace("_", " ")
                    )
                    raise ApplyFailed(
                        f"app backend {backend.name} guest exec {exec_error}",
                        phase="app_bootstrap",
                        details={
                            "backend": backend.name,
                            "container": container,
                            "guest_state": guest_state,
                        },
                    )
                if not guest_state.get("ready"):
                    raise ApplyFailed(
                        f"app backend {backend.name} guest systemd not ready",
                        phase="app_bootstrap",
                        details={
                            "backend": backend.name,
                            "container": container,
                            "guest_state": guest_state,
                        },
                    )
        except BlockingIOError as exc:
            raise ApplyFailed(
                f"app backend {backend.name} bootstrap already running",
                phase="app_bootstrap",
                details=_bootstrap_retry_details(backend, settings),
            ) from exc
    except (CommandError, ApplyFailed) as exc:
        details = (
            exc.as_details()
            if isinstance(exc, ApplyFailed)
            else {
                "backend": backend.name,
                "container": container,
                "stdout": clip_output(exc.result.stdout),
                "stderr": clip_output(exc.result.stderr),
            }
        )
        guest_state = details.get("guest_state")
        exec_outcome = (
            str(guest_state.get("exec_outcome") or "")
            if isinstance(guest_state, dict)
            else ""
        )
        if exec_outcome in {"guard_busy", "guard_unavailable"}:
            _restore_bootstrap_state_file(
                settings,
                backend.name,
                bootstrap_snapshot,
            )
            log_observation = (
                logger.warning if exec_outcome == "guard_unavailable" else logger.info
            )
            log_observation(
                "app.bootstrap.observation_deferred",
                backend_id=backend.id,
                backend=backend.name,
                container=container,
                exec_outcome=exec_outcome,
                prior_bootstrap_status=(existing_state or {}).get("status")
                if isinstance(existing_state, dict)
                else "missing",
            )
            raise ApplyFailed(
                f"app backend {backend.name} bootstrap observation unavailable",
                phase="app_bootstrap",
                details={
                    "backend": backend.name,
                    "container": container,
                    "dns_servers": dns_servers,
                    "bootstrap_observation": {
                        "status": "unavailable",
                        "reason": exec_outcome,
                        "guest_observed": False,
                    },
                    "bootstrap_cleanup": {
                        "action": "preserved",
                        "container": container,
                        "reason": exec_outcome,
                    },
                    **details,
                },
            ) from exc
        write_bootstrap_state(
            settings,
            backend.name,
            {
                "status": "failed",
                "started_at": (read_bootstrap_state(settings, backend.name) or {}).get(
                    "started_at"
                ),
                "finished_at": None,
                "container_name": container,
                "container_id": container_id,
                "command": "guest init readiness",
                "failure_phase": "app_bootstrap",
                "last_log_excerpt": clip_output(
                    str(details.get("error") or details.get("stderr") or details)
                ),
                "dns_servers": dns_servers,
            },
        )
        observation_failed = isinstance(guest_state, dict)
        should_stop = (
            observation_failed
            and stop_on_failure
            and exec_outcome not in {"guard_busy", "guard_unavailable"}
        )
        if should_stop:
            cleanup_details = await asyncio.to_thread(
                _stop_failed_bootstrap_container,
                backend,
                settings,
                services=services,
            )
        else:
            if exec_outcome in {"guard_busy", "guard_unavailable"}:
                preserve_reason = exec_outcome
            elif not stop_on_failure:
                preserve_reason = "runtime_not_started_by_reconcile"
            else:
                preserve_reason = "bootstrap_not_observed"
            cleanup_details = {
                "action": "preserved",
                "container": container,
                "reason": preserve_reason,
            }
        logger.warning(
            "app.bootstrap.failed",
            backend_id=backend.id,
            backend=backend.name,
            container=container,
            exec_outcome=exec_outcome or "not_observed",
            cleanup=cleanup_details,
            error=str(details.get("error") or details.get("stderr") or "")[:2000],
        )
        raise ApplyFailed(
            f"app backend {backend.name} bootstrap failed",
            phase="app_bootstrap",
            details={
                "backend": backend.name,
                "container": container,
                "dns_servers": dns_servers,
                "bootstrap_state": read_bootstrap_state(settings, backend.name),
                "bootstrap_cleanup": cleanup_details,
                **details,
            },
        ) from exc
    write_bootstrap_state(
        settings,
        backend.name,
        {
            "status": "succeeded",
            "started_at": (read_bootstrap_state(settings, backend.name) or {}).get(
                "started_at"
            ),
            "finished_at": None,
            "container_name": container,
            "container_id": container_id,
            "command": "guest init readiness",
            "failure_phase": None,
            "last_log_excerpt": None,
            "dns_servers": dns_servers,
        },
    )
    logger.info(
        "app.bootstrap.done",
        backend_id=backend.id,
        backend=backend.name,
        container=container,
        dns_servers=dns_servers,
        result="success",
    )
    return guest_state


async def _observe_app_health(
    backend: Backend,
    settings: Settings,
    *,
    host: str,
    port: int,
    services: AppRuntimeServices | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    probe = await asyncio.to_thread(
        probe_backend_health,
        backend,
        host=host,
        port=port,
        timeout_sec=min(settings.command_timeout_status_sec, 5),
        command_runner=services.run_command,
    )
    details: dict[str, Any] = {
        "ok": probe.ok,
        "checked": probe.checked,
        "status": probe.status,
        "mode": probe.plan.display_mode,
    }
    if probe.plan.effective_mode == "http":
        path = str(probe.plan.path or "/")
        details["url"] = f"http://{host}:{port}{path}"
        if probe.plan.host_header:
            details["host_header"] = probe.plan.host_header
        if probe.http_status is not None:
            details["http_status"] = probe.http_status
    else:
        details["host"] = host
        details["port"] = port
    if probe.error:
        details["error"] = clip_output(probe.error)
    return details


def _aggregate_healthcheck_status(
    healthchecks: dict[str, dict[str, Any]],
) -> str:
    checked = [item for item in healthchecks.values() if item.get("checked")]
    if not checked:
        return "unmonitored"
    return "ok" if all(item.get("ok") for item in checked) else "degraded"


async def _cleanup_disabled_app_backend(
    backend: Backend,
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> dict[str, Any]:
    services = services or default_app_runtime_services()
    container = container_name(backend.name)
    network = network_name(backend.name)
    _inspect_result, inspect_payload = services.inspect_container(
        container,
        timeout_sec=settings.command_timeout_status_sec,
    )
    _network_result, network_payload = services.inspect_network(
        network,
        timeout_sec=settings.command_timeout_status_sec,
    )
    state = inspect_payload.get("State") if isinstance(inspect_payload, dict) else {}
    container_running = isinstance(state, dict) and bool(state.get("Running"))
    container_exists = inspect_payload is not None
    network_exists = network_payload is not None
    actions: list[str] = []

    if container_running or container_exists:
        await _stop_app_quadlet_backend(backend.name, settings, services=services)
        actions.append(f"stopped {quadlet_container_service_name(backend.name)}")
    if container_exists:
        _remove_podman_container(container, settings, services=services)
        actions.append(f"removed {container}")
    if network_exists:
        _stop_app_quadlet_network(backend.name, settings, services=services)
        actions.append(f"stopped {quadlet_network_service_name(backend.name)}")
        _remove_podman_network(network, settings, services=services)
        actions.append(f"removed {network}")

    removed_quadlet_files = remove_app_quadlet_assets(settings, backend.name)
    if removed_quadlet_files:
        actions.extend(f"removed {path}" for path in removed_quadlet_files)
        await _systemctl_daemon_reload(settings, services=services)
        actions.append("daemon-reload")

    return {
        "backend": backend.name,
        "container": container,
        "network": network,
        "container_stopped": container_running,
        "container_removed": container_exists,
        "network_removed": network_exists,
        "quadlet_files_removed": removed_quadlet_files,
        "actions": actions,
    }
