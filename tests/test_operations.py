import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ApplyRun, BackendBackup, Operation, UpdateRun
from app.services.file_locks import FileLock
from app.services.operation_progress import (
    operation_progress_path,
    read_operation_progress,
    write_operation_progress,
)
from app.services.operations import (
    HostMutationBlocker,
    HostMutationLockError,
    OperationHandleProxy,
    create_operation,
    active_host_mutation_blocker,
    fail_interrupted_operations,
    host_mutation_operation,
    validate_host_mutation_lock_path,
    validate_operation_handle_contract,
)


async def _make_session(db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_host_mutation_operation_records_success(tmp_path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    async with host_mutation_operation(
        settings, kind="apply_host", phase="apply"
    ) as operation:
        await operation.complete("success", phase="completed", details={"ok": True})

    async with maker() as session:
        stored = (await session.execute(select(Operation))).scalar_one()

    assert stored.kind == "apply_host"
    assert stored.status == "success"
    assert stored.phase == "completed"
    assert stored.finished_at is not None


def test_host_mutation_lock_path_fails_closed_when_configured_parent_is_unusable(
    tmp_path,
) -> None:
    unusable_parent = tmp_path / "not-a-directory"
    unusable_parent.write_text("occupied", encoding="utf-8")
    settings = Settings(apply_lock_path=unusable_parent / "host.lock")

    with pytest.raises(RuntimeError, match="host mutation lock path is unusable"):
        validate_host_mutation_lock_path(settings)

    assert not (tmp_path / "cnc-host-mutation.lock").exists()


def test_host_mutation_lock_path_uses_the_exact_configured_location(tmp_path) -> None:
    configured = tmp_path / "locks" / "host.lock"

    resolved = validate_host_mutation_lock_path(Settings(apply_lock_path=configured))

    assert resolved == configured.resolve()
    assert configured.is_file()


@pytest.mark.parametrize("defer_on_update_blocker", [False, True])
@pytest.mark.asyncio
async def test_host_mutation_operation_blocks_parallel_mutation(
    tmp_path, defer_on_update_blocker: bool
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    with FileLock(
        settings.apply_lock_path, blocking=False, lock_path=settings.apply_lock_path
    ):
        with pytest.raises(HostMutationLockError):
            async with host_mutation_operation(
                settings,
                kind="backup_backend",
                phase="backup",
                defer_on_update_blocker=defer_on_update_blocker,
            ):
                pass

    async with maker() as session:
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id)))
            .scalars()
            .all()
        )

    assert [item.kind for item in operations] == ["backup_backend"]
    assert operations[0].status == "failed"
    assert operations[0].phase == "backup"


@pytest.mark.asyncio
async def test_host_mutation_operation_allows_nested_mutation_flow(tmp_path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    async with host_mutation_operation(
        settings, kind="restore_backend", phase="restore"
    ) as outer:
        async with host_mutation_operation(
            settings, kind="backup_backend", phase="import"
        ) as inner:
            await inner.complete("success", phase="completed")
        await outer.complete("success", phase="completed")

    async with maker() as session:
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id)))
            .scalars()
            .all()
        )

    assert [item.kind for item in operations] == ["restore_backend", "backup_backend"]
    assert [item.status for item in operations] == ["success", "success"]


