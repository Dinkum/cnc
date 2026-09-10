"""Mutation change detection and operator feedback for host-lock contention."""

from __future__ import annotations

from app.config import Settings
from app.models.entities import Backend
from app.schemas.backends import BackendUpdate
from app.services.error_reporting import ErrorCode
from app.services.operations import (
    HostMutationBlocker,
    active_host_mutation_blocker,
)
from app.ui.errors import operator_coded_error as _operator_coded_error

HOST_MUTATION_BLOCKER_LABELS = {
    "apply_host": "A host apply",
    "backup_backend": "A backup",
    "backend_ssh_key": "SSH key provisioning",
    "clone_backend": "An output clone",
    "cloudflare_sync": "Cloudflare sync",
    "create_backend": "Output create",
    "delete_backend": "Output delete",
    "host_mutation": "A host change",
    "repair_backend": "Output repair",
    "restore_backend": "Output restore",
    "setup_backend_replica": "Replica setup",
    "ui.backend.create": "Output create",
    "ui.backend.delete": "Output delete",
    "ui.input.create": "Input create",
    "ui.input.delete": "Input delete",
    "ui.input.update": "Input save",
    "update_control_plane": "A CNC update",
}


def backend_update_changed_keys(backend: Backend, payload: BackendUpdate) -> set[str]:
    values = payload.model_dump(exclude_unset=True)
    changed: set[str] = set()
    for key, value in values.items():
        if key == "kind" and value == backend.kind:
            continue
        if getattr(backend, key) != value:
            changed.add(key)
    return changed


def host_mutation_blocked_message(action: str, blocker: HostMutationBlocker) -> str:
    label = HOST_MUTATION_BLOCKER_LABELS.get(
        blocker.kind, blocker.kind.replace("_", " ").strip().title()
    )
    state = blocker.status.replace("_", " ").strip() or "running"
    phase = blocker.phase.replace("_", " ").strip()
    suffix = f" ({phase})" if phase and phase != state else ""
    return _operator_coded_error(
        f"{label} is already {state}{suffix}. {action} can't start until it finishes. Try again later.",
        ErrorCode.UI_ACTION_UNAVAILABLE,
    )


async def host_mutation_preflight_message(
    settings: Settings, action: str
) -> str | None:
    blocker = await active_host_mutation_blocker(settings)
    return host_mutation_blocked_message(action, blocker) if blocker else None
