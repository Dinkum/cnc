from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Backend, BackendReplicaReadiness, ClusterNode
from app.services.app_containers import configured_app_dns_servers
from app.services.resource_profile import (
    backend_resource_profile,
    build_resource_profile,
)
from app.services.sandbox_profiles import get_app_sandbox_profile
from app.services.update_service import current_app_version
from app.services.validators import ValidationError, parse_volumes_json


LOCAL_NODE_UID = "local"
REPLICA_RUNTIME_CONTRACT_VERSION = 1
REPLICA_SETUP_MODE_TRANSFER = "transfer"


@dataclass(frozen=True)
class ReplicaReadinessStatus:
    node_uid: str
    ready: bool
    reason: str = ""
    setup_mode: str = ""
    target_url: str = ""
    healthcheck_result: str = ""
    last_verified_at: datetime | None = None
    runtime_contract_hash: str = ""


def node_tailnet_ip(node: ClusterNode) -> str:
    return str(node.tailnet_ip or node.wireguard_ip or "").strip()


def replica_target_url(
    backend: Backend, node: ClusterNode | None, node_uid: str
) -> str:
    port = int(backend.port or 0)
    if node_uid == LOCAL_NODE_UID:
        return f"http://127.0.0.1:{port}"
    if node is None:
        return f"http://{node_uid}:{port}"
    return f"http://{node_tailnet_ip(node)}:{port}"


def replica_runtime_contract(backend: Backend, settings: Settings) -> dict[str, object]:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    resource_profile = backend_resource_profile(
        backend, build_resource_profile(settings, [backend])
    )
    return {
        "schema_version": REPLICA_RUNTIME_CONTRACT_VERSION,
        "cnc_version": current_app_version(),
        "dns_servers": configured_app_dns_servers(settings),
        "backend": {
            "id": backend.id,
            "name": backend.name,
            "kind": backend.kind,
            "port": backend.port,
            "handoff_port": backend.handoff_port,
            "sandbox_profile": profile.profile_id,
            "seed_image": profile.seed_image,
            "seed_revision": profile.seed_revision,
            "init_command": list(profile.init_command),
            "healthcheck_mode": backend.healthcheck_mode or "",
            "healthcheck_path": backend.healthcheck_path or "",
            "healthcheck_host_header": backend.healthcheck_host_header or "",
            "volumes": parse_volumes_json(backend.volumes_json),
            "resource_mode": resource_profile.mode,
            "resource_size": resource_profile.resource_size,
            "memory_high": resource_profile.memory_high,
            "memory_max": resource_profile.memory_max,
            "cpu_quota": resource_profile.cpu_quota,
            "cpu_shares": resource_profile.cpu_shares,
        },
    }


def replica_runtime_contract_hash(backend: Backend, settings: Settings) -> str:
    payload = json.dumps(
        replica_runtime_contract(backend, settings),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def record_replica_readiness(
    session: AsyncSession,
    settings: Settings,
    *,
    backend: Backend,
    node_uid: str,
    setup_mode: str,
    target_url: str,
    healthcheck_result: str,
) -> BackendReplicaReadiness:
    normalized_node_uid = str(node_uid or "").strip() or LOCAL_NODE_UID
    verified_at = datetime.now(UTC)
    values = {
        "backend_id": int(backend.id),
        "node_uid": normalized_node_uid,
        "setup_mode": str(setup_mode or "").strip() or REPLICA_SETUP_MODE_TRANSFER,
        "runtime_contract_hash": replica_runtime_contract_hash(backend, settings),
        "target_url": str(target_url or "").strip(),
        "healthcheck_result": str(healthcheck_result or "").strip(),
        "last_verified_at": verified_at,
    }
    statement = sqlite_insert(BackendReplicaReadiness).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["backend_id", "node_uid"],
            set_={
                "setup_mode": values["setup_mode"],
                "runtime_contract_hash": values["runtime_contract_hash"],
                "target_url": values["target_url"],
                "healthcheck_result": values["healthcheck_result"],
                "last_verified_at": values["last_verified_at"],
            },
        )
    )
    return (
        await session.execute(
            select(BackendReplicaReadiness).where(
                BackendReplicaReadiness.backend_id == backend.id,
                BackendReplicaReadiness.node_uid == normalized_node_uid,
            )
        )
    ).scalar_one()


async def delete_replica_readiness(
    session: AsyncSession,
    *,
    backend_id: int,
    node_uids: tuple[str, ...],
) -> int:
    normalized_node_uids = tuple(
        dict.fromkeys(
            str(node_uid or "").strip() or LOCAL_NODE_UID for node_uid in node_uids
        )
    )
    if not normalized_node_uids:
        return 0
    result = await session.execute(
        delete(BackendReplicaReadiness).where(
            BackendReplicaReadiness.backend_id == int(backend_id),
            BackendReplicaReadiness.node_uid.in_(normalized_node_uids),
        )
    )
    return int(result.rowcount or 0)


async def backend_replica_readiness_statuses(
    session: AsyncSession,
    settings: Settings,
    *,
    backend: Backend,
    node_uids: tuple[str, ...],
) -> dict[str, ReplicaReadinessStatus]:
    status_map = await backend_replica_readiness_status_map(
        session,
        settings,
        backends=[backend],
        backend_node_uids={int(backend.id): node_uids},
    )
    return status_map.get(int(backend.id), {})


