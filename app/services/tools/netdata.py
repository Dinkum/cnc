
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.services.apply_core import DesiredState
from app.services.netdata_runtime import (
    netdata_native_runtime_converged,
    reconcile_netdata_native_runtime,
)
from app.services.tools.core import Plan

tool_name = "netdata"
phases = ("netdata_runtime",)


async def netdata(plan: Plan) -> bool:
    if not plan.desired.netdata_required:
        return False
    if converged(plan.desired, plan.settings):
        plan.details["netdata_runtime"] = {
            "skipped": True,
            "reason": "live netdata assets already match",
            "required": True,
        }
        return False
    plan.touching(tool_name)
    plan.details["netdata_runtime"] = await _reconcile(
        plan.settings, services=plan.services
    )
    return True


def converged(desired: DesiredState, settings: Settings) -> bool:
    if not desired.netdata_required:
        return True
    return netdata_native_runtime_converged(settings)


async def _reconcile(
    settings: Settings,
    *,
    services: Any,
) -> dict[str, Any]:
    return await reconcile_netdata_native_runtime(
        settings, services=services.runtime_services
    )
