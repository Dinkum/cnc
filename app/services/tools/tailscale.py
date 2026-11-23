
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.apply_core import ApplyFailed, DesiredState, clip_output
from app.services.commands import (
    CommandError,
    command_result_is_retryable,
    run_command,
)
from app.services.host_state import write_json_atomic
from app.services.retry import retry_async_call
from app.services.tools.core import Plan

logger = get_logger("tools.tailscale")

TAILSCALE_SERVICE_TAG_REQUIRED_MESSAGE = (
    "Tailscale Services require this CNC host to use a tag-based Tailscale identity "
    "before publishing tailscale subdomain inputs."
)

tool_name = "tailscale"
phases = ("tailscale_paths", "tailscale_services")


def _default_services() -> Any:
    from app.services.apply_service import default_apply_services

    return default_apply_services()


async def check(plan: Plan) -> None:
    if converged(plan.desired, plan.settings, services=plan.services):
        return
    state_path = plan.settings.tailscale_serve_state_path
    owned = read_tailscale_state(state_path) or read_tailscale_service_state(state_path)
    if not plan.desired.tailscale_paths and not plan.desired.tailscale_services and not owned:
        return
    await _run_retryable_checked_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=plan.settings.command_timeout_status_sec,
        settings=plan.settings,
        services=plan.services,
    )
    if plan.desired.tailscale_services:
        await verify_tailscale_service_host_precheck(
            plan.desired.tailscale_services,
            plan.settings,
            services=plan.services,
        )


async def tailscale(plan: Plan) -> bool:
    if converged(plan.desired, plan.settings, services=plan.services):
        plan.details["tailscale_paths"] = [
            {"path": path, "target": target}
            for path, target in sorted(plan.desired.tailscale_paths.items())
        ]
        plan.details["tailscale_services"] = [
            {"service": name, "target": target}
            for name, target in sorted(plan.desired.tailscale_services.items())
        ]
        plan.details["tailscale_skipped"] = True
        plan.details["tailscale_skip_reason"] = "local serve state already matches"
        return False

    plan.touching(tool_name)
    plan.details.update(
        await _publish_paths(
            plan.desired.tailscale_paths, plan.settings, services=plan.services
        )
    )
    plan.details.update(
        await _publish_services(
            plan.desired.tailscale_services, plan.settings, services=plan.services
        )
    )
    return True


def converged(
    desired: DesiredState, settings: Settings, *, services: Any = None
) -> bool:
    state_path = settings.tailscale_serve_state_path
    owned = read_tailscale_state(state_path) or read_tailscale_service_state(state_path)
    if not desired.tailscale_paths and not desired.tailscale_services and not owned:
        return True
    record_matches = (
        read_tailscale_state(state_path) == desired.tailscale_paths
        and read_tailscale_service_state(state_path) == desired.tailscale_services
    )
    if not record_matches:
        return False
    live = observe_serve(settings, services=services)
    if live is None:
        return False
    live_paths, live_services = live
    return live_paths == dict(desired.tailscale_paths) and live_services == dict(
        desired.tailscale_services
    )


def observe_serve_paths(
    settings: Settings, *, services: Any = None
) -> dict[str, str] | None:
    observed = observe_serve(settings, services=services)
    return None if observed is None else observed[0]


def observe_serve(
    settings: Settings, *, services: Any = None
) -> tuple[dict[str, str], dict[str, str]] | None:
    runner = getattr(
        getattr(services, "runtime_services", None), "run_command", run_command
    )
    result = runner(
        ["tailscale", "serve", "status", "--json"],
        timeout_sec=settings.command_timeout_status_sec,
    )
    if not result.ok:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return _serve_paths(payload), _serve_services(payload)


def _serve_paths(payload: dict[str, Any]) -> dict[str, str]:
    handlers = payload.get("Web") or {}
    found: dict[str, str] = {}
    if isinstance(handlers, dict):
        for entry in handlers.values():
            paths = (entry or {}).get("Handlers") or {}
            if not isinstance(paths, dict):
                continue
            for path, handler in paths.items():
                target = (handler or {}).get("Proxy") or ""
                if target:
                    found[str(path)] = str(target)
    return found


def _serve_services(payload: dict[str, Any]) -> dict[str, str]:
    services_payload = payload.get("Services")
    if not isinstance(services_payload, dict):
        return {}
    found: dict[str, str] = {}
    for raw_name, entry in services_payload.items():
        name = str(raw_name)
        if name.startswith("svc:"):
            name = name[len("svc:") :]
        target = _first_web_proxy(entry)
        if target:
            found[name] = target
    return found


