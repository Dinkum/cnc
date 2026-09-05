from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from typing import Any, Sequence

from app.config import Settings
from app.models.entities import Backend


MIB = 1024 * 1024
DEFAULT_RESOURCE_MODE = "auto"
DEFAULT_RESOURCE_SIZE = "small"
RESOURCE_SIZES = ("small", "medium", "large")
RESOURCE_SIZE_WEIGHTS = {
    "small": 1,
    "medium": 2,
    "large": 4,
}
CPU_SHARES_PER_WEIGHT = 1024


def calculate_app_memory_budget_bytes(settings: Settings) -> int | None:
    memory_bytes = _detect_total_memory_bytes()
    if memory_bytes is None or memory_bytes < MIB:
        return None
    reserve_bytes = max(
        1, int(memory_bytes * settings.auto_memory_reserve_percent / 100)
    )
    return max(MIB, memory_bytes - reserve_bytes)


@dataclass(frozen=True)
class ResourceProfile:
    mode: str
    backend_count: int
    effective_backend_count: int
    host_cpu_count: int | None
    host_memory_bytes: int | None
    memory_high: str
    memory_max: str
    cpu_quota: str
    reason: str | None = None
    resource_size: str | None = None
    cpu_shares: int | None = None
    cpu_entitlement_percent_of_host: float | None = None
    host_memory_reserve_bytes: int | None = None
    app_memory_budget_bytes: int | None = None
    app_cpu_budget_percent: int | None = None
    size_counts: dict[str, int] = field(default_factory=dict)
    total_size_weight: int | None = None
    cpu_burst_factor: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "mode": self.mode,
            "backend_count": self.backend_count,
            "effective_backend_count": self.effective_backend_count,
            "host_cpu_count": self.host_cpu_count,
            "host_memory_bytes": self.host_memory_bytes,
            "memory_high": self.memory_high,
            "memory_max": self.memory_max,
            "cpu_quota": self.cpu_quota,
            "reason": self.reason,
            "resource_size": self.resource_size,
            "cpu_shares": self.cpu_shares,
            "cpu_entitlement_percent_of_host": (
                round(self.cpu_entitlement_percent_of_host, 1)
                if isinstance(self.cpu_entitlement_percent_of_host, (int, float))
                else None
            ),
            "host_memory_reserve_bytes": self.host_memory_reserve_bytes,
            "app_memory_budget_bytes": self.app_memory_budget_bytes,
            "app_cpu_budget_percent": self.app_cpu_budget_percent,
            "size_counts": dict(self.size_counts),
            "total_size_weight": self.total_size_weight,
            "cpu_burst_factor": self.cpu_burst_factor,
        }
        return payload


def normalize_resource_mode(value: str | None) -> str:
    normalized = (
        str(value or DEFAULT_RESOURCE_MODE).strip().lower() or DEFAULT_RESOURCE_MODE
    )
    return normalized if normalized in {"auto", "manual"} else DEFAULT_RESOURCE_MODE


def normalize_resource_size(value: str | None) -> str:
    normalized = (
        str(value or DEFAULT_RESOURCE_SIZE).strip().lower() or DEFAULT_RESOURCE_SIZE
    )
    return normalized if normalized in RESOURCE_SIZE_WEIGHTS else DEFAULT_RESOURCE_SIZE


