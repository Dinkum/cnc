"""Cluster node queries and placement presentation for dashboard and output pages."""

from __future__ import annotations

import json
import os
import shutil
import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Backend, ClusterNode
from app.services.cluster_nodes import (
    LatencySummary,
    cluster_latency_summaries,
    current_node_version_label,
    list_cluster_nodes,
)
from app.services.inter_app_interfaces import (
    INTERFACE_DIRECTION_BIDIRECTIONAL,
    INTERFACE_DIRECTION_IN,
    INTERFACE_STATUS_ACCEPTED,
    INTERFACE_STATUS_PENDING,
    INTERFACE_STATUS_REJECTED,
    clean_interface_direction,
    clean_interface_status,
    inbound_inter_app_interfaces,
    read_inter_app_interfaces,
)
from app.services.placement_config import (
    LOCAL_NODE_UID,
    PLACEMENT_MODE_FAILOVER,
    PLACEMENT_MODE_LIVE,
    clean_node_uid,
    read_backend_placement,
)
from app.services.replica_readiness import ReplicaReadinessStatus
from app.services.tailscale_urls import infer_tailnet_dns_name


def _format_node_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return (
                f"{amount:.0f} {unit}"
                if unit == "B"
                else f"{amount:.1f} {unit}".replace(".0 ", " ")
            )
        amount /= 1024
    return f"{amount:.0f} B"