def _first_web_proxy(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    web = entry.get("Web")
    if not isinstance(web, dict):
        return ""
    for _host_port in sorted(web, key=str):
        server = web[_host_port]
        if not isinstance(server, dict):
            continue
        handlers = server.get("Handlers")
        if not isinstance(handlers, dict):
            continue
        for path in sorted(handlers, key=str):
            handler = handlers[path]
            if isinstance(handler, dict) and handler.get("Proxy"):
                return str(handler["Proxy"])
    return ""


def read_tailscale_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_path, raw_target in paths.items():
        if not isinstance(raw_path, str) or not isinstance(raw_target, str):
            continue
        normalized[raw_path] = raw_target
    return normalized


def read_tailscale_service_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    services = payload.get("services")
    if not isinstance(services, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_service, raw_target in services.items():
        if not isinstance(raw_service, str) or not isinstance(raw_target, str):
            continue
        normalized[raw_service] = raw_target
    return normalized


def _write_tailscale_state(
    path: Path, *, paths: dict[str, str], services: dict[str, str]
) -> None:
    write_json_atomic(path, {"paths": paths, "services": services})


def tailscale_service_host_precheck_required(
    desired: DesiredState,
    settings: Settings,
) -> bool:
    if not desired.tailscale_services:
        return False
    return True
    return not converged(desired, settings)


def tailscale_service_host_precheck_skip_details(
    desired: DesiredState,
    settings: Settings,
) -> dict[str, object]:
    services = sorted(desired.tailscale_services)
    if not services:
        return {"skipped": True, "reason": "no tailscale service inputs"}
    if not tailscale_service_host_precheck_required(desired, settings):
        return {
            "skipped": True,
            "reason": "tailscale service slice unchanged and local state matches",
            "services": services,
        }
    return {"skipped": False, "services": services}


async def verify_tailscale_service_host_precheck(
    services_map: dict[str, str],
    settings: Settings,
    *,
    services: Any,
) -> dict[str, object]:
    service_names = sorted(services_map)
    command = ["tailscale", "status", "--self", "--json"]
    try:
        result = await _run_retryable_checked_command(
            command,
            timeout_sec=settings.command_timeout_status_sec,
            settings=settings,
            services=services,
        )
    except CommandError as exc:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": exc.result.stderr
                or exc.result.stdout
                or "tailscale status failed",
                "command": exc.result.command,
                "stdout": clip_output(exc.result.stdout),
                "stderr": clip_output(exc.result.stderr),
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        ) from exc

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": "tailscale status returned invalid JSON",
                "command": command,
                "stdout": clip_output(result.stdout),
                "stderr": clip_output(result.stderr),
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        ) from exc

    self_payload = payload.get("Self") if isinstance(payload, dict) else None
    if not isinstance(self_payload, dict):
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": "tailscale status did not include the local node identity",
                "command": command,
                "tailscale_service_host_precheck": {
                    "ok": False,
                    "services": service_names,
                    "tagged_host": False,
                },
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        )

    raw_tags = self_payload.get("Tags")
    tags = [str(tag).strip() for tag in raw_tags] if isinstance(raw_tags, list) else []
    tags = [tag for tag in tags if tag]
    hostname = str(
        self_payload.get("HostName") or self_payload.get("DNSName") or ""
    ).strip()
    precheck = {
        "ok": bool(tags),
        "services": service_names,
        "tagged_host": bool(tags),
        "tags": tags,
        "hostname": hostname,
    }
    if not tags:
        raise ApplyFailed(
            "tailscale service host precheck failed",
            phase="tailscale_services",
            details={
                "error": TAILSCALE_SERVICE_TAG_REQUIRED_MESSAGE,
                "command": command,
                "tailscale_service_host_precheck": precheck,
                "failure_mode": "clean",
                "manual_review_required": False,
            },
        )
    return precheck


