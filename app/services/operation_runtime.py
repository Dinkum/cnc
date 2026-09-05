from collections.abc import Iterable
from typing import Any

from app.config import Settings
from app.services.bootstrap_state import read_bootstrap_state


RUNTIME_EVENT_SUMMARY_LIMIT = 32
CREATE_BACKEND_RUNNING_PROGRESS_MAX = 96
RUNNING_PROGRESS_MAX = 96
ProgressSubstep = tuple[str, int]
ProgressState = tuple[str, int, tuple[ProgressSubstep, ...]]
ProgressPair = tuple[str, str]

RUNTIME_PHASE_LABELS = {
    "prepare": "Plan runtime",
    "create": "Prepare guest filesystem",
    "bootstrap": "Bootstrap guest",
    "publish": "Verify output",
    "verify": "Verify output",
    "steady_state": "Finalize",
}

RUNTIME_SUBSTEP_LABELS = {
    "rootfs_reuse": ("Prepare guest filesystem", "Reusing existing rootfs"),
    "seed_container_create": ("Prepare guest filesystem", "Creating seed container"),
    "seed_image_export": ("Prepare guest filesystem", "Exporting seed filesystem"),
    "seed_container_remove": ("Prepare guest filesystem", "Removing seed container"),
    "seed_archive_extract": ("Prepare guest filesystem", "Extracting guest filesystem"),
    "guest_identity_prepare": ("Prepare guest filesystem", "Preparing guest identity"),
    "guest_services_preserve": (
        "Prepare guest filesystem",
        "Preserving guest services",
    ),
    "guest_services_restore": ("Prepare guest filesystem", "Restoring guest services"),
    "profile_provision": ("Provision guest", "Preparing guest profile"),
    "provision_package_catalog": ("Provision guest", "Preparing package catalog"),
    "provision_guest_runtime": ("Provision guest", "Installing guest runtime"),
    "provision_guest_tools": ("Provision guest", "Installing guest tools"),
    "provision_package_cleanup": ("Provision guest", "Cleaning package cache"),
    "profile_self_test": ("Provision guest", "Running profile self test"),
    "rootfs_switch_prepare": ("Activate rootfs", "Preparing rootfs switch"),
    "rootfs_activate": ("Activate rootfs", "Activating guest filesystem"),
    "app_control_assets_write": ("Install service", "Writing app control assets"),
    "service_files_write": ("Install service", "Writing service files"),
    "service_manager_reload": ("Install service", "Reloading service manager"),
    "app_service_start": ("Install service", "Starting app service"),
    "resource_limits_apply": ("Install service", "Applying resource limits"),
    "guest_systemd_check": ("Bootstrap guest", "Checking guest systemd"),
    "guest_readiness_wait": ("Bootstrap guest", "Waiting for guest readiness"),
}

RUNTIME_PHASE_SUBSTEPS = {
    "prepare": "Selecting runtime action",
    "create": "Reconciling guest filesystem",
    "bootstrap": "Waiting for guest readiness",
    "publish": "Selecting private target",
    "verify": "Checking app health",
}

CREATE_BACKEND_PROGRESS_TREE = (
    (
        "Validate output",
        1,
        (("Checking ports and health settings", 1),),
    ),
    (
        "Save desired state",
        1,
        (
            ("Opening save transaction", 1),
            ("Staging output config", 1),
            ("Staging attached input routes", 1),
            ("Saving route graph", 1),
            ("Waiting for host mutation lock", 1),
            ("Building host plan", 1),
        ),
    ),
    (
        "Prepare host",
        1,
        (
            ("Checking tailnet service host", 1),
            ("Reconciling CNC runtime assets", 1),
            ("Checking admin exposure", 2),
        ),
    ),
    (
        "Plan runtime",
        2,
        (
            ("Inspecting app container", 1),
            ("Selecting runtime action", 1),
        ),
    ),
    (
        "Prepare guest filesystem",
        2,
        (
            ("Reconciling guest filesystem", 1),
            ("Creating seed container", 1),
            ("Exporting seed filesystem", 2),
            ("Removing seed container", 1),
            ("Extracting guest filesystem", 2),
        ),
    ),
    (
        "Provision guest",
        3,
        (
            ("Preparing package catalog", 1),
            ("Installing guest runtime", 2),
            ("Installing guest tools", 2),
            ("Cleaning package cache", 1),
            ("Running profile self test", 1),
        ),
    ),
    (
        "Activate rootfs",
        1,
        (
            ("Preparing rootfs switch", 1),
            ("Activating guest filesystem", 1),
        ),
    ),
    (
        "Install service",
        1,
        (
            ("Writing app control assets", 1),
            ("Writing service files", 1),
            ("Starting app service", 1),
            ("Applying resource limits", 1),
        ),
    ),
    (
        "Bootstrap guest",
        1,
        (
            ("Checking guest systemd", 1),
            ("Waiting for guest readiness", 1),
        ),
    ),
    (
        "Prepare Shield",
        1,
        (("Preparing Shield gate", 1),),
    ),
    (
        "Apply host access",
        2,
        (
            ("Reconciling backend SSH", 1),
            ("Staging host proxy config", 1),
            ("Validating host proxy config", 2),
            ("Reloading host proxy", 2),
            ("Updating tailnet paths", 1),
            ("Updating tailnet services", 1),
        ),
    ),
    (
        "Apply cluster",
        1,
        (("Syncing follower ingress", 1),),
    ),
    (
        "Verify output",
        2,
        (
            ("Verifying admin exposure", 1),
            ("Auditing control plane", 2),
            ("Selecting private target", 1),
            ("Checking app health", 1),
        ),
    ),
)

