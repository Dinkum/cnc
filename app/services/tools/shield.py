
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.services import nginx_runtime
from app.services.apply_core import DesiredState
from app.services.commands import CommandError, run_command
from app.services.nginx_contract import nginx_gateway_enabled
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
phases = ("shield_runtime",)


def shield_wanted(desired: DesiredState, settings: Settings) -> bool:
    return bool(desired.shield_required) or _assets_exist(settings)


async def shield(plan: Plan) -> bool:
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
    if plan.desired.shield_required and nginx_gateway_enabled(
        plan.settings, container_mode=plan.desired.nginx_container
    ):
        await nginx_runtime.ensure_nginx_network(plan)
    plan.details["shield_runtime"] = await _reconcile(
        plan.desired, plan.settings, services=plan.services
    )
    return True


def _service_active(plan: Plan) -> bool:
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
        shield_quadlet_path(settings): render_shield_quadlet(
            settings, nginx_container=desired.nginx_container
        ),
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
        nginx_container=desired.nginx_container,
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
