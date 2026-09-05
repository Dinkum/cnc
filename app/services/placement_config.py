from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Backend, ClusterNode
from app.services.validators import ValidationError


LOCAL_NODE_UID = "local"
PLACEMENT_MODE_SINGLE = "single"
PLACEMENT_MODE_FAILOVER = "failover"
PLACEMENT_MODE_LIVE = "live"
PLACEMENT_MODES = {
    PLACEMENT_MODE_SINGLE,
    PLACEMENT_MODE_FAILOVER,
    PLACEMENT_MODE_LIVE,
}
MULTI_NODE_PLACEMENT_MODES = {
    PLACEMENT_MODE_FAILOVER,
    PLACEMENT_MODE_LIVE,
}


@dataclass(frozen=True)
class PlacementConfig:
    enabled: bool
    mode: str
    active_node_uid: str
    selected_node_uids: tuple[str, ...]


def clean_node_uid(value: object) -> str:
    node_uid = str(value or "").strip()
    return node_uid or LOCAL_NODE_UID


def unique_node_uids(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        node_uid = clean_node_uid(value)
        if node_uid in seen:
            continue
        seen.add(node_uid)
        result.append(node_uid)
    return tuple(result)


def backend_single_node_uid(backend: Backend) -> str:
    return clean_node_uid(getattr(backend, "placement_node_uid", "") or LOCAL_NODE_UID)


def _backend_placement_mode(backend: Backend) -> str:
    mode = (
        str(getattr(backend, "placement_mode", "") or PLACEMENT_MODE_SINGLE)
        .strip()
        .lower()
    )
    return mode if mode in PLACEMENT_MODES else PLACEMENT_MODE_SINGLE


def _backend_selected_node_uids(backend: Backend) -> tuple[str, ...]:
    raw = str(getattr(backend, "placement_node_uids_json", "") or "[]").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()
    return unique_node_uids([str(item) for item in payload])


def read_backend_placement(backend: Backend) -> PlacementConfig:
    single_node_uid = backend_single_node_uid(backend)
    mode = _backend_placement_mode(backend)
    if mode == PLACEMENT_MODE_SINGLE:
        return PlacementConfig(
            enabled=False,
            mode=PLACEMENT_MODE_SINGLE,
            active_node_uid=single_node_uid,
            selected_node_uids=(),
        )

    selected_node_uids = _backend_selected_node_uids(backend)
    active_node_uid = clean_node_uid(
        getattr(backend, "placement_active_node_uid", "") or single_node_uid
    )
    if not selected_node_uids:
        selected_node_uids = (active_node_uid,)
    if active_node_uid not in selected_node_uids:
        selected_node_uids = unique_node_uids([active_node_uid, *selected_node_uids])
    return PlacementConfig(
        enabled=True,
        mode=mode,
        active_node_uid=active_node_uid,
        selected_node_uids=selected_node_uids,
    )


def backend_has_local_runtime(backend: Backend) -> bool:
    placement = read_backend_placement(backend)
    if placement.enabled:
        return LOCAL_NODE_UID in placement.selected_node_uids
    return placement.active_node_uid == LOCAL_NODE_UID


def apply_backend_placement(
    backend: Backend,
    *,
    enabled: bool,
    mode: str,
    active_node_uid: str,
    selected_node_uids: tuple[str, ...],
) -> PlacementConfig:
    normalized_active = clean_node_uid(active_node_uid)
    if not enabled:
        backend.placement_mode = PLACEMENT_MODE_SINGLE
        backend.placement_active_node_uid = normalized_active
        backend.placement_node_uids_json = "[]"
        backend.placement_node_uid = (
            None if normalized_active == LOCAL_NODE_UID else normalized_active
        )
        return read_backend_placement(backend)

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in MULTI_NODE_PLACEMENT_MODES:
        raise ValidationError("placement mode must be failover or live")
    normalized_selected = unique_node_uids(selected_node_uids)
    if normalized_active not in normalized_selected:
        normalized_selected = unique_node_uids(
            [normalized_active, *normalized_selected]
        )
    if len(normalized_selected) < 2:
        raise ValidationError("multi-node placement needs at least two selected nodes")

    backend.placement_mode = normalized_mode
    backend.placement_active_node_uid = normalized_active
    backend.placement_node_uids_json = json.dumps(
        list(normalized_selected), separators=(",", ":")
    )
    backend.placement_node_uid = (
        None if normalized_active == LOCAL_NODE_UID else normalized_active
    )
    return read_backend_placement(backend)


async def validate_backend_placement_nodes(
    session: AsyncSession,
    *,
    selected_node_uids: tuple[str, ...],
    active_node_uid: str,
) -> None:
    nodes = (
        (
            await session.execute(
                select(ClusterNode).where(ClusterNode.removed_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    node_map = {node.node_uid: node for node in nodes}
    valid_node_uids = {LOCAL_NODE_UID, *node_map}
    unknown = sorted(
        node_uid for node_uid in selected_node_uids if node_uid not in valid_node_uids
    )
    if unknown:
        raise ValidationError(f"unknown placement node: {', '.join(unknown)}")
    if active_node_uid not in valid_node_uids:
        raise ValidationError("active placement node is not linked")
    for node_uid in selected_node_uids:
        if node_uid == LOCAL_NODE_UID:
            continue
        node = node_map[node_uid]
        if not str(node.tailnet_ip or node.wireguard_ip or "").strip():
            raise ValidationError(
                f"placement node {node.name} has no Tailscale address"
            )