CREATE_BACKEND_RUNTIME_PROGRESS_SUBSTEPS = {
    "seed_container_create",
    "seed_image_export",
    "seed_container_remove",
    "seed_archive_extract",
    "provision_package_catalog",
    "provision_guest_runtime",
    "provision_guest_tools",
    "provision_package_cleanup",
    "profile_self_test",
    "rootfs_switch_prepare",
    "rootfs_activate",
    "app_control_assets_write",
    "service_files_write",
    "app_service_start",
    "resource_limits_apply",
    "guest_systemd_check",
    "guest_readiness_wait",
}


def _build_create_backend_progress_catalog() -> tuple[
    list[dict[str, object]],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]:
    steps: list[dict[str, object]] = []
    phase_points: dict[str, int] = {}
    operation_substate_points: dict[str, int] = {}
    runtime_substep_points: dict[str, int] = {}
    runtime_raw_by_display = {
        display: raw
        for raw, display in RUNTIME_SUBSTEP_LABELS.items()
        if raw in CREATE_BACKEND_RUNTIME_PROGRESS_SUBSTEPS
    }
    phase_raw_by_display = {
        (RUNTIME_PHASE_LABELS[raw_phase], substep): raw_phase
        for raw_phase, substep in RUNTIME_PHASE_SUBSTEPS.items()
    }
    weighted_steps, phase_points, operation_substate_points = (
        _build_weighted_progress_catalog(
            CREATE_BACKEND_PROGRESS_TREE,
            max_progress=CREATE_BACKEND_RUNNING_PROGRESS_MAX,
        )
    )
    for step in weighted_steps:
        phase = str(step["headline"])
        substep = str(step["substep"])
        progress = int(step["progress"])
        steps.append(step)
        raw_substep = runtime_raw_by_display.get((phase, substep))
        if raw_substep is not None:
            runtime_substep_points[raw_substep] = progress
        raw_phase = phase_raw_by_display.get((phase, substep))
        if raw_phase is not None:
            runtime_substep_points[raw_phase] = progress
    return steps, phase_points, operation_substate_points, runtime_substep_points


def _build_weighted_progress_catalog(
    tree: tuple[ProgressState, ...],
    *,
    max_progress: int = RUNNING_PROGRESS_MAX,
) -> tuple[list[dict[str, object]], dict[str, int], dict[str, int]]:
    steps: list[dict[str, object]] = []
    phase_points: dict[str, int] = {}
    substate_points: dict[str, int] = {}
    total_state_weight = sum(
        max(1, state_weight) for _phase, state_weight, _substeps in tree
    )
    state_progress_start = 0.0
    last_progress = 0

    for phase, raw_state_weight, substeps in tree:
        state_weight = max(1, int(raw_state_weight))
        state_start = state_progress_start / total_state_weight * max_progress
        state_span = state_weight / total_state_weight * max_progress
        substep_total = sum(
            max(1, int(substep_weight)) for _substep, substep_weight in substeps
        )
        substep_progress_start = 0.0
        first_state_progress = 0
        for substep, raw_substep_weight in substeps:
            substep_weight = max(1, int(raw_substep_weight))
            checkpoint = state_start + (
                (substep_progress_start + substep_weight) / substep_total * state_span
            )
            progress = max(1, min(max_progress, int(round(checkpoint))))
            if progress <= last_progress:
                progress = min(max_progress, last_progress + 1)
            last_progress = progress
            if first_state_progress == 0:
                first_state_progress = progress
            steps.append(
                {
                    "progress": progress,
                    "headline": phase,
                    "substep": substep,
                    "note": f"{substep}.",
                    "state_weight": state_weight,
                    "substep_weight": substep_weight,
                }
            )
            substate_points[substep] = progress
            substep_progress_start += substep_weight
        phase_points[phase] = first_state_progress
        state_progress_start += state_weight
    return steps, phase_points, substate_points


