
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

    kind: str
    outputs: frozenset[str] = frozenset()
    full_reconcile: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    explicit_scope: Scope | None = None

    def scope(self) -> Scope:
        if self.explicit_scope is not None:
            return self.explicit_scope
        if self.full_reconcile:
            return Scope.everything(reason=f"{self.kind} requested a full reconcile")
        return Scope.outputs_named(set(self.outputs), reason=self.kind)


async def converge_change(
    session: AsyncSession,
    settings: Settings,
    change: ConfigChange,
    **kwargs: Any,
) -> Any:
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
    return ConfigChange(kind=kind, outputs=frozenset({name}), detail=detail)


def route_change(kind: str, outputs: set[str], **detail: Any) -> ConfigChange:
    return ConfigChange(kind=kind, outputs=frozenset(outputs), detail=detail)


def full_reconcile(
    kind: str, *, actor: str = "system", **detail: Any
) -> ConfigChange:
    return ConfigChange(
        kind=kind, actor=actor, full_reconcile=True, detail=detail
    )
