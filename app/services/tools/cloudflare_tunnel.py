from __future__ import annotations

import asyncio

from app.services.cloudflare_tunnel import reconcile_tunnel
from app.services.tools.core import Plan

tool_name = "cloudflare_tunnel"
phases = ("cloudflare_tunnel",)


async def cloudflare_tunnel(plan: Plan) -> bool:
    if not plan.settings.cloudflare_tunnel_token:
        return False
    plan.touching(tool_name)
    changed = await asyncio.to_thread(reconcile_tunnel, plan.settings)
    plan.details["cloudflare_tunnel"] = {"changed": changed, "node": "leader"}
    return changed