(
    _CREATE_BACKEND_PROGRESS_STEPS,
    CREATE_BACKEND_PROGRESS_POINTS,
    CREATE_BACKEND_OPERATION_SUBSTATE_PROGRESS_POINTS,
    CREATE_BACKEND_SUBSTEP_PROGRESS_POINTS,
) = _build_create_backend_progress_catalog()

RUNTIME_PHASE_PROGRESS_POINTS = {
    raw_phase: CREATE_BACKEND_SUBSTEP_PROGRESS_POINTS[raw_phase]
    for raw_phase in RUNTIME_PHASE_SUBSTEPS
    if raw_phase in CREATE_BACKEND_SUBSTEP_PROGRESS_POINTS
}

CREATE_BACKEND_OPERATION_STEPS = tuple(
    (str(step["headline"]), str(step["substep"]), str(step["note"]))
    for step in _CREATE_BACKEND_PROGRESS_STEPS
)

INPUT_PROGRESS_TREES: dict[str, tuple[ProgressState, ...]] = {
    "createInput": (
        (
            "Validate input",
            1,
            (("Reading submitted config", 1), ("Checking route shape", 1)),
        ),
        ("Map route graph", 1, (("Resolving attached outputs", 1),)),
        (
            "Save desired state",
            1,
            (
                ("Checking uniqueness", 1),
                ("Staging input config", 1),
                ("Waiting for host mutation lock", 1),
                ("Building host plan", 1),
            ),
        ),
        (
            "Apply host access",
            2,
            (
                ("Loading runtime state", 1),
                ("Reconciling host routes", 1),
                ("Updating tailnet paths", 1),
                ("Updating tailnet services", 1),
                ("Validating host proxy config", 2),
            ),
        ),
        ("Finalize", 1, (("Refreshing dashboard", 1),)),
    ),
    "saveInput": (
        (
            "Validate input",
            1,
            (("Reading submitted config", 1), ("Checking route shape", 1)),
        ),
        ("Map route graph", 1, (("Resolving attached outputs", 1),)),
        (
            "Save desired state",
            1,
            (
                ("Staging input config", 1),
                ("Checking uniqueness", 1),
                ("Waiting for host mutation lock", 1),
                ("Building host plan", 1),
            ),
        ),
        (
            "Apply host access",
            2,
            (
                ("Loading runtime state", 1),
                ("Reconciling host routes", 1),
                ("Updating tailnet paths", 1),
                ("Updating tailnet services", 1),
                ("Validating host proxy config", 2),
            ),
        ),
        ("Finalize", 1, (("Refreshing dashboard", 1),)),
    ),
    "deleteInput": (
        ("Validate input", 1, (("Loading current route graph", 1),)),
        ("Map route graph", 1, (("Checking affected outputs", 1),)),
        (
            "Save desired state",
            1,
            (
                ("Removing input config", 1),
                ("Waiting for host mutation lock", 1),
                ("Building host plan", 1),
            ),
        ),
        (
            "Apply host access",
            2,
            (
                ("Reconciling host routes", 1),
                ("Updating tailnet paths", 1),
                ("Updating tailnet services", 1),
                ("Validating host proxy config", 2),
            ),
        ),
        ("Finalize", 1, (("Refreshing dashboard", 1),)),
    ),
}

