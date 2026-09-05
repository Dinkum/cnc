from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.models.entities import ApplyRun, BackendBackup, Operation, UpdateRun
from app.services.file_locks import FileLock
from app.services.history_retention import prune_control_plane_history
from app.services.operation_progress import (
    clear_operation_progress,
    write_operation_progress,
)


OperationStatus = Literal[
    "queued", "running", "success", "failed", "partial", "cancelled"
]
ACTIVE_OPERATION_STATUSES = ("queued", "running")
ACTIVE_UPDATE_RUN_STATUSES = ("queued", "running")
HOST_MUTATION_OPERATION_KINDS = (
    "apply_host",
    "auto_size",
    "backup_backend",
    "clone_backend",
    "cloudflare_sync",
    "create_backend",
    "delete_backend",
    "delete_backend_backup",
    "import_backend_backup",
    "repair_backend",
    "restore_backend",
    "setup_backend_replica",
    "transfer_backend",
    "ui.backend.create",
    "ui.backend.delete",
    "ui.backend.inputs",
    "ui.backend.interface",
    "ui.backend.placement",
    "ui.backend.state",
    "ui.backend.update",
    "ui.input.create",
    "ui.input.delete",
    "ui.input.update",
    "update_control_plane",
)

logger = get_logger("operations")
_LOCK_DEPTH: ContextVar[int] = ContextVar("host_mutation_lock_depth", default=0)
TERMINAL_UPDATE_RETRY_ATTEMPTS = 3
TERMINAL_UPDATE_RETRY_BACKOFF_SEC = 0.15


class HostMutationLockError(RuntimeError):
    def __init__(
        self, message: str, *, blocker: HostMutationBlocker | None = None
    ) -> None:
        super().__init__(message)
        self.blocker = blocker


class OperationHandleContractError(TypeError):
    pass


@dataclass(frozen=True)
class HostMutationBlocker:
    id: int | None
    kind: str
    status: str
    phase: str


@asynccontextmanager
async def _operation_session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_configured_async_engine(database_url, future=True, echo=False)
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _json_dumps(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, default=str)


def _json_loads_details(payload: str | None) -> dict[str, Any]:
    try:
        details = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {"raw": payload}
    return details if isinstance(details, dict) else {"raw": payload}


