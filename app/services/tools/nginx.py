from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
import stat
import tempfile
from typing import Iterator

from app.services.apply_core import ApplyFailed, clip_output
from app.services.commands import CommandError, run_command
from app.services.error_reporting import ErrorCode
from app.services.guest_isolation import ensure_subid_pool
from app.services.nginx_contract import container_ingress_marker
from app.services.nginx_firewall import (
    read_nginx_firewall_policy,
    reconcile_nginx_firewall,
    restore_nginx_firewall,
)
from app.services import nginx_runtime as runtime
from app.services.tools import certs
from app.services.tools.core import Plan, managed, managed_file

tool_name = "nginx"
phases = ("sync_files", "nginx_validate", "nginx_reload")

PREFIX = "cnc-"


HOST_NGINX_CONF_PATH = Path("/etc/nginx/nginx.conf")


def _validate_managed_nginx_names(nginx_files: dict[str, str]) -> None:
    for filename in sorted(nginx_files):
        if not filename.startswith(PREFIX) or "/" in filename or "\\" in filename:
            raise ApplyFailed(
                f"invalid managed nginx filename: {filename}",
                phase="sync_files",
                details={"filename": filename},
            )


def _nginx_dir_differs(directory: Path, wanted: dict[str, str]) -> bool:
    if not directory.exists():
        return bool(wanted)
    existing: dict[str, str] = {}
    for entry in directory.iterdir():
        if not entry.name.startswith(PREFIX):
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise OSError(f"expected a regular managed file at {entry}")
        existing[entry.name] = entry.read_text(encoding="utf-8")
    return existing != dict(wanted)


async def check(plan: Plan) -> None:
    _validate_managed_nginx_names(plan.desired.nginx_files)
    if plan.desired.nginx_container:
        return
    target_dir = plan.settings.nginx_generated_dir
    with _staging_errors(plan, target_dir):
        differs = _nginx_dir_differs(target_dir, plan.desired.nginx_files)
    if not differs:
        return
    if not HOST_NGINX_CONF_PATH.is_file():
        return
    # Stage candidate generated files into a temp directory and validate with
    # `nginx -t -c <temp_conf>` without touching live `/etc/nginx/generated`.
    with tempfile.TemporaryDirectory(prefix="cnc-nginx-preflight-") as stage_name:
        stage_dir = Path(stage_name)
        generated_dir = stage_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in sorted(plan.desired.nginx_files.items()):
            (generated_dir / filename).write_text(content, encoding="utf-8")
        try:
            host_conf = HOST_NGINX_CONF_PATH.read_text(encoding="utf-8")
        except OSError:
            return
        generated_pattern = str(plan.settings.nginx_generated_dir)
        if generated_pattern not in host_conf:
            return
        staged_conf = stage_dir / "nginx.conf"
        staged_conf.write_text(
            host_conf.replace(generated_pattern, str(generated_dir)),
            encoding="utf-8",
        )
        try:
            await plan.services.runtime_services.run_command_checked_async(
                ["nginx", "-t", "-c", str(staged_conf)],
                timeout_sec=plan.settings.command_timeout_apply_sec,
            )
        except CommandError as exc:
            raise _invalid_config(exc) from exc


async def nginx(plan: Plan) -> bool:
    if plan.desired.nginx_container:
        return await _container_nginx(plan)
    if container_ingress_marker(plan.settings).is_file():
        return await _restore_host_nginx(plan)
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


async def _restore_host_nginx(plan: Plan) -> bool:
    settings = plan.settings
    services = plan.services.runtime_services
    timeout = settings.command_timeout_apply_sec
    marker = container_ingress_marker(settings)
    legacy_enabled = await _service_has_status(
        plan, "is-enabled", runtime.LEGACY_SERVICE
    )
    legacy_active = await _service_has_status(plan, "is-active", runtime.LEGACY_SERVICE)
    container_active = await _service_has_status(
        plan, "is-active", runtime.NGINX_SERVICE
    )
    previous_policy = read_nginx_firewall_policy(settings)
    prepared = await certs.prepare(plan.desired.nginx_files, settings)
    handoff_attempted = False
    policy_attempted = False

    async def recover() -> None:
        nonlocal handoff_attempted, policy_attempted
        if not handoff_attempted:
            return
        commands = []
        if not legacy_active:
            commands.append(["systemctl", "stop", runtime.LEGACY_SERVICE])
        if not legacy_enabled:
            commands.append(["systemctl", "disable", runtime.LEGACY_SERVICE])
        if container_active:
            commands.append(["systemctl", "start", runtime.NGINX_SERVICE])
        failures = []
        if policy_attempted:
            try:
                await asyncio.to_thread(
                    restore_nginx_firewall, settings, previous_policy, services=services
                )
            except Exception as exc:
                failures.append(exc)
            else:
                policy_attempted = False
        await _recover_services(plan, commands, failures)
        handoff_attempted = False

    plan.on_unwind(tool_name, recover)
    try:
        with _staging_errors(plan), ExitStack() as stack:
            files = stack.enter_context(
                managed(
                    settings.nginx_generated_dir,
                    PREFIX,
                    plan.desired.nginx_files,
                    plan=plan,
                    tool=tool_name,
                )
            )
            plan.touching(tool_name)
            plan.details["nginx_changed_files"] = files
            await plan.report("Apply host access", "Validating host proxy config")
            await _validate(plan)
            await plan.report("Apply host access", "Restoring host proxy")
            await _guard_host_boot(plan)
            handoff_attempted = True
            if not legacy_enabled:
                await services.run_command_checked_async(
                    ["systemctl", "enable", runtime.LEGACY_SERVICE], timeout
                )
            if container_active:
                await services.run_command_checked_async(
                    ["systemctl", "stop", runtime.NGINX_SERVICE], timeout
                )
            stack.enter_context(_retire_marker(plan, marker))
            await services.run_command_checked_async(
                [
                    "systemctl",
                    "reload" if legacy_active else "start",
                    runtime.LEGACY_SERVICE,
                ],
                timeout,
            )
            policy_attempted = True
            await _with_firewall_update(
                asyncio.to_thread(
                    restore_nginx_firewall, settings, None, services=services
                )
            )
    except BaseException as exc:
        try:
            await recover()
        except Exception as recovery_exc:
            exc.add_note(str(recovery_exc))
        raise

    certs.activate(prepared, settings)
    return True


