from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.services import nginx_runtime
from app.services.app_containers import (
    container_contract_hash,
    container_is_running,
    container_live_cpu_limits,
    inspect_container,
    runtime_contract_hash,
)
from app.services.app_quadlet import (
    AppQuadletWriteError,
    managed_quadlet_backend_names,
    quadlet_container_path,
    quadlet_container_service_name,
    render_quadlet_container,
    verify_app_quadlet_dir_writable,
)
from app.services.apply_core import ApplyFailed, DesiredState
from app.services.change_scope import Scope
from app.services.commands import run_command as default_run_command
from app.services.container_runtime import classify_listing
from app.services.guest_isolation import (
    guest_volume_arguments,
    validate_guest_hardening,
)
from app.services.hardening_policy import hardening_volumes, read_hardening_policy
from app.services.nginx_contract import nginx_gateway_enabled
from app.services.observation import Present, known
from app.services.renderers import container_name
from app.services.resource_profile import backend_resource_profile
from app.services.systemd_memory import memory_value_bytes
from app.services.tools.core import Plan
from app.services.validators import parse_volumes_json

tool_name = "podman"
phases = ("app_runtime",)


def _backend_interfaces(desired: DesiredState, backend_name: str):
    return tuple(
        item
        for item in desired.runtime_graph.inter_app_interfaces
        if backend_name in (item.source_backend, item.target_backend)
    )


def _expected_nano_cpus(raw_quota: str, host_cpu_count: int | None) -> int | None:
    normalized = str(raw_quota or "").strip()
    if not normalized:
        return None
    if normalized.endswith("%"):
        normalized = normalized[:-1]
    try:
        quota_percent = float(normalized)
    except ValueError:
        return None
    host_cpus = (
        host_cpu_count if isinstance(host_cpu_count, int) and host_cpu_count > 0 else 1
    )
    cpus = round((quota_percent / 100.0) * host_cpus, 2)
    return int(round(cpus * 1_000_000_000))


def _live_memory_limits_match(
    backend,
    desired: DesiredState,
    settings: Settings,
    *,
    run_command=None,
) -> bool:
    if run_command is None:
        return True
    result = run_command(
        [
            "systemctl",
            "show",
            quadlet_container_service_name(backend.name),
            "--property=MemoryHigh,MemoryMax",
        ],
        settings.command_timeout_status_sec,
    )
    if not result.ok:
        return False
    properties: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            properties[key.strip()] = value.strip()
    if "MemoryHigh" not in properties and "MemoryMax" not in properties:
        return True
    profile = backend_resource_profile(backend, desired.resource_profile)
    expected_high = memory_value_bytes(profile.memory_high)
    expected_max = memory_value_bytes(profile.memory_max)
    observed_high = memory_value_bytes(properties.get("MemoryHigh"))
    observed_max = memory_value_bytes(properties.get("MemoryMax"))
    return observed_high == expected_high and observed_max == expected_max


def _live_resource_limits_match(
    backend,
    desired: DesiredState,
    settings: Settings,
    payload: dict[str, Any],
    *,
    run_command=None,
) -> bool:
    profile = backend_resource_profile(backend, desired.resource_profile)
    live_nano, live_shares = container_live_cpu_limits(payload)
    expected_nano = _expected_nano_cpus(
        profile.cpu_quota, desired.resource_profile.host_cpu_count
    )
    if live_nano is not None and expected_nano is not None and live_nano != expected_nano:
        return False
    if live_shares is not None and profile.cpu_shares and live_shares != profile.cpu_shares:
        return False
    return _live_memory_limits_match(backend, desired, settings, run_command=run_command)


def _inspect_live_container(
    name: str, settings: Settings, *, inspect=None
) -> dict[str, Any] | None:
    observation = (inspect or inspect_container)(
        container_name(name), timeout_sec=settings.command_timeout_status_sec
    )
    return known(observation, f"container {name}")


def _container_definitely_absent(
    name: str, settings: Settings, *, inspect=None
) -> bool:
    return _inspect_live_container(name, settings, inspect=inspect) is None


