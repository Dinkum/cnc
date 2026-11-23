
from __future__ import annotations

import asyncio
from app.services.ssh_access import (
    _sudoers_config,
    _validate_sudoers_config,
    reconcile_backend_ssh_access,
)
from app.services.tools.core import Plan

tool_name = "ssh"
phases = ("ssh_access",)


def wanted_access(plan: Plan) -> list[dict[str, object]]:
    return [
        {
            "name": backend.name,
            "public_key": str(backend.ssh_public_key or "").strip() or None,
        }
        for backend in plan.desired.enabled_app_backends
    ]


async def check(plan: Plan) -> None:
    if not plan.desired.known_app_backends:
        return
    reconcile = getattr(
        plan.services, "reconcile_backend_ssh_access", reconcile_backend_ssh_access
    )
    if reconcile is not reconcile_backend_ssh_access:
        return
    if not plan.settings.ssh_backend_sudoers_path.parent.exists():
        return
    content = _sudoers_config(plan.settings)
    await asyncio.to_thread(_validate_sudoers_config, plan.settings, content)


async def ssh(plan: Plan) -> bool:
    if not plan.desired.known_app_backends:
        return False

    await plan.report("Apply host access", "Reconciling backend SSH")
    plan.touching(tool_name)
    reconcile = getattr(
        plan.services, "reconcile_backend_ssh_access", reconcile_backend_ssh_access
    )
    scoped_to = None if plan.scope.is_full else set(plan.scope.outputs or frozenset())
    details = await reconcile(wanted_access(plan), plan.settings, scoped_to=scoped_to)
    plan.details.update(details)
    return _changed_anything(details)


def _changed_anything(details: dict[str, object]) -> bool:
    if details.get("ssh_reconcile_deferred"):
        return False
    if "ssh_changed" in details:
        return bool(details["ssh_changed"])
    return True