async def backend_replica_readiness_status_map(
    session: AsyncSession,
    settings: Settings,
    *,
    backends: list[Backend],
    backend_node_uids: Mapping[int, tuple[str, ...]],
) -> dict[int, dict[str, ReplicaReadinessStatus]]:
    backend_by_id = {
        int(backend.id): backend
        for backend in backends
        if getattr(backend, "id", None) is not None
        and int(backend.id) in backend_node_uids
    }
    if not backend_by_id:
        return {}
    node_rows = (
        (
            await session.execute(
                select(ClusterNode).where(ClusterNode.removed_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    nodes_by_uid = {node.node_uid: node for node in node_rows}
    backend_ids = tuple(sorted(backend_by_id))
    readiness_rows = (
        (
            await session.execute(
                select(BackendReplicaReadiness).where(
                    BackendReplicaReadiness.backend_id.in_(backend_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    readiness_by_backend_node = {
        (int(row.backend_id), row.node_uid): row for row in readiness_rows
    }
    result: dict[int, dict[str, ReplicaReadinessStatus]] = {}
    for backend_id, backend in backend_by_id.items():
        normalized_uids = tuple(
            dict.fromkeys(
                str(uid or "").strip() or LOCAL_NODE_UID
                for uid in backend_node_uids.get(backend_id, ())
            )
        )
        remote_uids = tuple(uid for uid in normalized_uids if uid != LOCAL_NODE_UID)
        contract_hash = replica_runtime_contract_hash(backend, settings)
        statuses: dict[str, ReplicaReadinessStatus] = {}
        for node_uid in normalized_uids:
            if node_uid == LOCAL_NODE_UID:
                statuses[node_uid] = ReplicaReadinessStatus(
                    node_uid=node_uid, ready=True
                )
                continue
            node = nodes_by_uid.get(node_uid)
            row = readiness_by_backend_node.get((backend_id, node_uid))
            statuses[node_uid] = _remote_readiness_status(
                backend,
                node_uid=node_uid,
                node=node,
                row=row,
                contract_hash=contract_hash,
            )
        for node_uid in remote_uids:
            statuses.setdefault(
                node_uid,
                ReplicaReadinessStatus(
                    node_uid=node_uid,
                    ready=False,
                    reason="node is not linked",
                ),
            )
        result[backend_id] = statuses
    return result


async def validate_replica_readiness_for_nodes(
    session: AsyncSession,
    settings: Settings,
    *,
    backend: Backend,
    node_uids: tuple[str, ...],
) -> None:
    statuses = await backend_replica_readiness_statuses(
        session,
        settings,
        backend=backend,
        node_uids=node_uids,
    )
    unavailable = [
        status
        for status in statuses.values()
        if status.node_uid != LOCAL_NODE_UID and not status.ready
    ]
    if unavailable:
        details = "; ".join(
            f"{status.node_uid}: {status.reason}" for status in unavailable
        )
        raise ValidationError(f"placement node is unavailable: {details}")


def _remote_readiness_status(
    backend: Backend,
    *,
    node_uid: str,
    node: ClusterNode | None,
    row: BackendReplicaReadiness | None,
    contract_hash: str,
) -> ReplicaReadinessStatus:
    if node is None:
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="node is not linked",
        )
    node_state = str(node.state or "").strip().lower()
    if node_state != "healthy":
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason=f"node state is {node.state or 'unknown'}",
        )
    if not node_tailnet_ip(node):
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="node has no Tailscale address",
        )
    if row is None:
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="replica readiness is missing",
        )
    target_url = replica_target_url(backend, node, node_uid)
    if row.runtime_contract_hash != contract_hash:
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="replica readiness is stale for this runtime contract",
            setup_mode=row.setup_mode,
            target_url=row.target_url,
            healthcheck_result=row.healthcheck_result,
            last_verified_at=row.last_verified_at,
            runtime_contract_hash=row.runtime_contract_hash,
        )
    if row.target_url != target_url:
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="replica target changed since verification",
            setup_mode=row.setup_mode,
            target_url=row.target_url,
            healthcheck_result=row.healthcheck_result,
            last_verified_at=row.last_verified_at,
            runtime_contract_hash=row.runtime_contract_hash,
        )
    if not str(row.healthcheck_result or "").strip():
        return ReplicaReadinessStatus(
            node_uid=node_uid,
            ready=False,
            reason="replica healthcheck result is missing",
            setup_mode=row.setup_mode,
            target_url=row.target_url,
            healthcheck_result=row.healthcheck_result,
            last_verified_at=row.last_verified_at,
            runtime_contract_hash=row.runtime_contract_hash,
        )
    return ReplicaReadinessStatus(
        node_uid=node_uid,
        ready=True,
        setup_mode=row.setup_mode,
        target_url=row.target_url,
        healthcheck_result=row.healthcheck_result,
        last_verified_at=row.last_verified_at,
        runtime_contract_hash=row.runtime_contract_hash,
    )