def backends_needing_work(
    desired: DesiredState,
    settings: Settings,
    scope: Scope,
    *,
    inspect=None,
    run_command=None,
) -> set[str]:
    needs: set[str] = set()
    for backend in desired.known_app_backends:
        in_scope = scope.covers(backend.name)
        rebalance_peer = (
            not in_scope
            and scope.rebalance_resources
            and backend.enabled
            and desired.resource_profile.mode == "auto"
        )
        if not in_scope and not rebalance_peer:
            continue

        if not backend.enabled:
            if quadlet_container_path(settings, backend.name).exists():
                needs.add(backend.name)
                continue
            if _inspect_live_container(backend.name, settings, inspect=inspect) is not None:
                needs.add(backend.name)
            continue

        if not _quadlet_matches(backend, desired, settings):
            needs.add(backend.name)
            continue

        payload = _inspect_live_container(backend.name, settings, inspect=inspect)
        if payload is None:
            needs.add(backend.name)
            continue

        desired_hash = runtime_contract_hash(
            backend,
            settings,
            inter_app_interfaces=_backend_interfaces(desired, backend.name),
            nginx_container=desired.nginx_container,
        )
        if container_contract_hash(payload) != desired_hash:
            needs.add(backend.name)
            continue
        if not container_is_running(payload):
            needs.add(backend.name)
            continue
        if not _live_resource_limits_match(
            backend, desired, settings, payload, run_command=run_command
        ):
            needs.add(backend.name)
    return needs


def orphaned_runtimes(
    desired: DesiredState,
    settings: Settings,
    scope: Scope,
    *,
    run_command=None,
) -> set[str]:
    wanted = {backend.name for backend in desired.known_app_backends}
    found: set[str] = set(managed_quadlet_backend_names(settings))

    if settings.app_sandbox_dir.exists():
        for entry in settings.app_sandbox_dir.iterdir():
            if entry.is_dir() and (
                (entry / "rootfs").exists() or (entry / "profile.json").exists()
            ):
                found.add(entry.name)

    runner = run_command
    if runner is not None:
        result = runner(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                "label=io.cnc.managed=true",
                "--format",
                "json",
            ],
            settings.command_timeout_status_sec,
        )
        observation = classify_listing(result)
        if isinstance(observation, Present):
            for entry in observation.value:
                labels = entry.get("Labels") or entry.get("labels") or {}
                if isinstance(labels, dict):
                    name = str(labels.get("io.cnc.backend") or "").strip()
                    if name:
                        found.add(name)

    orphans = found - wanted
    return scope.limit(orphans)


def _quadlet_matches(backend, desired: DesiredState, settings: Settings) -> bool:
    try:
        live = quadlet_container_path(settings, backend.name).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return live == render_quadlet_container(
        backend,
        settings,
        base_profile=desired.resource_profile,
        nginx_container=desired.nginx_container,
        inter_app_interfaces=_backend_interfaces(desired, backend.name),
    )


async def check(plan: Plan) -> None:
    runtime_services = getattr(plan.services, "runtime_services", None)
    inspect = getattr(runtime_services, "inspect_container", None)
    runner = getattr(runtime_services, "run_command", default_run_command)

    orphans = await asyncio.to_thread(
        lambda: plan.scope.limit(
            orphaned_runtimes(
                plan.desired, plan.settings, plan.scope, run_command=runner
            )
        )
    )
    if not plan.desired.known_app_backends and not orphans:
        return

    await plan.report("Plan runtime", "Inspecting app container")
    selected = await asyncio.to_thread(
        lambda: backends_needing_work(
            plan.desired,
            plan.settings,
            plan.scope,
            inspect=inspect,
            run_command=runner,
        )
    )
    plan.details["_podman_preflight"] = {
        "orphans": sorted(orphans),
        "selected": sorted(selected),
    }
    if not selected:
        return

    seen_ports: dict[int, str] = {}
    for backend in plan.desired.enabled_app_backends:
        if backend.port is not None:
            previous = seen_ports.get(int(backend.port))
            if previous is not None and previous != backend.name:
                raise ApplyFailed(
                    f"duplicate host port {backend.port} between {previous} and {backend.name}",
                    phase="app_runtime",
                    details={"port": int(backend.port), "backends": [previous, backend.name]},
                )
            seen_ports[int(backend.port)] = backend.name
        if backend.name not in selected:
            continue
        policy = read_hardening_policy(backend)
        validate_guest_hardening(policy.model_dump())
        guest_volume_arguments(
            hardening_volumes(policy, parse_volumes_json(backend.volumes_json))
        )

    _verify_quadlet_dir(plan)
    storage_details = await asyncio.to_thread(
        plan.services.preflight_app_storage, plan.desired, selected
    )
    plan.details.update(storage_details)
    plan.details["_podman_preflight"]["storage_checked"] = True


