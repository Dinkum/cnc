from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import time
from typing import Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, ClusterNode
from app.services.app_quadlet import (
    quadlet_container_unit_name,
    quadlet_container_service_name,
    quadlet_network_unit_name,
    quadlet_network_service_name,
    render_quadlet_container,
    render_quadlet_network,
    write_app_quadlet_assets,
)
from app.services.apply_service import run_apply
from app.services.commands import run_command
from app.services.renderers import container_name, network_name
from app.services.resource_profile import build_resource_profile
from app.services.replica_readiness import (
    REPLICA_SETUP_MODE_TRANSFER,
    delete_replica_readiness,
    record_replica_readiness,
    replica_target_url,
)
from app.services.operations import OperationHandle, host_mutation_operation
from app.services.placement_config import (
    apply_backend_placement,
    read_backend_placement,
)
from app.services.sandbox_profiles import app_sandbox_dir, get_app_sandbox_profile
from app.services.ssh_options import SSH_BATCH_OPTIONS


LOCAL_NODE_UID = "local"
TransferProgressCallback = Callable[[int, str], None]
logger = get_logger("cluster_transfer")


@dataclass(frozen=True)
class TransferResult:
    backend_id: int
    backend_name: str
    source_node_uid: str
    target_node_uid: str
    target_label: str
    route_target: str
    messages: list[str]


@dataclass(frozen=True)
class ReplicaSetupResult:
    backend_id: int
    backend_name: str
    target_node_uid: str
    target_label: str
    setup_mode: str
    route_target: str
    messages: list[str]


@dataclass(frozen=True)
class TargetRollbackState:
    target_uid: str
    target_path: str
    rollback_path: str
    unit_backup_path: str = ""
    service_was_active: bool = False


REPLICA_SETUP_MODE_CLONE = "clone"
REPLICA_SETUP_MODE_FRESH = "fresh"
REPLICA_SETUP_MODES = {REPLICA_SETUP_MODE_CLONE, REPLICA_SETUP_MODE_FRESH}


