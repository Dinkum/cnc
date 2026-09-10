from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.commands import run_command
from app.services import guest_isolation
from app.services.ssh_access import (
    backend_ssh_root_wrapper_script,
    reconcile_backend_ssh_root_wrapper,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING_DIR = REPO_ROOT / "packaging"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
MANAGED_SYSTEMD_ASSETS = (
    ("cnc-admin.service", "cnc-admin.service"),
    ("cnc-auto-size.service", "cnc-auto-size.service"),
    ("cnc-auto-size.timer", "cnc-auto-size.timer"),
    ("cnc-backend-alerts.service", "cnc-backend-alerts.service"),
    ("cnc-backend-alerts.timer", "cnc-backend-alerts.timer"),
    ("cnc-cloudflare-sync.service", "cnc-cloudflare-sync.service"),
    ("cnc-cloudflare-sync.timer", "cnc-cloudflare-sync.timer"),
    ("cnc-update-check.service", "cnc-update-check.service"),
    ("cnc-update-check.timer", "cnc-update-check.timer"),
    ("systemd/cnc-edge-public-ingress.slice", "cnc-edge-public-ingress.slice"),
    ("systemd/cnc-edge-tailnet.slice", "cnc-edge-tailnet.slice"),
    ("systemd/cnc-admin-control.slice", "cnc-admin-control.slice"),
    ("systemd/cnc-apps.slice", "cnc-apps.slice"),
)
MANAGED_SYSTEMD_UNITS = tuple(
    destination for _source, destination in MANAGED_SYSTEMD_ASSETS
)
MANAGED_TIMER_UNITS = (
    "cnc-auto-size.timer",
    "cnc-backend-alerts.timer",
    "cnc-cloudflare-sync.timer",
    "cnc-update-check.timer",
)


def _sync_file(source: Path, destination: Path) -> bool:
    if not source.exists():
        raise FileNotFoundError(f"managed asset missing from release: {source}")

    source_bytes = source.read_bytes()
    if destination.exists() and destination.read_bytes() == source_bytes:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.tmp")
    temp_path.write_bytes(source_bytes)
    temp_path.chmod(0o644)
    temp_path.replace(destination)
    return True


def reconcile_guest_runtime_helper(settings: Settings) -> None:
    content = guest_isolation.guest_helper_bytes()
    path = settings.guest_runtime_helper_path
    if path.is_symlink():
        raise ValueError("Guest runtime helper cannot be a symlink.")
    if not path.exists() or path.read_bytes() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o755)
        temporary.replace(path)
    settings.guest_runtime_helper_path.chmod(0o755)


def inspect_managed_systemd_assets(
    settings: Settings,
    *,
    packaging_dir: Path | None = None,
    systemd_unit_dir: Path | None = None,
) -> dict[str, Any]:
    packaging_root = packaging_dir or PACKAGING_DIR
    target_unit_dir = systemd_unit_dir or SYSTEMD_UNIT_DIR
    changed_units: list[str] = []
    changed_files: list[str] = []

    for source_name, destination_name in MANAGED_SYSTEMD_ASSETS:
        source = packaging_root / source_name
        destination = target_unit_dir / destination_name
        if not source.exists():
            raise FileNotFoundError(f"managed asset missing from release: {source}")
        if not destination.exists() or destination.read_bytes() != source.read_bytes():
            changed_units.append(destination_name)

    wrapper_path = settings.ssh_backend_root_wrapper_path
    expected_wrapper = backend_ssh_root_wrapper_script().encode("utf-8")
    if (
        not wrapper_path.exists()
        or wrapper_path.read_bytes() != expected_wrapper
        or wrapper_path.stat().st_mode & 0o777 != 0o755
    ):
        changed_files.append(str(wrapper_path))

    helper_path = settings.guest_runtime_helper_path
    if (
        not helper_path.exists()
        or helper_path.read_bytes() != guest_isolation.guest_helper_bytes()
        or helper_path.stat().st_mode & 0o777 != 0o755
    ):
        changed_files.append(str(helper_path))

    timer_states: dict[str, dict[str, bool]] = {}
    for timer_name in MANAGED_TIMER_UNITS:
        enabled_result = run_command(
            ["systemctl", "is-enabled", timer_name],
            timeout_sec=settings.command_timeout_status_sec,
        )
        active_result = run_command(
            ["systemctl", "is-active", timer_name],
            timeout_sec=settings.command_timeout_status_sec,
        )
        timer_needs_enable = (
            enabled_result.returncode != 0 or enabled_result.stdout.strip() != "enabled"
        )
        timer_needs_start = (
            active_result.returncode != 0 or active_result.stdout.strip() != "active"
        )
        timer_states[timer_name] = {
            "enabled": not timer_needs_enable,
            "active": not timer_needs_start,
        }

    return {
        "changed_units": changed_units,
        "changed_files": changed_files,
        "daemon_reload_needed": bool(changed_units),
        "admin_restart_required": "cnc-admin.service" in changed_units,
        "timer_states": timer_states,
        "timer_reconcile_needed": any(
            not state["enabled"] or not state["active"]
            for state in timer_states.values()
        ),
    }


def reconcile_managed_systemd_assets(
    settings: Settings,
    *,
    packaging_dir: Path | None = None,
    systemd_unit_dir: Path | None = None,
) -> dict[str, Any]:
    packaging_root = packaging_dir or PACKAGING_DIR
    target_unit_dir = systemd_unit_dir or SYSTEMD_UNIT_DIR
    observed = inspect_managed_systemd_assets(
        settings,
        packaging_dir=packaging_root,
        systemd_unit_dir=target_unit_dir,
    )
    changed_units = list(observed["changed_units"])
    changed_files = list(observed["changed_files"])

    changed_set = set(changed_units)
    for source_name, destination_name in MANAGED_SYSTEMD_ASSETS:
        if destination_name not in changed_set:
            continue
        source = packaging_root / source_name
        destination = target_unit_dir / destination_name
        _sync_file(source, destination)

    if changed_units:
        reload_result = run_command(
            ["systemctl", "daemon-reload"],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not reload_result.ok:
            raise RuntimeError(
                reload_result.stderr
                or reload_result.stdout
                or "systemctl daemon-reload failed"
            )

    if str(settings.ssh_backend_root_wrapper_path) in changed_files:
        reconcile_backend_ssh_root_wrapper(settings)
    if str(settings.guest_runtime_helper_path) in changed_files:
        reconcile_guest_runtime_helper(settings)

    timer_reconciled = False
    timer_states = observed["timer_states"]
    for timer_name, state in timer_states.items():
        if state["enabled"] and state["active"]:
            continue
        enable_result = run_command(
            ["systemctl", "enable", "--now", timer_name],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not enable_result.ok:
            raise RuntimeError(
                enable_result.stderr
                or enable_result.stdout
                or "systemctl enable --now failed"
            )
        timer_reconciled = True
        state["enabled"] = True
        state["active"] = True

    return {
        "changed_units": changed_units,
        "changed_files": changed_files,
        "daemon_reloaded": bool(changed_units),
        "admin_restart_required": "cnc-admin.service" in changed_units,
        "timer_states": timer_states,
        "timer_reconciled": timer_reconciled,
    }