async def _guard_host_boot(plan: Plan) -> bool:
    unit_dir = plan.settings.systemd_generated_dir
    drop_in = unit_dir / f"{runtime.LEGACY_SERVICE}.d" / "cnc-ingress-mode.conf"
    marker = container_ingress_marker(plan.settings)
    with managed_file(drop_in, f"[Unit]\nConditionPathExists=!{marker}\n") as changed:
        if changed:
            await plan.services.runtime_services.run_command_checked_async(
                ["systemctl", "daemon-reload"], plan.settings.command_timeout_apply_sec
            )
    return changed


@contextmanager
def _retire_marker(plan: Plan, marker: Path) -> Iterator[None]:
    inactive = marker.with_name("nginx-container-inactive")
    marker.replace(inactive)

    def restore() -> None:
        inactive.replace(marker)

    try:
        yield
    except BaseException:
        restore()
        raise
    plan.on_unwind(tool_name, restore)


async def _container_nginx(plan: Plan) -> bool:
    settings = plan.settings
    desired = plan.desired
    services = plan.services.runtime_services
    timeout = settings.command_timeout_apply_sec
    marker = container_ingress_marker(settings)
    migrated = marker.exists()
    was_running = await _service_has_status(plan, "is-active", runtime.NGINX_SERVICE)
    legacy_active = await _service_has_status(plan, "is-active", runtime.LEGACY_SERVICE)
    routes_directory = runtime.container_routes_directory(settings.app_control_dir)

    prepared = await certs.prepare(desired.nginx_files, settings)
    await asyncio.to_thread(ensure_subid_pool)
    await runtime.ensure_nginx_network(plan)
    previous_policy = read_nginx_firewall_policy(settings)
    service_changed = False
    policy_attempted = False
    restart_required = False

    async def recover() -> None:
        nonlocal service_changed, policy_attempted
        failures = []
        if policy_attempted:
            try:
                await asyncio.to_thread(
                    restore_nginx_firewall, settings, previous_policy, services=services
                )
            except Exception as exc:
                failures.append(exc)
            else:
                policy_attempted = False
        if not service_changed:
            if failures:
                raise failures[0]
            return
        if restart_required:
            recovery = [
                ["systemctl", "stop", runtime.NGINX_SERVICE],
                ["systemctl", "daemon-reload"],
            ]
            if was_running:
                recovery.append(["systemctl", "start", runtime.NGINX_SERVICE])
            if legacy_active and not migrated:
                recovery.append(["systemctl", "start", runtime.LEGACY_SERVICE])
        else:
            recovery = [runtime.reload_command(True)]
        await _recover_services(plan, recovery, failures)
        service_changed = False

    plan.on_unwind(tool_name, recover)
    try:
        with _staging_errors(plan, routes_directory), ExitStack() as stack:
            files = stack.enter_context(
                managed(
                    routes_directory,
                    PREFIX,
                    desired.nginx_files,
                    plan=plan,
                    tool=tool_name,
                )
            )
            config_changed = stack.enter_context(
                managed_file(
                    runtime.config_path(settings.app_control_dir),
                    runtime.render_nginx_config(),
                    plan=plan,
                    tool=tool_name,
                )
            )
            tls_dir = runtime.tls_directory(settings.app_control_dir)
            tls_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tls_files = stack.enter_context(
                managed(
                    tls_dir,
                    "",
                    runtime.origin_certificate_assets(
                        desired.nginx_files, settings.app_control_dir
                    ),
                    mode=0o600,
                    plan=plan,
                    tool=tool_name,
                )
            )
            volumes = runtime.runtime_volumes(settings, desired)
            unit_changed = stack.enter_context(
                managed_file(
                    settings.app_quadlet_dir / runtime.NGINX_UNIT,
                    runtime.render_nginx_quadlet(settings, desired, volumes),
                    plan=plan,
                    tool=tool_name,
                )
            )
            restart_required = (
                not was_running or unit_changed or not migrated or legacy_active
            )
            config_changed = bool(files or config_changed or tls_files or prepared)
            policy_attempted = True
            await _reconcile_firewall(plan)
            policy_changed = read_nginx_firewall_policy(settings) != previous_policy
            boot_guard_changed = await _guard_host_boot(plan)
            if not restart_required and not config_changed:
                plan.details["nginx_skipped"] = True
                plan.details["nginx_skip_reason"] = "container config already matches"
                if policy_changed or boot_guard_changed:
                    plan.touching(tool_name)
                return policy_changed or boot_guard_changed

            plan.touching(tool_name)
            plan.details["nginx_changed_files"] = files
            plan.details["nginx_live_drift_reconciled"] = bool(files)
            await plan.report("Apply host access", "Validating NGINX container config")
            try:
                await services.run_command_checked_async(
                    runtime.candidate_command(volumes), timeout
                )
            except CommandError as exc:
                raise _invalid_config(exc) from exc

            await plan.report("Apply host access", "Activating NGINX container")
            service_changed = True
            if restart_required:
                stack.enter_context(
                    managed_file(
                        marker, "active\n", mode=0o600, plan=plan, tool=tool_name
                    )
                )
                if legacy_active:
                    await services.run_command_checked_async(
                        ["systemctl", "stop", runtime.LEGACY_SERVICE], timeout
                    )
                await services.run_command_checked_async(
                    ["systemctl", "daemon-reload"], timeout
                )
                action = "restart" if was_running else "start"
                await services.run_command_checked_async(
                    ["systemctl", action, runtime.NGINX_SERVICE], timeout
                )
                await _wait_for_container(plan)
            else:
                await services.run_command_checked_async(
                    runtime.reload_command(True), timeout
                )
    except BaseException as exc:
        try:
            await recover()
        except Exception as recovery_exc:
            exc.add_note(str(recovery_exc))
        raise

    certs.activate(prepared, settings)
    return True