async def transfer_backend(
    session: AsyncSession,
    settings: Settings,
    *,
    backend_id: int,
    target_node_uid: str,
    progress_callback: TransferProgressCallback | None = None,
    acquire_lock: bool = True,
) -> TransferResult:
    if acquire_lock:
        async with host_mutation_operation(
            settings,
            kind="transfer_backend",
            actor="ui",
            backend_id=backend_id,
            phase="transfer_backend",
            operation=OperationHandle(
                id=None, kind="transfer_backend", settings=settings
            ),
        ):
            return await transfer_backend(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node_uid,
                progress_callback=progress_callback,
                acquire_lock=False,
            )

    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        raise ValueError("output not found")
    if backend.kind != "app":
        raise ValueError("only app outputs can be transferred")
    if not backend.enabled:
        raise ValueError("output must be enabled before transfer")

    previous_placement = read_backend_placement(backend)
    previous_runtime_node_uids = (
        previous_placement.selected_node_uids
        if previous_placement.enabled
        else (previous_placement.active_node_uid,)
    )
    target_uid = _clean_target_uid(target_node_uid)
    source_uid = previous_placement.active_node_uid
    if target_uid == source_uid:
        return TransferResult(
            backend_id=backend.id,
            backend_name=backend.name,
            source_node_uid=source_uid,
            target_node_uid=target_uid,
            target_label=_node_label(None, target_uid),
            route_target=_route_target(backend, None, target_uid),
            messages=["output is already on the selected node"],
        )

    source_node = (
        await _load_node(session, source_uid) if source_uid != LOCAL_NODE_UID else None
    )
    target_node = (
        await _load_node(session, target_uid) if target_uid != LOCAL_NODE_UID else None
    )
    messages: list[str] = []

    _report(progress_callback, 6, "Reading transfer request.")
    _report(progress_callback, 14, "Checking target node.")
    if target_node is not None and str(target_node.state or "").strip().lower() not in {
        "healthy",
        "joining",
    }:
        raise ValueError(f"target node is not healthy: {target_node.state}")

    with tempfile.TemporaryDirectory(
        prefix=f"cnc-transfer-{backend.name}-"
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        bundle_path = temp_dir / f"{backend.name}.tar.gz"
        if source_uid == LOCAL_NODE_UID:
            _report(progress_callback, 24, "Capturing source state.")
            await asyncio.to_thread(
                _archive_local_backend, backend, settings, bundle_path
            )
        else:
            if source_node is None:
                raise ValueError("source node is not linked")
            _report(progress_callback, 24, "Capturing source state from peer.")
            await asyncio.to_thread(
                _archive_remote_backend, backend, settings, source_node, bundle_path
            )
        messages.append("snapshot captured")

        target_rollback: TargetRollbackState | None = None
        if target_uid == LOCAL_NODE_UID:
            _report(progress_callback, 44, "Restoring output on leader.")
            target_rollback = await asyncio.to_thread(
                _restore_local_backend, backend, settings, bundle_path
            )
            messages.append("restored on leader")
        else:
            if target_node is None:
                raise ValueError("target node is not linked")
            _report(progress_callback, 38, "Copying snapshot to target.")
            target_rollback = await asyncio.to_thread(
                _restore_remote_backend, backend, settings, target_node, bundle_path
            )
            messages.append(f"restored on {target_node.name}")

        try:
            _report(progress_callback, 68, "Checking target health.")
            if target_uid == LOCAL_NODE_UID:
                await asyncio.to_thread(_start_local_backend_replica, backend, settings)
                healthcheck_result = await asyncio.to_thread(
                    _verify_local_backend, backend, settings
                )
                target_url = replica_target_url(backend, None, target_uid)
            else:
                if target_node is None:
                    raise ValueError("target node is not linked")
                healthcheck_result = await asyncio.to_thread(
                    _verify_remote_backend, backend, settings, target_node
                )
                target_url = replica_target_url(backend, target_node, target_uid)
            await record_replica_readiness(
                session,
                settings,
                backend=backend,
                node_uid=target_uid,
                setup_mode=REPLICA_SETUP_MODE_TRANSFER,
                target_url=target_url,
                healthcheck_result=healthcheck_result,
            )
            await session.flush()

            _report(progress_callback, 76, "Saving output placement.")
            apply_backend_placement(
                backend,
                enabled=False,
                mode="",
                active_node_uid=target_uid,
                selected_node_uids=(),
            )

            _report(progress_callback, 84, "Rendering cluster routes.")
            with session.no_autoflush:
                apply_response = await run_apply(
                    session,
                    settings,
                    operation_kind="transfer_backend",
                    actor="ui",
                )
            if apply_response.status != "success":
                raise RuntimeError(
                    f"host apply failed after transfer: {apply_response.message}"
                )
            if target_rollback is not None:
                await _discard_target_rollback(
                    backend,
                    settings,
                    target_rollback,
                    target_node=target_node,
                )
        except Exception as exc:
            rollback_messages: list[str] = []
            if target_rollback is not None:
                rollback_messages = await _restore_target_rollback(
                    backend,
                    settings,
                    target_rollback,
                    target_node=target_node,
                )
            if rollback_messages:
                raise RuntimeError(
                    f"{exc}; restored target rollback: {'; '.join(rollback_messages)}"
                ) from exc
            raise

    _report(progress_callback, 92, "Stopping source runtime.")
    cleaned_remote_node_uids: list[str] = []
    cleanup_remote_node_uids = tuple(
        node_uid
        for node_uid in dict.fromkeys(previous_runtime_node_uids)
        if node_uid not in {LOCAL_NODE_UID, target_uid}
    )
    if source_uid != target_uid and source_uid == LOCAL_NODE_UID:
        # run_apply already removes local runtime for remote-owned outputs.
        messages.append("source leader runtime stopped")
    for cleanup_node_uid in cleanup_remote_node_uids:
        cleanup_node: ClusterNode | None = None
        try:
            cleanup_node = (
                source_node
                if cleanup_node_uid == source_uid
                else await _load_node(session, cleanup_node_uid)
            )
            if cleanup_node is not None:
                await asyncio.to_thread(
                    _cleanup_remote_restored_target, backend, settings, cleanup_node
                )
                cleaned_remote_node_uids.append(cleanup_node_uid)
                messages.append(
                    f"source runtime and sandbox removed from {cleanup_node.name}"
                )
        except Exception as exc:
            logger.warning(
                "cluster_transfer.source_cleanup_failed",
                backend=backend.name,
                source_uid=cleanup_node_uid,
                error=str(exc),
            )
            cleanup_label = (
                cleanup_node.name if cleanup_node is not None else cleanup_node_uid
            )
            messages.append(f"source cleanup warning on {cleanup_label}: {exc}")
    if cleaned_remote_node_uids:
        deleted_count = await delete_replica_readiness(
            session,
            backend_id=int(backend.id),
            node_uids=tuple(cleaned_remote_node_uids),
        )
        await session.commit()
        if deleted_count:
            logger.info(
                "cluster_transfer.cleaned_readiness",
                backend=backend.name,
                node_uids=cleaned_remote_node_uids,
                count=deleted_count,
            )

    _report(progress_callback, 100, "Transfer complete.")
    return TransferResult(
        backend_id=backend.id,
        backend_name=backend.name,
        source_node_uid=source_uid,
        target_node_uid=target_uid,
        target_label=_node_label(target_node, target_uid),
        route_target=_route_target(backend, target_node, target_uid),
        messages=messages,
    )


async def setup_backend_replica(
    session: AsyncSession,
    settings: Settings,
    *,
    backend_id: int,
    target_node_uid: str,
    setup_mode: str,
    progress_callback: TransferProgressCallback | None = None,
    acquire_lock: bool = True,
) -> ReplicaSetupResult:
    if acquire_lock:
        async with host_mutation_operation(
            settings,
            kind="setup_backend_replica",
            actor="ui",
            backend_id=backend_id,
            phase="replica_setup",
            operation=OperationHandle(
                id=None, kind="setup_backend_replica", settings=settings
            ),
        ):
            return await setup_backend_replica(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node_uid,
                setup_mode=setup_mode,
                progress_callback=progress_callback,
                acquire_lock=False,
            )

    backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()
    if backend is None:
        raise ValueError("output not found")
    if backend.kind != "app":
        raise ValueError("only app outputs can be set up on multiple nodes")
    if not backend.enabled:
        raise ValueError("output must be enabled before setting up another node")

    normalized_mode = str(setup_mode or "").strip().lower()
    if normalized_mode not in REPLICA_SETUP_MODES:
        raise ValueError("replica setup mode must be clone or fresh")

    target_uid = _clean_target_uid(target_node_uid)
    source_uid = _backend_node_uid(backend)
    target_node = (
        await _load_node(session, target_uid) if target_uid != LOCAL_NODE_UID else None
    )
    source_node = (
        await _load_node(session, source_uid) if source_uid != LOCAL_NODE_UID else None
    )
    messages: list[str] = []

    _report(progress_callback, 6, "Reading setup request.")
    _report(progress_callback, 14, "Checking target node.")
    if target_node is not None and str(target_node.state or "").strip().lower() not in {
        "healthy",
        "joining",
    }:
        raise ValueError(f"target node is not healthy: {target_node.state}")

    if normalized_mode == REPLICA_SETUP_MODE_CLONE:
        target_rollback: TargetRollbackState | None = None
        if target_uid == source_uid:
            messages.append("selected node already has the source runtime")
        else:
            with tempfile.TemporaryDirectory(
                prefix=f"cnc-replica-{backend.name}-"
            ) as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                bundle_path = temp_dir / f"{backend.name}.tar.gz"
                if source_uid == LOCAL_NODE_UID:
                    _report(progress_callback, 24, "Capturing source state.")
                    await asyncio.to_thread(
                        _archive_local_backend, backend, settings, bundle_path
                    )
                else:
                    if source_node is None:
                        raise ValueError("source node is not linked")
                    _report(progress_callback, 24, "Capturing source state from peer.")
                    await asyncio.to_thread(
                        _archive_remote_backend,
                        backend,
                        settings,
                        source_node,
                        bundle_path,
                    )
                messages.append("snapshot captured")
                if target_uid == LOCAL_NODE_UID:
                    _report(progress_callback, 54, "Restoring replica on leader.")
                    target_rollback = await asyncio.to_thread(
                        _restore_local_backend, backend, settings, bundle_path
                    )
                    await asyncio.to_thread(
                        _start_local_backend_replica, backend, settings
                    )
                    messages.append("replica restored on leader")
                else:
                    if target_node is None:
                        raise ValueError("target node is not linked")
                    _report(progress_callback, 44, "Copying snapshot to target.")
                    target_rollback = await asyncio.to_thread(
                        _restore_remote_backend,
                        backend,
                        settings,
                        target_node,
                        bundle_path,
                    )
                    messages.append(f"replica restored on {target_node.name}")
    else:
        target_rollback = None
        if target_uid == LOCAL_NODE_UID:
            raise ValueError("fresh leader setup is handled by saving placement")
        if target_node is None:
            raise ValueError("target node is not linked")
        _report(progress_callback, 24, "Creating fresh guest.")
        target_rollback = await asyncio.to_thread(
            _setup_remote_fresh_backend, backend, settings, target_node
        )
        messages.append(f"fresh guest created on {target_node.name}")

    try:
        _report(progress_callback, 76, "Checking target health.")
        if target_uid == LOCAL_NODE_UID:
            healthcheck_result = await asyncio.to_thread(
                _verify_local_backend, backend, settings
            )
            target_url = replica_target_url(backend, None, target_uid)
        else:
            if target_node is None:
                raise ValueError("target node is not linked")
            healthcheck_result = await asyncio.to_thread(
                _verify_remote_backend, backend, settings, target_node
            )
            target_url = replica_target_url(backend, target_node, target_uid)
        await record_replica_readiness(
            session,
            settings,
            backend=backend,
            node_uid=target_uid,
            setup_mode=normalized_mode,
            target_url=target_url,
            healthcheck_result=healthcheck_result,
        )
        await session.commit()
        if target_rollback is not None:
            await _discard_target_rollback(
                backend,
                settings,
                target_rollback,
                target_node=target_node,
            )
    except Exception as exc:
        await session.rollback()
        rollback_messages: list[str] = []
        if target_rollback is not None:
            rollback_messages = await _restore_target_rollback(
                backend,
                settings,
                target_rollback,
                target_node=target_node,
            )
        if rollback_messages:
            raise RuntimeError(
                f"{exc}; replica target rollback: {'; '.join(rollback_messages)}"
            ) from exc
        raise

    _report(progress_callback, 100, "Node setup complete.")
    return ReplicaSetupResult(
        backend_id=backend.id,
        backend_name=backend.name,
        target_node_uid=target_uid,
        target_label=_node_label(target_node, target_uid),
        setup_mode=normalized_mode,
        route_target=_route_target(backend, target_node, target_uid),
        messages=messages,
    )


def _report(
    callback: TransferProgressCallback | None, progress: int, message: str
) -> None:
    if callback is None:
        return
    callback(progress, message)


def _clean_target_uid(raw: str) -> str:
    value = str(raw or "").strip()
    return value or LOCAL_NODE_UID


def _backend_node_uid(backend: Backend) -> str:
    return (
        str(getattr(backend, "placement_node_uid", "") or "").strip() or LOCAL_NODE_UID
    )


async def _load_node(session: AsyncSession, node_uid: str) -> ClusterNode:
    node = (
        await session.execute(
            select(ClusterNode).where(
                ClusterNode.node_uid == node_uid,
                ClusterNode.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise ValueError("node is not linked")
    return node


def _node_ip(node: ClusterNode) -> str:
    value = str(node.tailnet_ip or node.wireguard_ip or "").strip()
    if not value:
        raise ValueError(f"node {node.node_uid} has no Tailscale address")
    return value


def _node_label(node: ClusterNode | None, node_uid: str) -> str:
    if node_uid == LOCAL_NODE_UID:
        return "leader"
    return str(node.name if node is not None else node_uid)


def _route_target(backend: Backend, node: ClusterNode | None, node_uid: str) -> str:
    if node_uid == LOCAL_NODE_UID:
        return f"http://127.0.0.1:{backend.port}"
    if node is None:
        return f"{node_uid}:{backend.port}"
    return f"http://{_node_ip(node)}:{backend.port}"


def _archive_local_backend(
    backend: Backend, settings: Settings, bundle_path: Path
) -> None:
    sandbox = app_sandbox_dir(settings, backend.name)
    if not sandbox.exists():
        raise RuntimeError(f"source sandbox is missing: {sandbox}")
    _run_checked(
        [
            "tar",
            "-C",
            str(settings.app_sandbox_dir),
            "-czf",
            str(bundle_path),
            backend.name,
        ],
        settings,
    )


def _archive_remote_backend(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    bundle_path: Path,
) -> None:
    remote_path = _remote_transfer_path(backend)
    script = "\n".join(
        [
            "set -Eeuo pipefail",
            "install -d -m 0755 /var/lib/cnc/cluster-transfers",
            f"test -d {shlex.quote(str(Path('/var/lib/cnc/sandboxes') / backend.name))}",
            f"tar -C /var/lib/cnc/sandboxes -czf {shlex.quote(remote_path)} {shlex.quote(backend.name)}",
        ]
    )
    try:
        _ssh_checked(node, settings, script)
        _scp_checked(f"root@{_node_ip(node)}:{remote_path}", str(bundle_path), settings)
    finally:
        _cleanup_remote_transfer_path(node, settings, remote_path)


def _restore_local_backend(
    backend: Backend, settings: Settings, bundle_path: Path
) -> TargetRollbackState:
    target = app_sandbox_dir(settings, backend.name)
    stage_parent = (
        settings.app_sandbox_dir / f".cnc-transfer-stage-{backend.name}-{os.getpid()}"
    )
    rollback = (
        settings.app_sandbox_dir
        / f".cnc-transfer-rollback-{backend.name}-{os.getpid()}"
    )
    service_was_active = _local_backend_service_active(backend, settings)
    shutil.rmtree(stage_parent, ignore_errors=True)
    shutil.rmtree(rollback, ignore_errors=True)
    stage_parent.mkdir(parents=True, exist_ok=False)
    try:
        _run_checked(
            ["tar", "-C", str(stage_parent), "-xzf", str(bundle_path)], settings
        )
        staged = stage_parent / backend.name
        if not staged.is_dir():
            raise RuntimeError(
                f"transfer bundle missing sandbox directory: {backend.name}"
            )
        _stop_local_backend(backend, settings)
        if target.exists():
            shutil.move(str(target), str(rollback))
        try:
            shutil.move(str(staged), str(target))
        except Exception:
            if rollback.exists() and not target.exists():
                shutil.move(str(rollback), str(target))
            raise
        return TargetRollbackState(
            target_uid=LOCAL_NODE_UID,
            target_path=str(target),
            rollback_path=str(rollback),
            service_was_active=service_was_active,
        )
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def _restore_remote_backend(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    bundle_path: Path,
) -> TargetRollbackState:
    remote_path = _remote_transfer_path(backend)
    rollback = _remote_rollback_state(
        backend,
        node_uid=str(node.node_uid),
        target_path=str(Path("/var/lib/cnc/sandboxes") / backend.name),
        prefix="transfer",
        service_was_active=_remote_backend_service_active(backend, settings, node),
    )
    remote_ready = False
    try:
        _ssh_checked(
            node,
            settings,
            "set -Eeuo pipefail\ninstall -d -m 0755 /var/lib/cnc/cluster-transfers",
        )
        remote_ready = True
        _scp_checked(str(bundle_path), f"root@{_node_ip(node)}:{remote_path}", settings)
        _ssh_checked(
            node,
            settings,
            _remote_restore_script(
                backend,
                settings,
                node,
                remote_path,
                rollback=rollback,
            ),
        )
    finally:
        if remote_ready:
            _cleanup_remote_transfer_path(node, settings, remote_path)
    return rollback


def _setup_remote_fresh_backend(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
) -> TargetRollbackState:
    rollback = _remote_rollback_state(
        backend,
        node_uid=str(node.node_uid),
        target_path=str(Path("/var/lib/cnc/sandboxes") / backend.name),
        prefix="fresh",
        service_was_active=_remote_backend_service_active(backend, settings, node),
    )
    _ssh_checked(
        node,
        settings,
        _remote_fresh_setup_script(backend, settings, node, rollback=rollback),
    )
    return rollback


def _start_local_backend_replica(backend: Backend, settings: Settings) -> None:
    base_profile = build_resource_profile(settings, [backend])
    details = write_app_quadlet_assets(
        backend,
        settings,
        base_profile=base_profile,
    )
    if details.get("changed_files"):
        _run_checked(["systemctl", "daemon-reload"], settings)
    _run_checked(
        ["systemctl", "start", quadlet_network_service_name(backend.name)], settings
    )
    _run_checked(
        ["systemctl", "start", quadlet_container_service_name(backend.name)], settings
    )


def _local_backend_service_active(backend: Backend, settings: Settings) -> bool:
    result = run_command(
        [
            "systemctl",
            "is-active",
            "--quiet",
            quadlet_container_service_name(backend.name),
        ],
        timeout_sec=settings.command_timeout_status_sec,
    )
    return result.ok


def _remote_backend_service_active(
    backend: Backend, settings: Settings, node: ClusterNode
) -> bool:
    result = run_command(
        [
            "ssh",
            *SSH_BATCH_OPTIONS,
            f"root@{_node_ip(node)}",
            f"systemctl is-active --quiet {shlex.quote(quadlet_container_service_name(backend.name))}",
        ],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    return result.ok


def _cleanup_remote_transfer_path(
    node: ClusterNode, settings: Settings, remote_path: str
) -> None:
    try:
        _ssh_checked(node, settings, f"rm -f {shlex.quote(remote_path)}")
    except Exception as exc:
        logger.warning(
            "cluster_transfer.remote_archive_cleanup_failed",
            node_uid=node.node_uid,
            remote_path=remote_path,
            error=str(exc),
        )


def _remote_transfer_path(backend: Backend) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    token = uuid4().hex[:12]
    return f"/var/lib/cnc/cluster-transfers/{backend.name}-{stamp}-{token}.tar.gz"


def _remote_rollback_state(
    backend: Backend,
    *,
    node_uid: str,
    target_path: str,
    prefix: str,
    service_was_active: bool = False,
) -> TargetRollbackState:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    token = f"{prefix}-{backend.name}-{stamp}-{os.getpid()}-{uuid4().hex[:12]}"
    return TargetRollbackState(
        target_uid=node_uid,
        target_path=target_path,
        rollback_path=f"/var/lib/cnc/sandboxes/.cnc-{token}-rollback",
        unit_backup_path=f"/var/lib/cnc/cluster-transfers/.cnc-{token}-units",
        service_was_active=service_was_active,
    )


def _remote_quadlet_assets(
    backend: Backend, settings: Settings, node: ClusterNode
) -> tuple[str, str]:
    rootfs = Path("/var/lib/cnc/sandboxes") / backend.name / "rootfs"
    base_profile = build_resource_profile(settings, [backend])
    return (
        render_quadlet_network(backend),
        render_quadlet_container(
            backend,
            settings,
            base_profile=base_profile,
            rootfs_path=rootfs,
            publish_host=_node_ip(node),
        ),
    )


def _remote_restore_script(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    remote_path: str,
    *,
    rollback: TargetRollbackState,
) -> str:
    container = container_name(backend.name)
    network = network_name(backend.name)
    network_unit = quadlet_network_unit_name(backend.name)
    container_unit = quadlet_container_unit_name(backend.name)
    network_asset_content, container_asset_content = _remote_quadlet_assets(
        backend, settings, node
    )
    target_sandbox = str(Path("/var/lib/cnc/sandboxes") / backend.name)
    return f"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v podman >/dev/null 2>&1; then
  apt-get -qq update
  apt-get -qq install -y podman curl ca-certificates >/dev/null
fi
install -d -m 0755 /var/lib/cnc/sandboxes /var/lib/cnc/cluster-transfers /etc/containers/systemd
stage_dir="$(mktemp -d /var/lib/cnc/sandboxes/.cnc-transfer-stage-{shlex.quote(backend.name)}.XXXXXX)"
rollback_dir={shlex.quote(rollback.rollback_path)}
unit_backup_dir={shlex.quote(rollback.unit_backup_path)}
target_sandbox={shlex.quote(target_sandbox)}
network_asset=/etc/containers/systemd/{shlex.quote(network_unit)}
container_asset=/etc/containers/systemd/{shlex.quote(container_unit)}
previous_runtime_active={"1" if rollback.service_was_active else "0"}
activated=0
restore_previous_target() {{
  set +e
  systemctl stop {shlex.quote(container)}.service >/dev/null 2>&1 || true
  podman rm -f {shlex.quote(container)} >/dev/null 2>&1 || true
  systemctl stop {shlex.quote(network)}-network.service >/dev/null 2>&1 || true
  if [ "$activated" = "1" ]; then
    rm -rf "$target_sandbox"
    if [ -e "$rollback_dir" ]; then
      mv "$rollback_dir" "$target_sandbox"
    fi
    if [ -d "$unit_backup_dir" ]; then
      if [ -e "$unit_backup_dir/{shlex.quote(network_unit)}" ]; then
        cp -a "$unit_backup_dir/{shlex.quote(network_unit)}" "$network_asset"
      else
        rm -f "$network_asset"
      fi
      if [ -e "$unit_backup_dir/{shlex.quote(container_unit)}" ]; then
        cp -a "$unit_backup_dir/{shlex.quote(container_unit)}" "$container_asset"
      else
        rm -f "$container_asset"
      fi
      systemctl daemon-reload >/dev/null 2>&1 || true
      if [ "$previous_runtime_active" = "1" ]; then
        systemctl start {shlex.quote(network)}-network.service >/dev/null 2>&1 || true
        systemctl start {shlex.quote(container)}.service >/dev/null 2>&1 || true
      fi
    fi
  fi
  set -e
}}
cleanup() {{
  status=$?
  if [ "$status" -ne 0 ]; then
    restore_previous_target || true
  fi
  rm -rf "$stage_dir"
  exit "$status"
}}
trap cleanup EXIT
{_remote_cleanup_command(f"systemctl stop {shlex.quote(container)}.service", "not loaded|not-found|not found|could not be found")}
{_remote_cleanup_command(f"podman rm -f {shlex.quote(container)}", "no such container|does not exist|not found")}
rm -rf "$rollback_dir" "$unit_backup_dir"
mkdir -p "$unit_backup_dir"
for asset in "$network_asset" "$container_asset"; do
  if [ -e "$asset" ]; then
    cp -a "$asset" "$unit_backup_dir/$(basename "$asset")"
  fi
done
tar -C "$stage_dir" -xzf {shlex.quote(remote_path)}
test -d "$stage_dir/{shlex.quote(backend.name)}"
if [ -e "$target_sandbox" ]; then
  mv "$target_sandbox" "$rollback_dir"
fi
if ! mv "$stage_dir/{shlex.quote(backend.name)}" "$target_sandbox"; then
  if [ -e "$rollback_dir" ] && [ ! -e "$target_sandbox" ]; then
    mv "$rollback_dir" "$target_sandbox"
  fi
  exit 1
fi
activated=1
cat > /etc/containers/systemd/{shlex.quote(network_unit)} <<'EOF'
{network_asset_content}
EOF
cat > /etc/containers/systemd/{shlex.quote(container_unit)} <<'EOF'
{container_asset_content}
EOF
systemctl daemon-reload
systemctl start {shlex.quote(network)}-network.service >/dev/null
systemctl start {shlex.quote(container)}.service >/dev/null
"""


def _remote_fresh_setup_script(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    *,
    rollback: TargetRollbackState,
) -> str:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    container = container_name(backend.name)
    network = network_name(backend.name)
    network_unit = quadlet_network_unit_name(backend.name)
    container_unit = quadlet_container_unit_name(backend.name)
    network_asset_content, container_asset_content = _remote_quadlet_assets(
        backend, settings, node
    )
    provision_line = (
        'podman run --rm --rootfs "$stage_rootfs" /bin/bash -lc '
        f"{shlex.quote(profile.provision_script)}"
        if profile.provision_script.strip()
        else ":"
    )
    self_test_line = (
        'podman run --rm --rootfs "$stage_rootfs" '
        + " ".join(shlex.quote(part) for part in profile.self_test_command)
        if profile.self_test_command
        else ":"
    )
    profile_state = json.dumps(
        {
            "sandbox_profile": profile.profile_id,
            "seed_image": profile.seed_image,
            "init_command": list(profile.init_command),
            "seed_revision": profile.seed_revision,
        },
        indent=2,
        sort_keys=True,
    )
    sandbox_dir = str(Path("/var/lib/cnc/sandboxes") / backend.name)
    return f"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v podman >/dev/null 2>&1; then
  apt-get -qq update
  apt-get -qq install -y podman curl ca-certificates >/dev/null
fi
install -d -m 0755 /var/lib/cnc/sandboxes /etc/containers/systemd
stage_dir="$(mktemp -d /var/lib/cnc/sandboxes/.cnc-fresh-stage-{container}.XXXXXX)"
stage_sandbox="$stage_dir/{shlex.quote(backend.name)}"
stage_rootfs="$stage_sandbox/rootfs"
seed_container="{container}-seed-$$"
rollback_dir={shlex.quote(rollback.rollback_path)}
unit_backup_dir={shlex.quote(rollback.unit_backup_path)}
target_sandbox={shlex.quote(sandbox_dir)}
network_asset=/etc/containers/systemd/{shlex.quote(network_unit)}
container_asset=/etc/containers/systemd/{shlex.quote(container_unit)}
previous_runtime_active={"1" if rollback.service_was_active else "0"}
activated=0
restore_previous_target() {{
  set +e
  systemctl stop {shlex.quote(container)}.service >/dev/null 2>&1 || true
  podman rm -f {shlex.quote(container)} >/dev/null 2>&1 || true
  systemctl stop {shlex.quote(network)}-network.service >/dev/null 2>&1 || true
  if [ "$activated" = "1" ]; then
    rm -rf "$target_sandbox"
    if [ -e "$rollback_dir" ]; then
      mv "$rollback_dir" "$target_sandbox"
    fi
    if [ -d "$unit_backup_dir" ]; then
      if [ -e "$unit_backup_dir/{shlex.quote(network_unit)}" ]; then
        cp -a "$unit_backup_dir/{shlex.quote(network_unit)}" "$network_asset"
      else
        rm -f "$network_asset"
      fi
      if [ -e "$unit_backup_dir/{shlex.quote(container_unit)}" ]; then
        cp -a "$unit_backup_dir/{shlex.quote(container_unit)}" "$container_asset"
      else
        rm -f "$container_asset"
      fi
      systemctl daemon-reload >/dev/null 2>&1 || true
      if [ "$previous_runtime_active" = "1" ]; then
        systemctl start {shlex.quote(network)}-network.service >/dev/null 2>&1 || true
        systemctl start {shlex.quote(container)}.service >/dev/null 2>&1 || true
      fi
    fi
  fi
  set -e
}}
cleanup() {{
  status=$?
  if [ "$status" -ne 0 ]; then
    restore_previous_target || true
  fi
  podman rm -f "$seed_container" >/dev/null 2>&1 || true
  rm -rf "$stage_dir"
  exit "$status"
}}
trap cleanup EXIT
{_remote_cleanup_command(f"systemctl stop {shlex.quote(container)}.service", "not loaded|not-found|not found|could not be found")}
{_remote_cleanup_command(f"podman rm -f {shlex.quote(container)}", "no such container|does not exist|not found")}
{_remote_cleanup_command(f"systemctl stop {shlex.quote(network)}-network.service", "not loaded|not-found|not found|could not be found")}
rm -rf "$rollback_dir" "$unit_backup_dir"
mkdir -p "$unit_backup_dir"
for asset in "$network_asset" "$container_asset"; do
  if [ -e "$asset" ]; then
    cp -a "$asset" "$unit_backup_dir/$(basename "$asset")"
  fi
done
mkdir -p "$stage_rootfs"
podman pull {shlex.quote(profile.seed_image)} >/dev/null
podman create --name "$seed_container" {shlex.quote(profile.seed_image)} /bin/true >/dev/null
podman export "$seed_container" | tar -C "$stage_rootfs" -xf -
podman rm -f "$seed_container" >/dev/null
mkdir -p "$stage_rootfs/etc"
test -e "$stage_rootfs/etc/machine-id" || : > "$stage_rootfs/etc/machine-id"
{provision_line}
{self_test_line}
cat > "$stage_sandbox/profile.json" <<'EOF'
{profile_state}
EOF
if [ -e "$target_sandbox" ]; then
  mv "$target_sandbox" "$rollback_dir"
fi
if ! mv "$stage_sandbox" "$target_sandbox"; then
  if [ -e "$rollback_dir" ] && [ ! -e "$target_sandbox" ]; then
    mv "$rollback_dir" "$target_sandbox"
  fi
  exit 1
fi
activated=1
cat > /etc/containers/systemd/{shlex.quote(network_unit)} <<'EOF'
{network_asset_content}
EOF
cat > /etc/containers/systemd/{shlex.quote(container_unit)} <<'EOF'
{container_asset_content}
EOF
systemctl daemon-reload
systemctl start {shlex.quote(network)}-network.service >/dev/null
systemctl start {shlex.quote(container)}.service >/dev/null
"""


def _verify_remote_backend(
    backend: Backend, settings: Settings, node: ClusterNode
) -> str:
    return _verify_tcp(_node_ip(node), int(backend.port or 0), settings)


def _verify_local_backend(backend: Backend, settings: Settings) -> str:
    return _verify_tcp("127.0.0.1", int(backend.port or 0), settings)


def _verify_tcp(host: str, port: int, settings: Settings) -> str:
    if port <= 0:
        raise RuntimeError("backend has no published port")
    script = f"timeout 8 bash -c '</dev/tcp/{shlex.quote(host)}/{port}'"
    deadline = time.monotonic() + max(
        15, min(int(settings.command_timeout_apply_sec), 90)
    )
    last_message = "backend port is not reachable"
    while time.monotonic() < deadline:
        result = run_command(["bash", "-lc", script], timeout_sec=10)
        if result.ok:
            return f"tcp reachable at {host}:{port}"
        last_message = (result.stderr or result.stdout or last_message).strip()
        time.sleep(1)
    raise RuntimeError(last_message)


def _stop_local_backend(backend: Backend, settings: Settings) -> None:
    container = container_name(backend.name)
    network = network_name(backend.name)
    _run_cleanup_checked(
        ["systemctl", "stop", f"{container}.service"],
        settings,
        missing_markers=("not loaded", "not-found", "not found", "could not be found"),
    )
    _run_cleanup_checked(
        ["podman", "rm", "-f", container],
        settings,
        missing_markers=("no such container", "does not exist", "not found"),
    )
    _run_cleanup_checked(
        ["systemctl", "stop", f"{network}-network.service"],
        settings,
        missing_markers=("not loaded", "not-found", "not found", "could not be found"),
    )


async def _restore_target_rollback(
    backend: Backend,
    settings: Settings,
    rollback: TargetRollbackState,
    *,
    target_node: ClusterNode | None,
) -> list[str]:
    try:
        if rollback.target_uid == LOCAL_NODE_UID:
            await asyncio.to_thread(
                _restore_local_target_rollback, backend, settings, rollback
            )
            return ["local target rollback restored"]
        if target_node is None:
            return ["target node missing; restored target rollback skipped"]
        await asyncio.to_thread(
            _restore_remote_target_rollback,
            backend,
            settings,
            target_node,
            rollback,
        )
        return [f"remote target rollback restored on {target_node.name}"]
    except Exception as exc:
        logger.warning(
            "cluster_transfer.target_rollback_failed",
            backend=backend.name,
            target_uid=rollback.target_uid,
            error=str(exc),
        )
        return [f"rollback failed: {exc}"]


async def _discard_target_rollback(
    backend: Backend,
    settings: Settings,
    rollback: TargetRollbackState,
    *,
    target_node: ClusterNode | None,
) -> None:
    try:
        if rollback.target_uid == LOCAL_NODE_UID:
            await asyncio.to_thread(_discard_local_target_rollback, rollback)
            return
        if target_node is None:
            return
        await asyncio.to_thread(
            _discard_remote_target_rollback, settings, target_node, rollback
        )
    except Exception as exc:
        logger.warning(
            "cluster_transfer.target_rollback_discard_failed",
            backend=backend.name,
            target_uid=rollback.target_uid,
            error=str(exc),
        )


def _remove_tree_or_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


def _restore_local_target_rollback(
    backend: Backend, settings: Settings, rollback: TargetRollbackState
) -> None:
    _stop_local_backend(backend, settings)
    target_path = Path(rollback.target_path)
    rollback_path = Path(rollback.rollback_path)
    if target_path.exists():
        _remove_tree_or_path(target_path)
    if rollback_path.exists():
        shutil.move(str(rollback_path), str(target_path))
    if rollback.service_was_active:
        _run_checked(["systemctl", "daemon-reload"], settings)
        _run_checked(
            ["systemctl", "start", quadlet_network_service_name(backend.name)],
            settings,
        )
        _run_checked(
            ["systemctl", "start", quadlet_container_service_name(backend.name)],
            settings,
        )


def _discard_local_target_rollback(rollback: TargetRollbackState) -> None:
    rollback_path = Path(rollback.rollback_path)
    if rollback_path.exists():
        _remove_tree_or_path(rollback_path)


def _restore_remote_target_rollback(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    rollback: TargetRollbackState,
) -> None:
    _ssh_checked(node, settings, _remote_restore_rollback_script(backend, rollback))


def _discard_remote_target_rollback(
    settings: Settings,
    node: ClusterNode,
    rollback: TargetRollbackState,
) -> None:
    paths = [rollback.rollback_path]
    if rollback.unit_backup_path:
        paths.append(rollback.unit_backup_path)
    quoted_paths = " ".join(shlex.quote(path) for path in paths if path)
    if quoted_paths:
        _ssh_checked(node, settings, f"rm -rf {quoted_paths}")


def _remote_restore_rollback_script(
    backend: Backend, rollback: TargetRollbackState
) -> str:
    container = container_name(backend.name)
    network = network_name(backend.name)
    network_unit = f"{network}.network"
    container_unit = f"{container}.container"
    return f"""set -Eeuo pipefail
target_path={shlex.quote(rollback.target_path)}
rollback_path={shlex.quote(rollback.rollback_path)}
unit_backup_dir={shlex.quote(rollback.unit_backup_path)}
network_asset=/etc/containers/systemd/{shlex.quote(network_unit)}
container_asset=/etc/containers/systemd/{shlex.quote(container_unit)}
previous_runtime_active={"1" if rollback.service_was_active else "0"}
systemctl stop {shlex.quote(container)}.service >/dev/null 2>&1 || true
podman rm -f {shlex.quote(container)} >/dev/null 2>&1 || true
systemctl stop {shlex.quote(network)}-network.service >/dev/null 2>&1 || true
rm -rf "$target_path"
if [ -e "$rollback_path" ]; then
  mv "$rollback_path" "$target_path"
fi
if [ -d "$unit_backup_dir" ]; then
  if [ -e "$unit_backup_dir/{shlex.quote(network_unit)}" ]; then
    cp -a "$unit_backup_dir/{shlex.quote(network_unit)}" "$network_asset"
  else
    rm -f "$network_asset"
  fi
  if [ -e "$unit_backup_dir/{shlex.quote(container_unit)}" ]; then
    cp -a "$unit_backup_dir/{shlex.quote(container_unit)}" "$container_asset"
  else
    rm -f "$container_asset"
  fi
  rm -rf "$unit_backup_dir"
else
  rm -f "$network_asset" "$container_asset"
fi
systemctl daemon-reload
if [ "$previous_runtime_active" = "1" ]; then
  systemctl start {shlex.quote(network)}-network.service >/dev/null
  systemctl start {shlex.quote(container)}.service >/dev/null
fi
"""


def _cleanup_remote_restored_target(
    backend: Backend, settings: Settings, node: ClusterNode
) -> None:
    container = container_name(backend.name)
    network = network_name(backend.name)
    sandbox = str(Path("/var/lib/cnc/sandboxes") / backend.name)
    _ssh_checked(
        node,
        settings,
        "\n".join(
            [
                "set -Eeuo pipefail",
                _remote_cleanup_command(
                    f"systemctl stop {shlex.quote(container)}.service",
                    "not loaded|not-found|not found|could not be found",
                ),
                _remote_cleanup_command(
                    f"systemctl disable {shlex.quote(container)}.service",
                    "not loaded|not-found|not found|could not be found",
                ),
                _remote_cleanup_command(
                    f"podman rm -f {shlex.quote(container)}",
                    "no such container|does not exist|not found",
                ),
                _remote_cleanup_command(
                    f"systemctl stop {shlex.quote(network)}-network.service",
                    "not loaded|not-found|not found|could not be found",
                ),
                f"rm -rf {shlex.quote(sandbox)}",
                f"rm -f /etc/containers/systemd/{shlex.quote(network)}.network",
                f"rm -f /etc/containers/systemd/{shlex.quote(container)}.container",
                "systemctl daemon-reload",
            ]
        ),
    )


def _run_cleanup_checked(
    command: list[str], settings: Settings, *, missing_markers: tuple[str, ...]
) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if result.ok:
        return
    message = (result.stderr or result.stdout or "command failed").strip()
    lowered = message.lower()
    if any(marker in lowered for marker in missing_markers):
        return
    raise RuntimeError(message)


def _remote_cleanup_command(command: str, missing_pattern: str) -> str:
    return (
        f"if ! output=$({command} 2>&1); then "
        f"printf '%s\\n' \"$output\" | grep -Eiq {shlex.quote(missing_pattern)} "
        "|| { printf '%s\\n' \"$output\" >&2; exit 1; }; "
        "fi"
    )


def _ssh_checked(node: ClusterNode, settings: Settings, script: str) -> None:
    _run_checked(
        [
            "ssh",
            *SSH_BATCH_OPTIONS,
            f"root@{_node_ip(node)}",
            f"bash -lc {shlex.quote(script)}",
        ],
        settings,
    )


def _scp_checked(source: str, destination: str, settings: Settings) -> None:
    _run_checked(["scp", *SSH_BATCH_OPTIONS, source, destination], settings)


def _run_checked(command: list[str], settings: Settings) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if result.ok:
        return
    message = result.stderr or result.stdout or "command failed"
    raise RuntimeError(message.strip())
