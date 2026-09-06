import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from weakref import WeakSet

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError as AlembicCommandError
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_configured_sync_engines: WeakSet[Engine] = WeakSet()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = _PROJECT_ROOT / "migrations"
_ALEMBIC_BASELINE_REVISION = "0003_app_backends"
_ALEMBIC_CURRENT_SCHEMA_REVISION = "0037_control_event_target_indexes"
_ALEMBIC_COMPATIBILITY_BRIDGE_REVISIONS = {
    "0009_backend_migrate_command",
}


class Base(DeclarativeBase):
    pass


def _configure_engine(sync_engine) -> None:
    # Engine IDs can be reused after disposal; retain only live engine identities.
    if sync_engine in _configured_sync_engines:
        return

    @event.listens_for(sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

    _configured_sync_engines.add(sync_engine)


def create_configured_async_engine(database_url: str, **kwargs) -> AsyncEngine:
    active_engine = create_async_engine(database_url, **kwargs)
    _configure_engine(active_engine.sync_engine)
    return active_engine


def get_engine() -> AsyncEngine:
    global engine
    if engine is None:
        settings = get_settings()
        engine = create_configured_async_engine(
            settings.database_url, future=True, echo=False
        )
    _configure_engine(engine.sync_engine)
    return engine


async def dispose_engine() -> None:
    global engine
    global _session_factory
    if engine is None:
        return
    active_engine = engine
    engine = None
    _session_factory = None
    await active_engine.dispose()


def _session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    active_engine = get_engine()
    bound_engine = None if _session_factory is None else _session_factory.kw.get("bind")
    if _session_factory is None or bound_engine is not active_engine:
        _session_factory = async_sessionmaker(
            bind=active_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


def SessionLocal():
    return _session_maker()()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def verify_db_connection() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def init_db() -> None:
    await _migrate_database()


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_ALEMBIC_INI)) if _ALEMBIC_INI.exists() else Config()
    config.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


async def _table_names() -> set[str]:
    async with get_engine().connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )


async def _table_columns(table_name: str) -> set[str]:
    async with get_engine().connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in sa_inspect(sync_conn).get_columns(table_name)
            }
        )


async def _index_names(table_name: str) -> set[str]:
    async with get_engine().connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: {
                index["name"] for index in sa_inspect(sync_conn).get_indexes(table_name)
            }
        )


async def _has_foreign_key(
    table_name: str, column_name: str, referred_table: str
) -> bool:
    async with get_engine().connect() as conn:

        def matches(sync_conn) -> bool:
            for foreign_key in sa_inspect(sync_conn).get_foreign_keys(table_name):
                options = foreign_key.get("options")
                ondelete = ""
                if isinstance(options, dict):
                    ondelete = str(options.get("ondelete") or "").upper()
                if (
                    foreign_key.get("constrained_columns") == [column_name]
                    and foreign_key.get("referred_table") == referred_table
                    and foreign_key.get("referred_columns") == ["id"]
                    and ondelete == "SET NULL"
                ):
                    return True
            return False

        return await conn.run_sync(matches)


def _known_alembic_revisions(config: Config) -> set[str]:
    script = ScriptDirectory.from_config(config)
    return {str(revision.revision) for revision in script.walk_revisions()}


async def _stored_alembic_revisions(user_tables: set[str]) -> set[str]:
    if "alembic_version" not in user_tables:
        return set()
    async with get_engine().connect() as conn:
        rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return {str(row[0]) for row in rows if row[0]}


def _unknown_revision_message(
    unknown_revisions: set[str], known_revisions: set[str]
) -> str:
    unknown = ", ".join(sorted(unknown_revisions))
    known = ", ".join(sorted(known_revisions))
    return (
        "database migration state is incompatible with this CNC release: "
        f"stored Alembic revision(s) [{unknown}] are not present in this checkout. "
        "CNC did not modify the database. Restore a release that contains the stored revision, "
        "or inspect the database schema and run a deliberate Alembic stamp repair before starting CNC again. "
        f"Known revisions in this release: {known}"
    )