@dataclass
class OperationHandle:
    id: int | None
    kind: str
    settings: Settings
    completed: bool = False
    defer_db_updates: bool = False

    def with_deferred_db_updates(self) -> "OperationHandle":
        return OperationHandle(
            id=self.id,
            kind=self.kind,
            settings=self.settings,
            completed=self.completed,
            defer_db_updates=True,
        )

    async def update(
        self,
        *,
        status: OperationStatus | None = None,
        phase: str | None = None,
        error: str | None = None,
        config_revision: str | None = None,
        desired_state_hash: str | None = None,
        details: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> None:
        if self.id is None:
            if finished:
                self.completed = True
            return
        if (
            details is not None
            or phase is not None
            or status is not None
            or error is not None
        ):
            try:
                write_operation_progress(
                    self.settings,
                    self.id,
                    status=status,
                    phase=phase,
                    details=details,
                    error=error,
                )
            except OSError as exc:
                logger.warning(
                    "operation.progress_snapshot_skipped",
                    operation_id=self.id,
                    kind=self.kind,
                    error=str(exc),
                )
        if self.defer_db_updates:
            if finished:
                self.completed = True
            return
        attempts = TERMINAL_UPDATE_RETRY_ATTEMPTS if finished else 1
        for attempt in range(1, attempts + 1):
            try:
                async with _operation_session(self.settings.database_url) as session:
                    operation = await session.get(Operation, self.id)
                    if operation is None:
                        return
                    if status is not None:
                        operation.status = status
                    if phase is not None:
                        operation.phase = phase
                    if error is not None:
                        operation.error = error
                    if config_revision is not None:
                        operation.config_revision = config_revision
                    if desired_state_hash is not None:
                        operation.desired_state_hash = desired_state_hash
                    if details is not None:
                        operation.details_json = _json_dumps(
                            {**_json_loads_details(operation.details_json), **details}
                        )
                    if finished:
                        operation.finished_at = datetime.now(UTC)
                        await prune_control_plane_history(session)
                    await session.commit()
                    if finished:
                        self.completed = True
                        try:
                            clear_operation_progress(self.settings, self.id)
                        except OSError as exc:
                            logger.warning(
                                "operation.progress_snapshot_cleanup_skipped",
                                operation_id=self.id,
                                kind=self.kind,
                                error=str(exc),
                            )
                    return
            except SQLAlchemyError as exc:
                logger.warning(
                    "operation.update_skipped",
                    operation_id=self.id,
                    kind=self.kind,
                    finished=finished,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                )
                if attempt < attempts:
                    await asyncio.sleep(TERMINAL_UPDATE_RETRY_BACKOFF_SEC * attempt)

    async def complete(
        self,
        status: OperationStatus,
        *,
        phase: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.update(
            status=status, phase=phase, error=error, details=details, finished=True
        )


class OperationHandleProxy:
    def __init__(self, operation: OperationHandle) -> None:
        self.operation = operation
        self.settings = operation.settings

    @property
    def id(self) -> int | None:
        return self.operation.id

    @property
    def kind(self) -> str:
        return self.operation.kind

    @property
    def completed(self) -> bool:
        return self.operation.completed

    @completed.setter
    def completed(self, value: bool) -> None:
        self.operation.completed = value

    @property
    def defer_db_updates(self) -> bool:
        return self.operation.defer_db_updates

    def __getattr__(self, name: str) -> Any:
        return getattr(self.operation, name)

    def _wrap_operation(self, operation: OperationHandle) -> "OperationHandleProxy":
        return type(self)(operation)

    def with_deferred_db_updates(self) -> "OperationHandleProxy":
        return self._wrap_operation(self.operation.with_deferred_db_updates())

    async def update(
        self,
        *,
        status: OperationStatus | None = None,
        phase: str | None = None,
        error: str | None = None,
        config_revision: str | None = None,
        desired_state_hash: str | None = None,
        details: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> None:
        await self.operation.update(
            status=status,
            phase=phase,
            error=error,
            config_revision=config_revision,
            desired_state_hash=desired_state_hash,
            details=details,
            finished=finished,
        )

    async def complete(
        self,
        status: OperationStatus,
        *,
        phase: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.operation.complete(status, phase=phase, error=error, details=details)


def validate_operation_handle_contract(operation_handle: object) -> None:
    required_data = ("id", "kind", "settings", "completed")
    required_methods = ("update", "complete", "with_deferred_db_updates")
    for name in required_data:
        try:
            getattr(operation_handle, name)
        except AttributeError as exc:
            raise OperationHandleContractError(
                f"operation handle missing required attribute: {name}"
            ) from exc
    for name in required_methods:
        member = getattr(operation_handle, name, None)
        if not callable(member):
            raise OperationHandleContractError(
                f"operation handle missing required method: {name}"
            )
    try:
        operation_handle.completed = bool(operation_handle.completed)
    except AttributeError as exc:
        raise OperationHandleContractError(
            "operation handle must allow completed state updates"
        ) from exc
    deferred = operation_handle.with_deferred_db_updates()
    for name in required_data:
        try:
            getattr(deferred, name)
        except AttributeError as exc:
            raise OperationHandleContractError(
                f"deferred operation handle missing required attribute: {name}"
            ) from exc
    for name in required_methods:
        member = getattr(deferred, name, None)
        if not callable(member):
            raise OperationHandleContractError(
                f"deferred operation handle missing required method: {name}"
            )


async def create_operation(
    settings: Settings,
    *,
    kind: str,
    actor: str = "system",
    backend_id: int | None = None,
    phase: str | None = None,
    status: OperationStatus = "queued",
    config_revision: str | None = None,
    desired_state_hash: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperationHandle:
    database_url = getattr(settings, "database_url", None)
    if not database_url:
        return OperationHandle(id=None, kind=kind, settings=settings)
    async with _operation_session(str(database_url)) as session:
        operation = Operation(
            kind=kind,
            status=status,
            phase=phase,
            actor=actor,
            backend_id=backend_id,
            config_revision=config_revision,
            desired_state_hash=desired_state_hash,
            details_json=_json_dumps(details),
        )
        session.add(operation)
        try:
            await session.commit()
            await session.refresh(operation)
        except SQLAlchemyError as exc:
            await session.rollback()
            logger.warning("operation.create_skipped", kind=kind, error=str(exc))
            return OperationHandle(id=None, kind=kind, settings=settings)
        logger.info(
            "operation.created",
            operation_id=operation.id,
            kind=kind,
            status=status,
            phase=phase,
        )
        return OperationHandle(id=operation.id, kind=kind, settings=settings)


async def active_host_mutation_blocker(
    settings: Settings,
) -> HostMutationBlocker | None:
    database_url = getattr(settings, "database_url", None)
    if database_url:
        try:
            async with _operation_session(str(database_url)) as session:
                operation = (
                    await session.execute(
                        select(Operation)
                        .where(Operation.status.in_(ACTIVE_OPERATION_STATUSES))
                        .where(Operation.kind.in_(HOST_MUTATION_OPERATION_KINDS))
                        .order_by(Operation.id.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if operation is not None:
                    return HostMutationBlocker(
                        id=operation.id,
                        kind=operation.kind,
                        status=operation.status,
                        phase=operation.phase or "",
                    )
                update_blocker = await _active_update_run_blocker_from_session(
                    session, settings
                )
                if update_blocker is not None:
                    return update_blocker
        except SQLAlchemyError as exc:
            logger.warning("operation.active_blocker_lookup_skipped", error=str(exc))

    if _LOCK_DEPTH.get() > 0:
        return None
    lock_path = _host_lock_path(settings)
    lock = FileLock(lock_path, blocking=False, lock_path=lock_path)
    try:
        lock.__enter__()
    except BlockingIOError:
        return HostMutationBlocker(
            id=None, kind="host_mutation", status="running", phase="lock"
        )
    else:
        lock.__exit__(None, None, None)
    return None


async def _reconcile_update_run_for_blocker(
    session: AsyncSession, settings: Settings, run: UpdateRun
) -> UpdateRun:
    try:
        from app.services.update_service import reconcile_update_run

        reconciled = await reconcile_update_run(
            session, settings, run, notification_mode="background"
        )
    except Exception as exc:
        logger.warning(
            "operation.update_blocker_reconcile_failed",
            update_run_id=run.id,
            error=str(exc),
        )
        return run
    return reconciled or run


async def _active_update_run_blocker_from_session(
    session: AsyncSession, settings: Settings
) -> HostMutationBlocker | None:
    update_runs = list(
        (
            await session.execute(
                select(UpdateRun)
                .where(UpdateRun.status.in_(ACTIVE_UPDATE_RUN_STATUSES))
                .order_by(UpdateRun.id.asc())
            )
        ).scalars()
    )
    for update_run in update_runs:
        update_run = await _reconcile_update_run_for_blocker(
            session, settings, update_run
        )
        if update_run.status not in ACTIVE_UPDATE_RUN_STATUSES:
            continue
        return HostMutationBlocker(
            id=update_run.id,
            kind="update_control_plane",
            status=update_run.status,
            phase="background_update",
        )
    return None


async def _active_update_run_blocker(settings: Settings) -> HostMutationBlocker | None:
    database_url = getattr(settings, "database_url", None)
    if not database_url:
        return None
    try:
        async with _operation_session(str(database_url)) as session:
            return await _active_update_run_blocker_from_session(session, settings)
    except SQLAlchemyError as exc:
        logger.warning("operation.update_blocker_lookup_skipped", error=str(exc))
        return None


async def fail_interrupted_operations(settings: Settings) -> int:
    database_url = getattr(settings, "database_url", None)
    if not database_url:
        return 0
    async with _operation_session(str(database_url)) as session:
        operations = (
            (
                await session.execute(
                    select(Operation)
                    .where(Operation.status.in_(("queued", "running")))
                    .order_by(Operation.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not operations:
            await prune_control_plane_history(session)
            await session.commit()
            return 0
        finished_at = datetime.now(UTC)
        for operation in operations:
            try:
                details = json.loads(operation.details_json or "{}")
            except json.JSONDecodeError:
                details = {"raw": operation.details_json}
            if (
                operation.kind == "auto_size"
                and operation.status == "running"
                and await _operation_has_successful_apply_run(
                    session, operation, details
                )
            ):
                operation.status = "success"
                operation.phase = "completed"
                operation.error = None
                operation.finished_at = finished_at
                if isinstance(details, dict):
                    details["message"] = (
                        "Auto-size operation recovered from committed apply run."
                    )
                    details["startup_recovered"] = True
                    operation.details_json = _json_dumps(details)
                continue
            if (
                operation.kind in {"backup_backend", "import_backend_backup"}
                and operation.status == "running"
                and await _operation_has_successful_backup(session, operation, details)
            ):
                operation.status = "success"
                operation.phase = "completed"
                operation.error = None
                operation.finished_at = finished_at
                if isinstance(details, dict):
                    details["message"] = (
                        "Backup operation recovered from committed backup row."
                    )
                    details["startup_recovered"] = True
                    operation.details_json = _json_dumps(details)
                continue
            operation.status = "failed"
            operation.error = "operation interrupted by CNC restart"
            operation.finished_at = finished_at
            if isinstance(details, dict):
                details["message"] = (
                    details.get("message") or "Operation interrupted by CNC restart."
                )
                details["interrupted"] = True
                operation.details_json = _json_dumps(details)
        await prune_control_plane_history(session)
        await session.commit()
        logger.warning("operations.interrupted_marked_failed", count=len(operations))
        return len(operations)


async def _operation_has_successful_apply_run(
    session: AsyncSession,
    operation: Operation,
    details: dict[str, Any],
) -> bool:
    apply_run_id = details.get("apply_run_id")
    if isinstance(apply_run_id, int):
        run = await session.get(ApplyRun, apply_run_id)
        return (
            run is not None
            and run.operation_id == operation.id
            and run.status == "success"
        )
    run = (
        await session.execute(
            select(ApplyRun)
            .where(ApplyRun.operation_id == operation.id)
            .where(ApplyRun.status == "success")
            .order_by(ApplyRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        run is not None and run.operation_id == operation.id and run.status == "success"
    )


async def _operation_has_successful_backup(
    session: AsyncSession,
    operation: Operation,
    details: dict[str, Any],
) -> bool:
    backup_id = details.get("backup_id")
    if isinstance(backup_id, int):
        backup = await session.get(BackendBackup, backup_id)
        return (
            backup is not None
            and backup.status == "success"
            and backup.operation_id == operation.id
        )
    query = (
        select(BackendBackup)
        .where(BackendBackup.operation_id == operation.id)
        .where(BackendBackup.status == "success")
        .order_by(BackendBackup.id.desc())
        .limit(1)
    )
    if operation.backend_id is not None:
        query = query.where(BackendBackup.backend_id == operation.backend_id)
    backup = (await session.execute(query)).scalar_one_or_none()
    return backup is not None


def validate_host_mutation_lock_path(settings: Settings) -> Path:
    path = Path(
        getattr(settings, "apply_lock_path", Path("data/host-mutation.lock"))
    ).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
    except OSError as exc:
        raise RuntimeError(
            f"host mutation lock path is unusable: {path}: {exc}"
        ) from exc
    return path.resolve()


def _host_lock_path(settings: Settings) -> Path:
    return validate_host_mutation_lock_path(settings)


def try_acquire_host_mutation_lock(settings: Settings) -> FileLock | None:
    if _LOCK_DEPTH.get() > 0:
        return None
    lock_path = _host_lock_path(settings)
    lock = FileLock(lock_path, blocking=False, lock_path=lock_path)
    try:
        lock.__enter__()
    except BlockingIOError:
        return None
    return lock


@asynccontextmanager
async def host_mutation_operation(
    settings: Settings,
    *,
    kind: str,
    actor: str = "system",
    backend_id: int | None = None,
    phase: str | None = None,
    details: dict[str, Any] | None = None,
    operation: OperationHandle | None = None,
    defer_on_update_blocker: bool = False,
) -> AsyncIterator[OperationHandle]:
    operation = operation or await create_operation(
        settings,
        kind=kind,
        actor=actor,
        backend_id=backend_id,
        phase=phase,
        details=details,
    )
    lock: FileLock | None = None
    token = None

    async def complete_safely(
        status: OperationStatus,
        *,
        complete_phase: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        task = asyncio.create_task(
            operation.complete(
                status, phase=complete_phase, error=error, details=details
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def complete_update_blocker(
        update_blocker: HostMutationBlocker,
    ) -> HostMutationLockError:
        blocker_details = {
            "blocker_id": update_blocker.id,
            "blocker_kind": update_blocker.kind,
            "blocker_status": update_blocker.status,
            "blocker_phase": update_blocker.phase,
        }
        if defer_on_update_blocker:
            blocker_details.update(
                {
                    "deferred": True,
                    "deferred_reason": "control_plane_update_running",
                    "requested_phase": phase,
                }
            )
        await complete_safely(
            "partial" if defer_on_update_blocker else "failed",
            complete_phase=(
                "deferred"
                if defer_on_update_blocker
                else phase or update_blocker.phase or "lock"
            ),
            error=(
                None
                if defer_on_update_blocker
                else "a control-plane update is already running"
            ),
            details=blocker_details,
        )
        return HostMutationLockError(
            "a control-plane update is already running",
            blocker=update_blocker,
        )

    try:
        if _LOCK_DEPTH.get() == 0:
            if kind != "update_control_plane":
                update_blocker = await _active_update_run_blocker(settings)
                if update_blocker is not None:
                    blocker_error = await complete_update_blocker(update_blocker)
                    raise blocker_error
            lock_path = _host_lock_path(settings)
            lock = FileLock(lock_path, blocking=False, lock_path=lock_path)
            try:
                lock.__enter__()
            except BlockingIOError as exc:
                if kind != "update_control_plane":
                    update_blocker = await _active_update_run_blocker(settings)
                    if update_blocker is not None:
                        blocker_error = await complete_update_blocker(update_blocker)
                        raise blocker_error from exc
                await complete_safely(
                    "failed",
                    complete_phase=phase or "lock",
                    error="another host mutation is already running",
                    details={"lock_path": str(lock_path)},
                )
                raise HostMutationLockError(
                    "another host mutation is already running"
                ) from exc
        token = _LOCK_DEPTH.set(_LOCK_DEPTH.get() + 1)
        await operation.update(status="running", phase=phase)
        yield operation
        if not operation.completed:
            await complete_safely("success", complete_phase=phase)
    except BaseException as exc:
        if not operation.completed:
            if isinstance(exc, asyncio.CancelledError):
                await complete_safely(
                    "cancelled", complete_phase=phase, error="operation cancelled"
                )
            else:
                await complete_safely("failed", complete_phase=phase, error=str(exc))
        raise
    finally:
        if token is not None:
            _LOCK_DEPTH.reset(token)
        if lock is not None:
            lock.__exit__(None, None, None)
