
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, Input
from app.services.mutation_apply import commit_and_apply
from app.services.change_scope import Scope, ToolScope

logger = get_logger("config_change")


@dataclass(frozen=True)
class ConfigChange:

    kind: str
    outputs: frozenset[str] = frozenset()
    full_reconcile: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    tools: ToolScope = ToolScope.OUTPUT
    explicit_scope: Scope | None = None
    rebalance_resources: bool = False

    def scope(self) -> Scope:
        if self.explicit_scope is not None:
            return self.explicit_scope
        if self.full_reconcile:
            return Scope.everything(reason=f"{self.kind} requested a full reconcile")
        return Scope(self.outputs, self.kind, self.tools, self.rebalance_resources)


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


def _route_tools(kind: str, connection: str) -> ToolScope:
    if kind in {"tailnet_path", "tailnet_service"}:
        return ToolScope.TAILSCALE
    tools = ToolScope.NGINX | ToolScope.CLUSTER | ToolScope.SHIELD
    if connection == "cloudflare_tunnel":
        tools |= ToolScope.CLOUDFLARE_TUNNEL
    return tools


def input_edit(kind: str, item: Input) -> ConfigChange:
    attrs = inspect(item).attrs
    previous_kind = next(iter(attrs.kind.history.deleted), item.kind)
    previous_connection = next(iter(attrs.connection.history.deleted), item.connection)
    tools = _route_tools(previous_kind, previous_connection) | _route_tools(
        item.kind, item.connection
    )
    outputs = {
        backend.name for backend in (*item.backends, *attrs.backends.history.deleted)
    }
    return route_change(kind, outputs, tools=tools)


def attached_route_tools(backend: Backend) -> ToolScope:
    tools = ToolScope.NONE
    for item in backend.inputs:
        tools |= _route_tools(item.kind, item.connection)
    return tools


def output_tools(backend: Backend) -> ToolScope:
    tools = attached_route_tools(backend)
    if backend.kind == "app":
        tools |= ToolScope.PODMAN | ToolScope.SSH
    elif backend.kind == "shield":
        tools |= ToolScope.SHIELD
    return tools


def changed_output_fields(backend: Backend) -> set[str]:
    return {attr.key for attr in inspect(backend).attrs if attr.history.has_changes()}


def output_edit(
    kind: str,
    backend: Backend,
    *,
    membership_change: bool = False,
    requested_fields: set[str] | None = None,
) -> ConfigChange:
    state = inspect(backend)
    changed = changed_output_fields(backend)
    routes = attached_route_tools(backend)
    for item in state.attrs.inputs.history.deleted:
        routes |= _route_tools(item.kind, item.connection)
    fields = changed or requested_fields or set()
    routing_fields = {
        "inputs",
        "input_ids",
        "static_root",
        "shield_enabled",
        "shield_code_hash",
        "shield_access_code",
    }
    resource_fields = {
        "resource_size",
        "resource_mode",
        "memory_high_override",
        "memory_max_override",
        "cpu_quota_override",
    }
    if (
        membership_change
        or not fields
        or fields - routing_fields - resource_fields - {"notes"}
    ):
        tools = output_tools(backend) | routes
    else:
        tools = routes if fields & routing_fields else ToolScope.NONE
        if fields & resource_fields and backend.kind == "app":
            tools |= ToolScope.PODMAN
    pool_changed = (
        backend.enabled
        if membership_change
        else ("enabled" in changed or (backend.enabled and "resource_size" in changed))
    )
    return ConfigChange(
        kind,
        outputs=frozenset({backend.name}),
        tools=tools,
        rebalance_resources=bool(backend.kind == "app" and pool_changed),
    )


def output_change(
    kind: str, name: str, *, tools: ToolScope = ToolScope.OUTPUT, **detail: Any
) -> ConfigChange:
    return ConfigChange(
        kind=kind, outputs=frozenset({name}), tools=tools, detail=detail
    )


def route_change(
    kind: str, outputs: set[str], *, tools: ToolScope = ToolScope.ROUTES, **detail: Any
) -> ConfigChange:
    return ConfigChange(kind=kind, outputs=frozenset(outputs), tools=tools, detail=detail)


def full_reconcile(
    kind: str, *, actor: str = "system", **detail: Any
) -> ConfigChange:
    return ConfigChange(
        kind=kind, actor=actor, full_reconcile=True, detail=detail
    )
