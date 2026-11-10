
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.services.apply_core import ApplyFailed, clip_output
from app.services.commands import CommandError, run_command
from app.services.error_reporting import ErrorCode
from app.services.tools import certs
from app.services.tools.core import Plan, managed

tool_name = "nginx"
phases = ("sync_files", "nginx_validate", "nginx_reload")

PREFIX = "cnc-"


async def nginx(plan: Plan) -> bool:
    prepared = await certs.prepare(plan.desired.nginx_files, plan.settings)
    attempted_reload = False

    try:
        with (
            _staging_errors(plan),
            managed(
                plan.settings.nginx_generated_dir,
                PREFIX,
                plan.desired.nginx_files,
                plan=plan,
                tool=tool_name,
                after_restore=lambda: _reload_sync(plan),
            ) as changed,
        ):
            if not changed and not prepared:
                plan.details["nginx_skipped"] = True
                plan.details["nginx_skip_reason"] = "generated config already matches"
                return False

            plan.touching(tool_name)
            plan.details["nginx_changed_files"] = changed
            plan.details["nginx_live_drift_reconciled"] = bool(changed)
            await plan.report("Apply host access", "Validating host proxy config")
            await _validate(plan)
            await plan.report("Apply host access", "Reloading host proxy")
            attempted_reload = True
            await _reload(plan)
    except BaseException:
        if attempted_reload:
            await _reload_quietly(plan)
        raise

    certs.activate(prepared, plan.settings)
    return True


@contextmanager
def _staging_errors(plan: Plan) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise ApplyFailed(
            "failed to stage managed nginx files",
            phase="sync_files",
            details={
                "error": str(exc),
                "target_dir": str(plan.settings.nginx_generated_dir),
            },
        ) from exc


async def _validate(plan: Plan) -> None:
    try:
        await plan.services.runtime_services.run_command_checked_async(
            ["nginx", "-t"], timeout_sec=plan.settings.command_timeout_apply_sec
        )
    except CommandError as exc:
        raise ApplyFailed(
            "nginx -t failed",
            phase="nginx_validate",
            error_code=ErrorCode.APPLY_NGINX_CONFIG_INVALID,
            details={
                "command": exc.result.command,
                "stdout": clip_output(exc.result.stdout),
                "stderr": clip_output(exc.result.stderr),
            },
        ) from exc


async def _reload(plan: Plan) -> None:
    await plan.services.runtime_services.run_command_checked_async(
        ["systemctl", "reload", "nginx"],
        timeout_sec=plan.settings.command_timeout_apply_sec,
    )


def _reload_sync(plan: Plan) -> None:
    runner = getattr(plan.services.runtime_services, "run_command", run_command)
    result = runner(
        ["systemctl", "reload", "nginx"],
        timeout_sec=plan.settings.command_timeout_apply_sec,
    )
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout or "nginx reload failed")


async def _reload_quietly(plan: Plan) -> None:
    try:
        await _reload(plan)
    except Exception:
        pass
