from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.services.config_change import ConfigChange
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
from app.services.commands import CommandResult, run_command
from app.services.guest_isolation import (
    archive_guest,
    extract_guest,
    guest_helper_bytes,
    validate_guest_hardening,
)
from app.services.host_journal import CommitOutcome, commit_with_marker
from app.services.hardening_policy import read_hardening_policy
from app.services.renderers import container_name, network_name
from app.services.resource_profile import build_resource_profile
from app.services.replica_readiness import (
    REPLICA_SETUP_MODE_TRANSFER,
    delete_replica_readiness,
    record_replica_readiness,
    replica_target_url,
)
from app.services.operation_runtime import operation_progress_value
from app.services.operations import OperationHandle, host_mutation_operation
from app.services.app_healthchecks import probe_backend_health
from app.services.placement_config import (
    apply_backend_placement,
    backend_has_local_runtime,
    read_backend_placement,
)
from app.services.remote_node import RemoteNodeExecutor
from app.services.sandbox_profiles import app_sandbox_dir, get_app_sandbox_profile
from app.services.ssh_options import SSH_BATCH_OPTIONS
from app.services.thread_workers import HostMutationLifecycle
from app.services.validators import parse_volumes_json


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


class TransferCommitUnknown(RuntimeError):
    def __init__(self, message: str, run_id: int | None) -> None:
        super().__init__(message)
        self.details = {
            "apply_run_id": run_id,
            "commit_outcome": "unknown",
            "manual_review_required": True,
            "operator_action": message,
        }


REPLICA_SETUP_MODE_CLONE = "clone"
REPLICA_SETUP_MODE_FRESH = "fresh"
REPLICA_SETUP_MODES = {REPLICA_SETUP_MODE_CLONE, REPLICA_SETUP_MODE_FRESH}


async def transfer_backend(
    session: AsyncSession,
    settings: Settings,
    *,
    backend_id: int,
    target_node_uid: str,
    pause_source: bool,
    progress_callback: TransferProgressCallback | None = None,
    operation_handle: OperationHandle | None = None,
) -> TransferResult:
    """Move an app output to another node.

    With ``pause_source`` the source is frozen before its snapshot, so the
    target receives every write, and it is unfrozen again if the transfer
    fails at any point before cutover. Without it the source keeps serving
    during the copy and writes made after the snapshot are not carried over.
    """
    async with host_mutation_operation(
        settings,
        kind="transfer_backend",
        actor="ui",
        backend_id=backend_id,
        phase="Transfer output",
        operation=operation_handle.as_step() if operation_handle else None,
    ) as operation:
        if operation.id is None:
            raise RuntimeError("Could not record the transfer before starting it.")
        try:
            return await _transfer_backend(
                session,
                settings,
                backend_id=backend_id,
                target_node_uid=target_node_uid,
                pause_source=pause_source,
                progress_callback=progress_callback,
                operation=operation_handle or operation,
            )
        except BaseException as exc:
            try:
                await session.rollback()
            except SQLAlchemyError as rollback_error:
                logger.warning(
                    "cluster_transfer.session_rollback_failed",
                    error=str(rollback_error),
                )
            if isinstance(exc, TransferCommitUnknown):
                await operation.complete(
                    "partial",
                    phase="Verify placement",
                    error=str(exc),
                    details=exc.details,
                )
            raise