def build_resource_profile(
    settings: Settings, backends: Sequence[Backend] | int
) -> ResourceProfile:
    backend_list = _normalize_backend_list(backends)
    backend_count = len(backend_list) if backend_list is not None else int(backends)
    effective_backend_count = max(1, backend_count)

    if not settings.auto_resource_limits:
        return ResourceProfile(
            mode="static",
            backend_count=backend_count,
            effective_backend_count=effective_backend_count,
            host_cpu_count=None,
            host_memory_bytes=None,
            memory_high=settings.default_memory_high,
            memory_max=settings.default_memory_max,
            cpu_quota=settings.default_cpu_quota,
            reason="auto limits disabled",
            resource_size=DEFAULT_RESOURCE_SIZE,
            cpu_shares=CPU_SHARES_PER_WEIGHT,
            cpu_burst_factor=settings.auto_cpu_burst_factor,
        )

    cpu_count = os.cpu_count()
    memory_bytes = _detect_total_memory_bytes()
    if cpu_count is None or memory_bytes is None or cpu_count < 1 or memory_bytes < MIB:
        return ResourceProfile(
            mode="static",
            backend_count=backend_count,
            effective_backend_count=effective_backend_count,
            host_cpu_count=cpu_count,
            host_memory_bytes=memory_bytes,
            memory_high=settings.default_memory_high,
            memory_max=settings.default_memory_max,
            cpu_quota=settings.default_cpu_quota,
            reason="host resources unavailable",
            resource_size=DEFAULT_RESOURCE_SIZE,
            cpu_shares=CPU_SHARES_PER_WEIGHT,
            cpu_burst_factor=settings.auto_cpu_burst_factor,
        )

    size_counts = _size_counts(backend_list, backend_count)
    total_weight = (
        sum(RESOURCE_SIZE_WEIGHTS[size] * count for size, count in size_counts.items())
        or 1
    )

    memory_reserve_bytes = max(
        1, int(memory_bytes * settings.auto_memory_reserve_percent / 100)
    )
    memory_budget_bytes = max(MIB, memory_bytes - memory_reserve_bytes)
    cpu_budget_percent = max(1, min(100, int(100 - settings.auto_cpu_reserve_percent)))
    small_profile = _auto_backend_profile(
        resource_size=DEFAULT_RESOURCE_SIZE,
        size_counts=size_counts,
        total_weight=total_weight,
        memory_budget_bytes=memory_budget_bytes,
        cpu_budget_percent=cpu_budget_percent,
        settings=settings,
        backend_count=backend_count,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
    )
    return ResourceProfile(
        mode="auto",
        backend_count=backend_count,
        effective_backend_count=effective_backend_count,
        host_cpu_count=cpu_count,
        host_memory_bytes=memory_bytes,
        memory_high=small_profile.memory_high,
        memory_max=small_profile.memory_max,
        cpu_quota=small_profile.cpu_quota,
        reason=None,
        resource_size=DEFAULT_RESOURCE_SIZE,
        cpu_shares=small_profile.cpu_shares,
        cpu_entitlement_percent_of_host=small_profile.cpu_entitlement_percent_of_host,
        host_memory_reserve_bytes=memory_reserve_bytes,
        app_memory_budget_bytes=memory_budget_bytes,
        app_cpu_budget_percent=cpu_budget_percent,
        size_counts=size_counts,
        total_size_weight=total_weight,
        cpu_burst_factor=settings.auto_cpu_burst_factor,
    )


def backend_resource_profile(
    backend: Backend, base_profile: ResourceProfile
) -> ResourceProfile:
    resource_mode = normalize_resource_mode(getattr(backend, "resource_mode", None))
    resource_size = normalize_resource_size(getattr(backend, "resource_size", None))
    if any(
        str(getattr(backend, key, "") or "").strip()
        for key in ("memory_high_override", "memory_max_override", "cpu_quota_override")
    ):
        resource_mode = "manual"

    if (
        base_profile.mode != "auto"
        or base_profile.app_memory_budget_bytes is None
        or base_profile.app_cpu_budget_percent is None
        or base_profile.total_size_weight in {None, 0}
    ):
        memory_high = (
            str(backend.memory_high_override or "").strip() or base_profile.memory_high
        )
        memory_max = (
            str(backend.memory_max_override or "").strip() or base_profile.memory_max
        )
        cpu_quota = (
            str(backend.cpu_quota_override or "").strip() or base_profile.cpu_quota
        )
        return ResourceProfile(
            mode=resource_mode if resource_mode == "manual" else base_profile.mode,
            backend_count=base_profile.backend_count,
            effective_backend_count=base_profile.effective_backend_count,
            host_cpu_count=base_profile.host_cpu_count,
            host_memory_bytes=base_profile.host_memory_bytes,
            memory_high=memory_high,
            memory_max=memory_max,
            cpu_quota=cpu_quota,
            reason=base_profile.reason,
            resource_size=resource_size,
            cpu_shares=CPU_SHARES_PER_WEIGHT * RESOURCE_SIZE_WEIGHTS[resource_size],
            cpu_entitlement_percent_of_host=_parse_percent_string(cpu_quota),
            host_memory_reserve_bytes=base_profile.host_memory_reserve_bytes,
            app_memory_budget_bytes=base_profile.app_memory_budget_bytes,
            app_cpu_budget_percent=base_profile.app_cpu_budget_percent,
            size_counts=dict(base_profile.size_counts),
            total_size_weight=base_profile.total_size_weight,
            cpu_burst_factor=base_profile.cpu_burst_factor,
        )

    computed = _auto_backend_profile(
        resource_size=resource_size,
        size_counts=base_profile.size_counts,
        total_weight=base_profile.total_size_weight or 1,
        memory_budget_bytes=base_profile.app_memory_budget_bytes,
        cpu_budget_percent=base_profile.app_cpu_budget_percent,
        settings=None,
        backend_count=base_profile.backend_count,
        cpu_count=base_profile.host_cpu_count,
        memory_bytes=base_profile.host_memory_bytes,
        cpu_burst_factor=base_profile.cpu_burst_factor,
    )
    memory_high = (
        str(backend.memory_high_override or "").strip() or computed.memory_high
    )
    memory_max = str(backend.memory_max_override or "").strip() or computed.memory_max
    cpu_quota = str(backend.cpu_quota_override or "").strip() or computed.cpu_quota
    return ResourceProfile(
        mode=resource_mode,
        backend_count=base_profile.backend_count,
        effective_backend_count=base_profile.effective_backend_count,
        host_cpu_count=base_profile.host_cpu_count,
        host_memory_bytes=base_profile.host_memory_bytes,
        memory_high=memory_high,
        memory_max=memory_max,
        cpu_quota=cpu_quota,
        reason=base_profile.reason,
        resource_size=resource_size,
        cpu_shares=computed.cpu_shares,
        cpu_entitlement_percent_of_host=computed.cpu_entitlement_percent_of_host,
        host_memory_reserve_bytes=base_profile.host_memory_reserve_bytes,
        app_memory_budget_bytes=base_profile.app_memory_budget_bytes,
        app_cpu_budget_percent=base_profile.app_cpu_budget_percent,
        size_counts=dict(base_profile.size_counts),
        total_size_weight=base_profile.total_size_weight,
        cpu_burst_factor=base_profile.cpu_burst_factor,
    )