async def _detect_schema_revision(user_tables: set[str]) -> str:
    backend_columns = (
        await _table_columns("backends") if "backends" in user_tables else set()
    )
    input_columns = await _table_columns("inputs") if "inputs" in user_tables else set()
    backup_columns = (
        await _table_columns("backend_backups")
        if "backend_backups" in user_tables
        else set()
    )
    if "control_events" not in user_tables:
        if (
            "input_backend_links" not in user_tables
            or "backend_backups" not in user_tables
        ):
            return _ALEMBIC_BASELINE_REVISION
        if "bundle_path" not in backup_columns or "bundle_sha256" not in backup_columns:
            return "0004_current_schema"
        if (
            "include_container_snapshot" in backup_columns
            or "container_image_ref" in backup_columns
        ):
            return "0005_backup_bundle"
        if (
            "backend_resource_samples" not in user_tables
            or "resource_size" not in backend_columns
        ):
            return "0006_backend_backup_schema"
        if "sandbox_image" in backend_columns or "handoff_port" in backend_columns:
            if (
                "sandbox_profile" not in backend_columns
                or "healthcheck_host_header" not in backend_columns
            ):
                return "0009_sandbox_handoff_cutover"
            return "0010_systemd_guest_cutover"
        if "update_command" in backend_columns:
            return "0008_backend_update_command"
        if "resource_size" in backend_columns:
            return "0007_auto_size_resource_policy"
        return _ALEMBIC_BASELINE_REVISION
    if "operations" not in user_tables:
        return "0011_control_events"
    if "host_apply_state" not in user_tables:
        return "0012_operations"
    if "update_checks" not in user_tables:
        return "0013_apply_state_snapshots"
    resource_sample_columns = (
        await _table_columns("backend_resource_samples")
        if "backend_resource_samples" in user_tables
        else set()
    )
    required_resource_sample_rollup_columns = {
        "cpu_percent_of_host_count",
        "cpu_percent_of_host_sum",
        "cpu_percent_of_host_min",
        "cpu_percent_of_host_max",
        "cpu_percent_of_host_first",
        "cpu_percent_of_host_last",
        "cpu_percent_of_entitlement_count",
        "cpu_percent_of_entitlement_sum",
        "cpu_percent_of_entitlement_min",
        "cpu_percent_of_entitlement_max",
        "cpu_percent_of_entitlement_first",
        "cpu_percent_of_entitlement_last",
        "memory_percent_count",
        "memory_percent_sum",
        "memory_percent_min",
        "memory_percent_max",
        "memory_percent_first",
        "memory_percent_last",
    }
    if not required_resource_sample_rollup_columns <= resource_sample_columns:
        return "0014_update_checks"
    required_network_sample_columns = {
        "network_rx_bytes",
        "network_tx_bytes",
        "network_total_bps",
        "network_total_bps_count",
        "network_total_bps_sum",
        "network_total_bps_min",
        "network_total_bps_max",
        "network_total_bps_first",
        "network_total_bps_last",
    }
    if not required_network_sample_columns <= resource_sample_columns:
        return "0015_resource_sample_rollups"
    if "backend_hardening_runs" not in user_tables:
        return "0016_output_network_metrics"
    if (
        "shield_enabled" not in backend_columns
        or "shield_code_hash" not in backend_columns
    ):
        return "0017_backend_hardening_runs"
    if "host_resource_samples" not in user_tables:
        return "0018_shield_access_gate"
    if "shield_enabled" not in input_columns or "shield_code_hash" not in input_columns:
        return "0019_host_resource_samples"
    required_disk_sample_columns = {
        "disk_usage_bytes",
        "disk_usage_bytes_count",
        "disk_usage_bytes_sum",
        "disk_usage_bytes_min",
        "disk_usage_bytes_max",
        "disk_usage_bytes_first",
        "disk_usage_bytes_last",
    }
    if not required_disk_sample_columns <= resource_sample_columns:
        return "0020_input_shield_access_gate"
    apply_columns = (
        await _table_columns("apply_runs") if "apply_runs" in user_tables else set()
    )
    required_apply_columns = {
        "operation_id",
        "config_revision",
        "desired_state_hash",
        "desired_state_json",
        "generated_nginx_files_json",
        "runtime_graph_json",
        "route_contracts_json",
        "backend_contracts_json",
        "resource_profile_json",
    }
    if not required_apply_columns <= apply_columns:
        return "0012_operations"
    backend_resource_indexes = await _index_names("backend_resource_samples")
    host_resource_indexes = (
        await _index_names("host_resource_samples")
        if "host_resource_samples" in user_tables
        else set()
    )
    control_event_indexes = await _index_names("control_events")
    required_performance_indexes = {
        "ix_backend_resource_samples_bucket_start": backend_resource_indexes,
        "ix_host_resource_samples_bucket_start": host_resource_indexes,
        "ix_control_events_kind_created_at": control_event_indexes,
    }
    if any(
        index_name not in index_names
        for index_name, index_names in required_performance_indexes.items()
    ):
        return "0021_output_disk_metric_samples"
    if (
        "shield_access_code" not in backend_columns
        or "shield_access_code" not in input_columns
    ):
        return "0022_performance_indexes"
    if "cluster_nodes" not in user_tables or "cluster_join_tokens" not in user_tables:
        return "0023_visible_shield_access_codes"
    cluster_node_columns = await _table_columns("cluster_nodes")
    if "tailnet_ip" not in cluster_node_columns:
        return "0024_cluster_nodes"
    if "cluster_node_latency_samples" not in user_tables:
        return "0025_cluster_node_tailnet_ip"
    if "cpu_entitlement_percent_of_host" not in resource_sample_columns:
        return "0026_cluster_node_latency_samples"
    if "placement_node_uid" not in backend_columns:
        return "0027_backend_cpu_ceiling_samples"
    backend_backup_indexes = (
        await _index_names("backend_backups")
        if "backend_backups" in user_tables
        else set()
    )
    apply_indexes = (
        await _index_names("apply_runs") if "apply_runs" in user_tables else set()
    )
    required_hot_path_indexes = {
        "ix_apply_runs_status_id": apply_indexes,
        "ix_backend_backups_backend_id_id": backend_backup_indexes,
        "ix_backend_backups_backend_id_status_id": backend_backup_indexes,
    }
    if any(
        index_name not in index_names
        for index_name, index_names in required_hot_path_indexes.items()
    ):
        return "0028_backend_placement_node"
    host_resource_columns = (
        await _table_columns("host_resource_samples")
        if "host_resource_samples" in user_tables
        else set()
    )
    required_metric_accuracy_columns = {
        "sampled_at",
        "cpu_limit_percent_of_host",
        "disk_usage_complete",
        "disk_usage_skipped_paths",
        "network_rx_bps",
        "network_tx_bps",
        "memory_current_bytes_count",
        "memory_current_bytes_last",
        "network_rx_bps_count",
        "network_rx_bps_last",
        "network_tx_bps_count",
        "network_tx_bps_last",
    }
    required_host_metric_accuracy_columns = {
        "sampled_at",
        "network_rx_bps_count",
        "network_rx_bps_last",
        "network_tx_bps_count",
        "network_tx_bps_last",
    }
    if not required_metric_accuracy_columns <= resource_sample_columns:
        return "0029_hot_path_indexes"
    if not required_host_metric_accuracy_columns <= host_resource_columns:
        return "0029_hot_path_indexes"
    required_backend_placement_columns = {
        "placement_mode",
        "placement_active_node_uid",
        "placement_node_uids_json",
    }
    if not required_backend_placement_columns <= backend_columns:
        return "0030_metric_chart_accuracy"
    if "inter_app_interfaces_json" not in backend_columns:
        return "0031_backend_multi_node_placement"
    if (
        "operation_id" not in backup_columns
        or "ix_backend_backups_operation_id" not in backend_backup_indexes
        or not (await _has_foreign_key("backend_backups", "operation_id", "operations"))
    ):
        return "0032_backend_inter_app_interfaces"
    if "backend_replica_readiness" not in user_tables:
        return "0033_backend_backup_operation_id"
    if (
        "ssh_public_key" not in backend_columns
        or "ssh_private_key" not in backend_columns
    ):
        return "0034_backend_replica_readiness"
    required_memory_event_columns = {
        "memory_peak_bytes",
        "memory_events_max",
        "memory_events_oom_kill",
    }
    if not required_memory_event_columns <= resource_sample_columns:
        return "0035_backend_ssh_keys"
    required_control_event_target_indexes = {
        "ix_control_events_backend_name_id",
        "ix_control_events_scope_affects_all_id",
    }
    if not required_control_event_target_indexes <= control_event_indexes:
        return "0036_backend_memory_events"
    return _ALEMBIC_CURRENT_SCHEMA_REVISION