async def _transfer_backend(
    session: AsyncSession,
    settings: Settings,
    *,
    backend_id: int,
    target_node_uid: str,
    pause_source: bool,
    progress_callback: TransferProgressCallback | None,
    operation: OperationHandle,
) -> TransferResult:
    lifecycle = HostMutationLifecycle()

    def publish(message: str) -> None:
        operation.publish_progress(
            status="running",
            phase="Transfer output",
            details={
                "progress": operation_progress_value("transferOutput", message=message),
                "message": message,
                "substate": message,
                "backend_id": backend_id,
                "target_node": target_node_uid,
            },
        )

    def report(progress: int, message: str) -> None:
        publish(message)
        _report(progress_callback, progress, message)

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
    _reject_external_volumes(backend)

    source_node = (
        await _load_node(session, source_uid) if source_uid != LOCAL_NODE_UID else None
    )
    target_node = (
        await _load_node(session, target_uid) if target_uid != LOCAL_NODE_UID else None
    )
    messages: list[str] = []

    report(6, "Reading transfer request.")
    report(14, "Checking target node.")
    if target_node is not None and str(target_node.state or "").strip().lower() not in {
        "healthy",
        "joining",
    }:
        raise ValueError(f"target node is not healthy: {target_node.state}")

    # Host recovery may still need these values after a failed commit expires the session.
    session.expunge(backend)
    for node in (source_node, target_node):
        if node is not None:
            session.expunge(node)
    commit_unknown = False
    pause_started = False
    try:
        if pause_source:
            report(20, "Pausing source.")
            pause_started = True
            _write_transfer_pause_marker(settings, backend.name, source_node)
            await lifecycle.run(
                _set_source_paused, backend, settings, source_node, paused=True
            )
            messages.append("source paused so the copy includes every write")
        with tempfile.TemporaryDirectory(
            prefix=f"cnc-transfer-{backend.name}-"
        ) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            bundle_path = temp_dir / f"{backend.name}.tar.gz"
            if source_uid == LOCAL_NODE_UID:
                report(24, "Capturing source state.")
                await lifecycle.run(
                    _archive_local_backend, backend, settings, bundle_path
                )
            else:
                if source_node is None:
                    raise ValueError("source node is not linked")
                report(24, "Capturing source state from peer.")
                await lifecycle.run(
                    _archive_remote_backend, backend, settings, source_node, bundle_path
                )
            messages.append("snapshot captured")

            target_rollback: TargetRollbackState | None = None
            try:
                if target_uid == LOCAL_NODE_UID:
                    report(44, "Restoring output on leader.")
                    target_rollback = await lifecycle.run_recoverable(
                        _restore_local_backend, backend, settings, bundle_path
                    )
                    lifecycle.raise_if_cancelled()
                    messages.append("restored on leader")
                else:
                    if target_node is None:
                        raise ValueError("target node is not linked")
                    report(38, "Copying snapshot to target.")
                    target_rollback = await lifecycle.run_recoverable(
                        _restore_remote_backend,
                        backend,
                        settings,
                        target_node,
                        bundle_path,
                    )
                    lifecycle.raise_if_cancelled()
                    messages.append(f"restored on {target_node.name}")

                report(68, "Checking target health.")
                if target_uid == LOCAL_NODE_UID:
                    await lifecycle.run(
                        _start_local_backend_replica, backend, settings
                    )
                    healthcheck_result = await lifecycle.run(
                        _verify_local_backend, backend, settings
                    )
                    target_url = replica_target_url(backend, None, target_uid)
                else:
                    if target_node is None:
                        raise ValueError("target node is not linked")
                    healthcheck_result = await lifecycle.run(
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

                report(76, "Saving output placement.")
                placement_backend = await session.get(Backend, backend_id)
                apply_backend_placement(
                    placement_backend,
                    enabled=False,
                    mode="",
                    active_node_uid=target_uid,
                    selected_node_uids=(),
                )

                report(84, "Rendering cluster routes.")
                with session.no_autoflush:
                    apply_response = await lifecycle.finish(
                        run_apply(
                            session,
                            settings,
                            operation_kind="transfer_backend",
                            actor="ui",
                            operation_handle=operation.as_step(),
                            scope=ConfigChange(
                                "transfer_backend",
                                outputs=frozenset({backend.name}),
                                rebalance_resources=backend_has_local_runtime(backend)
                                != backend_has_local_runtime(placement_backend),
                            ).scope(),
                        )
                    )
                if apply_response.status != "success":
                    if (
                        getattr(apply_response, "details", {}).get("commit_outcome")
                        == "unknown"
                    ):
                        commit_unknown = True
                        source_action = (
                            "CNC requested a source pause."
                            if pause_source
                            else "The source stayed live during the copy."
                        )
                        message = (
                            f"Cannot verify transfer of {backend.name} from {source_uid} to {target_uid}. "
                            f"{source_action} Check the saved placement before retrying."
                        )
                        if target_rollback:
                            paths = filter(
                                None,
                                (
                                    target_rollback.rollback_path,
                                    target_rollback.unit_backup_path,
                                ),
                            )
                            message += f" Recovery files on {target_uid} are at {', '.join(paths)}."
                        raise TransferCommitUnknown(
                            message, apply_response.run_id,
                        )
                    raise RuntimeError(
                        f"host apply failed after transfer: {apply_response.message}"
                    )
                lifecycle.mark_committed()
                if pause_started:
                    _clear_transfer_pause_marker(settings, backend.name)
                if target_rollback is not None:
                    await lifecycle.finish(
                        _discard_target_rollback(
                            backend,
                            settings,
                            target_rollback,
                            target_node=target_node,
                            lifecycle=lifecycle,
                        )
                    )
                lifecycle.raise_if_cancelled()
            except BaseException as exc:
                if lifecycle.committed or commit_unknown:
                    raise
                rollback_messages: list[str] = []
                if target_rollback is not None:
                    publish("Restoring the target after transfer failure.")
                    rollback_messages = await lifecycle.recover(
                        lambda: _restore_target_rollback(
                            backend,
                            settings,
                            target_rollback,
                            target_node=target_node,
                            lifecycle=lifecycle,
                        )
                    )
                if rollback_messages and isinstance(exc, Exception):
                    raise RuntimeError(
                        f"{exc}; restored target rollback: {'; '.join(rollback_messages)}"
                    ) from exc
                raise
    except BaseException:
        if pause_started and not lifecycle.committed and not commit_unknown:
            publish("Resuming the source after transfer failure.")
            await lifecycle.recover(
                lambda: _resume_source_after_failure(
                    backend, settings, source_node, lifecycle=lifecycle
                )
            )
        raise
    if not pause_source:
        messages.append(
            "source kept running during the copy; writes made after the snapshot "
            "were not carried over"
        )

    report(92, "Stopping source runtime.")
    cleaned_remote_node_uids: list[str] = []
    cleanup_remote_node_uids = tuple(
        node_uid
        for node_uid in dict.fromkeys(previous_runtime_node_uids)
        if node_uid not in {LOCAL_NODE_UID, target_uid}
    )
    if source_uid != target_uid and source_uid == LOCAL_NODE_UID:
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
                await lifecycle.run(
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

    report(100, "Transfer complete.")
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
    _lifecycle: HostMutationLifecycle | None = None,
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
                _lifecycle=HostMutationLifecycle(),
            )

    lifecycle = _lifecycle or HostMutationLifecycle()

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

    # Recovery needs these already-loaded values even after SQLAlchemy expires
    # attached rows on rollback. Replica setup only writes the readiness row.
    session.expunge(backend)
    if target_node is not None:
        session.expunge(target_node)

    commit_outcome: CommitOutcome = "rolled_back"
    target_rollback: TargetRollbackState | None = None
    try:
        if normalized_mode == REPLICA_SETUP_MODE_CLONE:
            if target_uid == source_uid:
                messages.append("selected node already has the source runtime")
            else:
                _reject_external_volumes(backend)
                with tempfile.TemporaryDirectory(
                    prefix=f"cnc-replica-{backend.name}-"
                ) as temp_dir_name:
                    temp_dir = Path(temp_dir_name)
                    bundle_path = temp_dir / f"{backend.name}.tar.gz"
                    if source_uid == LOCAL_NODE_UID:
                        _report(progress_callback, 24, "Capturing source state.")
                        await lifecycle.run(
                            _archive_local_backend, backend, settings, bundle_path
                        )
                    else:
                        if source_node is None:
                            raise ValueError("source node is not linked")
                        _report(progress_callback, 24, "Capturing source state from peer.")
                        await lifecycle.run(
                            _archive_remote_backend,
                            backend,
                            settings,
                            source_node,
                            bundle_path,
                        )
                    messages.append("snapshot captured")
                    if target_uid == LOCAL_NODE_UID:
                        _report(progress_callback, 54, "Restoring replica on leader.")
                        target_rollback = await lifecycle.run_recoverable(
                            _restore_local_backend, backend, settings, bundle_path
                        )
                        lifecycle.raise_if_cancelled()
                        await lifecycle.run(
                            _start_local_backend_replica, backend, settings
                        )
                        messages.append("replica restored on leader")
                    else:
                        if target_node is None:
                            raise ValueError("target node is not linked")
                        _report(progress_callback, 44, "Copying snapshot to target.")
                        target_rollback = await lifecycle.run_recoverable(
                            _restore_remote_backend,
                            backend,
                            settings,
                            target_node,
                            bundle_path,
                        )
                        lifecycle.raise_if_cancelled()
                        messages.append(f"replica restored on {target_node.name}")
        else:
            if target_uid == LOCAL_NODE_UID:
                raise ValueError("fresh leader setup is handled by saving placement")
            if target_node is None:
                raise ValueError("target node is not linked")
            _report(progress_callback, 24, "Creating fresh guest.")
            target_rollback = await lifecycle.run_recoverable(
                _setup_remote_fresh_backend, backend, settings, target_node
            )
            lifecycle.raise_if_cancelled()
            messages.append(f"fresh guest created on {target_node.name}")

        _report(progress_callback, 76, "Checking target health.")
        if target_uid == LOCAL_NODE_UID:
            healthcheck_result = await lifecycle.run(
                _verify_local_backend, backend, settings
            )
            target_url = replica_target_url(backend, None, target_uid)
        else:
            if target_node is None:
                raise ValueError("target node is not linked")
            healthcheck_result = await lifecycle.run(
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
        marker = uuid4().hex
        commit_outcome = "unknown"
        commit_outcome, error = await lifecycle.finish(
            commit_with_marker(session, settings, marker, "replica_setup")
        )
        if commit_outcome == "unknown":
            raise RuntimeError(
                f"Cannot verify replica commit {marker} for {backend.name} on {target_uid}. "
                f"Target and recovery files retained: {target_rollback}. {error}"
            )
        if commit_outcome == "rolled_back":
            raise RuntimeError(f"Replica readiness was not saved: {error}")
        if target_rollback is not None:
            await lifecycle.finish(
                _discard_target_rollback(
                    backend,
                    settings,
                    target_rollback,
                    target_node=target_node,
                    lifecycle=lifecycle,
                )
            )
        lifecycle.raise_if_cancelled()
    except BaseException as exc:
        if commit_outcome != "rolled_back":
            raise

        async def restore_precommit_state() -> list[str]:
            await session.rollback()
            if target_rollback is None:
                return []
            return await _restore_target_rollback(
                backend,
                settings,
                target_rollback,
                target_node=target_node,
                lifecycle=lifecycle,
            )

        rollback_messages = await lifecycle.recover(restore_precommit_state)
        if rollback_messages and isinstance(exc, Exception):
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


def _reject_external_volumes(backend: Backend) -> None:
    # The transfer archive carries only the sandbox. Mounted host paths would be
    # left behind while the target still binds them, so refuse before copying.
    if parse_volumes_json(backend.volumes_json):
        raise ValueError(
            "outputs with mounted volumes cannot be copied to another node yet"
        )


def _archive_local_backend(
    backend: Backend, settings: Settings, bundle_path: Path
) -> None:
    sandbox = app_sandbox_dir(settings, backend.name)
    if not sandbox.exists():
        raise RuntimeError(f"source sandbox is missing: {sandbox}")
    archive_guest(sandbox, bundle_path, container_name(backend.name))


def _remote_executor(node: ClusterNode, settings: Settings) -> RemoteNodeExecutor:
    return RemoteNodeExecutor(
        str(node.node_uid), _node_ip(node), settings, run_command_fn=run_command
    )


def _archive_remote_backend(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    bundle_path: Path,
) -> None:
    remote_path = _remote_transfer_path(backend)
    executor = _remote_executor(node, settings)
    try:
        executor.invoke(
            "archive_sandbox",
            {
                "backend": backend.name,
                "remote_path": remote_path,
                "guest_helper_b64": base64.b64encode(guest_helper_bytes()).decode("ascii"),
            },
        )
        executor.scp_download(remote_path, bundle_path)
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
    stage_parent.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        extract_guest(bundle_path, stage_parent, backend.name)
        staged = stage_parent / backend.name
        if not staged.is_dir():
            raise RuntimeError(
                f"transfer bundle missing sandbox directory: {backend.name}"
            )
        try:
            _stop_local_backend(backend, settings)
            if target.exists():
                target.rename(rollback)
            staged.rename(target)
        except Exception:
            if rollback.exists() and not target.exists():
                rollback.rename(target)
            if service_was_active:
                _run_checked(
                    ["systemctl", "start", quadlet_network_service_name(backend.name)],
                    settings,
                )
                _run_checked(
                    ["systemctl", "start", quadlet_container_service_name(backend.name)],
                    settings,
                )
            raise
        return TargetRollbackState(
            target_uid=LOCAL_NODE_UID,
            target_path=str(target),
            rollback_path=str(rollback),
            service_was_active=service_was_active,
        )
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def _remote_restore_payload(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    remote_path: str,
    *,
    rollback: TargetRollbackState,
) -> dict[str, object]:
    network_asset_content, container_asset_content = _remote_quadlet_assets(
        backend, settings, node
    )
    return {
        "backend": backend.name,
        "container": container_name(backend.name),
        "network": network_name(backend.name),
        "network_unit": quadlet_network_unit_name(backend.name),
        "container_unit": quadlet_container_unit_name(backend.name),
        "network_unit_content": network_asset_content,
        "container_unit_content": container_asset_content,
        "remote_path": remote_path,
        "target_path": rollback.target_path,
        "rollback_path": rollback.rollback_path,
        "unit_backup_path": rollback.unit_backup_path,
        "service_was_active": rollback.service_was_active,
        "resource_mode": str(getattr(backend, "resource_mode", "auto") or "auto"),
        "resource_size": str(getattr(backend, "resource_size", "small") or "small"),
        "memory_high_override": getattr(backend, "memory_high_override", None),
        "memory_max_override": getattr(backend, "memory_max_override", None),
        "cpu_quota_override": getattr(backend, "cpu_quota_override", None),
        "guest_helper_b64": base64.b64encode(guest_helper_bytes()).decode("ascii"),
    }


def _remote_fresh_setup_payload(
    backend: Backend,
    settings: Settings,
    node: ClusterNode,
    *,
    rollback: TargetRollbackState,
) -> dict[str, object]:
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    payload = _remote_restore_payload(
        backend, settings, node, "", rollback=rollback
    )
    payload.update(
        {
            "seed_image": profile.seed_image,
            "provision_script": profile.provision_script,
            "self_test_command": list(profile.self_test_command),
            "profile_state": {
                "sandbox_profile": profile.profile_id,
                "seed_image": profile.seed_image,
                "init_command": list(profile.init_command),
                "seed_revision": profile.seed_revision,
            },
        }
    )
    return payload


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
    executor = _remote_executor(node, settings)
    remote_ready = False
    try:
        executor.ssh("set -Eeuo pipefail\ninstall -d -m 0755 /var/lib/cnc/cluster-transfers")
        remote_ready = True
        executor.scp_upload(bundle_path, remote_path)
        executor.invoke(
            "restore_sandbox",
            _remote_restore_payload(
                backend, settings, node, remote_path, rollback=rollback
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
    _remote_executor(node, settings).invoke(
        "setup_fresh",
        _remote_fresh_setup_payload(backend, settings, node, rollback=rollback),
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
            quadlet_container_service_name(backend.name),
        ],
        timeout_sec=settings.command_timeout_status_sec,
    )
    return _observed_active(result)


def _remote_backend_service_active(
    backend: Backend, settings: Settings, node: ClusterNode
) -> bool:
    result = run_command(
        [
            "ssh",
            *SSH_BATCH_OPTIONS,
            f"root@{_node_ip(node)}",
            f"systemctl is-active {shlex.quote(quadlet_container_service_name(backend.name))}",
        ],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    return _observed_active(result)


def _observed_active(result: CommandResult) -> bool:
    state = result.stdout.strip()
    if result.ok and state == "active":
        return True
    if result.returncode in {3, 4} and state in {"inactive", "failed", "unknown"}:
        return False
    raise RuntimeError(
        f"Cannot determine target service state: {result.stderr or state or result.returncode}"
    )


def _cleanup_remote_transfer_path(
    node: ClusterNode, settings: Settings, remote_path: str
) -> None:
    try:
        _remote_executor(node, settings).ssh(f"rm -f {shlex.quote(remote_path)}")
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
    validate_guest_hardening(read_hardening_policy(backend).model_dump())
    settings = settings.model_copy(
        update={"guest_runtime_helper_path": Path("/usr/local/bin/cnc-prepare-guest")}
    )
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


def _verify_remote_backend(
    backend: Backend, settings: Settings, node: ClusterNode
) -> str:
    port = int(backend.port or 0)
    if port <= 0:
        raise RuntimeError("backend has no published port")
    result = _remote_executor(node, settings).invoke(
        "verify_health",
        {
            "host": _node_ip(node),
            "port": port,
            "container": container_name(backend.name),
            "healthcheck_mode": str(backend.healthcheck_mode or "http"),
            "healthcheck_path": str(backend.healthcheck_path or "/"),
            "healthcheck_host_header": str(backend.healthcheck_host_header or ""),
            "timeout_sec": max(15, min(int(settings.command_timeout_apply_sec), 90)),
        },
    )
    return str(result.get("summary") or f"healthy at {_node_ip(node)}:{port}")


def _verify_local_backend(backend: Backend, settings: Settings) -> str:
    port = int(backend.port or 0)
    if port <= 0:
        raise RuntimeError("backend has no published port")
    deadline = time.monotonic() + max(
        15, min(int(settings.command_timeout_apply_sec), 90)
    )
    last_message = "backend healthcheck failed"
    while time.monotonic() < deadline:
        probe = probe_backend_health(
            backend, host="127.0.0.1", port=port, timeout_sec=5
        )
        if probe.ok:
            if probe.http_status is not None:
                return f"http {probe.http_status} at 127.0.0.1:{port}{probe.plan.path or '/'}"
            return f"healthy at 127.0.0.1:{port}"
        last_message = probe.error or last_message
        time.sleep(1)
    raise RuntimeError(last_message)


def _set_source_paused(
    backend: Backend,
    settings: Settings,
    node: ClusterNode | None,
    *,
    paused: bool,
) -> None:
    if node is None:
        _run_checked(
            ["podman", "pause" if paused else "unpause", container_name(backend.name)],
            settings,
        )
    else:
        _remote_executor(node, settings).invoke(
            "pause_container",
            {"container": container_name(backend.name), "paused": paused},
        )


def _transfer_pause_marker_path(settings: Settings, backend_name: str) -> Path:
    return settings.app_sandbox_dir / f".cnc-transfer-paused-{backend_name}.json"


def _write_transfer_pause_marker(
    settings: Settings, backend_name: str, source_node: ClusterNode | None
) -> None:
    path = _transfer_pause_marker_path(settings, backend_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "backend": backend_name,
        "container": container_name(backend_name),
        "source_node_uid": str(source_node.node_uid) if source_node is not None else LOCAL_NODE_UID,
        "source_node_ip": _node_ip(source_node) if source_node is not None else "",
        "paused_at": datetime.now(UTC).isoformat(),
    }
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def _clear_transfer_pause_marker(settings: Settings, backend_name: str) -> None:
    try:
        _transfer_pause_marker_path(settings, backend_name).unlink()
    except FileNotFoundError:
        pass


def recover_interrupted_transfer_artifacts(settings: Settings) -> dict[str, Any]:
    """Unpause any CNC-paused transfer source and remove stale rollback dirs on startup."""
    unpaused: list[str] = []
    removed_rollbacks: list[str] = []
    errors: list[str] = []

    if settings.app_sandbox_dir.is_dir():
        for marker in sorted(settings.app_sandbox_dir.glob(".cnc-transfer-paused-*.json")):
            if not marker.is_file():
                continue
            backend_name = marker.name.removeprefix(".cnc-transfer-paused-").removesuffix(".json")
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                backend_name = str(payload.get("backend") or backend_name)
                container = str(payload.get("container") or container_name(backend_name))
                source_uid = str(payload.get("source_node_uid") or LOCAL_NODE_UID)
                source_ip = str(payload.get("source_node_ip") or "").strip()
                if source_uid == LOCAL_NODE_UID or not source_ip:
                    result = run_command(
                        ["podman", "unpause", container],
                        timeout_sec=settings.command_timeout_status_sec,
                    )
                else:
                    result = run_command(
                        [
                            "ssh",
                            *SSH_BATCH_OPTIONS,
                            f"root@{source_ip}",
                            f"podman unpause {shlex.quote(container)}",
                        ],
                        timeout_sec=settings.command_timeout_apply_sec,
                    )
                if result.ok or "not paused" in (result.stderr or "").lower() or "no such container" in (result.stderr or "").lower():
                    marker.unlink(missing_ok=True)
                    unpaused.append(backend_name)
                else:
                    errors.append(
                        f"unpause {backend_name}: {result.stderr or result.stdout}"
                    )
            except Exception as exc:
                errors.append(f"unpause {backend_name}: {exc}")

        for entry in sorted(settings.app_sandbox_dir.iterdir()):
            if entry.is_dir() and entry.name.startswith(".cnc-transfer-rollback-"):
                try:
                    shutil.rmtree(entry)
                    removed_rollbacks.append(str(entry))
                except OSError as exc:
                    errors.append(f"remove {entry}: {exc}")

    if unpaused or removed_rollbacks or errors:
        logger.warning(
            "cluster_transfer.startup_recovery",
            unpaused=unpaused,
            removed_rollbacks=removed_rollbacks,
            errors=errors,
        )
    return {
        "unpaused": unpaused,
        "removed_rollbacks": removed_rollbacks,
        "errors": errors,
    }


async def _resume_source_after_failure(
    backend: Backend,
    settings: Settings,
    node: ClusterNode | None,
    *,
    lifecycle: HostMutationLifecycle,
) -> None:
    try:
        await lifecycle.run(
            _set_source_paused, backend, settings, node, paused=False
        )
        _clear_transfer_pause_marker(settings, backend.name)
    except Exception as exc:
        # The transfer error stays the one raised; this one must still reach
        # the operator, since the source is left frozen until it is resumed.
        logger.error(
            "cluster_transfer.source_resume_failed",
            backend=backend.name,
            error=str(exc),
            action=f"podman unpause {container_name(backend.name)}",
        )


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
    lifecycle: HostMutationLifecycle,
) -> list[str]:
    try:
        if rollback.target_uid == LOCAL_NODE_UID:
            await lifecycle.run(
                _restore_local_target_rollback, backend, settings, rollback
            )
            return ["local target rollback restored"]
        if target_node is None:
            return ["target node missing; restored target rollback skipped"]
        await lifecycle.run(
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
    lifecycle: HostMutationLifecycle,
) -> None:
    try:
        if rollback.target_uid == LOCAL_NODE_UID:
            await lifecycle.run(_discard_local_target_rollback, rollback)
            return
        if target_node is None:
            return
        await lifecycle.run(
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
    _remote_executor(node, settings).invoke(
        "restore_rollback",
        {
            "backend": backend.name,
            "container": container_name(backend.name),
            "network": network_name(backend.name),
            "network_unit": quadlet_network_unit_name(backend.name),
            "container_unit": quadlet_container_unit_name(backend.name),
            "target_path": rollback.target_path,
            "rollback_path": rollback.rollback_path,
            "unit_backup_path": rollback.unit_backup_path,
            "service_was_active": rollback.service_was_active,
        },
    )


def _discard_remote_target_rollback(
    settings: Settings,
    node: ClusterNode,
    rollback: TargetRollbackState,
) -> None:
    _remote_executor(node, settings).invoke(
        "discard_rollback",
        {
            "rollback_path": rollback.rollback_path,
            "unit_backup_path": rollback.unit_backup_path,
        },
    )


def _cleanup_remote_restored_target(
    backend: Backend, settings: Settings, node: ClusterNode
) -> None:
    _remote_executor(node, settings).invoke(
        "cleanup_backend",
        {
            "backend": backend.name,
            "container": container_name(backend.name),
            "network": network_name(backend.name),
            "network_unit": quadlet_network_unit_name(backend.name),
            "container_unit": quadlet_container_unit_name(backend.name),
        },
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


def _run_checked(command: list[str], settings: Settings) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if result.ok:
        return
    message = result.stderr or result.stdout or "command failed"
    raise RuntimeError(message.strip())
