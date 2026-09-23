"""The tools CNC configures on a host, and the loop that runs them.

Every name in ``TOOLS`` is something that exists on the box and that you can
check by hand: ``nginx -t``, ``ufw status``, ``podman ps``, ``systemctl
list-units``. A name you can verify against the machine cannot quietly drift
from what it actually does.

Order is list order, read top to bottom. Podman and ssh come before nginx so
public ingress stays on the previous config until the runtime and its access
are ready. Verification runs last because it checks the result of everything
before it.

Tearing down walks the same list backwards, which is why deleting an output
stops nginx routing before podman removes the container while creating one does
the reverse. One list, two directions.

Submodules are imported as modules rather than having their tool functions
re-exported here, so ``tools.shield`` always means the shield module and never
shadows it with the function of the same name.
"""

from __future__ import annotations

from app.services.tools import (
    certs,
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
    """Take a module's tool function and stamp its name and phases onto it.

    Each module declares ``tool_name`` and ``phases`` at the top, next to the
    code they describe. The executor reads them off the function it is handed,
    so they are attached here rather than repeated in two places.
    """
    fn = getattr(module, module.tool_name)
    fn.tool_name = module.tool_name
    fn.phases = module.phases
    return fn


#: Tools that have been moved onto the contract, in the order they converge.
TOOLS: list[Tool] = [
    _tool(podman),
    _tool(shield),
    _tool(netdata),
    _tool(ssh),
    _tool(nginx),
    _tool(ufw),
    _tool(cluster),
    _tool(tailscale),
]

#: Not tools, and named here so their absence is a recorded decision rather
#: than an oversight. certs is a half-tool called by nginx on either side of
#: its reload, because a certificate only counts as live once nginx has
#: reloaded; it has no independent commit point.
#: Not in TOOLS, and named here so the absence is a recorded decision. certs is
#: a half-tool that nginx calls on either side of its reload, because a
#: certificate only counts as live once nginx has reloaded and so it has no
#: commit point of its own. cnc_systemd runs earlier, in run_apply, because
#: CNC's own units must be right before the admin exposure check that follows.
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
