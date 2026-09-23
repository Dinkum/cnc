"""cluster: push generated ingress to follower nodes.

The odd one out, and worth saying why. Every other tool configures something on
this box. This one runs some of the same work on other machines over SSH, so a
follower being unreachable is not a failure of the leader's apply. It is
recorded and apply continues, which is the one place a tool reports "could not
reach it" rather than succeeding or raising.
"""

from __future__ import annotations

import asyncio

from app.services.apply_core import ApplyFailed
from app.services.cluster_ingress import reconcile_cluster_followers
from app.services.tools.core import Plan

tool_name = "cluster"
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("cluster_followers",)


async def cluster(plan: Plan) -> bool:
    if not plan.desired.cluster_nodes:
        plan.details["cluster_followers"] = []
        plan.details["cluster_followers_skipped"] = True
        return False

    await plan.report("Apply cluster", "Syncing follower ingress")
    plan.touching(tool_name)
    reconcile = getattr(
        plan.services, "reconcile_cluster_followers", reconcile_cluster_followers
    )
    try:
        plan.details.update(
            await asyncio.to_thread(reconcile, plan.desired, plan.settings)
        )
    except Exception as exc:
        raise ApplyFailed(
            "follower ingress reconcile failed",
            phase="cluster_followers",
            details={"error": str(exc)},
        ) from exc
    return True
