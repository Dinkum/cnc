
from __future__ import annotations

from app.services.tools import (
    certs,
    cloudflare_tunnel,
    cluster,
    cnc_systemd,
    netdata,
    nginx,
    podman,
    shield,
    ssh,
    tailscale,
    ufw,
)
from app.services.tools.core import (
    ApplyFailed,
    Plan,
    Tool,
    converge,
    managed,
    managed_file,
)


def _tool(module) -> Tool:
    fn = getattr(module, module.tool_name)
    fn.tool_name = module.tool_name
    fn.phases = module.phases
    fn.check = getattr(module, "check", None)
    return fn


TOOLS: list[Tool] = [
    _tool(podman),
    _tool(shield),
    _tool(netdata),
    _tool(ssh),
    _tool(nginx),
    _tool(cloudflare_tunnel),
    _tool(ufw),
    _tool(cluster),
    _tool(tailscale),
]

NOT_IN_TOOLS: tuple[tuple[str, str], ...] = (
    ("certs", "no independent commit point; nginx drives prepare and activate"),
    ("cnc_systemd", "runs before the admin exposure precheck in run_apply"),
)

__all__ = [
    "NOT_IN_TOOLS",
    "TOOLS",
    "ApplyFailed",
    "Plan",
    "Tool",
    "certs",
    "cluster",
    "cnc_systemd",
    "converge",
    "managed",
    "managed_file",
    "netdata",
    "nginx",
    "podman",
    "shield",
    "ssh",
    "tailscale",
    "ufw",
]
