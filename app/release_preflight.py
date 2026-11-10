from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys
import tempfile


SQLITE_PREFIX = "sqlite+aiosqlite:///"
SUPPORTED_UBUNTU_RELEASES = frozenset({"24.04", "26.04"})


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


class ReleaseIncompatibleError(RuntimeError):
    pass


def _staged_installer_remedy() -> str:
    release_root = Path(__file__).resolve().parent.parent
    return f"cd {shlex.quote(str(release_root))} && sudo bash scripts/install_ubuntu.sh"


def check_host_release() -> str:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        raise ReleaseIncompatibleError("cannot detect OS: missing /etc/os-release")
    fields: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip().strip('"').strip("'")
    if fields.get("ID") != "ubuntu":
        raise ReleaseIncompatibleError(
            f"CNC targets Ubuntu only (detected: {fields.get('ID') or 'unknown'})"
        )
    version = fields.get("VERSION_ID", "")
    if version not in SUPPORTED_UBUNTU_RELEASES:
        raise ReleaseIncompatibleError(
            "CNC supports Ubuntu "
            + " and ".join(sorted(SUPPORTED_UBUNTU_RELEASES))
            + f" (detected: {version or 'unknown'}). "
            "The running release was left active."
        )
    return version


def check_container_runtime() -> str:
    from app.services.guest_isolation import require_supported_podman

    remedy = (
        f"run this release's installer: {_staged_installer_remedy()}. "
        "The installer activates the release when it succeeds."
    )
    try:
        require_supported_podman()
    except FileNotFoundError as exc:
        raise ReleaseIncompatibleError(
            f"podman is not installed or not on PATH. {remedy}"
        ) from exc
    except Exception as exc:
        raise ReleaseIncompatibleError(f"{exc} {remedy}") from exc
    return "ok"


def check_installed_updater_assets(updater_script_path: Path) -> None:
    from app.services.runtime_assets import MANAGED_SYSTEMD_ASSETS, MANAGED_TIMER_UNITS

    try:
        updater = updater_script_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseIncompatibleError(
            f"cannot inspect installed updater at {updater_script_path}: {exc}"
        ) from exc

    if "UPDATER_ASSET_MANIFEST_V1=1" in updater:
        return

    missing = [
        destination
        for source, destination in MANAGED_SYSTEMD_ASSETS
        if (
            f'sync_systemd_asset_from_dir "${{source_dir}}" "packaging/{source}" "{destination}"'
            not in updater
        )
    ]
    missing.extend(
        timer
        for timer in MANAGED_TIMER_UNITS
        if f"systemctl enable --now {timer}" not in updater
        and timer not in missing
    )
    if missing:
        raise ReleaseIncompatibleError(
            "the installed updater cannot activate this release's systemd assets: "
            + ", ".join(missing)
            + f". Install this release first: {_staged_installer_remedy()}. "
            "The installer activates the release when it succeeds."
        )


def check_staged_systemd_assets() -> None:
    from app.services.runtime_assets import MANAGED_SYSTEMD_ASSETS, MANAGED_TIMER_UNITS

    packaging_dir = Path(__file__).resolve().parent.parent / "packaging"
    missing = [
        source
        for source, _destination in MANAGED_SYSTEMD_ASSETS
        if not (packaging_dir / source).is_file()
    ]
    destinations = {destination for _source, destination in MANAGED_SYSTEMD_ASSETS}
    missing.extend(timer for timer in MANAGED_TIMER_UNITS if timer not in destinations)
    if missing:
        raise ReleaseIncompatibleError(
            "staged release is missing required systemd assets: "
            + ", ".join(missing)
        )


def run_release_preflight(
    database_url: str,
    *,
    skip_runtime_check: bool = False,
    updater_script_path: Path | None = None,
) -> None:
    check_host_release()
    check_staged_systemd_assets()
    if updater_script_path is not None:
        check_installed_updater_assets(updater_script_path)
    if not skip_runtime_check:
        check_container_runtime()
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
    parser.add_argument("--skip-runtime-check", action="store_true")
    parser.add_argument("--installer-mode", action="store_true")
    args = parser.parse_args(argv)
    updater_script_path = (
        None
        if args.installer_mode
        else Path(
            os.environ.get(
                "UPDATER_SCRIPT_PATH",
                "/var/lib/cnc/current/scripts/update_from_github.sh",
            )
        )
    )
    try:
        run_release_preflight(
            args.database_url,
            skip_runtime_check=args.skip_runtime_check,
            updater_script_path=updater_script_path,
        )
    except ReleaseIncompatibleError as exc:
        print(f"[preflight][error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
