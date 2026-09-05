from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.apply_core import ApplyFailed
from app.services.resource_profile import (
    ResourceProfile,
    calculate_app_memory_budget_bytes,
)
from app.services.systemd_memory import (
    SystemdMemoryPolicyError,
    converge_systemd_memory_policy,
)


APPS_SLICE_UNIT = "cnc-apps.slice"
APPS_SLICE_DROP_IN = "50-memory.conf"
APPS_SLICE_HEADER = "# Managed by CNC. Do not edit by hand."
logger = get_logger("apply.apps_memory")


def apps_slice_drop_in_path(*, systemd_unit_dir: Path) -> Path:
    return systemd_unit_dir / f"{APPS_SLICE_UNIT}.d" / APPS_SLICE_DROP_IN


def app_memory_budget_bytes(profile: ResourceProfile, settings: Settings) -> int:
    budget = profile.app_memory_budget_bytes
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        budget = calculate_app_memory_budget_bytes(settings)
    if not isinstance(budget, int) or budget <= 0:
        raise ApplyFailed(
            "host application memory budget is unavailable",
            phase="app_runtime",
            details={"unit": APPS_SLICE_UNIT},
        )
    return budget


def render_apps_slice_drop_in(memory_max_bytes: int) -> str:
    if memory_max_bytes <= 0:
        raise ValueError("memory_max_bytes must be positive")
    return "\n".join(
        [
            APPS_SLICE_HEADER,
            "",
            "[Slice]",
            f"MemoryMax={memory_max_bytes}",
            "",
        ]
    )


def _write_atomic(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.chmod(0o644)
    temporary.replace(path)
    return True


async def reconcile_apps_slice(
    profile: ResourceProfile,
    settings: Settings,
    *,
    services: Any,
    systemd_unit_dir: Path | None = None,
) -> dict[str, Any]:
    budget = app_memory_budget_bytes(profile, settings)
    path = apps_slice_drop_in_path(
        systemd_unit_dir=systemd_unit_dir or settings.systemd_generated_dir
    )
    previous = path.read_bytes() if path.exists() else None
    changed = _write_atomic(path, render_apps_slice_drop_in(budget))
    live_policy: dict[str, Any] | None = None
    try:
        if changed:
            await services.run_command_checked_async(
                ["systemctl", "daemon-reload"],
                timeout_sec=settings.command_timeout_apply_sec,
            )
        await services.run_command_checked_async(
            [
                "systemd-analyze",
                "verify",
                str(settings.systemd_generated_dir / APPS_SLICE_UNIT),
            ],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        live_policy = await converge_systemd_memory_policy(
            APPS_SLICE_UNIT,
            services=services,
            timeout_sec=settings.command_timeout_apply_sec,
            memory_max=budget,
        )
        log_policy = (
            logger.warning
            if live_policy["memory_max_lowered_below_usage"]
            else logger.info
        )
        log_policy(
            "apps_memory.slice.policy_converged",
            app_id="cnc.admin",
            unit=APPS_SLICE_UNIT,
            memory_max_bytes=budget,
            memory_current_bytes=live_policy["memory_current_bytes"],
            control_group=live_policy["control_group"],
            changed=live_policy["changed"],
            memory_max_lowered_below_usage=live_policy[
                "memory_max_lowered_below_usage"
            ],
            verified=live_policy["verified"],
        )
    except Exception as exc:
        if isinstance(exc, SystemdMemoryPolicyError):
            logger.error(
                "apps_memory.slice.policy_failed",
                app_id="cnc.admin",
                unit=APPS_SLICE_UNIT,
                memory_max_bytes=budget,
                details=exc.details,
            )
        if changed:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                temporary = path.with_name(f".{path.name}.rollback")
                temporary.write_bytes(previous)
                temporary.chmod(0o644)
                temporary.replace(path)
            try:
                await services.run_command_checked_async(
                    ["systemctl", "daemon-reload"],
                    timeout_sec=settings.command_timeout_apply_sec,
                )
            except Exception:
                logger.exception(
                    "apps_memory.slice.rollback_reload_failed",
                    unit=APPS_SLICE_UNIT,
                    drop_in=str(path),
                )
        raise
    return {
        "unit": APPS_SLICE_UNIT,
        "drop_in": str(path),
        "memory_max_bytes": budget,
        "changed": changed,
        "verified": True,
        "live_policy": live_policy,
    }
