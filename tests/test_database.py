import importlib
from pathlib import Path
import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import config
from app import database


CURRENT_REVISION = "0037_control_event_target_indexes"


def test_database_import_does_not_validate_settings_at_import(monkeypatch) -> None:
    def raise_invalid_settings():
        raise ValueError("broken settings")

    with monkeypatch.context() as patch:
        patch.setattr(config, "get_settings", raise_invalid_settings)
        reloaded_database = importlib.reload(database)

        assert reloaded_database.engine is None

    importlib.reload(database)


@pytest.mark.asyncio
async def test_init_db_runs_alembic_upgrade_for_fresh_database(
    monkeypatch, tmp_path: Path
) -> None:
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", future=True
    )
    original_engine = database.engine

    monkeypatch.setattr(database, "engine", test_engine)

    try:
        await database.init_db()

        async with test_engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(database.sa_inspect(sync_conn).get_table_names())
            )
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            apply_indexes = await conn.execute(text("PRAGMA index_list(apply_runs)"))
            apply_index_names = {row[1] for row in apply_indexes.fetchall()}
            backup_indexes = await conn.execute(
                text("PRAGMA index_list(backend_backups)")
            )
            backup_index_names = {row[1] for row in backup_indexes.fetchall()}
            backup_foreign_keys = await conn.run_sync(
                lambda sync_conn: database.sa_inspect(sync_conn).get_foreign_keys(
                    "backend_backups"
                )
            )
            readiness_indexes = await conn.execute(
                text("PRAGMA index_list(backend_replica_readiness)")
            )
            readiness_index_names = {row[1] for row in readiness_indexes.fetchall()}
            control_event_indexes = await conn.execute(
                text("PRAGMA index_list(control_events)")
            )
            control_event_index_names = {
                row[1] for row in control_event_indexes.fetchall()
            }
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    assert {
        "apply_runs",
        "backends",
        "inputs",
        "input_backend_links",
        "update_runs",
        "backend_backups",
        "backend_resource_samples",
        "host_resource_samples",
        "operations",
        "host_apply_state",
        "update_checks",
        "backend_hardening_runs",
        "backend_replica_readiness",
    } <= tables
    assert revision == CURRENT_REVISION
    assert "ix_apply_runs_status_id" in apply_index_names
    assert "ix_backend_backups_backend_id_id" in backup_index_names
    assert "ix_backend_backups_backend_id_status_id" in backup_index_names
    assert "ix_backend_backups_operation_id" in backup_index_names
    assert "ix_backend_replica_readiness_backend_id" in readiness_index_names
    assert "ix_backend_replica_readiness_node_uid" in readiness_index_names
    assert "ix_control_events_backend_name_id" in control_event_index_names
    assert "ix_control_events_scope_affects_all_id" in control_event_index_names
    assert any(
        foreign_key.get("constrained_columns") == ["operation_id"]
        and foreign_key.get("referred_table") == "operations"
        and foreign_key.get("referred_columns") == ["id"]
        and str((foreign_key.get("options") or {}).get("ondelete") or "").upper()
        == "SET NULL"
        for foreign_key in backup_foreign_keys
    )


@pytest.mark.asyncio
async def test_init_db_stamps_existing_unversioned_database(
    monkeypatch, tmp_path: Path
) -> None:
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}", future=True
    )
    original_engine = database.engine

    async with test_engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)

    monkeypatch.setattr(database, "engine", test_engine)

    try:
        await database.init_db()

        async with test_engine.connect() as conn:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            columns = await conn.execute(text("PRAGMA table_info(backends)"))
            backend_columns = {row[1] for row in columns.fetchall()}
            input_columns = await conn.execute(text("PRAGMA table_info(inputs)"))
            input_column_names = {row[1] for row in input_columns.fetchall()}
            apply_columns = await conn.execute(text("PRAGMA table_info(apply_runs)"))
            apply_column_names = {row[1] for row in apply_columns.fetchall()}
            backup_columns = await conn.execute(
                text("PRAGMA table_info(backend_backups)")
            )
            backup_column_names = {row[1] for row in backup_columns.fetchall()}
            sample_columns = await conn.execute(
                text("PRAGMA table_info(backend_resource_samples)")
            )
            sample_column_names = {row[1] for row in sample_columns.fetchall()}
            backend_sample_indexes = await conn.execute(
                text("PRAGMA index_list(backend_resource_samples)")
            )
            backend_sample_index_names = {
                row[1] for row in backend_sample_indexes.fetchall()
            }
            host_sample_indexes = await conn.execute(
                text("PRAGMA index_list(host_resource_samples)")
            )
            host_sample_index_names = {row[1] for row in host_sample_indexes.fetchall()}
            control_event_indexes = await conn.execute(
                text("PRAGMA index_list(control_events)")
            )
            control_event_index_names = {
                row[1] for row in control_event_indexes.fetchall()
            }
            apply_indexes = await conn.execute(text("PRAGMA index_list(apply_runs)"))
            apply_index_names = {row[1] for row in apply_indexes.fetchall()}
            backup_indexes = await conn.execute(
                text("PRAGMA index_list(backend_backups)")
            )
            backup_index_names = {row[1] for row in backup_indexes.fetchall()}
            readiness_indexes = await conn.execute(
                text("PRAGMA index_list(backend_replica_readiness)")
            )
            readiness_index_names = {row[1] for row in readiness_indexes.fetchall()}
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    assert revision == CURRENT_REVISION
    assert {
        "sandbox_profile",
        "handoff_port",
        "healthcheck_host_header",
        "resource_mode",
        "resource_size",
        "resource_size_updated_at",
        "memory_high_override",
        "memory_max_override",
        "cpu_quota_override",
        "shield_enabled",
        "shield_code_hash",
        "shield_access_code",
    } <= backend_columns
    assert {
        "shield_enabled",
        "shield_code_hash",
        "shield_access_code",
    } <= input_column_names
    assert {"bundle_path", "bundle_sha256", "operation_id"} <= backup_column_names
    assert {
        "desired_state_hash",
        "desired_state_json",
        "config_revision",
        "operation_id",
    } <= apply_column_names
    assert {
        "cpu_percent_of_entitlement_count",
        "cpu_entitlement_percent_of_host",
        "cpu_limit_percent_of_host",
        "memory_percent_max",
        "memory_percent_last",
        "memory_current_bytes_count",
        "memory_current_bytes_last",
        "memory_peak_bytes",
        "memory_events_max",
        "memory_events_oom_kill",
        "network_rx_bytes",
        "network_tx_bytes",
        "network_rx_bps",
        "network_rx_bps_last",
        "network_tx_bps",
        "network_tx_bps_last",
        "network_total_bps_last",
        "disk_usage_bytes",
        "disk_usage_complete",
        "disk_usage_skipped_paths",
        "disk_usage_bytes_last",
    } <= sample_column_names
    assert "ix_backend_resource_samples_bucket_start" in backend_sample_index_names
    assert "ix_host_resource_samples_bucket_start" in host_sample_index_names
    assert "ix_control_events_kind_created_at" in control_event_index_names
    assert "ix_control_events_backend_name_id" in control_event_index_names
    assert "ix_control_events_scope_affects_all_id" in control_event_index_names
    assert "ix_apply_runs_status_id" in apply_index_names
    assert "ix_backend_backups_backend_id_id" in backup_index_names
    assert "ix_backend_backups_backend_id_status_id" in backup_index_names
    assert "ix_backend_backups_operation_id" in backup_index_names
    assert "ix_backend_replica_readiness_backend_id" in readiness_index_names
    assert "ix_backend_replica_readiness_node_uid" in readiness_index_names
    assert "include_container_snapshot" not in backup_column_names
    assert "container_image_ref" not in backup_column_names


