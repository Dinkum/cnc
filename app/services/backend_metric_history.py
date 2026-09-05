from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from math import floor
import re

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    Backend,
    BackendResourceSample,
    ControlEvent,
    HostResourceSample,
)
from app.services.control_events import event_applies_to_backend


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit_kind: str
    description: str


@dataclass(frozen=True)
class TimeframeSpec:
    key: str
    label: str
    hours: int
    target_points: int
    smoothing_window: int


METRIC_SPECS: dict[str, MetricSpec] = {
    "cpu": MetricSpec(
        key="cpu",
        label="CPU",
        unit_kind="percent",
        description="CPU usage as a share of total host CPU.",
    ),
    "memory": MetricSpec(
        key="memory",
        label="Memory",
        unit_kind="percent",
        description="Memory usage against the output limit.",
    ),
    "disk": MetricSpec(
        key="disk",
        label="Disk",
        unit_kind="bytes",
        description="Disk used by this output's managed sandbox and declared host mounts.",
    ),
    "network": MetricSpec(
        key="network",
        label="Network",
        unit_kind="rate",
        description="Network throughput for this output.",
    ),
}

METRIC_ALIASES = {
    "cpu_entitlement": "cpu",
    "cpu_host": "cpu",
    "disk_usage": "disk",
    "disk_used": "disk",
    "memory_percent": "memory",
    "memory_used": "memory",
}

HOST_METRIC_SPECS: dict[str, MetricSpec] = {
    "cpu": MetricSpec(
        key="cpu",
        label="CPU",
        unit_kind="percent",
        description="Host CPU usage.",
    ),
    "memory": MetricSpec(
        key="memory",
        label="Memory",
        unit_kind="percent",
        description="Host memory usage.",
    ),
    "disk": MetricSpec(
        key="disk",
        label="Disk",
        unit_kind="percent",
        description="Root filesystem usage on the CNC host.",
    ),
    "network": MetricSpec(
        key="network",
        label="Network",
        unit_kind="rate",
        description="Host network throughput.",
    ),
}

TIMEFRAME_SPECS: dict[str, TimeframeSpec] = {
    "hour": TimeframeSpec(
        key="hour", label="last hour", hours=1, target_points=60, smoothing_window=3
    ),
    "day": TimeframeSpec(
        key="day", label="last day", hours=24, target_points=288, smoothing_window=4
    ),
    "week": TimeframeSpec(
        key="week",
        label="last week",
        hours=24 * 7,
        target_points=168,
        smoothing_window=7,
    ),
    "month": TimeframeSpec(
        key="month",
        label="last month",
        hours=24 * 30,
        target_points=240,
        smoothing_window=10,
    ),
}
TIMEFRAME_ALIASES = {
    "1h": "hour",
    "60m": "hour",
    "24h": "day",
    "7d": "week",
    "30d": "month",
}

DEFAULT_METRIC_KEY = "cpu"
DEFAULT_TIMEFRAME_KEY = "day"
MIN_TARGET_POINTS = 24
MAX_TARGET_POINTS = 800
POINTS_PER_PIXEL = 0.25
CHART_EVENT_KINDS = {"auto_size_resized"}
CHART_EVENT_LABELS = {
    "auto_size_resized": "Auto-size resize",
}
CHART_EVENT_LIMIT = 48
CHART_EVENT_SCAN_LIMIT = 192
_RESIZE_CHANGE_PATTERN = re.compile(
    r"(?P<backend>[^:,()]+):\s*(?P<before>[-_\w]+)\s*->\s*(?P<after>[-_\w]+)"
)


def _metric_spec(metric_key: str | None) -> MetricSpec:
    normalized = str(metric_key or "").strip().lower()
    normalized = METRIC_ALIASES.get(normalized, normalized)
    return METRIC_SPECS.get(normalized, METRIC_SPECS["cpu"])


def _host_metric_spec(metric_key: str | None) -> MetricSpec:
    normalized = str(metric_key or "").strip().lower()
    normalized = METRIC_ALIASES.get(normalized, normalized)
    return HOST_METRIC_SPECS.get(normalized, HOST_METRIC_SPECS["cpu"])


def _timeframe_spec(timeframe_key: str | None) -> TimeframeSpec:
    normalized = str(timeframe_key or "").strip().lower()
    normalized = TIMEFRAME_ALIASES.get(normalized, normalized)
    return TIMEFRAME_SPECS.get(normalized, TIMEFRAME_SPECS[DEFAULT_TIMEFRAME_KEY])


