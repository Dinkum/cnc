from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


SQLITE_PREFIX = "sqlite+aiosqlite:///"


def _sqlite_path_from_url(database_url: str) -> Path:
    if not database_url.startswith(SQLITE_PREFIX):
        raise ValueError(
            f"unsupported database url for release preflight: {database_url!r}"
        )
    raw_path = database_url[len(SQLITE_PREFIX) :]
    if not raw_path:
        raise ValueError("database url is missing a sqlite path")
    return Path(raw_path)


def _clone_sqlite_database(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not source_path.exists():
        sqlite3.connect(target_path).close()
        return

    with sqlite3.connect(source_path) as source_conn:
        with sqlite3.connect(target_path) as target_conn:
            source_conn.backup(target_conn)


async def _run_database_startup_check(database_url: str) -> None:
    os.environ["DATABASE_URL"] = database_url
    from app.config import reload_settings
    import app.database as database

    reload_settings()
    database = importlib.reload(database)

    await database.verify_db_connection()
    await database.init_db()


def run_release_preflight(database_url: str) -> None:
    source_path = _sqlite_path_from_url(database_url)
    temp_dir = Path(tempfile.mkdtemp(prefix="cnc-release-preflight-"))
    temp_db_path = temp_dir / "preflight.db"
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        _clone_sqlite_database(source_path, temp_db_path)
        cloned_database_url = f"{SQLITE_PREFIX}{temp_db_path}"
        asyncio.run(_run_database_startup_check(cloned_database_url))
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.release_preflight")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    run_release_preflight(args.database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
