"""nginx: the host proxy that serves public ingress.

Owns every ``cnc-`` prefixed file in the generated directory. Nothing else in
that directory is touched, so an operator's own config keeps working.

The order here is the whole reason certs is not a tool of its own. A
certificate has to exist before ``nginx -t`` will accept a config referencing
it, and it only counts as live once the reload succeeds. So the sequence is
prepare certs, write config, validate, reload, activate certs, and a failure
anywhere in the middle puts the config files back untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.services.apply_core import ApplyFailed, clip_output
from app.services.commands import CommandError, run_command
from app.services.error_reporting import ErrorCode
from app.services.tools import certs
from app.services.tools.core import Plan, managed

tool_name = "nginx"
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("sync_files", "nginx_validate", "nginx_reload")

PREFIX = "cnc-"


async def nginx(plan: Plan) -> bool:
    """Converge the generated config, then reload only if something changed."""
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
                # Restoring the bytes is only half a rollback. nginx is already
                # serving the new ones, so the undo has to reload for the
                # previous config to actually be live again.
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
    except Exception:
        # managed has put the previous config back by the time this runs. If
        # nginx had already been told to reload, tell it again so the live
        # process matches the files on disk rather than a half-applied
        # candidate. Best effort: the original failure is what gets reported.
        if attempted_reload:
            await _reload_quietly(plan)
        raise

    # Outside the managed block: the reload has succeeded, so the new files are
    # the live ones and a certificate may now be recorded as active.
    certs.activate(prepared, plan.settings)
    return True


@contextmanager
def _staging_errors(plan: Plan) -> Iterator[None]:
    """Report a failure to write the generated tree as a staging failure.

    Phase names here stay as they are because they are published to clients in
    apply details and matched by the progress card. Renaming them is a
    user-facing change, separate from moving the code.
    """
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
    """Reload nginx from the unwind path, which is not async.

    Uses the injected runner so the rollback talks to the same host as the
    apply it is undoing, rather than shelling out behind the boundary.
    """
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