def _field(row: object, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    return getattr(row, key, None)


def _column(model: object, name: str) -> object:
    return getattr(model, name).label(name)


def _sample_bucket_start(row: object) -> datetime:
    value = _field(row, "bucket_start")
    assert isinstance(value, datetime)
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _sample_recorded_at(row: object) -> datetime:
    value = _field(row, "sampled_at")
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return _sample_bucket_start(row)


def _rollup_attrs(model: object, prefix: str) -> tuple[object, ...]:
    return (
        _column(model, prefix),
        _column(model, f"{prefix}_count"),
        _column(model, f"{prefix}_sum"),
        _column(model, f"{prefix}_min"),
        _column(model, f"{prefix}_max"),
        _column(model, f"{prefix}_first"),
        _column(model, f"{prefix}_last"),
    )


def _backend_sample_load_attrs(metric: MetricSpec) -> tuple[object, ...]:
    if metric.key == "cpu":
        return (
            _column(BackendResourceSample, "bucket_start"),
            _column(BackendResourceSample, "sampled_at"),
            _column(BackendResourceSample, "cpu_entitlement_percent_of_host"),
            _column(BackendResourceSample, "cpu_limit_percent_of_host"),
            *_rollup_attrs(BackendResourceSample, "cpu_percent_of_host"),
            *_rollup_attrs(BackendResourceSample, "cpu_percent_of_entitlement"),
        )
    if metric.key == "memory":
        return (
            _column(BackendResourceSample, "bucket_start"),
            _column(BackendResourceSample, "sampled_at"),
            _column(BackendResourceSample, "memory_max_bytes"),
            *_rollup_attrs(BackendResourceSample, "memory_current_bytes"),
            *_rollup_attrs(BackendResourceSample, "memory_percent"),
        )
    if metric.key == "disk":
        return (
            _column(BackendResourceSample, "bucket_start"),
            _column(BackendResourceSample, "sampled_at"),
            _column(BackendResourceSample, "disk_usage_complete"),
            *_rollup_attrs(BackendResourceSample, "disk_usage_bytes"),
        )
    if metric.key == "network":
        return (
            _column(BackendResourceSample, "bucket_start"),
            _column(BackendResourceSample, "sampled_at"),
            _column(BackendResourceSample, "network_rx_bytes"),
            _column(BackendResourceSample, "network_tx_bytes"),
            *_rollup_attrs(BackendResourceSample, "network_rx_bps"),
            *_rollup_attrs(BackendResourceSample, "network_tx_bps"),
            *_rollup_attrs(BackendResourceSample, "network_total_bps"),
        )
    return (_column(BackendResourceSample, "bucket_start"),)


def _host_sample_load_attrs(metric: MetricSpec) -> tuple[object, ...]:
    if metric.key in {"cpu", "memory", "disk"}:
        return (
            _column(HostResourceSample, "bucket_start"),
            _column(HostResourceSample, "sampled_at"),
            *_rollup_attrs(HostResourceSample, f"{metric.key}_percent"),
        )
    if metric.key == "network":
        return (
            _column(HostResourceSample, "bucket_start"),
            _column(HostResourceSample, "sampled_at"),
            *_rollup_attrs(HostResourceSample, "network_rx_bps"),
            *_rollup_attrs(HostResourceSample, "network_tx_bps"),
            *_rollup_attrs(HostResourceSample, "network_total_bps"),
        )
    return (_column(HostResourceSample, "bucket_start"),)


def _target_point_count(timeframe: TimeframeSpec, chart_width_px: int | None) -> int:
    if chart_width_px is None or chart_width_px <= 0:
        return timeframe.target_points
    width_target = int(round(chart_width_px * POINTS_PER_PIXEL))
    return max(MIN_TARGET_POINTS, min(MAX_TARGET_POINTS, width_target))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _event_details(event: ControlEvent) -> dict[str, object]:
    try:
        payload = json.loads(event.details_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resize_change_payload(
    change: dict[str, object],
    *,
    event: ControlEvent,
) -> dict[str, object] | None:
    backend = str(change.get("backend") or "").strip()
    previous_size = str(change.get("previous_size") or "").strip()
    next_size = str(change.get("next_size") or "").strip()
    if not backend or not previous_size or not next_size:
        return None
    created_at = _as_utc(event.created_at)
    if created_at is None:
        return None
    return {
        "timestamp": created_at.isoformat(),
        "kind": event.kind,
        "label": backend,
        "summary": backend,
        "severity": event.severity or "info",
        "source": event.source or "event",
        "backend": backend,
        "related_backends": [backend],
        "before": previous_size,
        "after": next_size,
        "rows": [
            {"label": "before", "value": previous_size},
            {"label": "after", "value": next_size},
        ],
    }


def _resize_changes_from_details(event: ControlEvent) -> list[dict[str, object]]:
    changes = _event_details(event).get("changes")
    if not isinstance(changes, list):
        return []
    return [item for item in changes if isinstance(item, dict)]


def _resize_changes_from_subevents(event: ControlEvent) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    for item in event.subevents:
        if str(item.get("label") or "").strip().lower() != "changes":
            continue
        value = str(item.get("value") or "")
        for match in _RESIZE_CHANGE_PATTERN.finditer(value):
            changes.append(
                {
                    "backend": match.group("backend").strip(),
                    "previous_size": match.group("before").strip(),
                    "next_size": match.group("after").strip(),
                }
            )
    return changes


def _resize_event_payloads(
    event: ControlEvent, *, backend_name: str | None
) -> list[dict[str, object]]:
    changes = _resize_changes_from_details(event) or _resize_changes_from_subevents(
        event
    )
    if backend_name:
        normalized_backend_name = str(backend_name or "").strip()
        changes = [
            change
            for change in changes
            if str(change.get("backend") or "").strip() == normalized_backend_name
        ]
    payloads = [_resize_change_payload(change, event=event) for change in changes]
    return [item for item in payloads if item is not None]


def _chart_event_rows(event: ControlEvent) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in event.subevents:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label or not value:
            continue
        rows.append({"label": label, "value": value})
        if len(rows) >= 4:
            break
    return rows


def _chart_event_payloads(
    event: ControlEvent, *, backend_name: str | None
) -> list[dict[str, object]]:
    if event.kind == "auto_size_resized":
        resize_payloads = _resize_event_payloads(event, backend_name=backend_name)
        if resize_payloads:
            return resize_payloads
    created_at = _as_utc(event.created_at)
    if created_at is None:
        return []
    summary = str(event.summary or "").strip() or str(event.kind or "event").replace(
        "_", " "
    )
    return [
        {
            "timestamp": created_at.isoformat(),
            "kind": event.kind,
            "label": CHART_EVENT_LABELS.get(str(event.kind or ""), summary),
            "summary": summary,
            "severity": event.severity or "info",
            "source": event.source or "event",
            "backend": event.backend_name,
            "related_backends": event.related_backends,
            "rows": _chart_event_rows(event),
        }
    ]


async def _chart_events_for_range(
    session: AsyncSession,
    *,
    start_at: datetime,
    end_at: datetime,
    backend_name: str | None = None,
) -> list[dict[str, object]]:
    query_limit = CHART_EVENT_SCAN_LIMIT if backend_name else CHART_EVENT_LIMIT
    filters = [
        ControlEvent.kind.in_(CHART_EVENT_KINDS),
        ControlEvent.created_at >= start_at,
        ControlEvent.created_at <= end_at,
    ]
    if backend_name:
        filters.append(
            or_(
                ControlEvent.backend_name == backend_name,
                ControlEvent.affects_all.is_(True),
                ControlEvent.related_backends_json.like(f'%"{backend_name}"%'),
            )
        )
    events = list(
        (
            await session.execute(
                select(ControlEvent)
                .where(*filters)
                .order_by(ControlEvent.created_at.desc())
                .limit(query_limit)
            )
        ).scalars()
    )
    events.reverse()
    if backend_name:
        events = [
            event for event in events if event_applies_to_backend(event, backend_name)
        ]
    payloads: list[dict[str, object]] = []
    for event in events:
        payloads.extend(_chart_event_payloads(event, backend_name=backend_name))
    return payloads[-CHART_EVENT_LIMIT:]


def _sample_rollup_for_prefix(
    sample: object,
    *,
    prefix: str,
    legacy_value: object,
) -> dict[str, float | int] | None:
    count = int(_field(sample, f"{prefix}_count") or 0)
    total = _field(sample, f"{prefix}_sum")
    minimum = _field(sample, f"{prefix}_min")
    maximum = _field(sample, f"{prefix}_max")
    first = _field(sample, f"{prefix}_first")
    last = _field(sample, f"{prefix}_last")
    if count > 0 and isinstance(total, (int, float)):
        avg = float(total) / count
        return {
            "value": avg,
            "min": float(minimum) if isinstance(minimum, (int, float)) else avg,
            "max": float(maximum) if isinstance(maximum, (int, float)) else avg,
            "first": float(first) if isinstance(first, (int, float)) else avg,
            "last": float(last) if isinstance(last, (int, float)) else avg,
            "count": count,
        }
    if isinstance(legacy_value, (int, float)):
        value = float(legacy_value)
        return {
            "value": value,
            "min": value,
            "max": value,
            "first": value,
            "last": value,
            "count": 1,
        }
    return None


def _sample_rollup(sample: object, metric: MetricSpec) -> dict[str, float | int] | None:
    if metric.key == "cpu":
        return _sample_rollup_for_prefix(
            sample,
            prefix="cpu_percent_of_host",
            legacy_value=_field(sample, "cpu_percent_of_host"),
        )
    if metric.key == "memory":
        return _sample_rollup_for_prefix(
            sample,
            prefix="memory_percent",
            legacy_value=_field(sample, "memory_percent"),
        )
    if metric.key == "disk":
        return _sample_rollup_for_prefix(
            sample,
            prefix="disk_usage_bytes",
            legacy_value=_field(sample, "disk_usage_bytes"),
        )
    if metric.key == "network":
        return _sample_rollup_for_prefix(
            sample,
            prefix="network_total_bps",
            legacy_value=_field(sample, "network_total_bps"),
        )
    return None


def _sample_cpu_pressure_rollup(sample: object) -> dict[str, float | int] | None:
    return _sample_rollup_for_prefix(
        sample,
        prefix="cpu_percent_of_entitlement",
        legacy_value=_field(sample, "cpu_percent_of_entitlement"),
    )


def _host_sample_rollup(
    sample: object, metric: MetricSpec
) -> dict[str, float | int] | None:
    if metric.key in {"cpu", "memory", "disk"}:
        prefix = f"{metric.key}_percent"
        legacy_value = _field(sample, prefix)
    elif metric.key == "network":
        prefix = "network_total_bps"
        legacy_value = _field(sample, "network_total_bps")
    else:
        return None

    return _sample_rollup_for_prefix(sample, prefix=prefix, legacy_value=legacy_value)


def _live_metric_value(
    metrics: dict[str, object] | None, metric: MetricSpec
) -> float | None:
    if not isinstance(metrics, dict):
        return None
    if metric.key == "cpu":
        value = metrics.get("cpu_percent_of_host")
    elif metric.key == "memory" and metric.unit_kind == "bytes":
        value = metrics.get("memory_current_bytes")
    elif metric.key == "memory":
        value = metrics.get("memory_percent")
    elif metric.key == "disk":
        value = metrics.get("disk_usage_bytes")
    elif metric.key == "network":
        value = metrics.get("network_total_bps")
    else:
        value = None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _live_host_metric_value(
    metrics: dict[str, object] | None, metric: MetricSpec
) -> float | None:
    if not isinstance(metrics, dict):
        return None
    if metric.key == "cpu":
        value = metrics.get("cpu_percent")
    elif metric.key == "memory":
        value = metrics.get("memory_percent")
    elif metric.key == "disk":
        value = metrics.get("disk_percent")
    elif metric.key == "network":
        value = metrics.get("network_total_bps")
    else:
        value = None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _live_network_direction_value(
    metrics: dict[str, object] | None, key: str
) -> float | None:
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(key)
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    return None


def _sample_direction_rollups(sample: object) -> dict[str, float] | None:
    rx_rollup = _sample_rollup_for_prefix(
        sample,
        prefix="network_rx_bps",
        legacy_value=_field(sample, "network_rx_bps"),
    )
    tx_rollup = _sample_rollup_for_prefix(
        sample,
        prefix="network_tx_bps",
        legacy_value=_field(sample, "network_tx_bps"),
    )
    if rx_rollup is None or tx_rollup is None:
        return None
    return {
        "network_rx_bps": float(rx_rollup["value"]),
        "network_tx_bps": float(tx_rollup["value"]),
    }


def _network_direction_rates(
    previous_sample: object | None,
    sample: object,
) -> dict[str, float] | None:
    if previous_sample is None:
        return None
    current_rx_bytes = _field(sample, "network_rx_bytes")
    current_tx_bytes = _field(sample, "network_tx_bytes")
    previous_rx_bytes = _field(previous_sample, "network_rx_bytes")
    previous_tx_bytes = _field(previous_sample, "network_tx_bytes")
    if current_rx_bytes is None or current_tx_bytes is None:
        return None
    if previous_rx_bytes is None or previous_tx_bytes is None:
        return None

    elapsed = (
        _sample_recorded_at(sample) - _sample_recorded_at(previous_sample)
    ).total_seconds()
    if elapsed <= 0:
        return None

    if not all(
        isinstance(value, (int, float))
        for value in (
            current_rx_bytes,
            current_tx_bytes,
            previous_rx_bytes,
            previous_tx_bytes,
        )
    ):
        return None
    rx_delta = float(current_rx_bytes) - float(previous_rx_bytes)
    tx_delta = float(current_tx_bytes) - float(previous_tx_bytes)
    if rx_delta < 0 or tx_delta < 0:
        return None
    return {
        "network_rx_bps": rx_delta / elapsed,
        "network_tx_bps": tx_delta / elapsed,
    }


def _nice_axis_min(min_val: float, max_val: float, unit_kind: str) -> float:
    """Return a nice lower bound for the Y axis, auto-scaling for bytes."""
    if unit_kind != "bytes" or min_val <= 0 or max_val <= 0:
        return 0.0
    # Only lift the baseline when data lives well above zero.
    if min_val / max_val < 0.35:
        return 0.0
    baseline = min_val * 0.75
    step = 1.0
    while step * 2 <= baseline:
        step *= 2.0
    return step * floor(baseline / step)


def _nice_decimal_axis_max(value: float) -> float:
    step = 1.0
    for factor in (1.0, 2.0, 5.0):
        if value <= factor:
            return factor
    while step * 10.0 < value:
        step *= 10.0
    for factor in (1.0, 2.0, 5.0, 10.0):
        candidate = factor * step
        if value <= candidate:
            return candidate
    return step * 10.0


def _nice_percent_axis_max(value: float, *, metric_key: str) -> float:
    if metric_key in {"memory", "disk"}:
        return 100.0
    if value <= 0:
        return 1.0
    padded = max(value * 1.2, value + 0.1)
    for stop in (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0):
        if padded <= stop:
            return stop
    return _nice_decimal_axis_max(padded)


def _nice_axis_max(value: float, *, unit_kind: str, metric_key: str = "") -> float:
    if value <= 0:
        return (
            _nice_percent_axis_max(value, metric_key=metric_key)
            if unit_kind == "percent"
            else 1.0
        )
    if unit_kind == "percent":
        return _nice_percent_axis_max(value, metric_key=metric_key)
    if unit_kind == "rate":
        return _nice_decimal_axis_max(value * 1.2)
    step = 1.0
    while step < value:
        step *= 2.0
    return step


def _bucket_points(
    raw_points: list[dict[str, object]],
    *,
    target_points: int,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, object]]:
    if len(raw_points) <= target_points:
        return raw_points

    range_seconds = max(1.0, (end_at - start_at).total_seconds())
    bucket_count = min(target_points, len(raw_points))
    bucket_seconds = max(60.0, range_seconds / bucket_count)
    buckets: list[list[dict[str, object]]] = [[] for _ in range(bucket_count)]
    for point in raw_points:
        timestamp = point["timestamp"]
        assert isinstance(timestamp, datetime)
        offset_seconds = max(
            0.0, min(range_seconds, (timestamp - start_at).total_seconds())
        )
        index = min(bucket_count - 1, int(offset_seconds // bucket_seconds))
        buckets[index].append(point)

    aggregated: list[dict[str, object]] = []
    for bucket in buckets:
        if not bucket:
            continue
        total_count = sum(int(item.get("count") or 1) for item in bucket)
        weighted_sum = sum(
            float(item["value"]) * int(item.get("count") or 1) for item in bucket
        )
        timestamps = [item["timestamp"] for item in bucket]
        avg = weighted_sum / total_count if total_count else float(bucket[-1]["value"])
        aggregated_point = {
            "timestamp": timestamps[-1],
            "value": avg,
            "min": min(float(item["min"]) for item in bucket),
            "max": max(float(item["max"]) for item in bucket),
            "first": float(bucket[0].get("first", bucket[0]["value"])),
            "last": float(bucket[-1].get("last", bucket[-1]["value"])),
            "count": total_count,
            "is_live": any(bool(item.get("is_live")) for item in bucket),
        }
        for optional_key in (
            "network_rx_bps",
            "network_tx_bps",
            "cpu_pressure",
            "cpu_limit_percent_of_host",
            "memory_percent",
        ):
            optional_items = [
                item
                for item in bucket
                if isinstance(item.get(optional_key), (int, float))
            ]
            optional_count = sum(int(item.get("count") or 1) for item in optional_items)
            if optional_count > 0:
                aggregated_point[optional_key] = (
                    sum(
                        float(item[optional_key]) * int(item.get("count") or 1)
                        for item in optional_items
                    )
                    / optional_count
                )
        entitlement_items = [
            item
            for item in bucket
            if isinstance(item.get("cpu_entitlement_percent_of_host"), (int, float))
        ]
        if entitlement_items:
            aggregated_point["cpu_entitlement_percent_of_host"] = entitlement_items[-1][
                "cpu_entitlement_percent_of_host"
            ]
        memory_limit_items = [
            item for item in bucket if isinstance(item.get("memory_limit_bytes"), int)
        ]
        if memory_limit_items:
            aggregated_point["memory_limit_bytes"] = memory_limit_items[-1][
                "memory_limit_bytes"
            ]
        disk_complete_items = [
            item for item in bucket if isinstance(item.get("disk_usage_complete"), bool)
        ]
        if disk_complete_items:
            aggregated_point["disk_usage_complete"] = all(
                bool(item["disk_usage_complete"]) for item in disk_complete_items
            )
        aggregated.append(aggregated_point)
    return aggregated


def _is_incomplete_chart_bucket(item: dict[str, object], *, now_utc: datetime) -> bool:
    timestamp = item["timestamp"]
    assert isinstance(timestamp, datetime)
    current_minute = now_utc.replace(second=0, microsecond=0)
    return bool(item.get("is_live")) or timestamp >= current_minute


def _visual_value(
    item: dict[str, object],
    *,
    now_utc: datetime,
    previous_visual_value: float | None,
) -> float:
    value = float(item["value"])
    if (
        _is_incomplete_chart_bucket(item, now_utc=now_utc)
        and previous_visual_value is not None
    ):
        return previous_visual_value
    return value


def _visual_extrema(
    item: dict[str, object],
    *,
    now_utc: datetime,
    visual_value: float,
) -> tuple[float, float]:
    if _is_incomplete_chart_bucket(item, now_utc=now_utc):
        return visual_value, visual_value
    return float(item["min"]), float(item["max"])


def _axis_peak_value(points: list[dict[str, object]]) -> float | None:
    if not points:
        return None
    return max(
        max(
            float(item["value"]),
            float(item.get("max", item["value"])),
        )
        for item in points
    )


def _axis_visual_peak_value(points: list[dict[str, object]]) -> float | None:
    if not points:
        return None
    return max(
        max(
            float(item.get("visual_value", item["value"])),
            float(item.get("visual_max", item.get("max", item["value"]))),
        )
        for item in points
    )


def _axis_visual_min_value(points: list[dict[str, object]]) -> float:
    if not points:
        return 0.0
    return min(
        float(item.get("visual_min", item.get("min", item["value"]))) for item in points
    )


def _cpu_limit_series(
    raw_points: list[dict[str, object]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, object]]:
    limit_points: list[tuple[datetime, float]] = []
    for item in raw_points:
        value = item.get("cpu_limit_percent_of_host")
        timestamp = item.get("timestamp")
        if not isinstance(value, (int, float)) or not isinstance(timestamp, datetime):
            continue
        rounded_value = round(float(value), 1)
        if limit_points and abs(limit_points[-1][1] - rounded_value) < 0.5:
            continue
        limit_points.append((timestamp, rounded_value))

    series: list[dict[str, object]] = []
    if not limit_points:
        return series

    first_timestamp, first_value = limit_points[0]
    previous_value = first_value
    series_start = start_at if first_timestamp <= start_at else first_timestamp
    series.append({"timestamp": series_start.isoformat(), "value": first_value})
    for timestamp, value in limit_points[1:]:
        series.append({"timestamp": timestamp.isoformat(), "value": previous_value})
        series.append({"timestamp": timestamp.isoformat(), "value": value})
        previous_value = value
    series.append({"timestamp": end_at.isoformat(), "value": previous_value})
    return series


def _memory_limit_series(
    raw_points: list[dict[str, object]],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, object]]:
    limit_points: list[tuple[datetime, int]] = []
    for item in raw_points:
        value = item.get("memory_limit_bytes")
        timestamp = item.get("timestamp")
        if (
            not isinstance(value, int)
            or value <= 0
            or not isinstance(timestamp, datetime)
        ):
            continue
        if limit_points and limit_points[-1][1] == value:
            continue
        limit_points.append((timestamp, value))

    if len({value for _timestamp, value in limit_points}) <= 1:
        return []

    series: list[dict[str, object]] = []
    first_value = limit_points[0][1]
    previous_value = first_value
    series.append({"timestamp": start_at.isoformat(), "value": first_value})
    for timestamp, value in limit_points[1:]:
        series.append({"timestamp": timestamp.isoformat(), "value": previous_value})
        series.append({"timestamp": timestamp.isoformat(), "value": value})
        previous_value = value
    series.append({"timestamp": end_at.isoformat(), "value": previous_value})
    return series


def _memory_limit_changes_in_range(
    samples: list[object],
    live_metrics: dict[str, object] | None,
) -> bool:
    values: list[int] = []
    for sample in samples:
        memory_max = _field(sample, "memory_max_bytes")
        if isinstance(memory_max, int) and memory_max > 0:
            values.append(memory_max)
    if isinstance(live_metrics, dict):
        live_memory_max = live_metrics.get("memory_max_bytes")
        if isinstance(live_memory_max, int) and live_memory_max > 0:
            values.append(live_memory_max)
    return len(set(values)) > 1


async def build_backend_metric_history(
    session: AsyncSession,
    backend: Backend,
    *,
    metric_key: str | None,
    timeframe_key: str | None,
    chart_width_px: int | None = None,
    live_metrics: dict[str, object] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    metric = _metric_spec(metric_key)
    timeframe = _timeframe_spec(timeframe_key)
    target_points = _target_point_count(timeframe, chart_width_px)
    now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)

    if str(backend.kind or "").lower() not in {"app", "shield"}:
        return {
            "available": False,
            "metric": metric.__dict__,
            "timeframe": timeframe.__dict__,
            "range_start_at": (now_utc - timedelta(hours=timeframe.hours)).isoformat(),
            "range_end_at": now_utc.isoformat(),
            "series": [],
            "chart_events": [],
            "summary": {},
            "note": "Resource history is available for app and shield outputs only.",
            "sample_cadence": "1min",
            "target_points": target_points,
        }

    cutoff = now_utc - timedelta(hours=timeframe.hours)
    chart_events = await _chart_events_for_range(
        session,
        start_at=cutoff,
        end_at=now_utc,
        backend_name=backend.name,
    )
    samples = (
        (
            await session.execute(
                select(*_backend_sample_load_attrs(metric))
                .where(
                    BackendResourceSample.backend_id == backend.id,
                    BackendResourceSample.bucket_start >= cutoff,
                    BackendResourceSample.bucket_start <= now_utc,
                )
                .order_by(BackendResourceSample.bucket_start.asc())
            )
        )
        .mappings()
        .all()
    )
    if metric.key == "memory" and _memory_limit_changes_in_range(
        list(samples), live_metrics
    ):
        metric = MetricSpec(
            key="memory",
            label="Memory",
            unit_kind="bytes",
            description="Memory used by this output.",
        )
    previous_network_sample = None
    if metric.key == "network":
        previous_network_sample = (
            (
                await session.execute(
                    select(
                        _column(BackendResourceSample, "bucket_start"),
                        _column(BackendResourceSample, "sampled_at"),
                        _column(BackendResourceSample, "network_rx_bytes"),
                        _column(BackendResourceSample, "network_tx_bytes"),
                    )
                    .where(
                        BackendResourceSample.backend_id == backend.id,
                        BackendResourceSample.bucket_start < cutoff,
                    )
                    .order_by(BackendResourceSample.bucket_start.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

    raw_points: list[dict[str, object]] = []
    for sample in samples:
        memory_percent_rollup = (
            _sample_rollup(sample, METRIC_SPECS["memory"])
            if metric.key == "memory"
            else None
        )
        if metric.key == "memory" and metric.unit_kind == "bytes":
            rollup = _sample_rollup_for_prefix(
                sample,
                prefix="memory_current_bytes",
                legacy_value=_field(sample, "memory_current_bytes"),
            )
            if rollup is None:
                continue
        else:
            rollup = _sample_rollup(sample, metric)
        if rollup is None:
            continue
        cpu_pressure_rollup = (
            _sample_cpu_pressure_rollup(sample) if metric.key == "cpu" else None
        )
        timestamp = _sample_bucket_start(sample)
        point = {
            "timestamp": timestamp,
            "value": float(rollup["value"]),
            "min": float(rollup["min"]),
            "max": float(rollup["max"]),
            "first": float(rollup["first"]),
            "last": float(rollup["last"]),
            "count": int(rollup["count"]),
            "is_live": False,
        }
        if metric.key == "cpu":
            if cpu_pressure_rollup is not None:
                point["cpu_pressure"] = float(cpu_pressure_rollup["value"])
            limit_percent = _field(sample, "cpu_limit_percent_of_host")
            if not isinstance(limit_percent, (int, float)):
                limit_percent = _field(sample, "cpu_entitlement_percent_of_host")
            if isinstance(limit_percent, (int, float)):
                point["cpu_limit_percent_of_host"] = float(limit_percent)
            entitlement_percent = _field(sample, "cpu_entitlement_percent_of_host")
            if isinstance(entitlement_percent, (int, float)):
                point["cpu_entitlement_percent_of_host"] = float(entitlement_percent)
        if metric.key == "disk":
            disk_usage_complete = _field(sample, "disk_usage_complete")
            if isinstance(disk_usage_complete, bool):
                point["disk_usage_complete"] = disk_usage_complete
        if metric.key == "memory" and metric.unit_kind == "bytes":
            memory_limit = _field(sample, "memory_max_bytes")
            if isinstance(memory_limit, int) and memory_limit > 0:
                point["memory_limit_bytes"] = memory_limit
            if memory_percent_rollup is not None:
                point["memory_percent"] = float(memory_percent_rollup["value"])
        if metric.key == "network":
            direction_rates = _sample_direction_rollups(
                sample
            ) or _network_direction_rates(previous_network_sample, sample)
            if direction_rates is not None:
                point.update(direction_rates)
            previous_network_sample = sample
        raw_points.append(point)

    live_value = _live_metric_value(live_metrics, metric)
    if live_value is not None:
        if not raw_points or now_utc > raw_points[-1]["timestamp"]:
            live_point = {
                "timestamp": now_utc,
                "value": live_value,
                "min": live_value,
                "max": live_value,
                "first": live_value,
                "last": live_value,
                "count": 1,
                "is_live": True,
            }
            if metric.key == "network":
                live_rx_bps = _live_network_direction_value(
                    live_metrics, "network_rx_bps"
                )
                live_tx_bps = _live_network_direction_value(
                    live_metrics, "network_tx_bps"
                )
                if live_rx_bps is not None:
                    live_point["network_rx_bps"] = live_rx_bps
                if live_tx_bps is not None:
                    live_point["network_tx_bps"] = live_tx_bps
            if metric.key == "cpu" and isinstance(live_metrics, dict):
                live_pressure = live_metrics.get("cpu_percent")
                live_entitlement = live_metrics.get("cpu_entitlement_percent_of_host")
                if isinstance(live_pressure, (int, float)):
                    live_point["cpu_pressure"] = float(live_pressure)
                live_limit = live_metrics.get("cpu_limit_percent_of_host")
                if not isinstance(live_limit, (int, float)):
                    live_limit = live_metrics.get("cpu_entitlement_percent_of_host")
                if isinstance(live_limit, (int, float)):
                    live_point["cpu_limit_percent_of_host"] = float(live_limit)
                if isinstance(live_entitlement, (int, float)):
                    live_point["cpu_entitlement_percent_of_host"] = float(
                        live_entitlement
                    )
            if (
                metric.key == "memory"
                and metric.unit_kind == "bytes"
                and isinstance(live_metrics, dict)
            ):
                live_memory_percent = live_metrics.get("memory_percent")
                live_memory_max = live_metrics.get("memory_max_bytes")
                if isinstance(live_memory_percent, (int, float)):
                    live_point["memory_percent"] = float(live_memory_percent)
                if isinstance(live_memory_max, int) and live_memory_max > 0:
                    live_point["memory_limit_bytes"] = live_memory_max
            raw_points.append(live_point)

    memory_limit_series = (
        _memory_limit_series(raw_points, start_at=cutoff, end_at=now_utc)
        if metric.key == "memory" and metric.unit_kind == "bytes"
        else []
    )
    aggregated = _bucket_points(
        raw_points, target_points=target_points, start_at=cutoff, end_at=now_utc
    )
    cpu_limit_series = (
        _cpu_limit_series(raw_points, start_at=cutoff, end_at=now_utc)
        if metric.key == "cpu"
        else []
    )
    series: list[dict[str, object]] = []
    previous_visual_value: float | None = None
    for item in aggregated:
        timestamp = item["timestamp"]
        assert isinstance(timestamp, datetime)
        visual_value = _visual_value(
            item, now_utc=now_utc, previous_visual_value=previous_visual_value
        )
        visual_min, visual_max = _visual_extrema(
            item, now_utc=now_utc, visual_value=visual_value
        )
        item["visual_value"] = visual_value
        item["visual_min"] = visual_min
        item["visual_max"] = visual_max
        point = {
            "timestamp": timestamp.isoformat(),
            "value": round(float(item["value"]), 2),
            "avg": round(float(item["value"]), 2),
            "visual_value": round(visual_value, 2),
            "min": round(float(item["min"]), 2),
            "max": round(float(item["max"]), 2),
            "visual_min": round(visual_min, 2),
            "visual_max": round(visual_max, 2),
            "last": round(float(item["last"]), 2),
            "count": int(item["count"]),
            "is_live": bool(item["is_live"]),
        }
        for optional_key in (
            "network_rx_bps",
            "network_tx_bps",
            "cpu_pressure",
            "cpu_entitlement_percent_of_host",
            "cpu_limit_percent_of_host",
            "memory_percent",
        ):
            if isinstance(item.get(optional_key), (int, float)):
                point[optional_key] = round(float(item[optional_key]), 2)
        if isinstance(item.get("disk_usage_complete"), bool):
            point["disk_usage_complete"] = bool(item["disk_usage_complete"])
        series.append(point)
        previous_visual_value = visual_value

    latest_value = float(aggregated[-1]["last"]) if aggregated else None
    peak_value = _axis_peak_value(aggregated)
    total_count = sum(int(item.get("count") or 1) for item in aggregated)
    weighted_sum = sum(
        float(item["value"]) * int(item.get("count") or 1) for item in aggregated
    )
    average_value = (weighted_sum / total_count) if total_count else None

    summary = {
        "latest_value": round(latest_value, 2) if latest_value is not None else None,
        "peak_value": round(peak_value, 2) if peak_value is not None else None,
        "average_value": round(average_value, 2) if average_value is not None else None,
        "point_count": len(series),
    }
    if metric.key == "disk":
        sample_cadence = "1h"
        partial_disk_samples = any(
            item.get("disk_usage_complete") is False for item in aggregated
        )
        note = "Hourly disk history"
        if partial_disk_samples:
            note = "Hourly disk history; some samples are partial."
    else:
        sample_cadence = "1min"
        note = (
            "Live and 1-minute history"
            if timeframe.key == "hour"
            else "1-minute history"
        )

    visual_peak_value = _axis_visual_peak_value(aggregated)
    axis_max_source = visual_peak_value if visual_peak_value is not None else 0.0
    if cpu_limit_series:
        axis_max_source = max(
            axis_max_source, max(float(item["value"]) for item in cpu_limit_series)
        )
    if memory_limit_series:
        axis_max_source = max(
            axis_max_source, max(float(item["value"]) for item in memory_limit_series)
        )
    axis_min_source = _axis_visual_min_value(aggregated)
    y_axis_min = _nice_axis_min(
        float(axis_min_source), float(axis_max_source), metric.unit_kind
    )
    y_axis_max = _nice_axis_max(
        float(axis_max_source), unit_kind=metric.unit_kind, metric_key=metric.key
    )
    limit_value = 100.0 if metric.unit_kind == "percent" else None
    if limit_value is not None and metric.key in {"memory", "disk"}:
        y_axis_max = max(y_axis_max, limit_value)
    return {
        "available": True,
        "metric": metric.__dict__,
        "timeframe": timeframe.__dict__,
        "range_start_at": cutoff.isoformat(),
        "range_end_at": now_utc.isoformat(),
        "series": series,
        "chart_events": chart_events,
        "summary": summary,
        "note": note,
        "sample_cadence": sample_cadence,
        "target_points": target_points,
        "y_axis_min": y_axis_min,
        "y_axis_max": y_axis_max,
        "limit_value": limit_value,
        "cpu_limit_series": cpu_limit_series,
        "memory_limit_series": memory_limit_series,
    }


async def build_host_metric_history(
    session: AsyncSession,
    *,
    metric_key: str | None,
    timeframe_key: str | None,
    chart_width_px: int | None = None,
    live_metrics: dict[str, object] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    metric = _host_metric_spec(metric_key)
    timeframe = _timeframe_spec(timeframe_key)
    target_points = _target_point_count(timeframe, chart_width_px)
    now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    cutoff = now_utc - timedelta(hours=timeframe.hours)
    chart_events = await _chart_events_for_range(
        session,
        start_at=cutoff,
        end_at=now_utc,
    )
    samples = (
        (
            await session.execute(
                select(*_host_sample_load_attrs(metric))
                .where(
                    HostResourceSample.bucket_start >= cutoff,
                    HostResourceSample.bucket_start <= now_utc,
                )
                .order_by(HostResourceSample.bucket_start.asc())
            )
        )
        .mappings()
        .all()
    )

    raw_points: list[dict[str, object]] = []
    for sample in samples:
        rollup = _host_sample_rollup(sample, metric)
        if rollup is None:
            continue
        timestamp = _sample_bucket_start(sample)
        point = {
            "timestamp": timestamp,
            "value": float(rollup["value"]),
            "min": float(rollup["min"]),
            "max": float(rollup["max"]),
            "first": float(rollup["first"]),
            "last": float(rollup["last"]),
            "count": int(rollup["count"]),
            "is_live": False,
        }
        if metric.key == "network":
            direction_rates = _sample_direction_rollups(sample)
            if direction_rates is not None:
                point.update(direction_rates)
        raw_points.append(point)

    live_value = _live_host_metric_value(live_metrics, metric)
    if live_value is not None and (
        not raw_points or now_utc > raw_points[-1]["timestamp"]
    ):
        live_point = {
            "timestamp": now_utc,
            "value": live_value,
            "min": live_value,
            "max": live_value,
            "first": live_value,
            "last": live_value,
            "count": 1,
            "is_live": True,
        }
        if metric.key == "network":
            live_rx_bps = _live_network_direction_value(live_metrics, "network_rx_bps")
            live_tx_bps = _live_network_direction_value(live_metrics, "network_tx_bps")
            if live_rx_bps is not None:
                live_point["network_rx_bps"] = live_rx_bps
            if live_tx_bps is not None:
                live_point["network_tx_bps"] = live_tx_bps
        raw_points.append(live_point)

    aggregated = _bucket_points(
        raw_points, target_points=target_points, start_at=cutoff, end_at=now_utc
    )
    series: list[dict[str, object]] = []
    previous_visual_value = None
    for item in aggregated:
        timestamp = item["timestamp"]
        assert isinstance(timestamp, datetime)
        visual_value = _visual_value(
            item, now_utc=now_utc, previous_visual_value=previous_visual_value
        )
        visual_min, visual_max = _visual_extrema(
            item, now_utc=now_utc, visual_value=visual_value
        )
        item["visual_value"] = visual_value
        item["visual_min"] = visual_min
        item["visual_max"] = visual_max
        point = {
            "timestamp": timestamp.isoformat(),
            "value": round(float(item["value"]), 2),
            "avg": round(float(item["value"]), 2),
            "visual_value": round(visual_value, 2),
            "min": round(float(item["min"]), 2),
            "max": round(float(item["max"]), 2),
            "visual_min": round(visual_min, 2),
            "visual_max": round(visual_max, 2),
            "last": round(float(item["last"]), 2),
            "count": int(item["count"]),
            "is_live": bool(item["is_live"]),
        }
        for optional_key in ("network_rx_bps", "network_tx_bps"):
            if isinstance(item.get(optional_key), (int, float)):
                point[optional_key] = round(float(item[optional_key]), 2)
        series.append(point)
        previous_visual_value = visual_value

    latest_value = float(aggregated[-1]["last"]) if aggregated else None
    peak_value = _axis_peak_value(aggregated)
    total_count = sum(int(item.get("count") or 1) for item in aggregated)
    weighted_sum = sum(
        float(item["value"]) * int(item.get("count") or 1) for item in aggregated
    )
    average_value = (weighted_sum / total_count) if total_count else None
    summary = {
        "latest_value": round(latest_value, 2) if latest_value is not None else None,
        "peak_value": round(peak_value, 2) if peak_value is not None else None,
        "average_value": round(average_value, 2) if average_value is not None else None,
        "point_count": len(series),
    }
    visual_peak_value = _axis_visual_peak_value(aggregated)
    axis_max_source = visual_peak_value if visual_peak_value is not None else 0.0
    axis_min_source = _axis_visual_min_value(aggregated)
    y_axis_min = _nice_axis_min(
        float(axis_min_source), float(axis_max_source), metric.unit_kind
    )
    y_axis_max = _nice_axis_max(
        float(axis_max_source), unit_kind=metric.unit_kind, metric_key=metric.key
    )
    limit_value = 100.0 if metric.unit_kind == "percent" else None
    if limit_value is not None and metric.key in {"memory", "disk"}:
        y_axis_max = max(y_axis_max, limit_value)
    return {
        "available": True,
        "metric": metric.__dict__,
        "timeframe": timeframe.__dict__,
        "range_start_at": cutoff.isoformat(),
        "range_end_at": now_utc.isoformat(),
        "series": series,
        "chart_events": chart_events,
        "summary": summary,
        "note": "Live and 1-minute host history"
        if timeframe.key == "hour"
        else "1-minute host history",
        "sample_cadence": "1min",
        "target_points": target_points,
        "y_axis_min": y_axis_min,
        "y_axis_max": y_axis_max,
        "limit_value": limit_value,
    }