async def _publish_paths(
    paths: dict[str, str],
    settings: Settings,
    *,
    services: Any = None,
) -> dict[str, Any]:
    services = services or _default_services()
    state_path = settings.tailscale_serve_state_path
    previous_paths = read_tailscale_state(state_path)
    previous_services = read_tailscale_service_state(state_path)
    if not paths and not previous_paths:
        return {"tailscale_paths": []}

    state_path.parent.mkdir(parents=True, exist_ok=True)
    await _run_retryable_checked_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=settings.command_timeout_status_sec,
        settings=settings,
        services=services,
    )

    for stale_path in sorted(set(previous_paths) - set(paths)):
        for stale_path_variant in _tailscale_path_off_variants(stale_path):
            await _run_tailscale_serve_off_idempotent(
                [
                    "tailscale",
                    "serve",
                    "--bg",
                    "--yes",
                    "--https=443",
                    f"--set-path={stale_path_variant}",
                    "off",
                ],
                timeout_sec=settings.command_timeout_apply_sec,
                settings=settings,
                services=services,
            )

    for path, target in sorted(paths.items()):
        await _run_retryable_checked_command(
            [
                "tailscale",
                "serve",
                "--bg",
                "--yes",
                "--https=443",
                f"--set-path={path}",
                target,
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    _write_tailscale_state(state_path, paths=paths, services=previous_services)
    return {
        "tailscale_paths": [
            {"path": path, "target": target} for path, target in sorted(paths.items())
        ]
    }


async def _publish_services(
    services_map: dict[str, str],
    settings: Settings,
    *,
    services: Any = None,
) -> dict[str, Any]:
    services = services or _default_services()
    state_path = settings.tailscale_serve_state_path
    previous_paths = read_tailscale_state(state_path)
    previous_services = read_tailscale_service_state(state_path)
    if not services_map and not previous_services:
        return {"tailscale_services": []}

    state_path.parent.mkdir(parents=True, exist_ok=True)
    await _run_retryable_checked_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=settings.command_timeout_status_sec,
        settings=settings,
        services=services,
    )

    for stale_service in sorted(set(previous_services) - set(services_map)):
        await _run_tailscale_serve_off_idempotent(
            [
                "tailscale",
                "serve",
                "--yes",
                f"--service=svc:{stale_service}",
                "--https=443",
                "off",
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    for service_name, target in sorted(services_map.items()):
        await _run_retryable_checked_command(
            [
                "tailscale",
                "serve",
                "--yes",
                f"--service=svc:{service_name}",
                "--https=443",
                target,
            ],
            timeout_sec=settings.command_timeout_apply_sec,
            settings=settings,
            services=services,
        )

    _write_tailscale_state(state_path, paths=previous_paths, services=services_map)
    return {
        "tailscale_services": [
            {"service": service_name, "target": target}
            for service_name, target in sorted(services_map.items())
        ]
    }


async def _run_retryable_checked_command(
    command: list[str],
    *,
    timeout_sec: int,
    settings: Settings,
    services: Any,
) -> Any:
    attempts = settings.transient_command_retry_attempts + 1

    def should_retry(exc: Exception) -> bool:
        return isinstance(exc, CommandError) and command_result_is_retryable(exc.result)

    def log_retry(
        exc: Exception, attempt: int, max_attempts: int, delay_sec: float
    ) -> None:
        logger.warning(
            "apply.command.retrying",
            command=" ".join(command),
            attempt=attempt,
            max_attempts=max_attempts,
            backoff_sec=delay_sec,
            error=str(exc),
        )

    return await retry_async_call(
        lambda: services.runtime_services.run_command_checked_async(
            command, timeout_sec=timeout_sec
        ),
        attempts=attempts,
        backoff_sec=settings.transient_command_retry_backoff_sec,
        should_retry=should_retry,
        on_retry=log_retry,
    )


async def _run_tailscale_serve_off_idempotent(
    command: list[str],
    *,
    timeout_sec: int,
    settings: Settings,
    services: Any,
) -> Any:
    try:
        return await _run_retryable_checked_command(
            command,
            timeout_sec=timeout_sec,
            settings=settings,
            services=services,
        )
    except CommandError as exc:
        if _tailscale_serve_off_missing_handler(exc):
            logger.info("tailscale.serve_off.already_absent", command=" ".join(command))
            return exc.result
        raise


def _tailscale_serve_off_missing_handler(exc: CommandError) -> bool:
    result = exc.result
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "handler does not exist" in output or "failed to remove web serve" in output


def _tailscale_path_off_variants(path: str) -> list[str]:
    variants = [path]
    if path != "/" and path.endswith("/"):
        variants.append(path.rstrip("/"))
    elif path != "/":
        variants.append(f"{path}/")
    return list(dict.fromkeys(variants))