def _auto_backend_profile(
    *,
    resource_size: str,
    size_counts: dict[str, int],
    total_weight: int,
    memory_budget_bytes: int,
    cpu_budget_percent: int,
    settings: Settings | None,
    backend_count: int,
    cpu_count: int | None,
    memory_bytes: int | None,
    cpu_burst_factor: float | None = None,
) -> ResourceProfile:
    weight = RESOURCE_SIZE_WEIGHTS[resource_size]
    per_size_floor_high_bytes = max(
        32 * MIB, (settings.auto_min_memory_high_mb if settings else 128) * MIB
    )
    memory_share_bytes = max(
        1, int(memory_budget_bytes * weight / max(1, total_weight))
    )
    memory_high_bytes = max(per_size_floor_high_bytes * weight, memory_share_bytes)
    memory_max_bytes = max(
        memory_high_bytes + (128 * MIB), int(memory_high_bytes * 1.5)
    )
    cpu_entitlement_percent = cpu_budget_percent * weight / max(1, total_weight)
    burst_factor = (
        cpu_burst_factor
        if cpu_burst_factor is not None
        else (settings.auto_cpu_burst_factor if settings else 2.0)
    )
    min_cpu_quota_percent = settings.auto_min_cpu_quota_percent if settings else 25
    cpu_quota_percent = int(
        min(
            100,
            cpu_budget_percent,
            max(
                min_cpu_quota_percent, math.ceil(cpu_entitlement_percent * burst_factor)
            ),
        )
    )
    return ResourceProfile(
        mode="auto",
        backend_count=backend_count,
        effective_backend_count=max(1, backend_count),
        host_cpu_count=cpu_count,
        host_memory_bytes=memory_bytes,
        memory_high=_to_mib_string(memory_high_bytes),
        memory_max=_to_mib_string(memory_max_bytes),
        cpu_quota=f"{cpu_quota_percent}%",
        resource_size=resource_size,
        cpu_shares=CPU_SHARES_PER_WEIGHT * weight,
        cpu_entitlement_percent_of_host=cpu_entitlement_percent,
        app_memory_budget_bytes=memory_budget_bytes,
        app_cpu_budget_percent=cpu_budget_percent,
        size_counts=dict(size_counts),
        total_size_weight=total_weight,
        cpu_burst_factor=burst_factor,
    )


def _normalize_backend_list(backends: Sequence[Backend] | int) -> list[Backend] | None:
    if isinstance(backends, int):
        return None
    return [backend for backend in backends if getattr(backend, "kind", None) == "app"]


def _size_counts(backends: list[Backend] | None, backend_count: int) -> dict[str, int]:
    counts = {size: 0 for size in RESOURCE_SIZES}
    if backends is None:
        counts[DEFAULT_RESOURCE_SIZE] = max(0, backend_count)
        return counts
    for backend in backends:
        counts[normalize_resource_size(getattr(backend, "resource_size", None))] += 1
    return counts


def _to_mib_string(raw_bytes: int) -> str:
    mib = max(1, int(raw_bytes / MIB))
    return f"{mib}M"


def _parse_percent_string(raw: str | None) -> float | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if value.endswith("%"):
        value = value[:-1]
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _detect_total_memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if not line.startswith("MemTotal:"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
        except (OSError, ValueError):
            return None

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        if isinstance(page_size, int) and isinstance(page_count, int):
            return page_size * page_count
    except (AttributeError, OSError, ValueError):
        return None
    return None
