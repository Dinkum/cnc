"""shield: the password gate container placed in front of an output.

Owns its Quadlet unit and two files under the Shield state directory. When
Shield is switched off the same tool takes the container down and removes them,
which is why it stays applicable while its assets still exist.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.services.apply_core import DesiredState
from app.services.commands import CommandError, run_command
from app.services.shield_runtime import (
    SHIELD_CONTAINER_SERVICE,
    remove_shield_runtime_assets,
    render_shield_config,
    render_shield_env,
    render_shield_quadlet,
    shield_quadlet_path,
    write_shield_runtime_assets,
)
from app.services.tools.core import Plan

tool_name = "shield"
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("shield_runtime",)


def shield_wanted(desired: DesiredState, settings: Settings) -> bool:
    """Whether Shield is in play at all, either wanted or still installed."""
    return bool(desired.shield_required) or _assets_exist(settings)


async def shield(plan: Plan) -> bool:
    """Converge Shield, including tearing it down when it is no longer wanted."""
    if (
        not _assets_exist(plan.settings)
        and not plan.desired.shield_required
    ):
        return False
    if converged(
        plan.desired, plan.settings, services=plan.services
    ) and _service_active(plan):
        plan.details["shield_runtime"] = {
            "skipped": True,
            "reason": "live Shield assets already match",
            "required": bool(plan.desired.shield_required),
        }
        return False
    plan.touching(tool_name)
    plan.details["shield_runtime"] = await _reconcile(
        plan.desired, plan.settings, services=plan.services
    )
    return True


def _service_active(plan: Plan) -> bool:
    """Whether the Shield container is actually running.

    Matching files are not convergence. A restart that failed after the files
    were written left them correct on disk, so the next attempt saw a match and
    skipped the restart, reporting a gate that was never up as converged.
    """
    if not plan.desired.shield_required:
        return True
    runner = getattr(plan.services.runtime_services, "run_command", run_command)
    result = runner(
        ["systemctl", "is-active", "--quiet", SHIELD_CONTAINER_SERVICE],
        timeout_sec=plan.settings.command_timeout_status_sec,
    )
    return bool(result.ok)


def converged(
    desired: DesiredState, settings: Settings, *, services: Any = None
) -> bool:
    if not desired.shield_required:
        return not _assets_exist(settings)
    expected_files = {
        shield_quadlet_path(settings): render_shield_quadlet(settings),
        settings.shield_env_file_path: render_shield_env(settings),
        settings.shield_config_path: render_shield_config(
            desired.shield_output_code_hashes
        ),
    }
    for path, expected_content in expected_files.items():
        try:
            if path.read_text(encoding="utf-8") != expected_content:
                return False
        except OSError:
            return False
    return True


def _assets_exist(settings: Settings) -> bool:
    return any(
        path.exists()
        for path in (
            shield_quadlet_path(settings),
            settings.shield_env_file_path,
            settings.shield_config_path,
        )
    )


async def _reconcile(
    desired: DesiredState,
    settings: Settings,
    *,
    services: Any,
) -> dict[str, Any]:
    if not desired.shield_required:
        had_assets = _assets_exist(settings)
        if had_assets:
            try:
                await services.runtime_services.run_command_checked_async(
                    ["systemctl", "disable", "--now", SHIELD_CONTAINER_SERVICE],
                    timeout_sec=settings.command_timeout_apply_sec,
                )
            except CommandError:
                pass
        removed = remove_shield_runtime_assets(settings)
        if removed:
            await services.runtime_services.run_command_checked_async(
                ["systemctl", "daemon-reload"],
                timeout_sec=settings.command_timeout_apply_sec,
            )
        return {"required": False, "removed_files": removed}

    result = write_shield_runtime_assets(
        settings,
        output_code_hashes=desired.shield_output_code_hashes,
    )
    await services.runtime_services.run_command_checked_async(
        ["systemctl", "daemon-reload"],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    await services.runtime_services.run_command_checked_async(
        ["systemctl", "restart", SHIELD_CONTAINER_SERVICE],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    return {"required": True, **result}