@pytest.mark.asyncio
async def test_init_db_detects_unversioned_control_events_schema_and_runs_later_migrations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-0011.db"
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    original_engine = database.engine
    config = database._alembic_config(f"sqlite+aiosqlite:///{db_path}")

    try:
        await asyncio.to_thread(database.command.upgrade, config, "0011_control_events")
        async with test_engine.begin() as conn:
            await conn.execute(text("DROP TABLE alembic_version"))

        monkeypatch.setattr(database, "engine", test_engine)
        await database.init_db()

        async with test_engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(database.sa_inspect(sync_conn).get_table_names())
            )
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    assert {
        "operations",
        "host_apply_state",
        "update_checks",
        "backend_hardening_runs",
    } <= tables
    assert revision == CURRENT_REVISION


@pytest.mark.asyncio
async def test_init_db_bridges_known_stale_alembic_revision(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "stale-bridge.db"
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    original_engine = database.engine
    config = database._alembic_config(f"sqlite+aiosqlite:///{db_path}")

    try:
        await asyncio.to_thread(
            database.command.upgrade, config, "0010_systemd_guest_cutover"
        )
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE alembic_version SET version_num = '0009_backend_migrate_command'"
                )
            )

        monkeypatch.setattr(database, "engine", test_engine)
        await database.init_db()

        async with test_engine.connect() as conn:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            tables = await conn.run_sync(
                lambda sync_conn: set(database.sa_inspect(sync_conn).get_table_names())
            )
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    assert revision == CURRENT_REVISION
    assert {
        "control_events",
        "operations",
        "host_apply_state",
        "update_checks",
        "backend_hardening_runs",
    } <= tables


@pytest.mark.asyncio
async def test_init_db_recovers_partially_applied_performance_indexes(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "partial-0022.db"
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    original_engine = database.engine
    config = database._alembic_config(f"sqlite+aiosqlite:///{db_path}")

    try:
        await asyncio.to_thread(
            database.command.upgrade, config, "0021_output_disk_metric_samples"
        )
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE INDEX ix_backend_resource_samples_bucket_start ON backend_resource_samples(bucket_start)"
                )
            )

        monkeypatch.setattr(database, "engine", test_engine)
        await database.init_db()

        async with test_engine.connect() as conn:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            host_sample_indexes = await conn.execute(
                text("PRAGMA index_list(host_resource_samples)")
            )
            host_sample_index_names = {row[1] for row in host_sample_indexes.fetchall()}
            control_event_indexes = await conn.execute(
                text("PRAGMA index_list(control_events)")
            )
            control_event_index_names = {
                row[1] for row in control_event_indexes.fetchall()
            }
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    assert revision == CURRENT_REVISION
    assert "ix_host_resource_samples_bucket_start" in host_sample_index_names
    assert "ix_control_events_kind_created_at" in control_event_index_names


@pytest.mark.asyncio
async def test_init_db_rejects_unknown_alembic_revision_with_repair_guidance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    test_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'unknown-revision.db'}", future=True
    )
    original_engine = database.engine

    async with test_engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await conn.execute(
            text(
                "INSERT INTO alembic_version(version_num) VALUES ('missing_dev_revision')"
            )
        )

    monkeypatch.setattr(database, "engine", test_engine)

    try:
        with pytest.raises(RuntimeError) as exc_info:
            await database.init_db()
    finally:
        monkeypatch.setattr(database, "engine", original_engine)
        await test_engine.dispose()

    message = str(exc_info.value)
    assert (
        "stored Alembic revision(s) [missing_dev_revision] are not present" in message
    )
    assert "CNC did not modify the database" in message
    assert "deliberate Alembic stamp repair" in message
