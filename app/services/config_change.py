"""One service-level path for every configuration change.

UI, JSON API, CLI and scheduled work all change configuration the same way:
say which outputs the change concerns, change the rows, converge the host,
commit only if it converged. This module owns that sequence so the decisions
that used to be made independently by each transport, and got made differently,
are made once.

What is centralized here:

scope
    Derived from the requested change, not from a comparison against the last
    successful apply. A rename touches one output; a delete touches one output;
    an explicit reconcile touches everything. Tools narrow their mutation to it,
    so an unrelated output that happens to have drifted is left alone.

commit point
    Rows are flushed so desired state can be rendered, and committed only after
    the host converges. A failed change keeps the previous configuration and
    unwinds the reversible host effects, so the database and the box agree.

evidence
    The outcome records the scope, what each tool did, what was unwound and
    what was left changed. "It ran" is never treated as "it worked".

Irreversible cleanup deliberately does not live here. Removing a guest
filesystem has to happen after the durable commit, so it stays with the caller
that owns the deletion and reports its own outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.services.mutation_apply import commit_and_apply
from app.services.tools.core import Scope

logger = get_logger("config_change")


@dataclass(frozen=True)
class ConfigChange:
    """A requested configuration change, normalized for any transport.

    ``outputs`` names what the caller is changing. It is the caller's
    declaration of intent, checked against desired state rather than inferred
    from it, because inferring scope from a diff is how a save became a
    fleet-wide repair.
    """

    #: Operation identity, e.g. ``ui.backend.update`` or ``cli.output.delete``.
    kind: str
    #: Outputs this change concerns. Empty means it touches no output runtime
    #: directly, as for an input edit that only moves routing.
    outputs: frozenset[str] = frozenset()
    #: Set for an explicit whole-host reconcile, which is the only case allowed
    #: to repair things the caller did not ask about.
    full_reconcile: bool = False
    #: Free-form identity for logs and the durable record.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Actor recorded by the apply operation when the caller did not supply an
    #: existing operation handle.
    actor: str = "system"
    #: A scope the caller already derived. The shared UI save path works out
    #: which outputs a change concerns before it reaches here, so it passes the
    #: answer rather than re-deriving it from a second description.
    explicit_scope: Scope | None = None

    def scope(self) -> Scope:
        if self.explicit_scope is not None:
            return self.explicit_scope
        if self.full_reconcile:
            return Scope.everything(reason=f"{self.kind} requested a full reconcile")
        return Scope.outputs_named(set(self.outputs), reason=self.kind)




#: Change the rows. Runs inside the caller's session, before convergence, and
#: must not commit: the commit point belongs to this module.




async def converge_change(
    session: AsyncSession,
    settings: Settings,
    change: ConfigChange,
    **kwargs: Any,
) -> Any:
    """Commit the edited rows only if the host converges within scope.

    The one place a configuration change reaches the host. Callers describe
    what they are changing with a ``ConfigChange``; this turns that into a
    scope, records it, and hands it to the commit boundary. Nothing else calls
    ``commit_and_apply`` directly, so scope cannot be forgotten and a change
    cannot quietly widen into a fleet reconcile.

    Returns the raw mutation result, because callers still need the apply
    response and its details to report their own outcomes.
    """
    scope = change.scope()
    logger.info(
        "config_change.accepted",
        kind=change.kind,
        **scope.as_details(),
        **change.detail,
    )
    result = await commit_and_apply(
        session,
        settings,
        operation=change.kind,
        actor=change.actor,
        scope=scope,
        **kwargs,
    )
    logger.info(
        "config_change.finished",
        kind=change.kind,
        status=result.apply_response.status,
        **scope.as_details(),
    )
    return result


def output_change(kind: str, name: str, **detail: Any) -> ConfigChange:
    """A change concerning exactly one output."""
    return ConfigChange(kind=kind, outputs=frozenset({name}), detail=detail)


def route_change(kind: str, outputs: set[str], **detail: Any) -> ConfigChange:
    """A change to routing, concerning whichever outputs it attaches or detaches.

    An input edit does not touch any output's runtime, but it does change what
    is published for them, so those outputs are in scope and no others are.
    """
    return ConfigChange(kind=kind, outputs=frozenset(outputs), detail=detail)


def full_reconcile(
    kind: str, *, actor: str = "system", **detail: Any
) -> ConfigChange:
    """An explicit request to converge the whole host.

    The only change permitted to repair things nobody asked about. Scheduled
    reconciles and ``cnc-admin host reconcile`` use this; an ordinary save
    never does.
    """
    return ConfigChange(
        kind=kind, actor=actor, full_reconcile=True, detail=detail
    )
