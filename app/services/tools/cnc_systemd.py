
from __future__ import annotations

from pathlib import Path

from app.services.commands import run_command
from app.services.runtime_assets import (
    MANAGED_SYSTEMD_ASSETS,
    MANAGED_TIMER_UNITS,
    PACKAGING_DIR,
    SYSTEMD_UNIT_DIR,
)
from app.services.tools.core import Plan, managed

tool_name = "cnc_systemd"
phases = ("runtime_assets",)

PREFIX = "cnc-"


def wanted(packaging_dir: Path | None = None) -> dict[str, str]:
    root = packaging_dir or PACKAGING_DIR
    units: dict[str, str] = {}
    for source_name, destination in MANAGED_SYSTEMD_ASSETS:
        source = root / source_name
        if source.exists():
            units[destination] = source.read_text(encoding="utf-8")
    return units


def cnc_systemd(plan: Plan) -> bool:
    unit_dir = Path(plan.extra("systemd_unit_dir") or SYSTEMD_UNIT_DIR)
    with managed(unit_dir, PREFIX, wanted(plan.extra("packaging_dir"))) as changed:
        if not changed:
            return _enable_timers(plan)
        plan.touching(tool_name)
        _run(plan, ["systemctl", "daemon-reload"])
        _enable_timers(plan, force=True)
        return True


def _enable_timers(plan: Plan, *, force: bool = False) -> bool:
    changed = False
    for timer in MANAGED_TIMER_UNITS:
        if not force and _is_active(plan, timer):
            continue
        _run(plan, ["systemctl", "enable", "--now", timer])
        changed = True
    return changed


def _is_active(plan: Plan, unit: str) -> bool:
    result = run_command(
        ["systemctl", "is-active", "--quiet", unit],
        timeout_sec=plan.settings.command_timeout_status_sec,
    )
    return result.ok


def _run(plan: Plan, command: list[str]) -> None:
    result = run_command(command, timeout_sec=plan.settings.command_timeout_apply_sec)
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout or " ".join(command))
