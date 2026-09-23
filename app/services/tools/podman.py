"""podman: the app containers, their Quadlet units and their guest filesystems.

The one tool that compares against a record rather than against the box, and
the one place that is the right call. Diffing a running container against a
desired spec through ``podman inspect`` is neither cheap nor faithful, so CNC
writes down the spec it asked for and compares against that.

The record is not trusted on its own. A backend is reconciled when its saved
spec differs, and also when the container it describes is not actually there,
so a record that has drifted from reality loses to reality. That is the
difference between this and the apply-slice hashes it replaces: those recorded
what the database looked like last time and inferred the host from it, which is
how a stub that reported success without doing anything could mark a surface
converged forever.

Skipping matters here in a way it does not for a config file. Reconciling a
backend can recreate its container, so touching twenty of them because one
route changed is a real outage, not a wasted write.
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.services.container_runtime import inspect_result_reports_missing
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
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("app_runtime",)


def backends_needing_work(
    desired: DesiredState,
    settings: Settings,
    *,
    inspect=None,
) -> set[str]:
    """Which app backends are not already in the state we asked for.

    Compares each backend's desired runtime spec against the spec CNC recorded
    when it last built that container, and treats a missing container as
    needing work regardless of what the record says.
    """
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
            # A disabled output converges to no runtime. It needs work only
            # while something of its runtime is still on the box.
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
            # Catches a change to the Quadlet renderer itself, which does not
            # move the runtime spec but does change the unit CNC writes.
            needs.add(backend.name)
            continue
        if backend.enabled and _container_definitely_absent(
            backend.name, settings, inspect=inspect
        ):
            # The record says this is built, the box says otherwise. Reality wins.
            needs.add(backend.name)
    return needs


def orphaned_runtimes(desired: DesiredState, settings: Settings) -> set[str]:
    """Backends CNC still has a runtime record for but desired state dropped.

    This is how a delete gets finished. Desired state simply stops mentioning
    the output, so nothing tells podman that a delete happened; it notices that
    it is still holding a record for something nobody wants and cleans up.
    """
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
    """Whether the generated Quadlet unit on disk is what we would write now."""
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
    """Whether Podman positively reports this container does not exist.

    An empty payload is not the same as absence. A timed-out inspect, a Podman
    database lock, or a permission error all produce no payload while the
    container keeps running, and treating that as "missing" would authorize
    rebuilding something that is perfectly healthy. Only Podman's definitive
    "no such container" counts, so an observation failure leaves the backend
    alone rather than selecting it for recreation.

    Uses the injected inspector so this asks the same Podman the rest of apply
    talks to, rather than shelling out behind the dependency boundary.
    """
    result, payload = (inspect or inspect_container)(
        container_name(name), timeout_sec=settings.command_timeout_status_sec
    )
    if payload is not None:
        return False
    return inspect_result_reports_missing(result)


async def podman(plan: Plan) -> bool:
    """Converge app containers, touching only the ones that are not already right."""
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
    # Only what this change is allowed to touch. Another output that has
    # drifted is a real problem, but it is not this save's problem: repairing
    # it here would turn one edit into fleet maintenance and put unrelated
    # containers at risk of a restart nobody asked for.
    selected = plan.scope.limit(drifted)
    out_of_scope = sorted(drifted - selected)
    if out_of_scope:
        plan.details["podman_drift_out_of_scope"] = out_of_scope

    # Containers built by the old direct-Podman path still need a one-time move
    # into Quadlet ownership. Their spec and unit can both look correct, so it
    # takes a live check, and it only asks about backends that otherwise look
    # converged rather than probing the whole fleet on every apply.
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

    # Recorded so the apply summary can say which outputs moved without
    # reconstructing it from hashes of the previous database state.
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
    # Both preflights have passed, so the next call is the first thing that
    # can leave the box changed.
    plan.touching(tool_name)

    kwargs = {
        "services": plan.services.runtime_services,
        "selected_backend_names": selected,
        "cleanup_deleted": True,
        # Cleanup is global by default; a scoped save narrows it so removing
        # one output cannot tear down another whose row is momentarily gone.
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