OUTPUT_SAVE_PROGRESS_TREE: tuple[ProgressState, ...] = (
    (
        "Validate output",
        1,
        (
            ("Reading output form", 1),
            ("Validating output", 1),
            ("Preparing save request", 1),
        ),
    ),
    (
        "Save desired state",
        1,
        (
            ("Saving desired state", 1),
            ("Building host plan", 1),
            ("Checking changed slices", 1),
        ),
    ),
    (
        "Prepare host",
        1,
        (
            ("Checking tailnet service host", 1),
            ("Reconciling CNC runtime assets", 1),
            ("Checking admin exposure", 1),
        ),
    ),
    (
        "Plan runtime",
        1,
        (("Inspecting app container", 1),),
    ),
    (
        "Prepare Shield",
        1,
        (("Preparing Shield gate", 1),),
    ),
    (
        "Apply host access",
        1,
        (
            ("Reconciling backend SSH", 1),
            ("Staging host proxy config", 1),
            ("Validating host proxy config", 1),
            ("Reloading host proxy", 1),
            ("Updating tailnet paths", 1),
            ("Updating tailnet services", 1),
        ),
    ),
    (
        "Apply cluster",
        1,
        (("Syncing follower ingress", 1),),
    ),
    (
        "Verify output",
        1,
        (
            ("Verifying admin exposure", 1),
            ("Auditing control plane", 1),
        ),
    ),
    ("Finalize", 1, (("Refreshing output", 1),)),
)

DELETE_OUTPUT_PROGRESS_TREE: tuple[ProgressState, ...] = (
    (
        "Delete output",
        1,
        (
            ("Opening delete operation", 1),
            ("Removing desired state", 1),
            ("Waiting for host mutation lock", 1),
            ("Building host plan", 1),
        ),
    ),
    (
        "Plan runtime",
        2,
        (("Removing app runtime", 1),),
    ),
    (
        "Apply host access",
        2,
        (
            ("Reconciling backend SSH", 1),
            ("Staging host proxy config", 1),
            ("Validating host proxy config", 2),
            ("Reloading host proxy", 2),
            ("Cleaning backend SSH access", 1),
        ),
    ),
    (
        "Verify output",
        1,
        (
            ("Verifying admin exposure", 1),
            ("Auditing control plane", 1),
        ),
    ),
    ("Finalize", 1, (("Refreshing outputs list", 1),)),
)

OPERATION_PROGRESS_TREES: dict[str, tuple[ProgressState, ...]] = {
    "outputSave": OUTPUT_SAVE_PROGRESS_TREE,
    "backup": (
        (
            "Backup",
            1,
            (
                ("Backup requested", 1),
                ("Gathering app data", 1),
                ("Capturing container snapshot", 1),
                ("Collecting mounted paths", 1),
                ("Measuring source data", 1),
                ("Exporting mounted paths", 1),
                ("Exporting container snapshot", 1),
                ("Compressing backup bundle", 1),
                ("Writing backup bundle", 1),
                ("Verifying backup", 1),
                ("Backup verified", 1),
                ("Saving backup record", 1),
            ),
        ),
    ),
    "restore": (
        (
            "Restore backup",
            1,
            (
                ("Preparing restore", 1),
                ("Verifying backup bundle", 1),
                ("Validating restore payload", 1),
                ("Stopping existing runtime", 1),
                ("Restoring mounted paths", 1),
                ("Restoring backend config", 1),
                ("Restoring container snapshot", 1),
                ("Writing app control assets", 1),
                ("Restarting runtime", 1),
                ("Reconciling restored host state", 1),
                ("Committing restore", 1),
                ("Refreshing output page", 1),
            ),
        ),
    ),
    "deleteBackup": (
        (
            "Delete backup",
            1,
            (
                ("Finding backup", 1),
                ("Removing backup bundle", 1),
                ("Refreshing backup history", 1),
            ),
        ),
    ),
    "importBackup": (
        (
            "Import backup",
            1,
            (
                ("Reading backup bundle", 1),
                ("Verifying backup bundle", 1),
                ("Adding backup to history", 1),
            ),
        ),
    ),
    "clone": (
        (
            "Clone output",
            1,
            (
                ("Preparing clone", 1),
                ("Capturing source state", 1),
                ("Verifying clone bundle", 1),
                ("Writing cloned output", 1),
                ("Restoring guest and data", 1),
                ("Writing app control assets", 1),
                ("Running clone checks", 1),
                ("Finalizing clone", 1),
            ),
        ),
    ),
    "deleteOutput": DELETE_OUTPUT_PROGRESS_TREE,
    "transferOutput": (
        ("Confirm transfer", 1, (("Reading transfer request", 1),)),
        ("Select target", 1, (("Checking target node", 1),)),
        (
            "Snapshot source",
            1,
            (("Capturing source state", 1), ("Capturing source state from peer", 1)),
        ),
        (
            "Restore target",
            1,
            (("Copying snapshot to target", 1), ("Restoring output on leader", 1)),
        ),
        ("Verify target", 1, (("Checking target health", 1),)),
        ("Update placement", 1, (("Saving output placement", 1),)),
        ("Render routes", 1, (("Rendering cluster routes", 1),)),
        ("Stop source", 1, (("Stopping source runtime", 1),)),
        ("Finalize", 1, (("Transfer complete", 1),)),
    ),
    "replicaSetup": (
        ("Setup request", 1, (("Reading setup request", 1),)),
        ("Check target", 1, (("Checking target node", 1),)),
        (
            "Prepare runtime",
            1,
            (("Capturing source state", 1), ("Creating fresh guest", 1)),
        ),
        ("Copy runtime", 1, (("Copying snapshot to target", 1),)),
        ("Restore runtime", 1, (("Restoring replica on target", 1),)),
        ("Verify runtime", 1, (("Checking target health", 1),)),
        ("Finalize", 1, (("Node setup complete", 1),)),
    ),
    "updateCnc": (
        ("Starting update", 1, (("Sending update request", 1),)),
        ("Queue update", 1, (("Watching background updater", 1),)),
        ("Update CNC", 1, (("Applying release", 1),)),
        ("Restart CNC", 1, (("Waiting for admin service", 1),)),
        ("Verify update", 1, (("Refreshing version card", 1),)),
    ),
}

