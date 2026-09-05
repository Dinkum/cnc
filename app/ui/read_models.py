from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models.entities import Backend, Input


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
