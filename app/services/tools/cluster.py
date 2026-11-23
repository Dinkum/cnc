
from __future__ import annotations

import asyncio

from app.services.apply_core import ApplyFailed
from app.services.cluster_ingress import reconcile_cluster_followers
from app.services.tools.core import Plan

tool_name = "cluster"
phases = ("cluster_followers",)


def check(plan: Plan) -> None:
    if not plan.desired.cluster_nodes:
        return
    nodes_by_uid = {
        str(node.get("node_uid") or "").strip(): node
        for node in plan.desired.cluster_nodes
        if str(node.get("node_uid") or "").strip()
    }
    for backend in plan.desired.enabled_app_backends:
        if not plan.scope.covers(backend.name):
            continue
        target_uid = str(getattr(backend, "placement_node_uid", "") or "").strip()
        if not target_uid or target_uid == "local":
            continue
        node = nodes_by_uid.get(target_uid)
        if node is None or not str(node.get("tailnet_ip") or "").strip():
            raise ApplyFailed(
                f"follower node {target_uid} for output {backend.name} is unavailable",
                phase="cluster_followers",
                details={"backend": backend.name, "node_uid": target_uid},
            )


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