async def _repair_compatible_alembic_revisions(
    config: Config,
    user_tables: set[str],
    stored_revisions: set[str],
) -> None:
    known_revisions = _known_alembic_revisions(config)
    unknown_revisions = stored_revisions - known_revisions
    if not unknown_revisions:
        return
    repairable_revisions = unknown_revisions & _ALEMBIC_COMPATIBILITY_BRIDGE_REVISIONS
    unsupported_revisions = unknown_revisions - repairable_revisions
    if unsupported_revisions:
        raise RuntimeError(
            _unknown_revision_message(unsupported_revisions, known_revisions)
        )
    detected_revision = await _detect_schema_revision(user_tables)
    await asyncio.to_thread(command.stamp, config, detected_revision, purge=True)


async def _migrate_database() -> None:
    active_engine = get_engine()
    table_names = await _table_names()
    user_tables = {name for name in table_names if not name.startswith("sqlite_")}
    config = _alembic_config(str(active_engine.url))

    if "alembic_version" not in user_tables and user_tables:
        baseline_revision = await _detect_schema_revision(user_tables)
        await asyncio.to_thread(command.stamp, config, baseline_revision)
    else:
        stored_revisions = await _stored_alembic_revisions(user_tables)
        if stored_revisions:
            await _repair_compatible_alembic_revisions(
                config, user_tables, stored_revisions
            )

    try:
        await asyncio.to_thread(command.upgrade, config, "head")
    except AlembicCommandError as exc:
        raise RuntimeError(
            "database migration failed before CNC could verify the schema. "
            "CNC did not modify operator configuration after the failure. "
            "Check the stored Alembic revision and migration files, then repair the database schema deliberately. "
            f"Alembic error: {exc}"
        ) from exc