# These exported views stay derived from weighted state/substate trees; adding
# separate absolute point catalogs would create a second progress source of truth.
INPUT_PROGRESS_PIPELINES = INPUT_PROGRESS_TREES
OPERATION_PROGRESS_PIPELINES = OPERATION_PROGRESS_TREES

(
    _OUTPUT_SAVE_PROGRESS_STEPS,
    _OUTPUT_SAVE_PROGRESS_POINTS,
    _OUTPUT_SAVE_SUBSTATE_PROGRESS_POINTS,
) = _build_weighted_progress_catalog(
    OUTPUT_SAVE_PROGRESS_TREE, max_progress=RUNNING_PROGRESS_MAX
)

_INPUT_PROGRESS_CATALOGS = {
    name: _build_weighted_progress_catalog(tree, max_progress=RUNNING_PROGRESS_MAX)
    for name, tree in INPUT_PROGRESS_TREES.items()
}
_INPUT_PROGRESS_STEPS = {
    name: catalog[0] for name, catalog in _INPUT_PROGRESS_CATALOGS.items()
}
_OPERATION_PROGRESS_CATALOGS = {
    name: _build_weighted_progress_catalog(tree, max_progress=RUNNING_PROGRESS_MAX)
    for name, tree in OPERATION_PROGRESS_TREES.items()
}


def _tree_pairs(tree: tuple[ProgressState, ...]) -> tuple[ProgressPair, ...]:
    return tuple(
        (phase, substep)
        for phase, _weight, substeps in tree
        for substep, _substep_weight in substeps
    )


def _filtered_progress_tree(
    tree: tuple[ProgressState, ...],
    pairs: Iterable[ProgressPair],
) -> tuple[ProgressState, ...]:
    wanted = {(str(phase), str(substep)) for phase, substep in pairs}
    filtered: list[ProgressState] = []
    for phase, state_weight, substeps in tree:
        kept_substeps = tuple(
            (substep, substep_weight)
            for substep, substep_weight in substeps
            if (phase, substep) in wanted
        )
        if kept_substeps:
            filtered.append((phase, state_weight, kept_substeps))
    return tuple(filtered)


def progress_plan_from_tree(
    pipeline: str,
    tree: tuple[ProgressState, ...],
    pairs: Iterable[ProgressPair],
    *,
    max_progress: int = RUNNING_PROGRESS_MAX,
) -> dict[str, object]:
    filtered_tree = _filtered_progress_tree(tree, pairs)
    if not filtered_tree:
        filtered_tree = tree
    steps, _phase_points, _substate_points = _build_weighted_progress_catalog(
        filtered_tree, max_progress=max_progress
    )
    return {
        "pipeline": pipeline,
        "steps": steps,
    }


def progress_plan_value(
    plan: dict[str, object] | None,
    *,
    phase: str = "",
    message: str = "",
    current: int = 0,
) -> int:
    progress = max(0, min(100, int(current)))
    if not isinstance(plan, dict):
        return progress
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return progress
    needle_phase = _normalize_progress_text(phase)
    needle_message = _normalize_progress_text(message)
    phase_match = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_phase = _normalize_progress_text(str(step.get("headline") or ""))
        step_substep = _normalize_progress_text(str(step.get("substep") or ""))
        step_note = _normalize_progress_text(str(step.get("note") or ""))
        try:
            step_progress = int(step.get("progress") or 0)
        except (TypeError, ValueError):
            step_progress = 0
        if needle_phase and needle_phase == step_phase:
            phase_match = max(phase_match, step_progress)
            if needle_message and needle_message in {step_substep, step_note}:
                return max(progress, step_progress)
    if phase_match:
        return max(progress, phase_match)
    return progress


