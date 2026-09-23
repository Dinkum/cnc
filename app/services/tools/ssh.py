"""ssh: per-output host accounts and the sshd config that admits them.

Not file-shaped. It runs ``useradd`` and ``usermod``, so its own reconciler
owns the comparison and reads the live account list off the box every time.

That read is what makes deleting an output work. A backend that is no longer in
desired state has its user removed because the reconciler compares the accounts
that exist against the accounts wanted, rather than being told that a delete
happened. Apply used to swap this reconciler out for a stub during a delete and
clean up afterwards in a handler that swallowed its own errors, which is how a
failure there could report "Output deleted." and leave the account behind.
"""

from __future__ import annotations

from app.services.ssh_access import reconcile_backend_ssh_access
from app.services.tools.core import Plan

tool_name = "ssh"
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("ssh_access",)


def wanted_access(plan: Plan) -> list[dict[str, object]]:
    return [
        {
            "name": backend.name,
            "public_key": str(backend.ssh_public_key or "").strip() or None,
        }
        for backend in plan.desired.enabled_app_backends
    ]


async def ssh(plan: Plan) -> bool:
    """Converge host accounts to exactly the enabled app outputs."""
    if not plan.desired.known_app_backends:
        return False

    await plan.report("Apply host access", "Reconciling backend SSH")
    plan.touching(tool_name)
    reconcile = getattr(
        plan.services, "reconcile_backend_ssh_access", reconcile_backend_ssh_access
    )
    # A scoped save may only create, re-key or remove the accounts it owns.
    # Another output's account is not this change's to touch.
    scoped_to = None if plan.scope.is_full else set(plan.scope.outputs or frozenset())
    details = await reconcile(wanted_access(plan), plan.settings, scoped_to=scoped_to)
    plan.details.update(details)
    return _changed_anything(details)


def _changed_anything(details: dict[str, object]) -> bool:
    """Whether the reconciler actually touched an account or a file.

    It reports this directly. A deferred run is a stub that did nothing, and
    an older reconciler that does not report gets the conservative answer,
    because claiming the host is untouched when it might not be is the
    dangerous direction to be wrong in.
    """
    if details.get("ssh_reconcile_deferred"):
        return False
    if "ssh_changed" in details:
        return bool(details["ssh_changed"])
    return True
