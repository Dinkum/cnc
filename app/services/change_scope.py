from __future__ import annotations

from dataclasses import dataclass
from enum import Flag, auto
from typing import Any


class ToolScope(Flag):
    NONE = 0
    PODMAN = auto()
    SHIELD = auto()
    NETDATA = auto()
    SSH = auto()
    NGINX = auto()
    CLOUDFLARE_TUNNEL = auto()
    UFW = auto()
    CLUSTER = auto()
    TAILSCALE = auto()

    DOMAIN_CONNECTION = NGINX | CLUSTER | CLOUDFLARE_TUNNEL
    ROUTES = DOMAIN_CONNECTION | SHIELD | TAILSCALE
    OUTPUT = ROUTES | PODMAN | SSH
    HOST = OUTPUT | UFW | NETDATA

    def includes(self, name: str) -> bool:
        return bool(self & ToolScope[name.upper()])


@dataclass(frozen=True)
class Scope:
    outputs: frozenset[str] | None = None
    reason: str = "full reconcile"
    tools: ToolScope = ToolScope.HOST
    rebalance_resources: bool = False

    @property
    def is_full(self) -> bool:
        return self.outputs is None

    def covers(self, output: str) -> bool:
        return self.is_full or output in (self.outputs or frozenset())

    def limit(self, names: set[str]) -> set[str]:
        if self.is_full:
            return set(names)
        return {name for name in names if self.covers(name)}

    def as_details(self) -> dict[str, Any]:
        return {
            "scope": "full" if self.is_full else sorted(self.outputs or ()),
            "scope_reason": self.reason,
            "tool_scope": self.tools.name.lower(),
            "rebalance_resources": self.rebalance_resources,
        }

    @classmethod
    def everything(cls, reason: str = "full reconcile") -> "Scope":
        return cls(outputs=None, reason=reason)

    @classmethod
    def outputs_named(
        cls, names: set[str], reason: str, tools: ToolScope = ToolScope.OUTPUT
    ) -> "Scope":
        return cls(outputs=frozenset(names), reason=reason, tools=tools)