def _node_details_payload(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _node_tailnet_url(
    node: ClusterNode, details: dict[str, object], settings: Settings
) -> str:
    for key in ("confirm", "register"):
        section = details.get(key)
        if not isinstance(section, dict):
            continue
        tailnet_url = str(section.get("tailnet_url") or "").strip()
        if tailnet_url.startswith("https://"):
            return tailnet_url

    tailnet_dns_name = infer_tailnet_dns_name(settings)
    node_host = str(node.name or "").strip().lower().rstrip(".")
    if node_host.endswith(".ts.net"):
        return f"https://{node_host}"
    if tailnet_dns_name and node_host and "." not in node_host:
        return f"https://{node_host}.{tailnet_dns_name}"
    return ""


def _node_version_label(raw_version: str | None, settings: Settings) -> str:
    version = str(raw_version or "").strip()
    if not version or version == "bootstrap":
        return "unknown"
    if "(" in version and ")" in version:
        return version
    normalized = (
        version if version == "dev" or version.startswith("v") else f"v{version}"
    )
    current = current_node_version_label(settings)
    if current.startswith(normalized) and "(" in current:
        return current
    return normalized


def _format_latency_value(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_latency_summary(
    summary: LatencySummary | None, latest_latency_ms: float | None
) -> str:
    if summary is not None:
        return (
            "1d avg / p95 / p99: "
            f"{_format_latency_value(summary.avg_ms)} / "
            f"{_format_latency_value(summary.p95_ms)} / "
            f"{_format_latency_value(summary.p99_ms)} ms"
        )
    if latest_latency_ms is not None:
        return f"latest: {_format_latency_value(latest_latency_ms)} ms"
    return "pending"


def _local_node_summary(settings: Settings) -> dict[str, str]:
    host_name = socket.gethostname() or "this-server"
    cpu_count = os.cpu_count()
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        physical_pages = int(os.sysconf("SC_PHYS_PAGES"))
        ram = _format_node_bytes(page_size * physical_pages)
    except (AttributeError, OSError, ValueError):
        ram = "unknown"
    try:
        disk = _format_node_bytes(shutil.disk_usage("/").total)
    except OSError:
        disk = "unknown"
    return {
        "id": "local",
        "name": host_name,
        "label": "the server (leader)",
        "role": "leader",
        "state": "healthy",
        "state_tone": "healthy",
        "ram": ram,
        "cpu": str(cpu_count) if cpu_count else "unknown",
        "disk": disk,
        "tailnet_ip": "this server",
        "tailnet_url": "",
        "latency": "local",
        "version": current_node_version_label(settings),
        "last_seen": "now",
        "removable": "false",
    }


def _format_relative_time(value: datetime) -> str:
    observed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    seconds = max(
        0, int((datetime.now(UTC) - observed.astimezone(UTC)).total_seconds())
    )
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _cluster_node_state_tone(state: object) -> str:
    normalized = str(state or "").strip().lower()
    if normalized == "healthy":
        return "healthy"
    if normalized in {"degraded", "offline", "error", "failed"}:
        return "degraded"
    if normalized in {"removed", "inactive"}:
        return "inactive"
    return "pending"


async def cluster_nodes(
    session: AsyncSession, settings: Settings
) -> list[dict[str, str]]:
    rows = [_local_node_summary(settings)]
    nodes = await list_cluster_nodes(session)
    latency_summaries = await cluster_latency_summaries(
        session,
        [node.node_uid for node in nodes],
        since=datetime.now(UTC) - timedelta(days=1),
    )
    for node in nodes:
        details = _node_details_payload(node.details_json)
        last_seen = "never"
        if node.last_seen_at is not None:
            last_seen = _format_relative_time(node.last_seen_at)
        latency = _format_latency_summary(
            latency_summaries.get(node.node_uid), node.latency_ms
        )
        rows.append(
            {
                "id": node.node_uid,
                "name": node.name,
                "label": "Follower",
                "role": node.role,
                "state": node.state,
                "state_tone": _cluster_node_state_tone(node.state),
                "ram": _format_node_bytes(node.ram_bytes),
                "cpu": str(node.cpu_count) if node.cpu_count else "unknown",
                "disk": _format_node_bytes(node.disk_bytes),
                "tailnet_ip": node.tailnet_ip or node.wireguard_ip,
                "tailnet_url": _node_tailnet_url(node, details, settings),
                "latency": latency,
                "version": _node_version_label(node.version, settings),
                "last_seen": last_seen,
                "removable": "true",
            }
        )
    return rows


async def cluster_nodes_for_context(
    session: AsyncSession, settings: Settings
) -> list[dict[str, str]]:
    if not settings.multi_node_enabled:
        return []
    return await cluster_nodes(session, settings)


def _placement_node_status(
    *,
    mode: str,
    enabled: bool,
    node_uid: str,
    selected: bool,
    active_node_uid: str,
) -> str:
    if not enabled or not selected:
        return "off"
    if mode == PLACEMENT_MODE_LIVE:
        return "active"
    return "active" if node_uid == active_node_uid else "standby"


def _placement_node_display_role(node_uid: str) -> str:
    return "primary" if node_uid == LOCAL_NODE_UID else "follower"


def _inter_app_interface_targets(
    backend: Backend, all_backends: list[Backend]
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for candidate in all_backends:
        if candidate.id == backend.id or candidate.kind != "app":
            continue
        targets.append({"id": candidate.id, "name": candidate.name})
    return targets


def _interface_status_label(status: str) -> str:
    if status == INTERFACE_STATUS_ACCEPTED:
        return "confirmed"
    if status == INTERFACE_STATUS_REJECTED:
        return "rejected"
    return "pending"


def _interface_path_view(
    *,
    current_backend: str,
    other_backend: str,
    direction: str,
    inbound: bool,
) -> dict[str, str]:
    normalized = clean_interface_direction(direction)
    if normalized == INTERFACE_DIRECTION_BIDIRECTIONAL:
        return {
            "left": other_backend if inbound else current_backend,
            "right": current_backend if inbound else other_backend,
            "direction_label": "BIDIRECTIONAL <->",
        }
    if inbound:
        if normalized == INTERFACE_DIRECTION_IN:
            return {
                "left": current_backend,
                "right": other_backend,
                "direction_label": "OUTBOUND ->",
            }
        return {
            "left": other_backend,
            "right": current_backend,
            "direction_label": "INBOUND ->",
        }
    if normalized == INTERFACE_DIRECTION_IN:
        return {
            "left": other_backend,
            "right": current_backend,
            "direction_label": "INBOUND ->",
        }
    return {
        "left": current_backend,
        "right": other_backend,
        "direction_label": "OUTBOUND ->",
    }


def build_output_placement_card(
    backend: Backend,
    cluster_nodes: list[dict[str, str]],
    all_backends: list[Backend],
    readiness_statuses: dict[str, ReplicaReadinessStatus] | None = None,
) -> dict[str, object]:
    placement = read_backend_placement(backend)
    active_node_uid = clean_node_uid(placement.active_node_uid)
    selected_node_uids = set(placement.selected_node_uids)
    readiness_statuses = readiness_statuses or {}
    rows: list[dict[str, object]] = []
    for node in cluster_nodes:
        node_uid = clean_node_uid(node.get("id", ""))
        selected = placement.enabled and node_uid in selected_node_uids
        readiness = readiness_statuses.get(node_uid)
        readiness_ready = (
            True
            if node_uid == LOCAL_NODE_UID
            else bool(readiness is not None and readiness.ready)
        )
        unavailable_reason = (
            ""
            if readiness_ready
            else (
                readiness.reason
                if readiness is not None
                else "replica readiness is missing"
            )
        )
        display_role = _placement_node_display_role(node_uid)
        rows.append(
            {
                "id": node_uid,
                "name": node.get("name") or node_uid,
                "tailnet_ip": node.get("tailnet_ip") or "unknown",
                "display_role": display_role,
                "setup_ready": readiness_ready,
                "unavailable_reason": unavailable_reason,
                "selected": selected,
                "active": selected and node_uid == active_node_uid,
                "status": _placement_node_status(
                    mode=placement.mode,
                    enabled=placement.enabled,
                    node_uid=node_uid,
                    selected=selected,
                    active_node_uid=active_node_uid,
                ),
            }
        )
    if not rows:
        rows.append(
            {
                "id": LOCAL_NODE_UID,
                "name": "leader",
                "tailnet_ip": "this server",
                "display_role": "primary",
                "setup_ready": True,
                "unavailable_reason": "",
                "selected": False,
                "active": False,
                "status": "off",
            }
        )
    interface_targets = _inter_app_interface_targets(backend, all_backends)
    targets_by_id = {
        int(target["id"]): str(target["name"]) for target in interface_targets
    }
    interfaces = [
        {
            "name": item.name,
            "port": "auto",
            "target_backend_id": item.target_backend_id,
            "target_backend_name": targets_by_id[item.target_backend_id],
            "status": clean_interface_status(item.status),
            "direction": clean_interface_direction(item.direction),
            **_interface_path_view(
                current_backend=backend.name,
                other_backend=targets_by_id[item.target_backend_id],
                direction=item.direction,
                inbound=False,
            ),
            "status_label": _interface_status_label(
                clean_interface_status(item.status)
            ),
        }
        for item in read_inter_app_interfaces(backend)
        if item.target_backend_id in targets_by_id
    ]
    inbound_interfaces = [
        {
            "name": item.name,
            "source_backend_id": item.source_backend_id,
            "source_backend_name": item.source_backend_name,
            "status": clean_interface_status(item.status),
            "direction": clean_interface_direction(item.direction),
            **_interface_path_view(
                current_backend=backend.name,
                other_backend=item.source_backend_name,
                direction=item.direction,
                inbound=True,
            ),
        }
        for item in inbound_inter_app_interfaces(backend, all_backends)
        if clean_interface_status(item.status) != INTERFACE_STATUS_REJECTED
    ]
    return {
        "enabled": placement.enabled,
        "mode": placement.mode if placement.enabled else PLACEMENT_MODE_FAILOVER,
        "active_node_uid": active_node_uid,
        "selected_node_uids": list(placement.selected_node_uids),
        "interface_targets": interface_targets,
        "interfaces": interfaces,
        "inbound_interfaces": inbound_interfaces,
        "interface_statuses": [
            {"value": INTERFACE_STATUS_PENDING, "label": "pending"},
            {"value": INTERFACE_STATUS_ACCEPTED, "label": "confirm"},
            {"value": INTERFACE_STATUS_REJECTED, "label": "reject"},
        ],
        "nodes": rows,
    }
