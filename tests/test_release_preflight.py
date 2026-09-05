from pathlib import Path
import sqlite3

from alembic.config import Config
from alembic.script import ScriptDirectory

from app import release_preflight


def _current_migration_head() -> str:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head is not None
    return head


def test_release_preflight_accepts_database_already_at_current_head(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (_current_migration_head(),),
        )
        connection.commit()

    release_preflight.run_release_preflight(f"sqlite+aiosqlite:///{db_path}")


def test_sqlite_path_from_url_rejects_non_sqlite_urls() -> None:
    try:
        release_preflight._sqlite_path_from_url("postgresql://example")
    except ValueError as exc:
        assert "unsupported database url" in str(exc)
    else:
        raise AssertionError("expected ValueError")