CREATE_OUTPUT_DB_PROGRESS_PAIRS: tuple[ProgressPair, ...] = (
    ("Validate output", "Checking ports and health settings"),
    ("Save desired state", "Opening save transaction"),
    ("Save desired state", "Staging output config"),
    ("Save desired state", "Staging attached input routes"),
    ("Save desired state", "Saving route graph"),
    ("Save desired state", "Waiting for host mutation lock"),
    ("Save desired state", "Building host plan"),
)

CREATE_OUTPUT_RUNTIME_PROGRESS_PAIRS: tuple[ProgressPair, ...] = (
    ("Plan runtime", "Inspecting app container"),
    ("Plan runtime", "Selecting runtime action"),
    ("Prepare guest filesystem", "Reconciling guest filesystem"),
    ("Prepare guest filesystem", "Creating seed container"),
    ("Prepare guest filesystem", "Exporting seed filesystem"),
    ("Prepare guest filesystem", "Removing seed container"),
    ("Prepare guest filesystem", "Extracting guest filesystem"),
    ("Provision guest", "Preparing package catalog"),
    ("Provision guest", "Installing guest runtime"),
    ("Provision guest", "Installing guest tools"),
    ("Provision guest", "Cleaning package cache"),
    ("Provision guest", "Running profile self test"),
    ("Activate rootfs", "Preparing rootfs switch"),
    ("Activate rootfs", "Activating guest filesystem"),
    ("Install service", "Writing app control assets"),
    ("Install service", "Writing service files"),
    ("Install service", "Starting app service"),
    ("Install service", "Applying resource limits"),
    ("Bootstrap guest", "Checking guest systemd"),
    ("Bootstrap guest", "Waiting for guest readiness"),
    ("Verify output", "Selecting private target"),
    ("Verify output", "Checking app health"),
)


def create_output_progress_plan(
    *,
    backend_kind: str = "app",
    input_kinds: Iterable[str] = (),
    include_runtime: bool | None = None,
    include_runtime_inspection: bool | None = None,
    include_tailnet_paths: bool | None = None,
    include_tailnet_services: bool | None = None,
    include_host_assets: bool = True,
    include_shield: bool = False,
    include_cluster: bool = False,
    include_ingress: bool = True,
    include_admin_verify: bool = True,
    include_audit: bool = True,
) -> dict[str, object]:
    normalized_kind = str(backend_kind or "app").strip().lower() or "app"
    normalized_input_kinds = {
        str(kind or "domain").strip().lower() or "domain" for kind in input_kinds
    }
    runtime_required = (
        normalized_kind == "app" if include_runtime is None else include_runtime
    )
    runtime_inspection_required = (
        runtime_required
        if include_runtime_inspection is None
        else include_runtime_inspection
    )
    tailnet_paths_required = (
        "tailnet_path" in normalized_input_kinds
        if include_tailnet_paths is None
        else include_tailnet_paths
    )
    tailnet_services_required = (
        "tailnet_service" in normalized_input_kinds
        if include_tailnet_services is None
        else include_tailnet_services
    )
    pairs: list[ProgressPair] = list(CREATE_OUTPUT_DB_PROGRESS_PAIRS)
    if tailnet_services_required:
        pairs.append(("Prepare host", "Checking tailnet service host"))
    if include_host_assets:
        pairs.append(("Prepare host", "Reconciling CNC runtime assets"))
    pairs.append(("Prepare host", "Checking admin exposure"))
    if runtime_inspection_required:
        pairs.append(("Plan runtime", "Inspecting app container"))
    if runtime_required:
        pairs.extend(
            pair
            for pair in CREATE_OUTPUT_RUNTIME_PROGRESS_PAIRS
            if pair != ("Plan runtime", "Inspecting app container")
        )
    if include_shield or normalized_kind == "shield":
        pairs.append(("Prepare Shield", "Preparing Shield gate"))
    pairs.append(("Apply host access", "Reconciling backend SSH"))
    if include_ingress:
        pairs.extend(
            (
                ("Apply host access", "Staging host proxy config"),
                ("Apply host access", "Validating host proxy config"),
                ("Apply host access", "Reloading host proxy"),
            )
        )
    if tailnet_paths_required:
        pairs.append(("Apply host access", "Updating tailnet paths"))
    if tailnet_services_required:
        pairs.append(("Apply host access", "Updating tailnet services"))
    if include_cluster:
        pairs.append(("Apply cluster", "Syncing follower ingress"))
    if include_admin_verify:
        pairs.append(("Verify output", "Verifying admin exposure"))
    if include_audit:
        pairs.append(("Verify output", "Auditing control plane"))
    return progress_plan_from_tree(
        "createOutput",
        CREATE_BACKEND_PROGRESS_TREE,
        pairs,
        max_progress=CREATE_BACKEND_RUNNING_PROGRESS_MAX,
    )


