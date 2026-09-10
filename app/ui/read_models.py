from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import ApplyRun, Backend, Input


@dataclass(frozen=True)
class DashboardScope:
    tab: str | None

    @property
    def full(self) -> bool:
        return self.tab is None

    @property
    def active_tab(self) -> str:
        return self.tab or "home"

    def includes(self, *tabs: str) -> bool:
        return self.full or self.tab in tabs

    @property
    def needs_status(self) -> bool:
        return self.includes("home", "outputs")

    @property
    def needs_settings(self) -> bool:
        return self.includes("settings")

    @property
    def needs_inputs(self) -> bool:
        return self.includes("inputs", "routing")

    @property
    def needs_routing_rows(self) -> bool:
        return self.includes("routing")

    @property
    def needs_outputs(self) -> bool:
        return self.includes("outputs")

    @property
    def needs_visible_entities(self) -> bool:
        return self.includes("inputs", "outputs")


@dataclass(frozen=True)
class DashboardRows:
    backends: list[Backend]
    inputs: list[Input]
    last_applied_at: datetime | None


def dashboard_status_fallback(current_version: str) -> dict[str, object]:
    return {
        "services": [],
        "nginx": {},
        "resource_profile": {},
        "resource_profile_drift": {"changed": False},
        "last_apply": {},
        "last_update": {},
        "update_version": {"current_version": current_version, "has_update": False},
        "host_metrics": {},
        "dashboard_overview": {},
        "app_network_isolation": {"checked": False},
    }


VALID_DASHBOARD_TABS = {"home", "inputs", "routing", "outputs", "settings"}


def dashboard_tab(value: object, default: str = "home") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_DASHBOARD_TABS else default


def dashboard_scope(active_tab: str | None) -> DashboardScope:
    tab = dashboard_tab(active_tab, "home") if active_tab is not None else None
    return DashboardScope(tab)


async def latest_successful_apply_at(session: AsyncSession) -> datetime | None:
    # Page freshness needs only a timestamp, not the potentially large snapshots.
    return (
        await session.execute(
            select(ApplyRun.created_at)
            .where(ApplyRun.status == "success")
            .order_by(ApplyRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
