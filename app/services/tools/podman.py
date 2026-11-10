
from __future__ import annotations

import asyncio

from app.config import Settings
from app.services.observation import Absent
from app.services.app_containers import (
    inspect_container,
    read_saved_spec,
    runtime_spec,
    spec_path,
)
from app.services.app_runtime import (
    legacy_direct_podman_migration_backend_reasons,
)
from app.services.apply_core import ApplyFailed, DesiredState
from app.services.app_quadlet import (
    AppQuadletWriteError,
    verify_app_quadlet_dir_writable,
)
from app.services.app_quadlet import (
    quadlet_container_path,
    render_quadlet_container,
)
from app.services.renderers import container_name
from app.services.tools.core import Plan

tool_name = "podman"
phases = ("app_runtime",)


def backends_needing_work(
    desired: DesiredState,
    settings: Settings,
    *,
    inspect=None,
) -> set[str]:
    needs: set[str] = set()
    for backend in desired.known_app_backends:
        wanted = runtime_spec(
            backend,
            settings,
            base_profile=desired.resource_profile,
            inter_app_interfaces=tuple(
                item
                for item in desired.runtime_graph.inter_app_interfaces
                if backend.name in (item.source_backend, item.target_backend)
            ),
        )
        saved = read_saved_spec(settings, backend.name)
        if not backend.enabled:
            if (
                saved is not None
                or quadlet_container_path(settings, backend.name).exists()
            ):
                needs.add(backend.name)
            continue
        if saved != wanted:
            needs.add(backend.name)
            continue
        if backend.enabled and not _quadlet_matches(backend, desired, settings):
            needs.add(backend.name)
            continue
        if backend.enabled and _container_definitely_absent(
            backend.name, settings, inspect=inspect
        ):
            needs.add(backend.name)
    return needs


def orphaned_runtimes(desired: DesiredState, settings: Settings) -> set[str]:
    wanted = {backend.name for backend in desired.known_app_backends}
    root = settings.app_control_dir
    if not root.is_dir():
        return set()
    return {
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and entry.name not in wanted
        and spec_path(settings, entry.name).exists()
    }


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
        inter_app_interfaces=tuple(
            item
            for item in desired.runtime_graph.inter_app_interfaces
            if backend.name in (item.source_backend, item.target_backend)
        ),
    )


def _container_definitely_absent(
    name: str, settings: Settings, *, inspect=None
) -> bool:
    observation = (inspect or inspect_container)(
        container_name(name), timeout_sec=settings.command_timeout_status_sec
    )
    return isinstance(observation, Absent)


async def podman(plan: Plan) -> bool:
    orphans = await asyncio.to_thread(orphaned_runtimes, plan.desired, plan.settings)
    if not plan.desired.known_app_backends and not orphans:
        return False

    await plan.report("Plan runtime", "Inspecting app container")
    drifted = await asyncio.to_thread(
        lambda: backends_needing_work(
            plan.desired,
            plan.settings,
            inspect=getattr(plan.services.runtime_services, "inspect_container", None),
        )
    )
    selected = plan.scope.limit(drifted)
    out_of_scope = sorted(drifted - selected)
    if out_of_scope:
        plan.details["podman_drift_out_of_scope"] = out_of_scope

    candidates = plan.scope.limit(
        plan.desired.runtime_graph.enabled_app_backend_names - selected
    )
    if candidates:
        legacy = await asyncio.to_thread(
            lambda: legacy_direct_podman_migration_backend_reasons(
                plan.desired,
                plan.settings,
                services=plan.services.runtime_services,
                candidate_backend_names=candidates,
            )
        )
        if legacy:
            plan.details["runtime_live_drift_backends"] = sorted(legacy)
            plan.details["runtime_live_drift_reasons"] = dict(sorted(legacy.items()))
            selected = selected | set(legacy)

    if not selected and not orphans:
        plan.details.update(_unchanged_details(plan))
        return False
    if orphans:
        plan.details["podman_orphans"] = sorted(orphans)

    plan.details["podman_selected"] = sorted(selected)
    plan.details["podman_created"] = sorted(
        name for name in selected if read_saved_spec(plan.settings, name) is None
    )
    _verify_quadlet_dir(plan)
    plan.details.update(
        await asyncio.to_thread(
            plan.services.preflight_app_storage, plan.desired, selected
        )
    )
    plan.touching(tool_name)

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