@pytest.mark.asyncio
async def test_active_host_mutation_blocker_reports_pending_update_run(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    async def keep_update_pending(_session, _settings, run):
        return run

    monkeypatch.setattr(
        "app.services.operations._reconcile_update_run_for_blocker",
        keep_update_pending,
    )
    async with maker() as session:
        session.add(
            UpdateRun(
                status="queued",
                message="update queued",
                details_json='{"unit": "cnc-update-run-1.service"}',
            )
        )
        await session.commit()

    blocker = await active_host_mutation_blocker(settings)

    assert blocker is not None
    assert blocker.kind == "update_control_plane"
    assert blocker.status == "queued"


@pytest.mark.asyncio
async def test_active_host_mutation_blocker_reports_scheduled_apply_operation(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )
    async with maker() as session:
        session.add(Operation(kind="auto_size", status="running", phase="apply"))
        await session.commit()

    blocker = await active_host_mutation_blocker(settings)

    assert blocker is not None
    assert blocker.kind == "auto_size"
    assert blocker.status == "running"
    assert blocker.phase == "apply"


@pytest.mark.asyncio
async def test_host_mutation_operation_blocks_active_update_run(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    async def keep_update_pending(_session, _settings, run):
        return run

    monkeypatch.setattr(
        "app.services.operations._reconcile_update_run_for_blocker",
        keep_update_pending,
    )
    async with maker() as session:
        session.add(
            UpdateRun(
                status="running",
                message="update running",
                details_json='{"unit": "cnc-update-run-1.service"}',
            )
        )
        await session.commit()

    with pytest.raises(HostMutationLockError):
        async with host_mutation_operation(settings, kind="apply_host", phase="apply"):
            pass

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert operation.status == "failed"
    assert operation.phase == "apply"
    assert operation.error == "a control-plane update is already running"


@pytest.mark.asyncio
async def test_host_mutation_operation_can_defer_active_update_run(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    async def keep_update_pending(_session, _settings, run):
        return run

    monkeypatch.setattr(
        "app.services.operations._reconcile_update_run_for_blocker",
        keep_update_pending,
    )
    async with maker() as session:
        update_run = UpdateRun(
            status="running",
            message="update running",
            details_json='{"unit": "cnc-update-run-1.service"}',
        )
        session.add(update_run)
        await session.commit()
        await session.refresh(update_run)

    with pytest.raises(HostMutationLockError) as exc_info:
        async with host_mutation_operation(
            settings,
            kind="cloudflare_sync",
            phase="firewall",
            defer_on_update_blocker=True,
        ):
            pass

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert exc_info.value.blocker is not None
    assert exc_info.value.blocker.id == update_run.id
    assert exc_info.value.blocker.kind == "update_control_plane"
    assert operation.status == "partial"
    assert operation.phase == "deferred"
    assert operation.error is None
    assert operation.finished_at is not None
    details = json.loads(operation.details_json)
    assert details["deferred"] is True
    assert details["deferred_reason"] == "control_plane_update_running"
    assert details["requested_phase"] == "firewall"
    assert details["blocker_id"] == update_run.id
    assert details["blocker_kind"] == "update_control_plane"
    assert details["blocker_status"] == "running"


@pytest.mark.asyncio
async def test_host_mutation_operation_rechecks_update_after_lock_race(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )
    update_blocker = HostMutationBlocker(
        id=27,
        kind="update_control_plane",
        status="running",
        phase="background_update",
    )
    lookup_count = 0

    async def update_starts_between_lookup_and_lock(_settings):
        nonlocal lookup_count
        lookup_count += 1
        return None if lookup_count == 1 else update_blocker

    monkeypatch.setattr(
        "app.services.operations._active_update_run_blocker",
        update_starts_between_lookup_and_lock,
    )

    with FileLock(
        settings.apply_lock_path, blocking=False, lock_path=settings.apply_lock_path
    ):
        with pytest.raises(HostMutationLockError) as exc_info:
            async with host_mutation_operation(
                settings,
                kind="cloudflare_sync",
                phase="firewall",
                defer_on_update_blocker=True,
            ):
                pass

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert lookup_count == 2
    assert exc_info.value.blocker == update_blocker
    assert operation.status == "partial"
    assert operation.phase == "deferred"
    assert operation.error is None


@pytest.mark.asyncio
async def test_active_host_mutation_blocker_clears_stale_update_without_unit(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )
    async with maker() as session:
        session.add(
            UpdateRun(status="queued", message="update queued", details_json="{}")
        )
        await session.commit()

    blocker = await active_host_mutation_blocker(settings)

    async with maker() as session:
        stored = (await session.execute(select(UpdateRun))).scalar_one()

    assert blocker is None
    assert stored.status == "error"
    assert stored.message == "update failed"


@pytest.mark.asyncio
async def test_host_mutation_operation_marks_cancelled_operation_finished(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    with pytest.raises(asyncio.CancelledError):
        async with host_mutation_operation(settings, kind="apply_host", phase="apply"):
            raise asyncio.CancelledError()

    async with maker() as session:
        stored = (await session.execute(select(Operation))).scalar_one()

    assert stored.status == "cancelled"
    assert stored.phase == "apply"
    assert stored.error == "operation cancelled"
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_deferred_operation_handle_writes_snapshot_without_db_update(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
    )
    operation = await create_operation(settings, kind="create_backend", phase="queued")

    deferred = operation.with_deferred_db_updates()
    await deferred.update(
        status="running",
        phase="Prepare host",
        details={"progress": 17, "message": "Checking admin exposure"},
    )

    snapshot = read_operation_progress(settings, operation.id or 0)
    async with maker() as session:
        stored = await session.get(Operation, operation.id)

    assert snapshot is not None
    assert snapshot["phase"] == "Prepare host"
    assert snapshot["details"]["progress"] == 17
    assert stored is not None
    assert stored.phase == "queued"
    assert stored.status == "queued"


@pytest.mark.asyncio
async def test_operation_handle_update_merges_details(tmp_path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
    )
    operation = await create_operation(
        settings,
        kind="create_backend",
        phase="queued",
        details={
            "progress": 12,
            "message": "Queued",
            "progress_plan": {"pipeline": "createOutput", "steps": []},
        },
    )

    await operation.update(
        status="running",
        phase="Save desired state",
        details={"progress": 42, "message": "Building host plan"},
    )

    async with maker() as session:
        stored = await session.get(Operation, operation.id)

    assert stored is not None
    assert stored.details_json is not None
    details = json.loads(stored.details_json)
    assert details == {
        "progress": 42,
        "message": "Building host plan",
        "progress_plan": {"pipeline": "createOutput", "steps": []},
    }


@pytest.mark.asyncio
async def test_operation_handle_update_preserves_invalid_existing_details_as_raw(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
    )
    operation = await create_operation(settings, kind="restore_backend", phase="queued")
    async with maker() as session:
        stored = await session.get(Operation, operation.id)
        assert stored is not None
        stored.details_json = "{not-json"
        await session.commit()

    await operation.update(details={"message": "Restoring"})

    async with maker() as session:
        stored = await session.get(Operation, operation.id)

    assert stored is not None
    details = json.loads(stored.details_json)
    assert details == {"raw": "{not-json", "message": "Restoring"}


@pytest.mark.asyncio
async def test_operation_handle_complete_cleans_progress_snapshot(tmp_path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
    )
    operation = await create_operation(settings, kind="restore_backend", phase="queued")

    await operation.update(
        status="running",
        phase="restore",
        details={"progress": 50, "message": "Restoring"},
    )
    assert operation_progress_path(settings, operation.id or 0).exists()

    await operation.complete(
        "success",
        phase="completed",
        details={"progress": 100, "message": "Restored"},
    )

    async with maker() as session:
        stored = await session.get(Operation, operation.id)

    assert stored is not None
    assert stored.status == "success"
    assert read_operation_progress(settings, operation.id or 0) is None


@pytest.mark.asyncio
async def test_operation_handle_proxy_preserves_deferred_handle_contract(
    tmp_path,
) -> None:
    await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )
    operation = await create_operation(settings, kind="create_backend", phase="queued")
    proxy = OperationHandleProxy(operation)

    validate_operation_handle_contract(proxy)
    deferred = proxy.with_deferred_db_updates()
    deferred.completed = True

    assert proxy.id == operation.id
    assert proxy.kind == "create_backend"
    assert deferred.id == operation.id
    assert deferred.kind == "create_backend"
    assert deferred.completed is True


@pytest.mark.asyncio
async def test_fail_interrupted_operations_marks_queued_and_running_operations(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        session.add_all(
            [
                Operation(
                    kind="create_backend",
                    status="running",
                    phase="reconciling",
                    details_json='{"progress": 42}',
                ),
                Operation(
                    kind="backup_backend",
                    status="queued",
                    phase="backup",
                    details_json="{}",
                ),
                Operation(
                    kind="restore_backend",
                    status="success",
                    phase="restore",
                    details_json="{}",
                ),
            ]
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id)))
            .scalars()
            .all()
        )

    assert count == 2
    assert [operation.status for operation in operations] == [
        "failed",
        "failed",
        "success",
    ]
    assert operations[0].error == "operation interrupted by CNC restart"
    assert operations[0].finished_at is not None
    assert '"interrupted": true' in operations[0].details_json


@pytest.mark.asyncio
async def test_fail_interrupted_operations_propagates_query_failure(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'uninitialized.db'}",
        apply_lock_path=tmp_path / "host.lock",
    )

    with pytest.raises(SQLAlchemyError):
        await fail_interrupted_operations(settings)


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_committed_auto_size_apply(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="auto_size",
            status="running",
            phase="database_commit",
            details_json='{"apply_run_id": 7}',
        )
        session.add(operation)
        await session.flush()
        session.add(
            ApplyRun(
                id=7,
                operation_id=operation.id,
                status="success",
                message="apply completed",
                details_json="{}",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"
    assert operation.finished_at is not None
    assert '"startup_recovered": true' in operation.details_json


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_auto_size_apply_without_details_run_id(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="auto_size",
            status="running",
            phase="apply",
            details_json="{}",
        )
        session.add(operation)
        await session.flush()
        session.add(
            ApplyRun(
                operation_id=operation.id,
                status="success",
                message="apply completed",
                details_json="{}",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_committed_backup_operation(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="backup_backend",
            status="running",
            phase="backup",
            details_json='{"backup_id": 7}',
        )
        session.add(operation)
        await session.flush()
        session.add(
            BackendBackup(
                id=7,
                operation_id=operation.id,
                status="success",
                scope="metadata_only",
                notes="verified",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"
    assert operation.finished_at is not None
    assert '"startup_recovered": true' in operation.details_json


@pytest.mark.asyncio
async def test_fail_interrupted_operations_does_not_recover_backup_id_without_owner(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="backup_backend",
            status="running",
            phase="backup",
            details_json='{"backup_id": 7}',
        )
        session.add(operation)
        session.add(
            BackendBackup(
                id=7,
                status="success",
                scope="metadata_only",
                notes="unowned backup",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "failed"
    assert operation.error == "operation interrupted by CNC restart"


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_ui_backup_operation_without_backup_id(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="backup_backend",
            actor="ui",
            backend_id=42,
            status="running",
            phase="backup",
            details_json='{"operation_role":"ui_backup"}',
        )
        session.add(operation)
        await session.flush()
        session.add(
            BackendBackup(
                operation_id=operation.id,
                backend_id=42,
                status="success",
                scope="metadata_only",
                notes="verified",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_ui_import_backup_operation(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="import_backend_backup",
            actor="ui",
            backend_id=42,
            status="running",
            phase="import",
            details_json='{"operation_role":"ui_backup_import"}',
        )
        session.add(operation)
        await session.flush()
        session.add(
            BackendBackup(
                operation_id=operation.id,
                backend_id=42,
                status="success",
                scope="metadata_only",
                notes="verified",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"


@pytest.mark.asyncio
async def test_fail_interrupted_operations_recovers_operation_owned_import_without_details(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        operation = Operation(
            kind="import_backend_backup",
            actor="ui",
            backend_id=42,
            status="running",
            phase="import",
            details_json="{}",
        )
        session.add(operation)
        await session.flush()
        session.add(
            BackendBackup(
                operation_id=operation.id,
                backend_id=42,
                status="success",
                scope="metadata_only",
                notes="verified",
            )
        )
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "success"
    assert operation.phase == "completed"


@pytest.mark.asyncio
async def test_fail_interrupted_operations_does_not_recover_ui_backup_from_old_backup(
    tmp_path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        session.add(
            BackendBackup(
                operation_id=999,
                backend_id=42,
                status="success",
                scope="metadata_only",
                notes="older backup",
            )
        )
        await session.commit()
        operation = Operation(
            kind="backup_backend",
            actor="ui",
            backend_id=42,
            status="running",
            phase="backup",
            details_json='{"operation_role":"ui_backup"}',
        )
        session.add(operation)
        await session.commit()

    count = await fail_interrupted_operations(settings)

    async with maker() as session:
        operation = (await session.execute(select(Operation))).scalar_one()

    assert count == 1
    assert operation.status == "failed"
    assert operation.phase == "backup"
    assert operation.error == "operation interrupted by CNC restart"


def test_operation_progress_snapshot_does_not_regress(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
    )

    write_operation_progress(
        settings,
        7,
        status="running",
        phase="Apply host access",
        details={"progress": 84, "message": "Reconciling backend SSH"},
    )
    write_operation_progress(
        settings,
        7,
        status="running",
        phase="Bootstrap guest",
        details={
            "progress": 74,
            "message": "Waiting for guest readiness",
            "substate": "Waiting for guest readiness",
        },
        error="still waiting",
    )

    payload = read_operation_progress(settings, 7)

    assert payload is not None
    assert payload["phase"] == "Bootstrap guest"
    assert payload["error"] == "still waiting"
    assert payload["details"]["progress"] == 84
    assert payload["details"]["message"] == "Waiting for guest readiness"
    assert payload["details"]["substate"] == "Waiting for guest readiness"


def test_operation_progress_snapshot_metadata_preserves_progress(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
    )

    write_operation_progress(
        settings,
        7,
        status="running",
        phase="Save desired state",
        details={
            "progress": 42,
            "message": "Building host plan",
            "progress_plan": {"pipeline": "createOutput", "steps": []},
        },
    )
    write_operation_progress(
        settings,
        7,
        status="running",
        phase=None,
        details={"changed_slices": ["nginx.managed"]},
    )

    payload = read_operation_progress(settings, 7)

    assert payload is not None
    assert payload["status"] == "running"
    assert payload["phase"] == "Save desired state"
    assert payload["details"]["progress"] == 42
    assert payload["details"]["progress_plan"] == {
        "pipeline": "createOutput",
        "steps": [],
    }
    assert payload["details"]["changed_slices"] == ["nginx.managed"]
