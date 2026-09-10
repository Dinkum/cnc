"""Resource limits, metrics, and sizing presentation shared by UI surfaces."""

from __future__ import annotations

from app.models.entities import Backend
from app.services.resource_profile import (
    RESOURCE_SIZES,
    ResourceProfile,
    backend_resource_profile,
)
from app.ui.view_models import _format_bytes


def _parse_memory_limit_bytes(raw: object) -> int | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().replace(" ", "")
    if normalized == "":
        return None
    units = [
        ("TIB", 1024 * 1024 * 1024 * 1024),
        ("GIB", 1024 * 1024 * 1024),
        ("MIB", 1024 * 1024),
        ("KIB", 1024),
        ("TI", 1024 * 1024 * 1024 * 1024),
        ("GI", 1024 * 1024 * 1024),
        ("MI", 1024 * 1024),
        ("KI", 1024),
        ("TB", 1000 * 1000 * 1000 * 1000),
        ("GB", 1000 * 1000 * 1000),
        ("MB", 1000 * 1000),
        ("KB", 1000),
        ("T", 1024 * 1024 * 1024 * 1024),
        ("G", 1024 * 1024 * 1024),
        ("M", 1024 * 1024),
        ("K", 1024),
        ("B", 1),
    ]
    upper = normalized.upper()
    for suffix, multiplier in units:
        if not upper.endswith(suffix):
            continue
        number = normalized[: -len(suffix)]
        try:
            return int(float(number) * multiplier)
        except ValueError:
            return None
    try:
        return int(normalized)
    except ValueError:
        return None


def format_memory_limit(value: object) -> str:
    parsed = _parse_memory_limit_bytes(value)
    if parsed is None:
        return str(value or "-")
    return _format_bytes(parsed)


def _parse_percent_value(raw: object) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().rstrip("%")
    if normalized == "":
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def format_cpu_limit(
    raw_quota: object,
    *,
    host_cpu_count: object | None = None,
    entitlement_percent_of_host: object | None = None,
) -> str:
    cores = None
    quota_percent = _parse_percent_value(raw_quota)
    if quota_percent is not None:
        cores = quota_percent / 100.0
    host_count = (
        int(host_cpu_count)
        if isinstance(host_cpu_count, int) and host_cpu_count > 0
        else None
    )
    host_percent = (
        quota_percent / host_count
        if quota_percent is not None and host_count is not None
        else None
    )
    if host_percent is None and isinstance(entitlement_percent_of_host, (int, float)):
        host_percent = float(entitlement_percent_of_host)
    if host_percent is None and quota_percent is not None:
        host_percent = quota_percent
    if host_percent is not None:
        return f"{host_percent:.0f}% of total CPU"
    if quota_percent is not None:
        return f"{quota_percent:.0f}% quota"
    if cores is None:
        return str(raw_quota or "-")
    return str(raw_quota or "-")


def _format_rate(value_bps: object) -> str:
    if not isinstance(value_bps, (int, float)):
        return "-"
    bits_per_sec = max(0.0, float(value_bps) * 8.0)
    if bits_per_sec < 1000:
        return f"{bits_per_sec:.0f} bps"
    for unit in ["Kbps", "Mbps", "Gbps"]:
        bits_per_sec /= 1000.0
        if bits_per_sec < 1000 or unit == "Gbps":
            return f"{bits_per_sec:.1f} {unit}"
    return f"{bits_per_sec:.1f} Gbps"


def memory_soft_limit_percent_for_profile(
    backend: Backend, base_profile: ResourceProfile
) -> float | None:
    if str(backend.kind or "").lower() != "app":
        return None
    backend_profile = backend_resource_profile(backend, base_profile)
    soft_bytes = _parse_memory_limit_bytes(backend_profile.memory_high)
    hard_bytes = _parse_memory_limit_bytes(backend_profile.memory_max)
    if soft_bytes is None or hard_bytes is None or hard_bytes <= 0:
        return None
    return round(max(0.0, min(100.0, soft_bytes * 100.0 / hard_bytes)), 2)


def memory_limit_bytes_for_profile(
    backend: Backend, base_profile: ResourceProfile
) -> dict[str, int | None]:
    if str(backend.kind or "").lower() != "app":
        return {"soft": None, "hard": None}
    backend_profile = backend_resource_profile(backend, base_profile)
    return {
        "soft": _parse_memory_limit_bytes(backend_profile.memory_high),
        "hard": _parse_memory_limit_bytes(backend_profile.memory_max),
    }


def format_metric_data_size(value: object) -> str:
    amount = float(value) if isinstance(value, (int, float)) else None
    if amount is None or amount < 0:
        return "n/a"
    if amount >= 1024**3:
        return f"{amount / 1024**3:.1f} GB"
    if amount >= 1024**2:
        return f"{amount / 1024**2:.0f} MB"
    if amount >= 1024:
        return f"{amount / 1024:.0f} KB"
    return f"{amount:.0f} B"


def resource_size_matrix_rows(
    base_profile: ResourceProfile,
    *,
    host_cpu_count: object | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for size in RESOURCE_SIZES:
        profile = backend_resource_profile(
            Backend(
                name=f"resource-{size}",
                kind="app",
                resource_mode="auto",
                resource_size=size,
            ),
            base_profile,
        )
        rows.append(
            {
                "size": size,
                "memory_target": format_memory_limit(profile.memory_high),
                "memory_cap": format_memory_limit(profile.memory_max),
                "cpu_limit": format_cpu_limit(
                    profile.cpu_quota,
                    host_cpu_count=host_cpu_count,
                    entitlement_percent_of_host=profile.cpu_entitlement_percent_of_host,
                ),
            }
        )
    return rows
