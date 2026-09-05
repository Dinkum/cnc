from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.models.entities import Backend
from app.services.app_containers import control_dir
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.app_quadlet import quadlet_container_service_name
from app.services.bootstrap_state import bootstrap_lock_is_held
from app.services.commands import run_command
from app.services.guest_exec import clear_guest_exec_circuit
from app.services.renderers import container_name, network_name, safe_slug


OBSERVATION_ONLY_DIAGNOSES = {
    "backend_observation_deferred",
    "backend_observation_unavailable",
    "sandbox_observation_unavailable",
}


def _remove_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _clear_runtime_artifacts(
    settings: Settings,
    backend_name: str,
    *,
    clear_lock: bool,
) -> list[str]:
    backend_dir = control_dir(settings, backend_name)
    removed: list[str] = []
    for filename in ("spec.json", "bootstrap.json"):
        if _remove_file(backend_dir / filename):
            removed.append(filename)
    if clear_lock and _remove_file(backend_dir / "bootstrap.lock"):
        removed.append("bootstrap.lock")
    return removed


def _restart_runtime(backend_name: str, settings: Settings) -> list[str]:
    command = ["systemctl", "restart", quadlet_container_service_name(backend_name)]
    result = run_command(
        command,
        timeout_sec=min(60, max(10, settings.command_timeout_apply_sec)),
    )
    if not result.ok:
        raise RuntimeError(
            result.stderr or result.stdout or f"command failed: {' '.join(command)}"
        )
    return [" ".join(command)]


def _remove_container(backend_name: str, settings: Settings) -> list[str]:
    command = ["podman", "rm", "-f", container_name(backend_name)]
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if not result.ok:
        raise RuntimeError(
            result.stderr or result.stdout or f"command failed: {' '.join(command)}"
        )
    return [" ".join(command)]


def _remove_network(backend_name: str, settings: Settings) -> list[str]:
    command = ["podman", "network", "rm", network_name(backend_name)]
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if not result.ok:
        raise RuntimeError(
            result.stderr or result.stdout or f"command failed: {' '.join(command)}"
        )
    return [" ".join(command)]


def _unmount_container_storage(candidate: str, settings: Settings) -> list[str]:
    command = ["podman", "unmount", "--force", candidate]
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if not result.ok:
        raise RuntimeError(
            result.stderr or result.stdout or f"command failed: {' '.join(command)}"
        )
    return [" ".join(command)]


def _legacy_loopback_proxy_unit_names(backend_name: str) -> tuple[str, str]:
    slug = safe_slug(backend_name)
    return (f"cnc-proxy-{slug}.service", f"cnc-proxy-{slug}.socket")


def _remove_legacy_loopback_proxy(backend_name: str, settings: Settings) -> list[str]:
    service_name, socket_name = _legacy_loopback_proxy_unit_names(backend_name)
    service_path = settings.systemd_generated_dir / service_name
    socket_path = settings.systemd_generated_dir / socket_name
    present_paths = [path for path in (service_path, socket_path) if path.exists()]
    if not present_paths:
        return []

    actions: list[str] = []
    stop_command = ["systemctl", "stop", socket_name, service_name]
    stop_result = run_command(
        stop_command, timeout_sec=settings.command_timeout_apply_sec
    )
    if stop_result.ok:
        actions.append(" ".join(stop_command))

    for path in present_paths:
        if _remove_file(path):
            actions.append(f"removed {path.name}")

    reload_command = ["systemctl", "daemon-reload"]
    reload_result = run_command(
        reload_command, timeout_sec=settings.command_timeout_apply_sec
    )
    if not reload_result.ok:
        raise RuntimeError(
            reload_result.stderr
            or reload_result.stdout
            or "systemctl daemon-reload failed"
        )
    actions.append(" ".join(reload_command))
    return actions