def delete_input_progress_plan(*, input_kind: str = "domain") -> dict[str, object]:
    normalized_kind = str(input_kind or "domain").strip().lower() or "domain"
    pairs: list[ProgressPair] = [
        ("Validate input", "Loading current route graph"),
        ("Map route graph", "Checking affected outputs"),
        ("Save desired state", "Removing input config"),
        ("Save desired state", "Waiting for host mutation lock"),
        ("Save desired state", "Building host plan"),
        ("Apply host access", "Reconciling host routes"),
    ]
    if normalized_kind == "tailnet_path":
        pairs.append(("Apply host access", "Updating tailnet paths"))
    if normalized_kind == "tailnet_service":
        pairs.append(("Apply host access", "Updating tailnet services"))
    pairs.extend(
        (
            ("Apply host access", "Validating host proxy config"),
            ("Finalize", "Refreshing dashboard"),
        )
    )
    return progress_plan_from_tree(
        "deleteInput", INPUT_PROGRESS_TREES["deleteInput"], pairs
    )


def delete_output_progress_plan(*, backend_kind: str = "app") -> dict[str, object]:
    normalized_kind = str(backend_kind or "app").strip().lower() or "app"
    pairs: list[ProgressPair] = [
        ("Delete output", "Opening delete operation"),
        ("Delete output", "Removing desired state"),
        ("Delete output", "Waiting for host mutation lock"),
        ("Delete output", "Building host plan"),
        ("Apply host access", "Staging host proxy config"),
        ("Apply host access", "Validating host proxy config"),
        ("Apply host access", "Reloading host proxy"),
        ("Verify output", "Verifying admin exposure"),
        ("Verify output", "Auditing control plane"),
        ("Finalize", "Refreshing outputs list"),
    ]
    if normalized_kind == "app":
        pairs.extend(
            (
                ("Plan runtime", "Removing app runtime"),
                ("Apply host access", "Reconciling backend SSH"),
                ("Apply host access", "Cleaning backend SSH access"),
            )
        )
    return progress_plan_from_tree("deleteOutput", DELETE_OUTPUT_PROGRESS_TREE, pairs)


def operation_progress_plan(pipeline: str) -> dict[str, object]:
    tree = OPERATION_PROGRESS_TREES.get(pipeline)
    if tree is None:
        return {"pipeline": pipeline, "steps": []}
    return progress_plan_from_tree(pipeline, tree, _tree_pairs(tree))


def create_backend_runtime_summary(
    settings: Settings,
    backend_name: str,
    *,
    include_events: bool = True,
) -> dict[str, Any] | None:
    if not backend_name:
        return None
    state = read_bootstrap_state(settings, backend_name)
    if not isinstance(state, dict):
        return None
    phase = str(state.get("reconcile_phase") or "").strip()
    status = str(state.get("reconcile_phase_status") or "").strip()
    details = state.get("reconcile_details")
    substep = ""
    if isinstance(details, dict):
        substep = str(details.get("substep") or details.get("step") or "").strip()
    if not phase and not substep:
        return None

    display_phase, substep_label = _runtime_display(phase, substep)
    message = substep_label or display_phase

    summary: dict[str, Any] = {
        "phase": display_phase[:64],
        "reconcile_phase": phase,
        "status": status,
        "substep": substep,
        "substate": substep_label,
        "message": message,
        "updated_at": state.get("updated_at")
        if isinstance(state.get("updated_at"), str)
        else "",
    }
    if include_events:
        raw_events = state.get("events")
        summary["events"] = (
            raw_events[-RUNTIME_EVENT_SUMMARY_LIMIT:]
            if isinstance(raw_events, list)
            else []
        )
    return summary