async def _reconcile_firewall(plan: Plan) -> None:
    await _with_firewall_update(
        asyncio.to_thread(
            reconcile_nginx_firewall,
            plan.settings,
            plan.desired.nginx_backend_ports,
            services=plan.services.runtime_services,
        )
    )


async def _with_firewall_update(update) -> None:
    task = asyncio.create_task(update)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _recover_services(
    plan: Plan, commands: list[list[str]], failures: list[Exception]
) -> None:
    for command in commands:
        try:
            await plan.services.runtime_services.run_command_checked_async(
                command, plan.settings.command_timeout_apply_sec
            )
        except CommandError as exc:
            if command[:2] != ["systemctl", "stop"] or await _service_has_status(
                plan, "is-active", command[2]
            ):
                failures.append(exc)
    if failures:
        raise ExceptionGroup("NGINX recovery failed", failures)


async def _service_has_status(plan: Plan, verb: str, service: str) -> bool:
    result = await asyncio.to_thread(
        plan.services.runtime_services.run_command,
        ["systemctl", verb, "--quiet", service],
        plan.settings.command_timeout_status_sec,
    )
    return result.ok


async def _wait_for_container(plan: Plan) -> None:
    for attempt in range(10):
        result = await asyncio.to_thread(
            plan.services.runtime_services.run_command,
            runtime.health_command(),
            plan.settings.command_timeout_status_sec,
        )
        if result.ok:
            return
        if attempt != 9:
            await asyncio.sleep(0.3)
    raise ApplyFailed("NGINX container did not become ready", phase="nginx_reload")


@contextmanager
def _staging_errors(plan: Plan, directory: Path | None = None) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise ApplyFailed(
            "failed to stage managed nginx files",
            phase="sync_files",
            details={
                "error": str(exc),
                "target_dir": str(directory or plan.settings.nginx_generated_dir),
            },
        ) from exc


async def _validate(plan: Plan) -> None:
    try:
        await plan.services.runtime_services.run_command_checked_async(
            ["nginx", "-t"], timeout_sec=plan.settings.command_timeout_apply_sec
        )
    except CommandError as exc:
        raise _invalid_config(exc) from exc


def _invalid_config(exc: CommandError) -> ApplyFailed:
    return ApplyFailed(
        "nginx -t failed",
        phase="nginx_validate",
        error_code=ErrorCode.APPLY_NGINX_CONFIG_INVALID,
        details={
            "command": exc.result.command,
            "stdout": clip_output(exc.result.stdout),
            "stderr": clip_output(exc.result.stderr),
        },
    )


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