async def podman(plan: Plan) -> bool:
    runtime_services = getattr(plan.services, "runtime_services", None)
    inspect = getattr(runtime_services, "inspect_container", None)
    runner = getattr(runtime_services, "run_command", default_run_command)

    preflight = plan.details.pop("_podman_preflight", None)
    if isinstance(preflight, dict):
        orphans = set(preflight.get("orphans") or [])
        selected = set(preflight.get("selected") or [])
        storage_checked = bool(preflight.get("storage_checked"))
    else:
        orphans = await asyncio.to_thread(
            lambda: plan.scope.limit(
                orphaned_runtimes(
                    plan.desired, plan.settings, plan.scope, run_command=runner
                )
            )
        )
        if not plan.desired.known_app_backends and not orphans:
            return False
        await plan.report("Plan runtime", "Inspecting app container")
        selected = await asyncio.to_thread(
            lambda: backends_needing_work(
                plan.desired,
                plan.settings,
                plan.scope,
                inspect=inspect,
                run_command=runner,
            )
        )
        storage_checked = False

    if not plan.desired.known_app_backends and not orphans:
        return False

    if not selected and not orphans:
        plan.details.update(_unchanged_details(plan))
        return False
    if orphans:
        plan.details["podman_orphans"] = sorted(orphans)

    plan.details["podman_selected"] = sorted(selected)
    plan.details["podman_created"] = sorted(
        name
        for name in selected
        if name in plan.desired.runtime_graph.enabled_app_backend_names
        and not quadlet_container_path(plan.settings, name).exists()
    )
    if not storage_checked:
        _verify_quadlet_dir(plan)
        plan.details.update(
            await asyncio.to_thread(
                plan.services.preflight_app_storage, plan.desired, selected
            )
        )
    plan.touching(tool_name)
    if nginx_gateway_enabled(
        plan.settings, container_mode=plan.desired.nginx_container
    ) and any(backend.name in selected for backend in plan.desired.enabled_app_backends):
        await nginx_runtime.ensure_nginx_network(plan)

    kwargs = {
        "services": plan.services.runtime_services,
        "selected_backend_names": selected,
        "cleanup_deleted": True,
        "removable_backend_names": (
            None if plan.scope.is_full else set(plan.scope.outputs or frozenset())
        ),
        "reconcile_network_isolation": True,
        "operation_id": plan.extra("operation_id"),
    }
    if not plan.services.cleanup_deleted_app_filesystem:
        kwargs["cleanup_deleted_filesystem"] = False
    plan.details.update(
        await plan.services.apply_app_backends(plan.desired, plan.settings, **kwargs)
    )
    return True


def _verify_quadlet_dir(plan: Plan) -> None:
    try:
        plan.details["app_quadlet_dir_preflight"] = verify_app_quadlet_dir_writable(
            plan.settings
        )
    except AppQuadletWriteError as exc:
        raise ApplyFailed(
            "app Quadlet directory is not writable",
            phase="app_runtime",
            details=exc.details,
        ) from exc


def _unchanged_details(plan: Plan) -> dict[str, object]:
    from app.services.apply_service import _skipped_app_runtime_details

    return _skipped_app_runtime_details(plan.desired, plan.settings)