def plan_app_backend_repair(before: dict[str, Any]) -> list[dict[str, str]]:
    """Build a small, idempotent host-runtime plan from one diagnostic snapshot."""
    issues = {str(issue) for issue in before.get("issues") or []}
    diagnosis = str(before.get("diagnosis") or "")
    plan: list[dict[str, str]] = []
    planned: set[tuple[str, str]] = set()

    def add(action: str, target: str = "") -> None:
        key = (action, target)
        if key not in planned:
            planned.add(key)
            plan.append({"action": action, **({"target": target} if target else {})})

    if diagnosis in OBSERVATION_ONLY_DIAGNOSES:
        return plan

    # Guest exec failure is a host/runtime boundary failure. Do not enter the
    # guest or clear its durable state; restart the owning Quadlet exactly once.
    if diagnosis == "backend_exec_unavailable":
        exec_status = str(before.get("backend_exec_status") or "").strip().lower()
        if before.get("container_exists") and exec_status not in {
            "busy",
            "guard_unavailable",
        }:
            add("restart_quadlet")
        return plan

    if "storage_orphan" in issues:
        mounted_ids = {
            entry.get("Id")
            for entry in before.get("mounted_external_containers") or []
            if isinstance(entry, dict) and isinstance(entry.get("Id"), str)
        }
        for entry in before.get("external_containers") or []:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("Id") or entry.get("Name")
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            if candidate in mounted_ids:
                add("unmount_storage", candidate)
            add("remove_storage", candidate)

    remove_container = bool(
        issues
        & {
            "bridge_dns_enabled",
            "missing_saved_spec",
            "bootstrap_failed",
            "bootstrap_stuck",
            "network_missing",
            "private_ip_missing",
            "loopback_publish_missing",
            "legacy_loopback_proxy_present",
        }
    ) and bool(before.get("container_exists"))
    if "stale_name_registration" in issues and before.get("container_name_registered"):
        remove_container = True
    if remove_container:
        add("remove_container")

    remove_network = bool(
        issues
        & {
            "bridge_dns_enabled",
            "network_missing",
            "private_ip_missing",
            "network_orphan",
        }
    ) and bool(before.get("network_exists"))
    if remove_network:
        add("remove_network")

    if issues & {"loopback_publish_missing", "legacy_loopback_proxy_present"}:
        add("remove_legacy_proxy")

    clear_runtime = bool(
        issues
        & {
            "bridge_dns_enabled",
            "bootstrap_failed",
            "bootstrap_stuck",
            "network_missing",
            "private_ip_missing",
            "loopback_publish_missing",
            "legacy_loopback_proxy_present",
        }
    ) or ("container_missing" in issues and bool(before.get("saved_spec_present")))
    if clear_runtime:
        add("clear_runtime")

    should_restart = (
        bool(issues & {"private_unreachable", "loopback_unreachable"})
        and bool(before.get("container_exists"))
        and (before.get("bootstrap") or {}).get("status") == "succeeded"
    )
    if should_restart and not remove_container:
        add("restart_quadlet")
    return plan


def repair_app_backend(
    backend: Backend,
    settings: Settings,
    *,
    before: dict[str, Any] | None = None,
    plan: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if before is None:
        before = collect_app_backend_diagnostics(backend, settings)
    plan = plan if plan is not None else plan_app_backend_repair(before)
    actions: list[str] = []
    errors: list[str] = []
    changed = False

    try:
        for step in plan:
            action = step["action"]
            target = step.get("target", "")
            if action == "unmount_storage":
                actions.extend(_unmount_container_storage(target, settings))
                changed = True
            elif action == "remove_storage":
                result = run_command(
                    ["podman", "rm", "--storage", "--force", target],
                    timeout_sec=settings.command_timeout_apply_sec,
                )
                if result.ok:
                    changed = True
                    actions.append(f"podman rm --storage --force {target}")
                else:
                    raise RuntimeError(
                        result.stderr
                        or result.stdout
                        or f"failed removing storage orphan {target}"
                    )
            elif action == "remove_container":
                actions.extend(_remove_container(backend.name, settings))
                changed = True
            elif action == "remove_network":
                actions.extend(_remove_network(backend.name, settings))
                changed = True
            elif action == "remove_legacy_proxy":
                proxy_actions = _remove_legacy_loopback_proxy(backend.name, settings)
                if proxy_actions:
                    changed = True
                    actions.extend(proxy_actions)
            elif action == "clear_runtime":
                removed = _clear_runtime_artifacts(
                    settings,
                    backend.name,
                    clear_lock=not bootstrap_lock_is_held(settings, backend.name),
                )
                if removed:
                    changed = True
                    actions.extend(f"removed {name}" for name in removed)
            elif action == "restart_quadlet":
                actions.extend(_restart_runtime(backend.name, settings))
                clear_guest_exec_circuit(
                    settings, backend.name, reason="quadlet_restarted"
                )
                changed = True
            else:
                raise RuntimeError(f"unknown repair action: {action}")
    except RuntimeError as exc:
        errors.append(str(exc))

    return {
        "backend": backend.name,
        "changed": changed,
        "ok": not errors,
        "before": before,
        "plan": plan,
        "actions": actions,
        "errors": errors,
    }