def create_backend_runtime_progress(
    settings: Settings, backend_name: str
) -> dict[str, Any] | None:
    summary = create_backend_runtime_summary(
        settings, backend_name, include_events=False
    )
    if summary is None:
        return None
    if str(summary.get("status") or "").strip() != "running":
        return None
    return {
        "phase": str(summary.get("phase") or "app runtime"),
        "status": str(summary.get("status") or ""),
        "substep": str(summary.get("substep") or ""),
        "substate": str(summary.get("substate") or ""),
        "message": str(summary.get("message") or "Reconciling app runtime."),
        "progress": create_backend_progress_value(
            str(summary.get("phase") or "app runtime"),
            substep=str(summary.get("substep") or ""),
        ),
        "updated_at": str(summary.get("updated_at") or ""),
    }


def create_backend_progress_value(
    phase: str, current: int = 0, *, substep: str = ""
) -> int:
    substep_progress = CREATE_BACKEND_SUBSTEP_PROGRESS_POINTS.get(substep)
    if substep_progress is not None:
        return max(current, substep_progress)
    substate_progress = CREATE_BACKEND_OPERATION_SUBSTATE_PROGRESS_POINTS.get(substep)
    if substate_progress is not None:
        return max(current, substate_progress)
    if substep:
        return current
    return max(current, CREATE_BACKEND_PROGRESS_POINTS.get(phase, current))


def create_backend_progress_steps() -> list[dict[str, object]]:
    return [dict(step) for step in _CREATE_BACKEND_PROGRESS_STEPS]


def input_progress_pipelines() -> dict[str, list[dict[str, object]]]:
    return {
        name: [dict(step) for step in steps]
        for name, steps in _INPUT_PROGRESS_STEPS.items()
    }


def output_save_progress_steps() -> list[dict[str, object]]:
    return [dict(step) for step in _OUTPUT_SAVE_PROGRESS_STEPS]


def operation_progress_pipelines() -> dict[str, list[dict[str, object]]]:
    return {
        name: [dict(step) for step in catalog[0]]
        for name, catalog in _OPERATION_PROGRESS_CATALOGS.items()
    }


def _progress_value_from_catalog(
    catalogs: dict[str, tuple[list[dict[str, object]], dict[str, int], dict[str, int]]],
    pipeline: str,
    *,
    phase: str = "",
    message: str = "",
    current: int = 0,
) -> int:
    progress = max(0, min(100, int(current)))
    needle_phase = _normalize_progress_text(phase)
    needle_message = _normalize_progress_text(message)
    matched_message = False
    steps, phase_points, substate_points = catalogs.get(pipeline, ([], {}, {}))
    _ = steps
    for substep, step_progress in substate_points.items():
        message_candidates = {
            _normalize_progress_text(substep),
            _normalize_progress_text(f"{substep}."),
        }
        if needle_message and needle_message in message_candidates:
            progress = max(progress, step_progress)
            matched_message = True
    if matched_message:
        return progress
    if needle_phase:
        for phase, step_progress in phase_points.items():
            if needle_phase == _normalize_progress_text(phase):
                progress = max(progress, step_progress)
    return progress


def input_progress_value(
    pipeline: str,
    *,
    phase: str = "",
    message: str = "",
    current: int = 0,
) -> int:
    return _progress_value_from_catalog(
        _INPUT_PROGRESS_CATALOGS,
        pipeline,
        phase=phase,
        message=message,
        current=current,
    )


def operation_progress_value(
    pipeline: str,
    *,
    phase: str = "",
    message: str = "",
    current: int = 0,
) -> int:
    return _progress_value_from_catalog(
        _OPERATION_PROGRESS_CATALOGS,
        pipeline,
        phase=phase,
        message=message,
        current=current,
    )


def _runtime_display(phase: str, substep: str) -> tuple[str, str]:
    mapped = RUNTIME_SUBSTEP_LABELS.get(substep)
    if mapped is not None:
        return mapped
    display_phase = (
        RUNTIME_PHASE_LABELS.get(phase) or _runtime_label(phase) or "App runtime"
    )
    substep_label = RUNTIME_PHASE_SUBSTEPS.get(phase) or _runtime_label(substep)
    return display_phase, substep_label


def _runtime_label(value: str) -> str:
    return value.replace("_", " ").strip().capitalize()


def _normalize_progress_text(value: str) -> str:
    return " ".join(str(value or "").replace(".", " ").strip().lower().split())
