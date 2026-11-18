from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from weakref import WeakKeyDictionary
import copy
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time
from typing import Any, Callable
import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.services.thread_workers import BoundedThreadWorker, HostMutationLifecycle
from app.services.tools.core import unwind_recovery
from app.config import Settings, get_settings
from app.services.config_change import full_reconcile
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.models.entities import Backend, BackendBackup, Input
from app.services.app_containers import write_app_control_assets
from app.services.app_quadlet import (
    quadlet_container_path,
    quadlet_container_service_name,
)
from app.services.apply_service import run_apply
from app.services.cluster_backups import mirror_backend_backup_to_cluster
from app.services.commands import run_command
from app.services.container_runtime import container_presence
from app.services.host_journal import HostJournal, journal_action
from app.services.observation import known
from app.services.control_events import emit_control_event
from app.services.error_reporting import CNCError, ErrorCode
from app.services.inter_app_interfaces import pending_inter_app_interfaces_json
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
)
from app.services.operations import OperationHandle, host_mutation_operation
from app.services.renderers import container_name, safe_slug
from app.services.sandbox_profiles import app_sandbox_dir
from app.services.safe_tar import safe_extract_tar, validate_safe_tar
from app.services.guest_metadata import GuestArchiveView, guest_tar_filter
from app.services.guest_isolation import validate_guest_volume_paths
from app.services.validators import (
    ValidationError,
    ensure_backend_name,
    ensure_healthcheck_host_header,
    ensure_host_paths_not_nested,
    parse_volume_binding,
    parse_volumes_json,
    validate_backend_collection,
    validate_backend_shape,
    validate_input_bindings,
)

BUNDLE_METADATA_NAME = "metadata.json"
# Upload limits measure compressed bytes. These bound what a bundle may expand
# to, read from member headers before any payload is decompressed to disk.
BUNDLE_METADATA_MAX_BYTES = 16 * 1024 * 1024
BUNDLE_MAX_MEMBERS = 5_000_000
CONTAINER_SNAPSHOT_NAME = "container-image.tar"
BUNDLE_FORMAT_VERSION = 1
GUEST_CONTRACT_VERSION = 1
CLONE_CONTRACT_VERSION = 1
logger = get_logger("backup")
BackupProgressCallback = Callable[[int, str], None]
BACKUP_DESCRIPTION_CACHE_TTL_SEC = 300
BACKUP_DESCRIPTION_CACHE_MAX = 128
_description_workers = BoundedThreadWorker(2)
_runtime_workers = BoundedThreadWorker(2)
_description_tasks: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[object, ...], asyncio.Task]
] = WeakKeyDictionary()
_backup_description_cache: dict[tuple[object, ...], tuple[float, dict[str, Any]]] = {}
BACKUP_RESTART_ERROR = "backup interrupted by CNC restart"


def _bundle_extension(settings: Settings) -> str:
    return ".tar" if settings.backend_backup_archive_format == "tar" else ".tar.gz"


def _bundle_write_open_kwargs(settings: Settings) -> tuple[str, dict[str, Any]]:
    if settings.backend_backup_archive_format == "tar":
        return "w", {}
    return "w:gz", {"compresslevel": settings.backend_backup_gzip_compresslevel}


def _timestamp_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


# Access protection travels with the archive: an input recreated by a restore
# must come back behind the same Shield and HTTPS settings it was saved with.
_INPUT_PROTECTION_FIELDS = (
    "https_enabled",
    "https_provider",
    "shield_enabled",
    "shield_code_hash",
)


def _metadata_snapshot(backend: Backend) -> dict[str, Any]:
    return {
        "backend": {
            "id": backend.id,
            "name": backend.name,
            "kind": backend.kind,
            "port": backend.port,
            "static_root": backend.static_root,
            "sandbox_profile": backend.sandbox_profile,
            "handoff_port": backend.handoff_port,
            "healthcheck_mode": backend.healthcheck_mode,
            "healthcheck_path": backend.healthcheck_path,
            "healthcheck_host_header": backend.healthcheck_host_header,
            "resource_mode": backend.resource_mode,
            "resource_size": backend.resource_size,
            "memory_high_override": backend.memory_high_override,
            "memory_max_override": backend.memory_max_override,
            "cpu_quota_override": backend.cpu_quota_override,
            "inter_app_interfaces_json": backend.inter_app_interfaces_json,
            "volumes_json": backend.volumes_json,
            "hardening_config_json": backend.hardening_config_json or "{}",
            "shield_enabled": backend.shield_enabled,
            "shield_code_hash": backend.shield_code_hash,
            "enabled": backend.enabled,
            "notes": backend.notes,
        },
        "inputs": [
            {
                "kind": item.kind,
                "hostname": item.hostname,
                "enabled": item.enabled,
                **{field: getattr(item, field) for field in _INPUT_PROTECTION_FIELDS},
                "connection": item.connection or "public_ip",
            }
            for item in sorted(backend.inputs, key=lambda item: item.id)
        ],
    }


def _resolved_mount_entries(
    backend: Backend, settings: Settings
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_entry(
        *,
        host_path: str,
        declared: str,
        target_path: str | None,
        link_policy: str = "strict",
    ) -> None:
        normalized = str(Path(host_path))
        if normalized in seen:
            return
        seen.add(normalized)
        path = Path(normalized)
        entries.append(
            {
                "host_path": normalized,
                "declared": declared,
                "target_path": target_path,
                "exists": path.exists(),
                "kind": "dir"
                if path.is_dir()
                else ("file" if path.is_file() else "missing"),
                "archive_prefix": f"mounts/{len(entries)}",
                "link_policy": link_policy,
            }
        )

    if (
        backend.kind == "static"
        and backend.static_root
        and Path(backend.static_root).is_absolute()
    ):
        add_entry(
            host_path=backend.static_root,
            declared=backend.static_root,
            target_path=None,
        )

    if backend.kind == "app":
        sandbox_dir = app_sandbox_dir(settings, backend.name)
        add_entry(
            host_path=str(sandbox_dir),
            declared=str(sandbox_dir),
            target_path=None,
            link_policy="guest",
        )
        for volume in parse_volumes_json(backend.volumes_json):
            source, _, remainder = volume.partition(":")
            if not source.startswith("/"):
                continue
            target_path = remainder.split(":", 1)[0] if remainder else ""
            add_entry(
                host_path=source,
                declared=volume,
                target_path=target_path or None,
                link_policy="guest",
            )
            try:
                validate_guest_volume_paths([volume])
                options = volume.split(":", 2)[2:] or [""]
                if "ro" not in options[0].split(","):
                    entries[-1]["ownership_policy"] = "canonical_guest"
            except (ValueError, OSError):
                pass

    return entries


def _bundle_metadata(backend: Backend, settings: Settings) -> dict[str, Any]:
    mount_entries = _resolved_mount_entries(backend, settings)
    ensure_host_paths_not_nested(
        (entry["host_path"] for entry in mount_entries if entry["exists"]),
        field_name=f"backup paths for {backend.name}",
    )
    metadata = _metadata_snapshot(backend)
    metadata["bundle_format_version"] = BUNDLE_FORMAT_VERSION
    metadata["created_at"] = datetime.now(UTC).isoformat()
    # Files are copied without pausing the app, so a structurally valid bundle
    # may still hold files from different moments. Say so instead of implying
    # application consistency.
    metadata["capture_mode"] = (
        "offline"
        if backend.kind == "app"
        and not _backend_container_exists(backend.name, settings)
        else "live"
    )
    metadata["mount_entries"] = mount_entries
    metadata["resolved_host_paths"] = [entry["host_path"] for entry in mount_entries]
    metadata["guest_contract"] = _guest_contract_snapshot(
        backend, settings, mount_entries
    )
    return metadata


def _guest_contract_snapshot(
    backend: Backend,
    settings: Settings,
    mount_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": GUEST_CONTRACT_VERSION,
        "backend_name": backend.name,
        "backend_kind": backend.kind,
        "filesystem_semantics": {
            "archive": "posix-tar",
            "preserve_guest_links": any(
                entry.get("link_policy") == "guest" for entry in mount_entries
            ),
            "reject_links_for_host_mounts": True,
            "reject_devices": True,
        },
        "mounts": [
            {
                "host_path": entry.get("host_path"),
                "target_path": entry.get("target_path"),
                "archive_prefix": entry.get("archive_prefix"),
                "link_policy": entry.get("link_policy", "strict"),
                "required_for_clone": bool(entry.get("exists")),
            }
            for entry in mount_entries
            if isinstance(entry, dict)
        ],
        "clone_defaults": {
            "inputs": "strip",
            "enabled": False,
            "container_snapshot": "exclude",
            "healthcheck_host_header": "preserve-and-validate",
        },
    }
    if backend.kind == "app":
        payload["guest"] = {
            "sandbox_profile": backend.sandbox_profile,
            "sandbox_dir": str(app_sandbox_dir(settings, backend.name)),
            "guest_rootfs": str(app_sandbox_dir(settings, backend.name) / "rootfs"),
            "handoff_port": backend.handoff_port,
            "declared_volumes": [
                _volume_contract_payload(volume)
                for volume in parse_volumes_json(backend.volumes_json)
            ],
        }
    return payload


def _volume_contract_payload(volume: str) -> dict[str, str | None]:
    binding = parse_volume_binding(volume)
    return {
        "source": binding.source,
        "target": binding.target,
        "options": binding.options,
    }


def _bundle_has_container_snapshot(metadata: dict[str, Any]) -> bool:
    snapshot = metadata.get("container_snapshot")
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("included")
        and snapshot.get("archive_name") == CONTAINER_SNAPSHOT_NAME
    )


def _bundle_scope(metadata: dict[str, Any]) -> str:
    if _bundle_has_container_snapshot(metadata):
        return "full_state"
    mount_entries = metadata.get("mount_entries")
    if not isinstance(mount_entries, list):
        return "metadata_only"
    return (
        "mounted_data"
        if any(
            isinstance(entry, dict) and entry.get("exists") for entry in mount_entries
        )
        else "metadata_only"
    )


def _bundle_notes(metadata: dict[str, Any], *, imported: bool = False) -> str:
    mount_entries = metadata.get("mount_entries")
    mounted_count = 0
    if isinstance(mount_entries, list):
        mounted_count = sum(
            1
            for entry in mount_entries
            if isinstance(entry, dict) and entry.get("exists")
        )
    prefix = "imported bundle" if imported else "bundle"
    has_snapshot = _bundle_has_container_snapshot(metadata)
    snapshot_suffix = " plus container snapshot" if has_snapshot else ""
    if mounted_count:
        return f"{prefix} with metadata plus {mounted_count} mounted path(s){snapshot_suffix}"
    if has_snapshot:
        return f"{prefix} with metadata plus container snapshot"
    return f"{prefix} with metadata only"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_size_bytes(path: Path) -> int:
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    try:
        walker = path.rglob("*")
    except OSError:
        return 0
    for child in walker:
        try:
            if child.is_symlink():
                total += child.lstat().st_size
            elif child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _format_progress_bytes(value: int) -> str:
    if value <= 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit = units[0]
    for current in units:
        unit = current
        if size < 1000 or current == units[-1]:
            break
        size /= 1000
    if unit in {"B", "KB", "MB"}:
        return f"{int(round(size))}{unit}"
    return f"{size:.1f}{unit}"


def _compression_savings_percent(source_size: int, bundle_size: int) -> int:
    if source_size <= 0 or bundle_size <= 0:
        return 0
    savings = max(0.0, 1.0 - (bundle_size / source_size))
    return int(round(min(0.99, savings) * 100))


def _report_backup_progress(
    callback: BackupProgressCallback | None,
    progress: int,
    message: str,
) -> None:
    if callback is None:
        return
    try:
        callback(progress, message)
    except Exception:
        logger.warning("backup.progress_callback_failed", exc_info=True)


def _progressing_backup_tar_filter(
    *,
    allow_links: bool,
    path_label: str,
    path_total_bytes: int,
    progress_start: int,
    progress_end: int,
    progress_callback: BackupProgressCallback | None,
    metadata_filter: Callable[[tarfile.TarInfo], tarfile.TarInfo | None] | None = None,
) -> Callable[[tarfile.TarInfo], tarfile.TarInfo | None]:
    base_filter = _backup_tar_filter(allow_links=allow_links)
    exported_bytes = 0
    last_progress = progress_start

    def progress_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal exported_bytes, last_progress
        filtered = base_filter(tarinfo)
        if filtered is None:
            return None
        if metadata_filter is not None:
            filtered = metadata_filter(filtered)
            if filtered is None:
                return None
        if filtered.isfile():
            exported_bytes += max(0, int(filtered.size))
            if path_total_bytes > 0 and progress_end > progress_start:
                ratio = min(1.0, exported_bytes / path_total_bytes)
                next_progress = progress_start + int(
                    (progress_end - progress_start) * ratio
                )
                if next_progress > last_progress:
                    last_progress = next_progress
                    _report_backup_progress(
                        progress_callback,
                        next_progress,
                        (
                            f"Exporting path {path_label}: "
                            f"{_format_progress_bytes(min(exported_bytes, path_total_bytes))} "
                            f"of {_format_progress_bytes(path_total_bytes)}."
                        ),
                    )
        return filtered

    return progress_filter


def _temporary_snapshot_image_ref(backend_name: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return (
        f"localhost/cnc-backup-snapshot-{safe_slug(backend_name)}:{stamp}-{os.getpid()}"
    )


def restored_snapshot_image_ref(backend_name: str) -> str:
    return f"localhost/cnc-restored-{safe_slug(backend_name)}:latest"


def _snapshot_capture_is_unsupported(error_text: str) -> bool:
    normalized = str(error_text or "").strip().lower()
    return "cannot commit a container that uses an exploded rootfs" in normalized


def _capture_container_snapshot(
    *,
    backend: Backend,
    settings: Settings,
    temp_dir: Path,
) -> dict[str, Any] | None:
    if backend.kind != "app" or not _backend_container_exists(backend.name, settings):
        return None

    container = container_name(backend.name)
    snapshot_image = _temporary_snapshot_image_ref(backend.name)
    snapshot_path = temp_dir / CONTAINER_SNAPSHOT_NAME
    try:
        commit_result = run_command(
            ["podman", "commit", container, snapshot_image],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not commit_result.ok:
            error_text = "\n".join(
                part
                for part in (commit_result.stderr, commit_result.stdout)
                if str(part or "").strip()
            )
            if _snapshot_capture_is_unsupported(error_text):
                logger.info(
                    "backup.snapshot.skipped",
                    backend=backend.name,
                    container=container,
                    reason="exploded_rootfs_unsupported",
                )
                return None
            raise RuntimeError(
                commit_result.stderr or commit_result.stdout or "podman commit failed"
            )

        save_result = run_command(
            ["podman", "save", "-o", str(snapshot_path), snapshot_image],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not save_result.ok:
            raise RuntimeError(
                save_result.stderr or save_result.stdout or "podman save failed"
            )

        return {
            "included": True,
            "archive_name": CONTAINER_SNAPSHOT_NAME,
            "image_format": "podman-archive",
            "size_bytes": snapshot_path.stat().st_size,
        }
    finally:
        run_command(
            ["podman", "image", "rm", "-f", snapshot_image],
            timeout_sec=settings.command_timeout_apply_sec,
        )


def _write_bundle_member_to_file(
    bundle_path: Path, member_name: str, destination_path: Path
) -> None:
    with tarfile.open(bundle_path, "r:*") as archive:
        try:
            member = archive.getmember(member_name)
        except KeyError as exc:
            raise RuntimeError(f"backup bundle member missing: {member_name}") from exc
        handle = archive.extractfile(member)
        if handle is None:
            raise RuntimeError(f"backup bundle member missing: {member_name}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with destination_path.open("wb") as output:
            shutil.copyfileobj(handle, output)


def _parse_loaded_image_ref(output: str) -> str | None:
    pattern = re.compile(r"Loaded image(?:\(s\))?:\s*(.+)")
    for line in output.splitlines():
        match = pattern.search(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _restore_container_snapshot_image(
    *,
    bundle_path: Path,
    metadata: dict[str, Any],
    backend_name: str,
    settings: Settings,
) -> str | None:
    if not _bundle_has_container_snapshot(metadata):
        return None

    with tempfile.TemporaryDirectory(prefix="cnc-backup-image-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        snapshot_tar = temp_dir / CONTAINER_SNAPSHOT_NAME
        _write_bundle_member_to_file(bundle_path, CONTAINER_SNAPSHOT_NAME, snapshot_tar)

        load_result = run_command(
            ["podman", "load", "-i", str(snapshot_tar)],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not load_result.ok:
            raise RuntimeError(
                load_result.stderr or load_result.stdout or "podman load failed"
            )
        loaded_ref = _parse_loaded_image_ref(
            "\n".join(filter(None, [load_result.stdout, load_result.stderr]))
        )
        if not loaded_ref:
            raise RuntimeError("unable to determine loaded container image ref")

        restore_ref = restored_snapshot_image_ref(backend_name)
        run_command(
            ["podman", "image", "rm", "-f", restore_ref],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        tag_result = run_command(
            ["podman", "tag", loaded_ref, restore_ref],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if not tag_result.ok:
            raise RuntimeError(
                tag_result.stderr or tag_result.stdout or "podman tag failed"
            )
        if loaded_ref != restore_ref:
            run_command(
                ["podman", "image", "rm", "-f", loaded_ref],
                timeout_sec=settings.command_timeout_apply_sec,
            )
        return restore_ref


def _write_backup_bundle(
    *,
    backend: Backend,
    bundle_path: Path,
    settings: Settings,
    include_container_snapshot: bool = True,
    progress_callback: BackupProgressCallback | None = None,
) -> dict[str, Any]:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    temp_bundle_path = bundle_path.with_name(f".{bundle_path.name}.{os.getpid()}.tmp")
    if temp_bundle_path.exists():
        temp_bundle_path.unlink()
    with tempfile.TemporaryDirectory(prefix="cnc-backup-bundle-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        _report_backup_progress(progress_callback, 12, "Gathering app data.")
        if include_container_snapshot:
            _report_backup_progress(
                progress_callback, 18, "Capturing container snapshot."
            )
        snapshot_payload = (
            _capture_container_snapshot(
                backend=backend, settings=settings, temp_dir=temp_dir
            )
            if include_container_snapshot
            else None
        )
        if include_container_snapshot:
            if snapshot_payload is None:
                _report_backup_progress(
                    progress_callback, 22, "Container snapshot skipped."
                )
            else:
                _report_backup_progress(
                    progress_callback, 22, "Container snapshot captured."
                )
        _report_backup_progress(progress_callback, 28, "Collecting mounted paths.")
        metadata = _bundle_metadata(backend, settings)
        guest_view = (
            GuestArchiveView.capture(container_name(backend.name), run_command)
            if backend.kind == "app"
            else None
        )
        metadata["container_snapshot"] = snapshot_payload or {
            "included": False,
            "archive_name": CONTAINER_SNAPSHOT_NAME,
        }
        metadata_bytes = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8")
        archived_entries = [
            entry
            for entry in metadata.get("mount_entries", [])
            if isinstance(entry, dict) and entry.get("exists")
        ]
        source_size_bytes = len(metadata_bytes)
        path_sizes: dict[str, int] = {}
        for entry in archived_entries:
            host_path = entry.get("host_path")
            if isinstance(host_path, str):
                path_size = _path_size_bytes(Path(host_path))
                path_sizes[host_path] = path_size
                source_size_bytes += path_size
        snapshot_path = temp_dir / CONTAINER_SNAPSHOT_NAME
        if snapshot_payload is not None and snapshot_path.exists():
            source_size_bytes += _path_size_bytes(snapshot_path)
        _report_backup_progress(
            progress_callback,
            32,
            f"Measured {_format_progress_bytes(source_size_bytes)} of source data.",
        )

        try:
            open_mode, open_kwargs = _bundle_write_open_kwargs(settings)
            with tarfile.open(temp_bundle_path, open_mode, **open_kwargs) as archive:
                metadata_info = tarfile.TarInfo(BUNDLE_METADATA_NAME)
                metadata_info.size = len(metadata_bytes)
                metadata_info.mtime = int(datetime.now(UTC).timestamp())
                metadata_info.mode = 0o600
                archive.addfile(metadata_info, io.BytesIO(metadata_bytes))
                path_count = max(len(archived_entries), 1)
                for index, entry in enumerate(archived_entries, start=1):
                    allow_links = entry.get("link_policy") == "guest"
                    path_progress_start = 34 + round((index - 1) * 30 / path_count)
                    path_progress_end = 34 + round(index * 30 / path_count)
                    host_path = str(entry["host_path"])
                    path_size = path_sizes.get(host_path, 0)
                    _report_backup_progress(
                        progress_callback,
                        path_progress_start,
                        f"Exporting path {host_path} ({_format_progress_bytes(path_size)}).",
                    )
                    archive.add(
                        host_path,
                        arcname=str(entry["archive_prefix"]),
                        filter=_progressing_backup_tar_filter(
                            allow_links=allow_links,
                            path_label=host_path,
                            path_total_bytes=path_size,
                            progress_start=path_progress_start,
                            progress_end=path_progress_end,
                            progress_callback=progress_callback,
                            metadata_filter=guest_tar_filter(
                                source=Path(host_path),
                                archive_prefix=str(entry["archive_prefix"]),
                                view=guest_view,
                                rootfs_prefix="rootfs"
                                if entry.get("target_path") is None
                                else "",
                                include_xattrs=entry.get("target_path") is None,
                            )
                            if allow_links and guest_view is not None
                            else None,
                        ),
                    )
                    _report_backup_progress(
                        progress_callback,
                        path_progress_end,
                        f"Exported path {host_path} ({_format_progress_bytes(path_size)}).",
                    )
                if snapshot_payload is not None:
                    _report_backup_progress(
                        progress_callback, 66, "Exporting container snapshot."
                    )
                    archive.add(
                        str(temp_dir / CONTAINER_SNAPSHOT_NAME),
                        arcname=CONTAINER_SNAPSHOT_NAME,
                    )
                _report_backup_progress(
                    progress_callback,
                    76,
                    f"Compressing backup bundle from {_format_progress_bytes(source_size_bytes)}.",
                )
            if guest_view is not None:
                guest_view.verify(run_command)
            temp_bundle_path.replace(bundle_path)
        except Exception:
            try:
                temp_bundle_path.unlink()
            except FileNotFoundError:
                pass
            raise

    bundle_size = bundle_path.stat().st_size
    compression_savings_percent = _compression_savings_percent(
        source_size_bytes, bundle_size
    )
    _report_backup_progress(
        progress_callback,
        84,
        (
            f"Compressed backup to {_format_progress_bytes(bundle_size)} "
            f"from {_format_progress_bytes(source_size_bytes)}; "
            f"{compression_savings_percent}% saving achieved."
        ),
    )

    return {
        "scope": _bundle_scope(metadata),
        "bundle_path": str(bundle_path),
        "bundle_sha256": _sha256_file(bundle_path),
        "size_bytes": bundle_size,
        "source_size_bytes": source_size_bytes,
        "compression_savings_percent": compression_savings_percent,
        "notes": _bundle_notes(metadata),
    }


def _backup_tar_filter(*, allow_links: bool):
    def _filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if member.isdev() or member.isfifo():
            return None
        if (member.issym() or member.islnk()) and not allow_links:
            return None
        return member

    return _filter


def _load_bundle_metadata(bundle_path: Path) -> dict[str, Any]:
    with tarfile.open(bundle_path, "r:*") as archive:
        _scan_bundle_members(archive, max_expanded_bytes=None)
        return _metadata_from_open_archive(archive)


def _scan_bundle_members(
    archive: tarfile.TarFile, *, max_expanded_bytes: int | None
) -> int:
    """Read every member header within budget; return the expanded byte total.

    Iterating stops at the first header over budget, so an archive that
    declares millions of members or terabytes of payload is rejected without
    being decompressed. Extraction writes exactly the sizes these headers
    declare, so the total is also what a restore will need on disk.
    """
    expanded_bytes = 0
    for count, member in enumerate(archive, start=1):
        if count > BUNDLE_MAX_MEMBERS:
            raise RuntimeError(
                f"backup bundle has more than {BUNDLE_MAX_MEMBERS} members"
            )
        if member.isfile():
            expanded_bytes += member.size
        if max_expanded_bytes is not None and expanded_bytes > max_expanded_bytes:
            raise RuntimeError(
                "backup bundle expands beyond the configured limit of "
                f"{max_expanded_bytes} bytes"
            )
    return expanded_bytes


def _metadata_from_open_archive(archive: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = archive.getmember(BUNDLE_METADATA_NAME)
    except KeyError as exc:
        raise RuntimeError("backup bundle metadata missing") from exc
    if member.size > BUNDLE_METADATA_MAX_BYTES:
        raise RuntimeError("backup bundle metadata is too large")
    handle = archive.extractfile(member)
    if handle is None:
        raise RuntimeError("backup bundle metadata missing")
    with handle:
        payload = json.loads(handle.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("backup bundle metadata is invalid")
    return payload


def _bundle_archived_mount_count(metadata: dict[str, Any]) -> int:
    mount_entries = metadata.get("mount_entries")
    if not isinstance(mount_entries, list):
        return 0
    return sum(
        1 for entry in mount_entries if isinstance(entry, dict) and entry.get("exists")
    )


def _bundle_declared_mounts(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    mount_entries = metadata.get("mount_entries")
    if not isinstance(mount_entries, list):
        return []

    declared_mounts: list[dict[str, Any]] = []
    for entry in mount_entries:
        if not isinstance(entry, dict):
            continue
        host_path = entry.get("host_path")
        if not isinstance(host_path, str) or not host_path:
            continue
        declared_mounts.append(
            {
                "host_path": host_path,
                "declared": str(entry.get("declared") or host_path),
                "target_path": entry.get("target_path")
                if isinstance(entry.get("target_path"), str)
                else None,
                "archived": bool(entry.get("exists")),
                "kind": str(entry.get("kind") or "unknown"),
            }
        )
    return declared_mounts


def _bundle_covered_paths(metadata: dict[str, Any]) -> list[str]:
    return [
        str(entry["host_path"])
        for entry in _bundle_declared_mounts(metadata)
        if entry.get("archived")
    ]


def _bundle_covered_paths_summary(metadata: dict[str, Any]) -> str:
    covered_paths = _bundle_covered_paths(metadata)
    snapshot_included = _bundle_has_container_snapshot(metadata)

    if covered_paths:
        if len(covered_paths) <= 2:
            summary = ", ".join(covered_paths)
        else:
            summary = f"{', '.join(covered_paths[:2])} +{len(covered_paths) - 2} more"
        return f"{summary}; container snapshot" if snapshot_included else summary

    if snapshot_included:
        return "container snapshot only"
    return "metadata only"


def _bundle_coverage_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "declared_mounts": _bundle_declared_mounts(metadata),
        "covered_paths": _bundle_covered_paths(metadata),
        "covered_paths_summary": _bundle_covered_paths_summary(metadata),
    }


def _bundle_restore_signals(
    metadata: dict[str, Any],
    *,
    current_backend: Backend | None = None,
) -> dict[str, Any]:
    backend_payload = (
        metadata.get("backend") if isinstance(metadata.get("backend"), dict) else {}
    )
    bundle_backend_kind = (
        str(backend_payload.get("kind") or "").strip().lower() or "unknown"
    )
    bundle_backend_name = str(backend_payload.get("name") or "").strip() or None
    declared_mounts = _bundle_declared_mounts(metadata)
    archived_mounts = [entry for entry in declared_mounts if entry.get("archived")]
    missing_mounts = [entry for entry in declared_mounts if not entry.get("archived")]
    container_snapshot_included = _bundle_has_container_snapshot(metadata)

    risk_flags: list[str] = []
    risk_details: list[str] = []

    if missing_mounts:
        risk_flags.append("partial_mount_coverage")
        risk_details.append(
            f"{len(missing_mounts)} declared mount path(s) were missing when the bundle was created"
        )

    if not archived_mounts and not container_snapshot_included:
        risk_flags.append("metadata_only_restore")
        risk_details.append(
            "bundle contains metadata only with no mounted data or container snapshot payload"
        )
    elif not archived_mounts and container_snapshot_included:
        risk_flags.append("snapshot_only_restore")
        risk_details.append(
            "bundle contains a container snapshot but no mounted data payload"
        )

    if bundle_backend_kind == "app" and not container_snapshot_included:
        risk_flags.append("app_without_container_snapshot")
        risk_details.append(
            "app bundle does not include a container snapshot and will rely on metadata-driven rebuild"
        )

    if current_backend is not None:
        current_kind = str(current_backend.kind or "").strip().lower()
        if (
            current_kind
            and bundle_backend_kind != "unknown"
            and current_kind != bundle_backend_kind
        ):
            risk_flags.append("backend_kind_mismatch")
            risk_details.append(
                f"bundle was captured from a {bundle_backend_kind} output but the current output is {current_kind}"
            )

    if "backend_kind_mismatch" in risk_flags or "metadata_only_restore" in risk_flags:
        restore_readiness = "high_risk"
    elif risk_flags:
        restore_readiness = "review"
    else:
        restore_readiness = "ready"

    restore_readiness_label = {
        "ready": "ready",
        "review": "review before restore",
        "high_risk": "high risk",
    }[restore_readiness]

    capture_consistency = (
        "offline" if metadata.get("capture_mode") == "offline" else "live"
    )
    return {
        "bundle_backend_name": bundle_backend_name,
        "bundle_backend_kind": bundle_backend_kind,
        "capture_consistency": capture_consistency,
        "capture_consistency_label": {
            "offline": "offline copy",
            "live": "live copy; files changed during the backup may not match each other",
        }[capture_consistency],
        "restore_readiness": restore_readiness,
        "restore_readiness_label": restore_readiness_label,
        "risk_flags": risk_flags,
        "risk_summary": "; ".join(risk_details)
        if risk_details
        else "no known restore risks",
    }


def _verify_bundle_contents(
    bundle_path: Path,
    *,
    expected_sha256: str | None = None,
    max_expanded_bytes: int | None = None,
) -> dict[str, Any]:
    if not bundle_path.exists():
        raise RuntimeError("backup bundle is missing")

    actual_sha256 = _sha256_file(bundle_path)
    expected = str(expected_sha256 or "").strip()
    if expected and actual_sha256 != expected:
        raise CNCError(
            ErrorCode.BACKUP_CHECKSUM_MISMATCH,
            "backup bundle checksum mismatch",
        )

    if max_expanded_bytes is None:
        max_expanded_bytes = get_settings().backend_backup_expanded_max_bytes
    with tarfile.open(bundle_path, "r:*") as archive:
        expanded_bytes = _scan_bundle_members(
            archive, max_expanded_bytes=max_expanded_bytes
        )
        metadata = _metadata_from_open_archive(archive)
        backend_payload = metadata.get("backend")
        if not isinstance(backend_payload, dict):
            raise RuntimeError("backup metadata missing backend snapshot")

        bundle_format_version = metadata.get("bundle_format_version")
        if not isinstance(bundle_format_version, int) or bundle_format_version < 1:
            raise RuntimeError("backup bundle format version is invalid")

        member_names: set[str] = set()
        for member in archive.getmembers():
            member_name = member.name.strip()
            if not member_name:
                continue
            if member_name.startswith("/") or ".." in Path(member_name).parts:
                raise RuntimeError(f"invalid bundle member path: {member_name}")
            member_names.add(member_name)
        _validate_bundle_restore_members(archive, metadata)

    mount_entries = metadata.get("mount_entries")
    archived_mounts = 0
    if isinstance(mount_entries, list):
        for entry in mount_entries:
            if not isinstance(entry, dict) or not entry.get("exists"):
                continue
            archive_prefix = entry.get("archive_prefix")
            if not isinstance(archive_prefix, str) or not archive_prefix:
                raise RuntimeError("backup bundle metadata mount entry is invalid")
            if not any(
                name == archive_prefix or name.startswith(archive_prefix + "/")
                for name in member_names
            ):
                raise RuntimeError(
                    f"backup bundle missing mounted path payload: {archive_prefix}"
                )
            archived_mounts += 1

    snapshot_included = _bundle_has_container_snapshot(metadata)
    if snapshot_included and CONTAINER_SNAPSHOT_NAME not in member_names:
        raise RuntimeError("backup bundle missing container snapshot payload")

    return {
        "bundle_path": str(bundle_path),
        "bundle_sha256": actual_sha256,
        "bundle_size_bytes": bundle_path.stat().st_size,
        "bundle_expanded_bytes": expanded_bytes,
        "bundle_format_version": bundle_format_version,
        "scope": _bundle_scope(metadata),
        "notes": _bundle_notes(metadata),
        "mount_entries_archived": archived_mounts,
        "mount_entries_total": _bundle_archived_mount_count(metadata),
        "container_snapshot_included": snapshot_included,
        **_bundle_coverage_payload(metadata),
        **_bundle_restore_signals(metadata),
        "metadata": metadata,
    }


def _bundle_restore_member_policy(
    metadata: dict[str, Any],
) -> tuple[set[str], set[str]]:
    mount_entries = metadata.get("mount_entries")
    if not isinstance(mount_entries, list):
        return set(), set()
    allowed_prefixes = {
        str(entry.get("archive_prefix"))
        for entry in mount_entries
        if isinstance(entry, dict)
        and entry.get("exists")
        and isinstance(entry.get("archive_prefix"), str)
    }
    guest_symlink_roots = {
        str(entry.get("archive_prefix"))
        for entry in mount_entries
        if (
            isinstance(entry, dict)
            and entry.get("exists")
            and entry.get("link_policy") == "guest"
            and isinstance(entry.get("archive_prefix"), str)
        )
    }
    return allowed_prefixes, guest_symlink_roots


def _bundle_restore_member_include(
    allowed_prefixes: set[str],
) -> Callable[[tarfile.TarInfo], bool]:
    def include_member(member: tarfile.TarInfo) -> bool:
        member_name = member.name.strip()
        if member_name == BUNDLE_METADATA_NAME:
            return False
        return any(
            member_name == prefix or member_name.startswith(prefix + "/")
            for prefix in allowed_prefixes
        )

    return include_member


def _validate_bundle_restore_members(
    archive: tarfile.TarFile,
    metadata: dict[str, Any],
) -> None:
    allowed_prefixes, guest_symlink_roots = _bundle_restore_member_policy(metadata)
    validate_safe_tar(
        archive,
        include=_bundle_restore_member_include(allowed_prefixes),
        allow_relative_symlinks=True,
        guest_symlink_roots=guest_symlink_roots,
        guest_metadata_roots={
            str(PurePosixPath(entry["archive_prefix"]) / "rootfs")
            for entry in metadata.get("mount_entries", [])
            if isinstance(entry, dict)
            and entry.get("archive_prefix") in guest_symlink_roots
            and entry.get("target_path") is None
        },
        guest_owner_roots=_bundle_guest_owner_roots(metadata),
    )


def _bundle_guest_owner_roots(metadata: dict[str, Any]) -> set[str]:
    return {
        str(entry["archive_prefix"])
        for entry in metadata.get("mount_entries", [])
        if isinstance(entry, dict)
        and entry.get("exists")
        and entry.get("ownership_policy") == "canonical_guest"
        and isinstance(entry.get("archive_prefix"), str)
        and entry.get("target_path") is not None
    }


def _backend_payload_fields() -> tuple[str, ...]:
    return (
        "port",
        "static_root",
        "sandbox_profile",
        "handoff_port",
        "healthcheck_mode",
        "healthcheck_path",
        "healthcheck_host_header",
        "resource_mode",
        "resource_size",
        "memory_high_override",
        "memory_max_override",
        "cpu_quota_override",
        "inter_app_interfaces_json",
        "hardening_config_json",
        "shield_enabled",
        "shield_code_hash",
        "volumes_json",
        "enabled",
        "notes",
    )


def _input_from_metadata(item: dict[str, Any], *, kind: str, hostname: str) -> Input:
    recorded = all(field in item for field in _INPUT_PROTECTION_FIELDS)
    restored = Input(
        connection=item.get("connection", "public_ip"),
        kind=kind,
        hostname=hostname,
        # An archive from before protection was recorded cannot say whether
        # this input sat behind Shield or HTTPS. Keep it unpublished until an
        # operator has reviewed it rather than exposing it unprotected.
        enabled=bool(item.get("enabled", True)) and recorded,
    )
    if recorded:
        restored.https_enabled = bool(item["https_enabled"])
        restored.https_provider = str(item["https_provider"] or "self_signed")
        restored.shield_enabled = bool(item["shield_enabled"])
        restored.shield_code_hash = item["shield_code_hash"] or None
    else:
        logger.warning(
            "restore.input_protection_unrecorded",
            hostname=hostname,
            action="input restored disabled for review",
        )
    return restored


def _backend_payload_name(metadata: dict[str, Any]) -> str:
    backend_payload = metadata.get("backend")
    if not isinstance(backend_payload, dict):
        raise RuntimeError("backup metadata missing backend snapshot")
    return ensure_backend_name(str(backend_payload.get("name") or ""))


def _backend_from_metadata(
    metadata: dict[str, Any], *, backend_name: str | None = None
) -> Backend:
    backend_payload = metadata.get("backend")
    if not isinstance(backend_payload, dict):
        raise RuntimeError("backup metadata missing backend snapshot")

    resolved_name = ensure_backend_name(
        backend_name or str(backend_payload.get("name") or "")
    )
    backend = Backend(
        name=resolved_name,
        kind=str(backend_payload.get("kind") or "").strip().lower(),
        handoff_port=int(backend_payload.get("handoff_port") or 8000),
        volumes_json=str(backend_payload.get("volumes_json") or "[]"),
        enabled=bool(backend_payload.get("enabled", True)),
    )
    for field in _backend_payload_fields():
        if field not in backend_payload:
            continue
        setattr(backend, field, backend_payload.get(field))
    validate_backend_shape(backend)
    return backend


def _validate_restore_mount_entries(
    metadata: dict[str, Any],
    settings: Settings,
    *,
    backend_name: str,
) -> None:
    candidate_backend = _backend_from_metadata(metadata, backend_name=backend_name)
    expected_entries = {
        str(entry.get("archive_prefix")): str(entry.get("host_path"))
        for entry in _resolved_mount_entries(candidate_backend, settings)
        if isinstance(entry.get("archive_prefix"), str)
        and isinstance(entry.get("host_path"), str)
    }
    mount_entries = metadata.get("mount_entries")
    if not isinstance(mount_entries, list):
        return
    try:
        ensure_host_paths_not_nested(
            (
                str(entry["host_path"])
                for entry in mount_entries
                if isinstance(entry, dict)
                and entry.get("exists")
                and isinstance(entry.get("host_path"), str)
            ),
            field_name="backup restore paths",
        )
    except ValidationError as exc:
        raise CNCError(ErrorCode.RESTORE_MOUNT_PATH_MISMATCH, str(exc)) from exc
    seen_targets: set[str] = set()
    for entry in mount_entries:
        if not isinstance(entry, dict) or not entry.get("exists"):
            continue
        archive_prefix = entry.get("archive_prefix")
        host_path = entry.get("host_path")
        if not isinstance(archive_prefix, str) or not isinstance(host_path, str):
            raise RuntimeError("backup bundle metadata mount entry is invalid")
        expected_host_path = expected_entries.get(archive_prefix)
        if expected_host_path is None:
            raise CNCError(
                ErrorCode.RESTORE_MOUNT_PATH_MISMATCH,
                f"backup bundle mount entry is not restorable: {archive_prefix}",
            )
        if Path(host_path) != Path(expected_host_path):
            raise CNCError(
                ErrorCode.RESTORE_MOUNT_PATH_MISMATCH,
                "backup bundle restore path does not match the target backend: "
                f"{archive_prefix} -> {host_path}",
            )
        if host_path in seen_targets:
            raise RuntimeError(f"backup bundle restore path collision: {host_path}")
        seen_targets.add(host_path)


def _rewrite_clone_path_segment(
    segment: str,
    *,
    source_backend_name: str,
    target_backend_name: str,
) -> str:
    if segment == source_backend_name:
        return target_backend_name
    if segment.startswith(source_backend_name + "-"):
        return target_backend_name + segment[len(source_backend_name) :]
    if segment.endswith("-" + source_backend_name):
        return segment[: -len(source_backend_name)] + target_backend_name
    return segment


def _derive_clone_target_host_path(
    host_path: str,
    *,
    source_backend_name: str,
    target_backend_name: str,
) -> str:
    source_path = PurePosixPath(host_path)
    if not source_path.is_absolute():
        raise RuntimeError(f"clone only supports absolute mounted paths: {host_path}")

    rewritten_parts = [source_path.parts[0]]
    changed = False
    for segment in source_path.parts[1:]:
        rewritten = _rewrite_clone_path_segment(
            segment,
            source_backend_name=source_backend_name,
            target_backend_name=target_backend_name,
        )
        if rewritten != segment:
            changed = True
        rewritten_parts.append(rewritten)

    candidate = PurePosixPath(*rewritten_parts)
    if not changed:
        basename = source_path.name
        if not basename:
            raise RuntimeError(
                f"clone cannot derive target path for mount root: {host_path}"
            )
        candidate = source_path.with_name(f"{basename}-{target_backend_name}")
    return str(candidate)


def _clone_healthcheck_contract(
    backend_payload: dict[str, Any],
    *,
    source_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_header = backend_payload.get("healthcheck_host_header")
    normalized_header = ensure_healthcheck_host_header(
        str(raw_header or ""),
        field_name="healthcheck_host_header for clone",
    )
    backend_payload["healthcheck_host_header"] = normalized_header

    source_domain_inputs = sorted(
        hostname
        for hostname in (
            str(item.get("hostname") or "").strip().lower().rstrip(".")
            for item in source_inputs
            if isinstance(item, dict)
            and str(item.get("kind") or "domain").strip().lower() == "domain"
        )
        if hostname
    )
    warnings: list[str] = []
    if normalized_header and normalized_header in source_domain_inputs:
        backend_payload["healthcheck_host_header"] = ""
        normalized_header = ""
        warnings.append("source_domain_healthcheck_host_header_cleared")

    return {
        "mode": backend_payload.get("healthcheck_mode"),
        "path": backend_payload.get("healthcheck_path"),
        "host_header": normalized_header,
        "source_domain_inputs": source_domain_inputs,
        "warnings": warnings,
    }


def _clone_contract_payload(
    *,
    metadata: dict[str, Any],
    source_backend_name: str,
    target_backend_name: str,
    target_port: int | None,
    path_rewrites: list[dict[str, Any]],
    healthcheck: dict[str, Any],
) -> dict[str, Any]:
    guest_contract = metadata.get("guest_contract")
    if not isinstance(guest_contract, dict):
        guest_contract = {}
    return {
        "version": CLONE_CONTRACT_VERSION,
        "source_backend": source_backend_name,
        "target_backend": target_backend_name,
        "target_port": target_port,
        "guest_contract_version": guest_contract.get("version"),
        "metadata_rewrites": {
            "backend.name": target_backend_name,
            "backend.port": target_port,
            "backend.enabled": False,
            "inputs": [],
            "container_snapshot.included": False,
        },
        "filesystem": {
            "path_rewrites": path_rewrites,
            "preserve_guest_links": bool(
                guest_contract.get("filesystem_semantics", {}).get(
                    "preserve_guest_links"
                )
            )
            if isinstance(guest_contract.get("filesystem_semantics"), dict)
            else False,
        },
        "healthcheck": healthcheck,
        "post_clone_checks": [
            "backend_metadata_rewritten",
            "route_inputs_stripped",
            "restored_paths_exist",
            "healthcheck_host_header_valid",
            "app_control_assets_written",
        ],
    }


def _clone_bundle_metadata_for_new_backend(
    metadata: dict[str, Any],
    *,
    source_backend_name: str,
    target_backend_name: str,
    target_port: int | None,
) -> dict[str, Any]:
    clone_metadata = copy.deepcopy(metadata)
    backend_payload = clone_metadata.get("backend")
    if not isinstance(backend_payload, dict):
        raise RuntimeError("backup metadata missing backend snapshot")

    mount_entries = clone_metadata.get("mount_entries")
    if mount_entries is None:
        mount_entries = []
        clone_metadata["mount_entries"] = mount_entries
    if not isinstance(mount_entries, list):
        raise RuntimeError("backup metadata mount entries are invalid")

    rewritten_sources: dict[str, str] = {}
    missing_mounts: list[str] = []
    seen_targets: set[str] = set()
    rewritten_mount_entries: list[dict[str, Any]] = []
    path_rewrites: list[dict[str, Any]] = []

    for entry in mount_entries:
        if not isinstance(entry, dict):
            continue
        host_path = entry.get("host_path")
        if not isinstance(host_path, str) or not host_path.startswith("/"):
            rewritten_mount_entries.append(entry)
            continue
        if not entry.get("exists"):
            missing_mounts.append(host_path)
            rewritten_mount_entries.append(entry)
            continue

        target_host_path = _derive_clone_target_host_path(
            host_path,
            source_backend_name=source_backend_name,
            target_backend_name=target_backend_name,
        )
        if target_host_path in seen_targets:
            raise RuntimeError(f"clone target path collision: {target_host_path}")
        if Path(target_host_path).exists():
            raise RuntimeError(f"clone target path already exists: {target_host_path}")
        seen_targets.add(target_host_path)
        rewritten_sources[host_path] = target_host_path
        path_rewrites.append(
            {
                "source": host_path,
                "target": target_host_path,
                "archive_prefix": entry.get("archive_prefix"),
                "target_path": entry.get("target_path"),
                "link_policy": entry.get("link_policy", "strict"),
            }
        )

        rewritten_entry = dict(entry)
        rewritten_entry["host_path"] = target_host_path
        declared = entry.get("declared")
        if isinstance(declared, str):
            if str(backend_payload.get("kind") or "").strip().lower() == "app":
                target_path = entry.get("target_path")
                if isinstance(target_path, str) and ":" in declared:
                    binding = parse_volume_binding(declared)
                    options_suffix = f":{binding.options}" if binding.options else ""
                    rewritten_entry["declared"] = (
                        f"{target_host_path}:{binding.target}{options_suffix}"
                    )
                else:
                    rewritten_entry["declared"] = target_host_path
            else:
                rewritten_entry["declared"] = target_host_path
        rewritten_mount_entries.append(rewritten_entry)

    if missing_mounts:
        raise RuntimeError(
            "clone requires all declared mounted paths to exist: "
            + ", ".join(sorted(missing_mounts))
        )

    backend_payload["name"] = target_backend_name
    backend_payload["port"] = target_port
    backend_payload["enabled"] = False
    backend_payload["inter_app_interfaces_json"] = pending_inter_app_interfaces_json(
        str(backend_payload.get("inter_app_interfaces_json") or "[]")
    )
    source_inputs = [
        item for item in clone_metadata.get("inputs", []) if isinstance(item, dict)
    ]
    healthcheck_contract = _clone_healthcheck_contract(
        backend_payload,
        source_inputs=source_inputs,
    )
    source_notes = str(backend_payload.get("notes") or "").strip()
    clone_note = f"Cloned from {source_backend_name}."
    backend_payload["notes"] = (
        f"{source_notes}\n\n{clone_note}" if source_notes else clone_note
    )
    clone_metadata["inputs"] = []
    clone_metadata["mount_entries"] = rewritten_mount_entries
    clone_metadata["resolved_host_paths"] = list(rewritten_sources.values())
    clone_metadata["container_snapshot"] = {
        "included": False,
        "archive_name": CONTAINER_SNAPSHOT_NAME,
    }
    clone_metadata["clone_contract"] = _clone_contract_payload(
        metadata=clone_metadata,
        source_backend_name=source_backend_name,
        target_backend_name=target_backend_name,
        target_port=target_port,
        path_rewrites=path_rewrites,
        healthcheck=healthcheck_contract,
    )

    kind = str(backend_payload.get("kind") or "").strip().lower()
    if kind == "static":
        static_root = str(backend_payload.get("static_root") or "").strip()
        if static_root.startswith("/"):
            backend_payload["static_root"] = rewritten_sources.get(
                static_root, static_root
            )
    elif kind == "app":
        cloned_volumes: list[str] = []
        for volume in parse_volumes_json(
            str(backend_payload.get("volumes_json") or "[]")
        ):
            binding = parse_volume_binding(volume)
            rewritten_source = (
                rewritten_sources.get(binding.source, binding.source)
                if binding.source.startswith("/")
                else binding.source
            )
            options_suffix = f":{binding.options}" if binding.options else ""
            cloned_volumes.append(
                f"{rewritten_source}:{binding.target}{options_suffix}"
            )
        backend_payload["volumes_json"] = json.dumps(cloned_volumes)

    return clone_metadata


def _clone_post_check(
    name: str,
    passed: bool,
    detail: str,
) -> dict[str, str]:
    return {
        "name": name,
        "status": "passed" if passed else "failed",
        "detail": detail,
    }


def _run_clone_post_checks(
    backend: Backend,
    metadata: dict[str, Any],
    *,
    restored_paths: list[str],
    settings: Settings,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    clone_contract = metadata.get("clone_contract")
    backend_payload = metadata.get("backend")
    mount_entries = metadata.get("mount_entries")

    checks.append(
        _clone_post_check(
            "backend_metadata_rewritten",
            isinstance(clone_contract, dict)
            and isinstance(backend_payload, dict)
            and backend_payload.get("name") == backend.name
            and backend_payload.get("enabled") is False
            and backend.enabled is False,
            "clone backend metadata matches the new output identity",
        )
    )
    checks.append(
        _clone_post_check(
            "route_inputs_stripped",
            metadata.get("inputs") == [] and list(backend.inputs) == [],
            "clones start without copied public or tailnet routes",
        )
    )

    restored_path_set = set(restored_paths)
    required_paths = (
        [
            str(entry.get("host_path"))
            for entry in mount_entries
            if isinstance(entry, dict)
            and entry.get("exists")
            and isinstance(entry.get("host_path"), str)
        ]
        if isinstance(mount_entries, list)
        else []
    )
    missing_paths = [
        path
        for path in required_paths
        if path not in restored_path_set
        or not (Path(path).exists() or Path(path).is_symlink())
    ]
    checks.append(
        _clone_post_check(
            "restored_paths_exist",
            not missing_paths,
            "all cloned guest and volume paths exist"
            if not missing_paths
            else ", ".join(missing_paths),
        )
    )

    try:
        normalized_header = ensure_healthcheck_host_header(
            backend.healthcheck_host_header,
            field_name=f"healthcheck_host_header for {backend.name}",
        )
    except Exception as exc:
        normalized_header = None
        healthcheck_detail = str(exc)
    else:
        healthcheck_detail = normalized_header or "none configured"
    checks.append(
        _clone_post_check(
            "healthcheck_host_header_valid",
            normalized_header == backend.healthcheck_host_header,
            healthcheck_detail,
        )
    )

    if backend.kind == "app":
        spec_path = settings.app_control_dir / backend.name / "spec.json"
        profile_path = app_sandbox_dir(settings, backend.name) / "profile.json"
        checks.append(
            _clone_post_check(
                "app_control_assets_written",
                spec_path.exists() and profile_path.exists(),
                f"{spec_path}; {profile_path}",
            )
        )

    failed = [check["name"] for check in checks if check["status"] != "passed"]
    if failed:
        raise RuntimeError("clone post-check failed: " + ", ".join(failed))
    return checks


async def _validate_restored_backend_state(session: AsyncSession) -> None:
    backends = list(
        (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .order_by(Backend.id.asc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    inputs = list(
        (
            await session.execute(
                select(Input)
                .options(selectinload(Input.backends))
                .order_by(Input.id.asc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    validate_backend_collection(backends)
    validate_input_bindings(inputs)


def _extract_bundle_entries(
    bundle_path: Path, destination_dir: Path, metadata: dict[str, Any]
) -> None:
    allowed_prefixes, guest_symlink_roots = _bundle_restore_member_policy(metadata)
    if not allowed_prefixes:
        return

    include = _bundle_restore_member_include(allowed_prefixes)
    with tarfile.open(bundle_path, "r:*") as archive:
        _ensure_free_space(
            destination_dir,
            sum(
                member.size
                for member in archive.getmembers()
                if member.isfile() and include(member)
            ),
        )
        safe_extract_tar(
            archive,
            destination_dir,
            include=include,
            allow_relative_symlinks=True,
            guest_symlink_roots=guest_symlink_roots,
            guest_metadata_roots={
                str(PurePosixPath(entry["archive_prefix"]) / "rootfs")
                for entry in metadata.get("mount_entries", [])
                if isinstance(entry, dict)
                and entry.get("archive_prefix") in guest_symlink_roots
                and entry.get("target_path") is None
            },
            guest_owner_roots=_bundle_guest_owner_roots(metadata),
        )


def _ensure_free_space(path: Path, required_bytes: int) -> None:
    existing = next(
        candidate for candidate in (path, *path.parents) if candidate.exists()
    )
    available = shutil.disk_usage(existing).free
    if required_bytes > available:
        raise RuntimeError(
            f"not enough free space to restore: {required_bytes} bytes needed "
            f"under {path}, {available} available"
        )


def _remove_existing_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def _restore_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _rollback_restore_entries(entries: list[dict[str, Any]]) -> None:
    for entry in reversed(entries):
        target = Path(entry["target"])
        stage = Path(entry["stage"])
        rollback = Path(entry["rollback"])
        if _restore_entry_exists(rollback):
            if _restore_entry_exists(target):
                _remove_existing_path(target)
            shutil.move(str(rollback), str(target))
        elif not entry["had_existing"] and _restore_entry_exists(target):
            _remove_existing_path(target)
        if _restore_entry_exists(stage):
            _remove_existing_path(stage)


def _discard_restore_rollbacks(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        for key in ("rollback", "stage"):
            path = Path(entry[key])
            if not _restore_entry_exists(path):
                continue
            try:
                _remove_existing_path(path)
            except Exception as exc:
                logger.warning(
                    "restore.rollback_cleanup_failed", path=str(path), error=str(exc)
                )


class _RestoreCommitUnknown(RuntimeError):
    def __init__(self, journal_id: str) -> None:
        super().__init__(
            "restore commit outcome could not be determined; host left as is"
        )
        self.journal_id = journal_id


class _RestoreBundleTransaction:
    def __init__(
        self,
        metadata: dict[str, Any],
        bundle_path: Path | None,
        *,
        token: str | None = None,
    ) -> None:
        self.metadata = metadata
        self.bundle_path = bundle_path
        self.token = token or uuid.uuid4().hex[:12]
        self._temp_dir_context: tempfile.TemporaryDirectory[str] | None = None
        self._temp_dir: Path | None = None
        self.entries = self._plan_entries()
        self.restored_paths: list[str] = []

    def _plan_entries(self) -> list[dict[str, Any]]:
        if self.bundle_path is None or not self.bundle_path.exists():
            return []
        mount_entries = self.metadata.get("mount_entries")
        if not isinstance(mount_entries, list):
            return []
        planned: list[dict[str, Any]] = []
        for entry in mount_entries:
            if not isinstance(entry, dict) or not entry.get("exists"):
                continue
            host_path = entry.get("host_path")
            archive_prefix = entry.get("archive_prefix")
            if not isinstance(host_path, str) or not isinstance(archive_prefix, str):
                continue
            target = Path(host_path)
            index = len(planned)
            planned.append(
                {
                    "target": str(target),
                    "stage": str(
                        target.parent
                        / f".cnc-restore-stage-{target.name}-{self.token}-{index}"
                    ),
                    "rollback": str(
                        target.parent
                        / f".cnc-restore-rollback-{target.name}-{self.token}-{index}"
                    ),
                    "archive_prefix": archive_prefix,
                    "had_existing": _restore_entry_exists(target),
                    "private_stage": entry.get("link_policy") == "guest"
                    and entry.get("target_path") is None,
                }
            )
        return planned

    def apply(self) -> list[str]:
        if not self.entries:
            return []
        assert self.bundle_path is not None
        self._temp_dir_context = tempfile.TemporaryDirectory(
            prefix="cnc-backup-restore-"
        )
        self._temp_dir = Path(self._temp_dir_context.name)
        _extract_bundle_entries(self.bundle_path, self._temp_dir, self.metadata)
        guest_owner_roots = _bundle_guest_owner_roots(self.metadata)
        try:
            staged: list[dict[str, Any]] = []
            for entry in self.entries:
                extracted_path = self._temp_dir / entry["archive_prefix"]
                if not extracted_path.exists():
                    continue
                target = Path(entry["target"])
                stage = Path(entry["stage"])
                rollback = Path(entry["rollback"])
                if entry["archive_prefix"] in guest_owner_roots:
                    validate_guest_volume_paths([f"{target}:/:rw"], allow_missing=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                for leftover in (stage, rollback):
                    if _restore_entry_exists(leftover):
                        _remove_existing_path(leftover)
                shutil.move(str(extracted_path), str(stage))
                if entry["private_stage"]:
                    stage.chmod(0o700)
                staged.append(entry)
            for entry in staged:
                target = Path(entry["target"])
                if entry["had_existing"]:
                    shutil.move(str(target), entry["rollback"])
                shutil.move(entry["stage"], str(target))
                self.restored_paths.append(str(target))
            return list(self.restored_paths)
        except Exception:
            self.rollback()
            raise

    def commit(self) -> None:
        _discard_restore_rollbacks(self.entries)
        try:
            self._cleanup()
        except Exception as exc:
            logger.warning("restore.temp_cleanup_failed", error=str(exc))

    def rollback(self) -> None:
        _rollback_restore_entries(self.entries)
        self._cleanup()

    def _cleanup(self) -> None:
        self.restored_paths = []
        if self._temp_dir_context is not None:
            self._temp_dir_context.cleanup()
            self._temp_dir_context = None
            self._temp_dir = None


def _backend_container_exists(backend_name: str, settings: Settings) -> bool:
    container = container_name(backend_name)
    presence = container_presence(container, settings.command_timeout_status_sec)
    return known(presence, f"container {container}") is not None


def _backend_quadlet_service_exists(backend_name: str, settings: Settings) -> bool:
    return quadlet_container_path(settings, backend_name).exists()


def _run_runtime_lifecycle_command(command: list[str], settings: Settings) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout or "runtime lifecycle failed")


def _stop_backend_runtime_if_present(backend_name: str, settings: Settings) -> None:
    if _backend_quadlet_service_exists(backend_name, settings):
        _run_runtime_lifecycle_command(
            ["systemctl", "stop", quadlet_container_service_name(backend_name)],
            settings,
        )
        return
    if not _backend_container_exists(backend_name, settings):
        return
    _run_runtime_lifecycle_command(
        ["podman", "stop", container_name(backend_name)],
        settings,
    )


def _start_backend_runtime_if_present(backend_name: str, settings: Settings) -> None:
    if _backend_quadlet_service_exists(backend_name, settings):
        _run_runtime_lifecycle_command(
            ["systemctl", "start", quadlet_container_service_name(backend_name)],
            settings,
        )
        return
    if not _backend_container_exists(backend_name, settings):
        return
    _run_runtime_lifecycle_command(
        ["podman", "start", container_name(backend_name)],
        settings,
    )


async def _load_backend_for_recovery(
    settings: Settings, backend_id: int
) -> Backend | None:
    engine = create_configured_async_engine(settings.database_url)
    try:
        maker = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with maker() as session:
            return (
                await session.execute(
                    select(Backend)
                    .options(selectinload(Backend.inputs))
                    .where(Backend.id == backend_id)
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()


@journal_action("restore.rollback_paths")
def _undo_restored_paths(_settings: Settings, args: dict[str, Any]) -> None:
    _rollback_restore_entries(args["entries"])


@journal_action("restore.discard_rollbacks")
def _discard_restored_path_rollbacks(_settings: Settings, args: dict[str, Any]) -> None:
    _discard_restore_rollbacks(args["entries"])


@journal_action("runtime.stop_if_present")
def _undo_runtime_start(settings: Settings, args: dict[str, Any]) -> None:
    _stop_backend_runtime_if_present(args["backend"], settings)


@journal_action("runtime.start_if_enabled")
async def _undo_runtime_stop(settings: Settings, args: dict[str, Any]) -> None:
    backend = await _load_backend_for_recovery(settings, int(args["backend_id"]))
    if backend is None or backend.kind != "app" or not backend.enabled:
        return
    await _runtime_workers.run(_start_backend_runtime_if_present, backend.name, settings)


@journal_action("control_assets.remove")
def _undo_new_control_assets(settings: Settings, args: dict[str, Any]) -> None:
    _remove_existing_path(settings.app_control_dir / ensure_backend_name(args["backend"]))


@journal_action("control_assets.rewrite_from_db")
async def _undo_control_assets(settings: Settings, args: dict[str, Any]) -> None:
    backend = await _load_backend_for_recovery(settings, int(args["backend_id"]))
    if backend is not None and backend.kind == "app":
        await asyncio.to_thread(write_app_control_assets, backend, settings)


async def _converge_restored_backend_host_state(
    session: AsyncSession,
    settings: Settings,
    operation: OperationHandle,
    *,
    recovery: list[Any],
    progress_callback: BackupProgressCallback | None = None,
) -> dict[str, Any]:
    _report_backup_progress(progress_callback, 0, "Reconciling restored host state.")
    response = await run_apply(
        session,
        settings,
        commit_on_success=False,
        operation_kind="restore_backend",
        operation_handle=operation,
        scope=full_reconcile("restore_backend").scope(),
        emit_success_event=False,
        recovery=recovery,
    )
    if response.status != "success":
        details = response.details if isinstance(response.details, dict) else {}
        phase = str(details.get("phase") or details.get("failed_phase") or "apply")
        error = str(
            details.get("error")
            or details.get("stderr")
            or response.message
            or "host convergence failed"
        )
        raise RuntimeError(f"restore convergence failed during {phase}: {error}")
    details = response.details if isinstance(response.details, dict) else {}
    return {
        "apply_run_id": response.run_id,
        "apply_message": response.message,
        "app_healthcheck_status": details.get("app_healthcheck_status"),
        "desired_state_hash": details.get("desired_state_hash"),
        "config_revision": details.get("config_revision"),
    }


async def create_backend_backup(
    session: AsyncSession,
    backend: Backend,
    settings: Settings,
    progress_callback: BackupProgressCallback | None = None,
    operation_id: int | None = None,
    operation: OperationHandle | None = None,
) -> BackendBackup:
    logger.info("backup.create.requested", backend=backend.name, kind=backend.kind)
    loaded_backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend.id)
        )
    ).scalar_one()
    backend_id = loaded_backend.id
    backend_name = loaded_backend.name
    owner_operation_id = operation_id
    if owner_operation_id is None and operation is not None:
        owner_operation_id = operation.id
    record = BackendBackup(
        backend_id=backend_id,
        operation_id=owner_operation_id,
        status="running",
        scope="metadata_only",
        notes="backup queued",
    )
    lifecycle = HostMutationLifecycle(_runtime_workers)

    async def save_backup_plan() -> Path:
        session.add(record)
        await session.flush()
        path = (
            settings.backend_backup_dir
            / backend_name
            / f"{_timestamp_slug()}-{record.id}{_bundle_extension(settings)}"
        )
        record.bundle_path = str(path)
        await session.commit()
        await session.refresh(record)
        return path

    bundle_path = await lifecycle.finish(save_backup_plan())
    backup_id = record.id
    success_details: dict[str, Any] = {"backup_id": backup_id, "backend": backend_name}

    async def record_failure(error: str) -> bool:
        await session.rollback()
        await session.refresh(record)
        if record.status == "success":
            return False
        await lifecycle.run(_unlink_backup_bundle, settings, bundle_path)
        record.status = "error"
        record.error = error
        record.notes = "backup failed"
        await session.commit()
        return True

    host_operation = None
    if operation is not None:
        host_operation = operation.with_deferred_db_updates()
    elif operation_id is not None:
        host_operation = OperationHandle(
            id=operation_id, kind="backup_backend", settings=settings
        )
    recorded_failure = False
    try:
        async with AsyncExitStack() as stack:
            mutation_operation = host_operation
            try:
                lifecycle.raise_if_cancelled()
                mutation_operation = await stack.enter_async_context(
                    host_mutation_operation(
                        settings,
                        kind="backup_backend",
                        backend_id=backend_id,
                        phase="backup",
                        details=success_details,
                        operation=host_operation,
                    )
                )
                if record.operation_id is None and mutation_operation.id is not None:
                    record.operation_id = mutation_operation.id
                    await session.commit()
                    await session.refresh(record)
                artifact_payload = await lifecycle.run(
                    _write_backup_bundle,
                    backend=loaded_backend,
                    bundle_path=bundle_path,
                    settings=settings,
                    progress_callback=progress_callback,
                )

                def verify_created_bundle() -> None:
                    _report_backup_progress(progress_callback, 90, "Verifying backup.")
                    _verify_bundle_contents(
                        bundle_path,
                        expected_sha256=str(artifact_payload["bundle_sha256"]),
                    )
                    _report_backup_progress(progress_callback, 96, "Backup verified.")

                await lifecycle.run(verify_created_bundle)
                record.status = "success"
                record.scope = str(artifact_payload["scope"])
                record.bundle_path = str(artifact_payload["bundle_path"])
                record.bundle_sha256 = str(artifact_payload["bundle_sha256"])
                record.size_bytes = int(artifact_payload["size_bytes"])
                record.notes = f"{artifact_payload['notes']}; verified"
                record.error = None
                success_details.update(artifact_payload)
                await lifecycle.commit(session.commit())
                await lifecycle.finish(session.refresh(record))
                await lifecycle.finish(
                    mutation_operation.complete(
                        "success", phase="completed", details=success_details
                    )
                )
                lifecycle.raise_if_cancelled()
            except (Exception, asyncio.CancelledError) as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                if not lifecycle.committed:
                    error = "backup cancelled" if cancelled else str(exc)
                    recorded_failure = await lifecycle.finish(record_failure(error))
                cancelled = cancelled or lifecycle.cancel_requested
                if cancelled and mutation_operation is not None:
                    mutation_operation.defer_db_updates = False
                    await lifecycle.finish(
                        mutation_operation.complete(
                            "cancelled" if recorded_failure else "success",
                            phase="backup" if recorded_failure else "completed",
                            error="backup cancelled" if recorded_failure else None,
                            details=None if recorded_failure else success_details,
                        )
                    )
                if cancelled:
                    raise asyncio.CancelledError from exc
                if recorded_failure:
                    raise
                if mutation_operation is not None:
                    logger.warning(
                        "backup.create.post_commit_error",
                        backup_id=backup_id,
                        error=str(exc),
                    )
                    await mutation_operation.complete(
                        "success", phase="completed", details=success_details
                    )
    except Exception:
        if not recorded_failure:
            raise
    if record.status == "success":
        mirror_result = await mirror_backend_backup_to_cluster(
            session,
            loaded_backend,
            record,
            settings,
        )
        if not mirror_result.get("skipped"):
            mirrored_count = len(mirror_result.get("mirrored") or [])
            failure_count = len(mirror_result.get("failures") or [])
            mirror_note = f"mirrored to {mirrored_count} node(s)"
            if failure_count:
                mirror_note = f"{mirror_note}; mirror failed on {failure_count} node(s)"
            record.notes = (
                f"{record.notes}; {mirror_note}" if record.notes else mirror_note
            )
            await session.commit()
        logger.info(
            "backup.create.succeeded",
            backend=backend_name,
            backup_id=record.id,
            scope=record.scope,
            bundle_path=record.bundle_path or "",
            cluster_mirror=mirror_result,
        )
        await emit_control_event(
            settings,
            kind="backup_created",
            source="backup",
            summary="Backup created",
            severity="success",
            scope="backend",
            backend_name=backend_name,
            subevents=[
                {"label": "backup", "value": f"#{record.id}"},
                {"label": "scope", "value": record.scope},
                {"label": "size_bytes", "value": str(record.size_bytes or 0)},
            ],
            details={
                "backend": backend_name,
                "backup_id": record.id,
                "scope": record.scope,
                "bundle_path": record.bundle_path,
                "cluster_mirror": mirror_result,
            },
        )
        await _prune_backend_backups(session, backend_id, settings)
    await session.refresh(record)
    if record.status == "error":
        logger.warning(
            "backup.create.failed",
            backend=backend_name,
            backup_id=record.id,
            error=record.error or "unknown error",
        )
        await emit_control_event(
            settings,
            kind="backup_failed",
            source="backup",
            summary="Backup failed",
            severity="error",
            scope="backend",
            backend_name=backend_name,
            subevents=[
                {"label": "backup", "value": f"#{record.id}"},
                {"label": "scope", "value": record.scope},
                {"label": "error", "value": record.error or "unknown error"},
            ],
            details={
                "backend": backend_name,
                "backup_id": record.id,
                "error": record.error or "unknown error",
            },
        )
        await send_pushover_notification_async(
            settings,
            title=f"CNC backup failed: {backend_name}",
            message=build_operator_message(
                "Backend backup failed before a restorable bundle was produced.",
                status="failure",
                facts=[
                    ("backend", backend_name),
                    ("backup_id", record.id),
                    ("scope", record.scope),
                    ("error", record.error or "unknown error"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            priority=0,
            event="backup_failed",
            source="backup",
            backend=backend_name,
            url=admin_dashboard_url(settings, path=f"/outputs/{backend_id}"),
            url_title=f"Open {backend_name}",
        )
    return record


async def fail_interrupted_backend_backups_in_session(
    session: AsyncSession, settings: Settings | None = None
) -> int:
    backups = (
        (
            await session.execute(
                select(BackendBackup)
                .where(BackendBackup.status == "running")
                .order_by(BackendBackup.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not backups:
        return 0

    cleanup_paths: list[Path] = []
    settings = settings or get_settings()
    for backup in backups:
        if backup.bundle_path:
            bundle_path = _contained_backup_bundle_path(
                settings, Path(backup.bundle_path), backup_id=backup.id
            )
            if bundle_path is not None:
                cleanup_paths.append(bundle_path)
        backup.status = "error"
        backup.error = BACKUP_RESTART_ERROR
        backup.notes = (
            f"{backup.notes}; interrupted by CNC restart"
            if backup.notes
            else "interrupted by CNC restart"
        )
    await session.commit()
    for bundle_path in cleanup_paths:
        _unlink_backup_temp_bundles(settings, bundle_path)
        _unlink_backup_bundle(settings, bundle_path)
    logger.warning("backup.interrupted_marked_failed", count=len(backups))
    return len(backups)


async def fail_interrupted_backend_backups(settings: Settings) -> int:
    database_url = getattr(settings, "database_url", None)
    if not database_url:
        return 0

    engine = create_configured_async_engine(str(database_url), future=True, echo=False)
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            return await fail_interrupted_backend_backups_in_session(session, settings)
    except SQLAlchemyError as exc:
        logger.warning("backup.interrupted_mark_failed_skipped", error=str(exc))
        return 0
    finally:
        await engine.dispose()


async def latest_successful_backend_backup(
    session: AsyncSession,
    backend_id: int,
) -> BackendBackup | None:
    return (
        await session.execute(
            select(BackendBackup)
            .where(BackendBackup.backend_id == backend_id)
            .where(BackendBackup.status == "success")
            .order_by(BackendBackup.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_backend_backups(
    session: AsyncSession,
    backend_id: int,
    *,
    limit: int = 10,
) -> list[BackendBackup]:
    effective_limit = max(1, limit)
    return list(
        (
            await session.execute(
                select(BackendBackup)
                .where(BackendBackup.backend_id == backend_id)
                .order_by(BackendBackup.id.desc())
                .limit(effective_limit)
            )
        )
        .scalars()
        .all()
    )


async def get_backend_backup(
    session: AsyncSession,
    backend_id: int,
    backup_id: int,
) -> BackendBackup | None:
    return (
        await session.execute(
            select(BackendBackup)
            .where(BackendBackup.backend_id == backend_id)
            .where(BackendBackup.id == backup_id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def delete_backend_backup(
    session: AsyncSession,
    backend_id: int,
    backup_id: int,
    settings: Settings,
) -> dict[str, Any]:
    backup = await get_backend_backup(session, backend_id, backup_id)
    if backup is None:
        raise LookupError("backup not found for this output")

    backend = await session.get(Backend, backend_id)
    backend_name = backend.name if backend is not None else "unknown"
    bundle_path = (
        _contained_backup_bundle_path(
            settings, Path(backup.bundle_path), backup_id=backup.id
        )
        if backup.bundle_path
        else None
    )
    bundle_name = bundle_path.name if bundle_path is not None else "-"
    deleted_bundle = False
    bundle_existed = bundle_path.exists() if bundle_path is not None else False

    await session.delete(backup)
    await session.commit()

    if bundle_path is not None:
        deleted_bundle = _unlink_backup_bundle(settings, bundle_path)
        if bundle_existed and not deleted_bundle:
            logger.warning(
                "backup.bundle_delete_after_row_commit_failed",
                backup_id=backup_id,
                bundle_path=str(bundle_path),
            )

    await emit_control_event(
        settings,
        kind="backup_deleted",
        source="backup",
        summary="Backup deleted",
        severity="info",
        scope="backend",
        backend_name=backend_name,
        subevents=[
            {"label": "backup", "value": f"#{backup_id}"},
            {"label": "file", "value": bundle_name},
            {"label": "bundle_deleted", "value": "yes" if deleted_bundle else "no"},
        ],
        details={
            "backend": backend_name,
            "backup_id": backup_id,
            "bundle": str(bundle_path) if bundle_path is not None else "",
            "bundle_deleted": deleted_bundle,
        },
    )

    return {
        "backup_id": backup_id,
        "backend": backend_name,
        "bundle": bundle_name,
        "bundle_deleted": deleted_bundle,
    }


async def _prune_backend_backups(
    session: AsyncSession,
    backend_id: int,
    settings: Settings,
) -> list[int]:
    retention = settings.backend_backup_retention_per_backend
    if retention <= 0:
        return []

    successful_backups = list(
        (
            await session.execute(
                select(BackendBackup)
                .where(BackendBackup.backend_id == backend_id)
                .where(BackendBackup.status == "success")
                .order_by(BackendBackup.id.desc())
            )
        )
        .scalars()
        .all()
    )
    expired = successful_backups[retention:]
    if not expired:
        return []

    pruned_ids: list[int] = []
    cleanup_paths: list[tuple[int, Path]] = []
    for backup in expired:
        if backup.bundle_path:
            bundle_path = _contained_backup_bundle_path(
                settings, Path(backup.bundle_path), backup_id=backup.id
            )
            if bundle_path is not None:
                cleanup_paths.append((backup.id, bundle_path))
        await session.delete(backup)
        pruned_ids.append(backup.id)
    await session.commit()
    for backup_id, bundle_path in cleanup_paths:
        bundle_existed = bundle_path.exists()
        deleted_bundle = _unlink_backup_bundle(settings, bundle_path)
        if bundle_existed and not deleted_bundle:
            logger.warning(
                "backup.prune_bundle_delete_after_row_commit_failed",
                backup_id=backup_id,
                bundle_path=str(bundle_path),
            )
    return pruned_ids


def _contained_backup_bundle_path(
    settings: Settings, bundle_path: Path, *, backup_id: int | None = None
) -> Path | None:
    try:
        root = settings.backend_backup_dir.expanduser().resolve(strict=False)
        resolved = bundle_path.expanduser().resolve(strict=False)
        resolved.relative_to(root)
        return resolved
    except (OSError, ValueError) as exc:
        logger.warning(
            "backup.bundle_path_outside_root",
            backup_id=backup_id,
            bundle_path=str(bundle_path),
            backup_root=str(settings.backend_backup_dir),
            error=str(exc),
        )
        return None


def _unlink_backup_bundle(settings: Settings, bundle_path: Path) -> bool:
    contained_path = _contained_backup_bundle_path(settings, bundle_path)
    if contained_path is None:
        return False
    bundle_path = contained_path
    deleted = False
    try:
        if bundle_path.exists():
            bundle_path.unlink()
            deleted = True
        backend_dir = bundle_path.parent
        if backend_dir.exists() and not any(backend_dir.iterdir()):
            backend_dir.rmdir()
    except OSError as exc:
        logger.warning(
            "backup.bundle_cleanup_failed", bundle_path=str(bundle_path), error=str(exc)
        )
    return deleted


def _unlink_backup_temp_bundles(settings: Settings, bundle_path: Path) -> int:
    contained_path = _contained_backup_bundle_path(settings, bundle_path)
    if contained_path is None:
        return 0
    bundle_path = contained_path
    deleted = 0
    backend_dir = bundle_path.parent
    if not backend_dir.exists():
        return 0
    for temp_path in backend_dir.glob(f".{bundle_path.name}.*.tmp"):
        try:
            temp_path.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning(
                "backup.temp_bundle_cleanup_failed",
                bundle_path=str(temp_path),
                error=str(exc),
            )
    try:
        if backend_dir.exists() and not any(backend_dir.iterdir()):
            backend_dir.rmdir()
    except OSError as exc:
        logger.warning(
            "backup.temp_bundle_dir_cleanup_failed",
            bundle_dir=str(backend_dir),
            error=str(exc),
        )
    return deleted


async def restore_backend_backup(
    session: AsyncSession,
    backend: Backend,
    backup: BackendBackup,
    settings: Settings,
    *,
    notify_on_success: bool = True,
    notify_on_failure: bool = True,
    progress_callback: BackupProgressCallback | None = None,
    operation: OperationHandle | None = None,
) -> dict[str, Any]:
    logger.info(
        "restore.requested",
        backend=backend.name,
        backup_id=backup.id,
        kind=backend.kind,
        notify_on_success=notify_on_success,
        notify_on_failure=notify_on_failure,
    )
    if backup.backend_id is not None and backup.backend_id != backend.id:
        raise LookupError("backup not found for this output")
    if backup.status != "success":
        raise LookupError("backup is not restorable")
    if not backup.bundle_path:
        raise RuntimeError("backup bundle path missing")

    bundle_path = Path(backup.bundle_path)
    _report_backup_progress(progress_callback, 0, "Verifying backup bundle.")
    verification = await asyncio.to_thread(
        _verify_bundle_contents,
        bundle_path,
        expected_sha256=str(backup.bundle_sha256 or ""),
    )
    metadata = verification["metadata"]
    backend_payload = metadata.get("backend")
    snapshot_kind = str(backend_payload.get("kind") or "")
    if snapshot_kind and snapshot_kind != backend.kind:
        raise RuntimeError("minimal restore only supports the same backend kind")

    backend_id = backend.id
    backend_name = backend.name
    backend_kind = backend.kind
    backup_id = backup.id
    had_container = backend_kind == "app" and await _runtime_workers.run(
        _backend_container_exists, backend_name, settings
    )
    restored_paths: list[str] = []
    host_operation = (
        operation.with_deferred_db_updates() if operation is not None else None
    )
    operation_context = host_mutation_operation(
        settings,
        kind="restore_backend",
        backend_id=backend_id,
        phase="restore",
        details={"backend": backend_name, "backup_id": backup_id},
        operation=host_operation,
    )
    mutation_operation = await operation_context.__aenter__()
    try:
        journal = HostJournal.begin(
            settings, kind="restore_backend", subject=backend_name
        )
        restore_txn = _RestoreBundleTransaction(
            metadata, bundle_path, token=journal.id[:12]
        )
    except BaseException as exc:
        await operation_context.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    convergence_details: dict[str, Any] = {}
    committed = False
    lifecycle = HostMutationLifecycle(_runtime_workers)
    host_recovery: list[Any] = []

    try:
        _report_backup_progress(progress_callback, 0, "Validating restore payload.")
        _validate_restore_mount_entries(metadata, settings, backend_name=backend.name)
        restored_policy = _backend_from_metadata(metadata).hardening_config_json
        if had_container:
            _report_backup_progress(progress_callback, 0, "Stopping existing runtime.")
            await journal.step(
                "stop_runtime",
                lambda: _runtime_workers.run(
                    _stop_backend_runtime_if_present, backend_name, settings
                ),
                undo="runtime.start_if_enabled",
                undo_args={"backend_id": backend_id},
            )

        _report_backup_progress(progress_callback, 0, "Restoring mounted paths.")
        restored_paths = await journal.step(
            "restore_paths",
            lambda: asyncio.to_thread(restore_txn.apply),
            undo="restore.rollback_paths",
            undo_args={"entries": restore_txn.entries},
        )
        journal.after_commit(
            "restore.discard_rollbacks", {"entries": restore_txn.entries}
        )

        _report_backup_progress(progress_callback, 0, "Restoring backend config.")
        backend.hardening_config_json = restored_policy
        backend.hardening_previous_json = None
        for field in _backend_payload_fields():
            if field in backend_payload:
                setattr(backend, field, backend_payload.get(field))
        validate_backend_shape(backend)

        input_payload = metadata.get("inputs")
        restored_inputs: list[Input] = []
        if isinstance(input_payload, list):
            for item in input_payload:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "domain").strip().lower() or "domain"
                hostname = str(item.get("hostname") or "").strip()
                if not hostname:
                    continue
                existing = (
                    await session.execute(
                        select(Input)
                        .where(Input.hostname == hostname, Input.kind == kind)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = _input_from_metadata(item, kind=kind, hostname=hostname)
                    session.add(existing)
                    await session.flush()
                else:
                    existing.enabled = bool(item.get("enabled", existing.enabled))
                restored_inputs.append(existing)
        backend.inputs = restored_inputs
        await session.flush()
        await _validate_restored_backend_state(session)

        if backend.kind == "app":
            _report_backup_progress(
                progress_callback, 0, "Restoring container snapshot."
            )
            await asyncio.to_thread(
                _restore_container_snapshot_image,
                bundle_path=bundle_path,
                metadata=metadata,
                backend_name=backend.name,
                settings=settings,
            )

        if backend.kind == "app":
            _report_backup_progress(progress_callback, 0, "Writing app control assets.")
            await journal.step(
                "write_control_assets",
                lambda: write_app_control_assets(backend, settings),
                undo="control_assets.rewrite_from_db",
                undo_args={"backend_id": backend_id},
            )

        if had_container and backend_kind == "app" and backend.enabled:
            _report_backup_progress(progress_callback, 0, "Restarting runtime.")
            await journal.step(
                "start_runtime",
                lambda: _runtime_workers.run(
                    _start_backend_runtime_if_present, backend_name, settings
                ),
                undo="runtime.stop_if_present",
                undo_args={"backend": backend_name},
            )

        convergence_details = await _converge_restored_backend_host_state(
            session,
            settings,
            mutation_operation,
            progress_callback=progress_callback,
            recovery=host_recovery,
        )

        _report_backup_progress(progress_callback, 0, "Committing restore.")
        commit_outcome = await lifecycle.finish(journal.commit(session))
        if commit_outcome == "unknown":
            raise _RestoreCommitUnknown(journal.id)
        if commit_outcome == "rolled_back":
            raise RuntimeError(f"restore commit was not kept ({journal.error})")
        committed = True
        await lifecycle.finish(_runtime_workers.run(restore_txn.commit))
        lifecycle.mark_committed()
        lifecycle.raise_if_cancelled()
        await session.refresh(backend)
        _report_backup_progress(progress_callback, 0, "Refreshing output page.")

        logger.info(
            "restore.succeeded",
            backend=backend.name,
            backup_id=backup_id,
            restored_paths=len(restored_paths),
            notified=notify_on_success,
        )
        await emit_control_event(
            settings,
            kind="restore_completed",
            source="backup",
            summary="Backup restore completed",
            severity="success",
            scope="backend",
            backend_name=backend.name,
            subevents=[
                {"label": "backup", "value": f"#{backup_id}"},
                {"label": "paths", "value": str(len(restored_paths))},
                {"label": "notified", "value": "yes" if notify_on_success else "no"},
            ],
            details={
                "backend": backend.name,
                "backup_id": backup_id,
                "restored_paths": len(restored_paths),
                "convergence": convergence_details,
            },
            notify=False,
        )
        if notify_on_success:
            await send_pushover_notification_async(
                settings,
                title=f"CNC restore completed: {backend.name}",
                message=build_operator_message(
                    "Backend restore completed.",
                    status="success",
                    facts=[
                        ("backend", backend.name),
                        ("backup_id", backup_id),
                        ("restored_paths", len(restored_paths)),
                    ],
                    action=f"cnc-admin app doctor {backend.name}",
                ),
                priority=0,
                event="restore_completed",
                source="backup",
                backend=backend.name,
                url=admin_dashboard_url(settings, path=f"/outputs/{backend.id}"),
                url_title=f"Open {backend.name}",
            )

        await mutation_operation.complete(
            "success",
            phase="completed",
            details={
                "backend": backend.name,
                "backup_id": backup_id,
                "restored_paths": len(restored_paths),
                "convergence": convergence_details,
            },
        )
        return {
            "backup": backup,
            "restored_paths": restored_paths,
            "convergence": convergence_details,
        }
    except BaseException as exc:
        if committed:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning(
                "restore.post_commit_reporting_failed",
                backend=backend_name,
                backup_id=backup_id,
                error=str(exc),
            )
            return {
                "backup": backup,
                "restored_paths": restored_paths,
                "convergence": convergence_details,
            }
        if isinstance(exc, _RestoreCommitUnknown):
            unwind_details: dict[str, Any] = {
                "commit_outcome": "unknown",
                "failure_mode": "partial",
                "manual_review_required": True,
                "journal_id": journal.id,
                "operator_action": (
                    "CNC retries this restore's recovery on its next start; "
                    "compare the output against the backup before rerunning"
                ),
            }
        else:
            async def recover() -> dict[str, Any]:
                await session.rollback()
                host_unwind = await unwind_recovery(host_recovery)
                details = (await journal.unwind()).details()
                await _runtime_workers.run(restore_txn.rollback)
                if host_unwind.get("manual_review_required"):
                    details.update(host_unwind)
                return details
            unwind_details = await lifecycle.finish(recover())
        await mutation_operation.complete(
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            phase="restore",
            error=str(exc) or "Restore cancelled.",
            details={
                "backend": backend_name,
                "backup_id": backup_id,
                **unwind_details,
            },
        )
        logger.warning(
            "restore.failed",
            backend=backend_name,
            backup_id=backup_id,
            error=str(exc),
            **unwind_details,
        )
        await emit_control_event(
            settings,
            kind="restore_failed",
            source="backup",
            summary="Backup restore failed",
            severity="error",
            scope="backend",
            backend_name=backend_name,
            subevents=[
                {"label": "backup", "value": f"#{backup_id}"},
                {"label": "error", "value": str(exc)},
            ],
            details={
                "backend": backend_name,
                "backup_id": backup_id,
                "error": str(exc),
                **unwind_details,
            },
            notify=False,
        )
        if notify_on_failure:
            await send_pushover_notification_async(
                settings,
                title=f"CNC restore failed: {backend_name}",
                message=build_operator_message(
                    "Backend restore failed before the output was returned to a verified state.",
                    status="failure",
                    facts=[
                        ("backend", backend_name),
                        ("backup_id", backup_id),
                        ("error", str(exc)),
                    ],
                    action=f"cnc-admin app doctor {backend_name}",
                ),
                priority=0,
                event="restore_failed",
                source="backup",
                backend=backend_name,
                url=admin_dashboard_url(settings, tab="outputs"),
                url_title="Open outputs",
            )
        raise
    finally:
        await operation_context.__aexit__(None, None, None)


async def restore_latest_backend_backup(
    session: AsyncSession,
    backend: Backend,
    settings: Settings,
    *,
    progress_callback: BackupProgressCallback | None = None,
    operation: OperationHandle | None = None,
) -> dict[str, Any]:
    backup = await latest_successful_backend_backup(session, backend.id)
    if backup is None:
        raise LookupError("no successful backup available")
    return await restore_backend_backup(
        session,
        backend,
        backup,
        settings,
        progress_callback=progress_callback,
        operation=operation,
    )


def _sanitize_bundle_name(filename: str) -> str:
    candidate = Path(filename or "imported-backup").name
    safe = "".join(
        ch if ch.isalnum() or ch in {".", "-", "_"} else "-" for ch in candidate
    ).strip("-.")
    return safe or "imported-backup"


async def import_backend_backup_bundle(
    session: AsyncSession,
    backend: Backend,
    source_path: Path,
    *,
    original_name: str,
    settings: Settings,
    operation: OperationHandle | None = None,
    operation_id: int | None = None,
) -> BackendBackup:
    logger.info(
        "backup.import.requested",
        backend=backend.name,
        bundle_path=str(source_path),
        original_name=original_name,
    )
    loaded_backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend.id)
        )
    ).scalar_one()
    backend_id = loaded_backend.id
    backend_name = loaded_backend.name
    backend_kind = loaded_backend.kind
    owner_operation_id = operation_id
    if owner_operation_id is None and operation is not None:
        owner_operation_id = operation.id
    record = BackendBackup(
        backend_id=backend_id,
        operation_id=owner_operation_id,
        status="running",
        scope="metadata_only",
        notes="bundle import queued",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    destination_name = (
        f"{_timestamp_slug()}-{record.id}-{_sanitize_bundle_name(original_name)}"
    )
    if not (destination_name.endswith(".tar.gz") or destination_name.endswith(".tar")):
        destination_name = f"{destination_name}{_bundle_extension(settings)}"
    destination_path = settings.backend_backup_dir / backend_name / destination_name
    record.bundle_path = str(destination_path)
    await session.commit()
    await session.refresh(record)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_destination_path = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.tmp"
    )
    if temp_destination_path.exists():
        temp_destination_path.unlink()

    host_operation = None
    if operation is not None:
        host_operation = operation.with_deferred_db_updates()
    elif operation_id is not None:
        host_operation = OperationHandle(
            id=operation_id, kind="import_backend_backup", settings=settings
        )
    async with host_mutation_operation(
        settings,
        kind=operation.kind if operation is not None else "import_backend_backup",
        backend_id=backend_id,
        phase="import",
        details={
            "backend": backend_name,
            "backup_id": record.id,
            "source": original_name,
        },
        operation=host_operation,
    ) as mutation_operation:
        if record.operation_id is None and mutation_operation.id is not None:
            record.operation_id = mutation_operation.id
            await session.commit()
            await session.refresh(record)
        try:
            await asyncio.to_thread(shutil.copy2, source_path, temp_destination_path)
            bundle_sha256 = await asyncio.to_thread(_sha256_file, temp_destination_path)
            verification = await asyncio.to_thread(
                _verify_bundle_contents,
                temp_destination_path,
                expected_sha256=bundle_sha256,
                max_expanded_bytes=settings.backend_backup_expanded_max_bytes,
            )
            metadata = verification["metadata"]
            snapshot_kind = str(metadata["backend"].get("kind") or "")
            if snapshot_kind and snapshot_kind != backend_kind:
                raise RuntimeError(
                    "imported bundle backend kind does not match this output"
                )
            temp_destination_path.replace(destination_path)
            record.status = "success"
            record.scope = str(verification["scope"])
            record.bundle_path = str(destination_path)
            record.bundle_sha256 = bundle_sha256
            record.size_bytes = destination_path.stat().st_size
            record.notes = f"{_bundle_notes(metadata, imported=True)}; verified"
            record.error = None
        except Exception as exc:
            record.status = "error"
            record.error = str(exc)
            record.notes = "bundle import failed"
            _unlink_backup_bundle(settings, temp_destination_path)
            _unlink_backup_bundle(settings, destination_path)
        try:
            await session.commit()
        except Exception:
            if record.status == "success":
                _unlink_backup_bundle(settings, destination_path)
            raise
        if record.status == "success":
            logger.info(
                "backup.import.succeeded",
                backend=loaded_backend.name,
                backup_id=record.id,
                bundle_path=str(destination_path),
            )
        else:
            logger.warning(
                "backup.import.failed",
                backend=loaded_backend.name,
                backup_id=record.id,
                bundle_path=str(destination_path),
                error=record.error or "unknown error",
            )
            await send_pushover_notification_async(
                settings,
                title=f"CNC backup import failed: {loaded_backend.name}",
                message=build_operator_message(
                    "Backup bundle import failed before it was added to history.",
                    status="failure",
                    facts=[
                        ("backend", loaded_backend.name),
                        ("backup_id", record.id),
                        ("bundle", original_name),
                        ("error", record.error or "unknown error"),
                    ],
                    action=f"cnc-admin app doctor {loaded_backend.name}",
                ),
                priority=0,
                event="backup_import_failed",
                source="backup",
                backend=loaded_backend.name,
                url=admin_dashboard_url(settings, path=f"/outputs/{loaded_backend.id}"),
                url_title=f"Open {loaded_backend.name}",
            )
        if record.status == "success":
            await _prune_backend_backups(session, loaded_backend.id, settings)
        await session.refresh(record)
        await mutation_operation.complete(
            "success" if record.status == "success" else "failed",
            phase="completed" if record.status == "success" else "import",
            error=record.error,
            details={
                "backend": loaded_backend.name,
                "backup_id": record.id,
                "bundle_path": record.bundle_path,
            },
        )
        return record


async def restore_backend_bundle_as_new_backend(
    session: AsyncSession,
    source_path: Path,
    *,
    original_name: str,
    settings: Settings,
    backend_name: str | None = None,
) -> dict[str, Any]:
    verification = await asyncio.to_thread(
        _verify_bundle_contents,
        source_path,
        max_expanded_bytes=settings.backend_backup_expanded_max_bytes,
    )
    metadata = verification["metadata"]
    source_backend_name = _backend_payload_name(metadata)
    target_backend_name = ensure_backend_name(backend_name or source_backend_name)
    if target_backend_name != source_backend_name:
        raise RuntimeError(
            "restore as new cannot change the backend name because backup storage paths are bound to the original name"
        )
    logger.info(
        "restore.bundle.requested",
        backend=target_backend_name,
        bundle_path=str(source_path),
    )

    existing = (
        await session.execute(
            select(Backend).where(Backend.name == target_backend_name).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise LookupError(f"output already exists: {target_backend_name}")

    async with host_mutation_operation(
        settings,
        kind="restore_backend",
        phase="restore_as_new",
        details={"backend": target_backend_name, "bundle": source_path.name},
    ) as operation:
        backend = _backend_from_metadata(metadata, backend_name=target_backend_name)
        session.add(backend)
        await session.flush()
        await _validate_restored_backend_state(session)
        await session.commit()
        await session.refresh(backend)

        imported_backup: BackendBackup | None = None
        try:
            imported_backup = await import_backend_backup_bundle(
                session,
                backend,
                source_path,
                original_name=original_name,
                settings=settings,
                operation=operation,
            )
            if imported_backup.status != "success":
                raise RuntimeError(imported_backup.error or "bundle import failed")
            restore_result = await restore_backend_backup(
                session,
                backend,
                imported_backup,
                settings,
                notify_on_success=False,
                notify_on_failure=False,
                operation=operation,
            )
            await send_pushover_notification_async(
                settings,
                title=f"CNC backend restored: {backend.name}",
                message=build_operator_message(
                    "Backup bundle restored as a new backend.",
                    status="success",
                    facts=[
                        ("backend", backend.name),
                        ("backup_id", imported_backup.id),
                        (
                            "restored_paths",
                            len(restore_result.get("restored_paths") or []),
                        ),
                    ],
                    action=f"cnc-admin app doctor {backend.name}",
                ),
                priority=0,
                event="backend_restored",
                source="backup",
                backend=backend.name,
                url=admin_dashboard_url(settings, path=f"/outputs/{backend.id}"),
                url_title=f"Open {backend.name}",
            )
            logger.info(
                "restore.bundle.succeeded",
                backend=backend.name,
                backup_id=imported_backup.id,
                restored_paths=len(restore_result.get("restored_paths") or []),
            )
            await operation.complete(
                "success",
                phase="completed",
                details={
                    "backend": backend.name,
                    "backup_id": imported_backup.id,
                    "restored_paths": len(restore_result.get("restored_paths") or []),
                    "convergence": restore_result.get("convergence") or {},
                },
            )
            return {
                "backend": backend,
                "backup": imported_backup,
                "restored_paths": restore_result.get("restored_paths") or [],
                "bundle_path": str(source_path),
            }
        except Exception as exc:
            await operation.complete(
                "failed",
                phase="restore_as_new",
                error=str(exc),
                details={"backend": target_backend_name, "bundle": source_path.name},
            )
            logger.warning(
                "restore.bundle.failed", backend=target_backend_name, error=str(exc)
            )
            await session.rollback()
            if imported_backup is not None:
                managed_backup = await get_backend_backup(
                    session, backend.id, imported_backup.id
                )
                if managed_backup is not None:
                    if managed_backup.bundle_path:
                        managed_path = Path(managed_backup.bundle_path)
                        try:
                            if managed_path.exists():
                                managed_path.unlink()
                            if managed_path.parent.exists() and not any(
                                managed_path.parent.iterdir()
                            ):
                                managed_path.parent.rmdir()
                        except OSError as cleanup_exc:
                            logger.warning(
                                "restore.bundle.import_cleanup_failed",
                                backend=target_backend_name,
                                bundle_path=str(managed_path),
                                error=str(cleanup_exc),
                            )
                    await session.delete(managed_backup)
                    await session.commit()
            created_backend = (
                await session.execute(
                    select(Backend).where(Backend.id == backend.id).limit(1)
                )
            ).scalar_one_or_none()
            if created_backend is not None:
                await session.delete(created_backend)
                await session.commit()
            await send_pushover_notification_async(
                settings,
                title=f"CNC restore failed: {target_backend_name}",
                message=build_operator_message(
                    "Backup bundle restore failed before the new backend was ready.",
                    status="failure",
                    facts=[
                        ("backend", target_backend_name),
                        ("bundle", source_path.name),
                        ("error", str(exc)),
                    ],
                    action="cnc-admin logs admin --errors",
                ),
                priority=0,
                event="restore_failed",
                source="backup",
                backend=target_backend_name,
                url=admin_dashboard_url(settings, tab="outputs"),
                url_title="Open outputs",
            )
            raise


async def clone_backend_as_new_backend(
    session: AsyncSession,
    backend: Backend,
    settings: Settings,
    *,
    backend_name: str,
    target_port: int | None = None,
    progress_callback: BackupProgressCallback | None = None,
) -> dict[str, Any]:
    loaded_backend = (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend.id)
        )
    ).scalar_one_or_none()
    if loaded_backend is None:
        raise LookupError("backend not found")

    target_backend_name = ensure_backend_name(backend_name)
    existing = (
        await session.execute(
            select(Backend.id).where(Backend.name == target_backend_name).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise LookupError(f"output already exists: {target_backend_name}")

    async with host_mutation_operation(
        settings,
        kind="clone_backend",
        backend_id=loaded_backend.id,
        phase="clone",
        details={
            "source_backend": loaded_backend.name,
            "clone_backend": target_backend_name,
        },
    ) as operation:
        with tempfile.TemporaryDirectory(prefix="cnc-backend-clone-") as temp_dir_name:
            bundle_path = (
                Path(temp_dir_name)
                / f"{target_backend_name}{_bundle_extension(settings)}"
            )
            _report_backup_progress(progress_callback, 0, "Capturing source state.")
            artifact_payload = await asyncio.to_thread(
                _write_backup_bundle,
                backend=loaded_backend,
                bundle_path=bundle_path,
                settings=settings,
                include_container_snapshot=False,
            )
            _report_backup_progress(progress_callback, 0, "Verifying clone bundle.")
            verification = await asyncio.to_thread(
                _verify_bundle_contents,
                bundle_path,
                expected_sha256=str(artifact_payload["bundle_sha256"]),
            )
            clone_metadata = await asyncio.to_thread(
                _clone_bundle_metadata_for_new_backend,
                verification["metadata"],
                source_backend_name=loaded_backend.name,
                target_backend_name=target_backend_name,
                target_port=target_port,
            )
            clone_backend = _backend_from_metadata(
                clone_metadata, backend_name=target_backend_name
            )
            _report_backup_progress(progress_callback, 0, "Writing cloned output.")
            session.add(clone_backend)
            await session.flush()
            await _validate_restored_backend_state(session)
            journal = HostJournal.begin(
                settings, kind="clone_backend", subject=target_backend_name
            )
            clone_txn = _RestoreBundleTransaction(
                clone_metadata, bundle_path, token=journal.id[:12]
            )
            restored_paths: list[str] = []
            post_clone_checks: list[dict[str, str]] = []
            committed = False
            lifecycle = HostMutationLifecycle(_runtime_workers)

            try:
                _report_backup_progress(
                    progress_callback, 0, "Restoring guest and data."
                )
                restored_paths = await journal.step(
                    "clone_paths",
                    lambda: asyncio.to_thread(clone_txn.apply),
                    undo="restore.rollback_paths",
                    undo_args={"entries": clone_txn.entries},
                )
                journal.after_commit(
                    "restore.discard_rollbacks", {"entries": clone_txn.entries}
                )
                if clone_backend.kind == "app":
                    _report_backup_progress(
                        progress_callback, 0, "Writing app control assets."
                    )
                    await journal.step(
                        "write_control_assets",
                        lambda: write_app_control_assets(clone_backend, settings),
                        undo="control_assets.remove",
                        undo_args={"backend": target_backend_name},
                    )
                _report_backup_progress(progress_callback, 0, "Running clone checks.")
                post_clone_checks = await asyncio.to_thread(
                    _run_clone_post_checks,
                    clone_backend,
                    clone_metadata,
                    restored_paths=restored_paths,
                    settings=settings,
                )
                _report_backup_progress(progress_callback, 0, "Finalizing clone.")
                commit_outcome = await lifecycle.finish(journal.commit(session))
                if commit_outcome == "unknown":
                    raise _RestoreCommitUnknown(journal.id)
                if commit_outcome == "rolled_back":
                    raise RuntimeError(f"clone commit was not kept ({journal.error})")
                committed = True
                await lifecycle.finish(_runtime_workers.run(clone_txn.commit))
                lifecycle.mark_committed()
                lifecycle.raise_if_cancelled()
                await session.refresh(clone_backend)
                logger.info(
                    "backend.clone.succeeded",
                    source_backend=loaded_backend.name,
                    clone_backend=clone_backend.name,
                    restored_paths=len(restored_paths),
                    post_clone_checks=len(post_clone_checks),
                )
                await operation.complete(
                    "success",
                    phase="completed",
                    details={
                        "source_backend": loaded_backend.name,
                        "clone_backend": clone_backend.name,
                        "restored_paths": len(restored_paths),
                    },
                )
                return {
                    "backend": clone_backend,
                    "restored_paths": restored_paths,
                    "clone_contract": clone_metadata.get("clone_contract"),
                    "post_clone_checks": post_clone_checks,
                }
            except BaseException as exc:
                if committed:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.warning(
                        "backend.clone.post_commit_reporting_failed",
                        clone_backend=target_backend_name,
                        error=str(exc),
                    )
                    return {
                        "backend": clone_backend,
                        "restored_paths": restored_paths,
                        "clone_contract": clone_metadata.get("clone_contract"),
                        "post_clone_checks": post_clone_checks,
                    }
                if isinstance(exc, _RestoreCommitUnknown):
                    unwind_details: dict[str, Any] = {
                        "commit_outcome": "unknown",
                        "failure_mode": "partial",
                        "manual_review_required": True,
                        "journal_id": journal.id,
                    }
                else:
                    async def recover() -> dict[str, Any]:
                        await session.rollback()
                        details = (await journal.unwind()).details()
                        await _runtime_workers.run(clone_txn.rollback)
                        return details
                    unwind_details = await lifecycle.finish(recover())
                await operation.complete(
                    "failed",
                    phase="clone",
                    error=str(exc),
                    details={
                        "source_backend": loaded_backend.name,
                        "clone_backend": target_backend_name,
                        **unwind_details,
                    },
                )
                await send_pushover_notification_async(
                    settings,
                    title=f"CNC clone failed: {target_backend_name}",
                    message=build_operator_message(
                        "Output clone failed before the new backend was ready.",
                        status="failure",
                        facts=[
                            ("source_backend", loaded_backend.name),
                            ("clone_backend", target_backend_name),
                            ("error", str(exc)),
                        ],
                        action=f"cnc-admin app doctor {loaded_backend.name}",
                    ),
                    priority=0,
                    event="clone_failed",
                    source="backup",
                    backend=target_backend_name,
                    url=admin_dashboard_url(settings, tab="outputs"),
                    url_title="Open outputs",
                )
                raise


async def verify_backend_backup(
    session: AsyncSession,
    backend: Backend,
    backup: BackendBackup,
) -> dict[str, Any]:
    if backup.backend_id is not None and backup.backend_id != backend.id:
        raise LookupError("backup not found for this output")
    if backup.status != "success":
        raise LookupError("backup is not verifiable")
    if not backup.bundle_path:
        raise RuntimeError("backup bundle path missing")

    verification = await asyncio.to_thread(
        _verify_bundle_contents,
        Path(backup.bundle_path),
        expected_sha256=str(backup.bundle_sha256 or ""),
    )
    return {
        "backend": backend.name,
        "backup_id": backup.id,
        "bundle_path": verification["bundle_path"],
        "bundle_sha256": verification["bundle_sha256"],
        "bundle_size_bytes": verification["bundle_size_bytes"],
        "bundle_format_version": verification["bundle_format_version"],
        "scope": verification["scope"],
        "mount_entries_archived": verification["mount_entries_archived"],
        "mount_entries_total": verification["mount_entries_total"],
        "container_snapshot_included": verification["container_snapshot_included"],
        "declared_mounts": verification["declared_mounts"],
        "covered_paths": verification["covered_paths"],
        "covered_paths_summary": verification["covered_paths_summary"],
        "bundle_backend_name": verification["bundle_backend_name"],
        "bundle_backend_kind": verification["bundle_backend_kind"],
        "verification_status": "verified",
        "restore_readiness": verification["restore_readiness"],
        "restore_readiness_label": verification["restore_readiness_label"],
        "risk_flags": verification["risk_flags"],
        "risk_summary": verification["risk_summary"],
        "notes": verification["notes"],
        "ok": True,
    }


def _backup_description_cache_key(
    backup: BackendBackup,
    current_backend: Backend | None,
) -> tuple[object, ...] | None:
    if not backup.bundle_path:
        return None
    path = Path(backup.bundle_path)
    try:
        stat_result = path.stat()
    except OSError:
        stat_signature: tuple[object, ...] = ("missing",)
    else:
        stat_signature = (stat_result.st_size, stat_result.st_mtime_ns)
    return (
        backup.id,
        str(path),
        str(backup.bundle_sha256 or ""),
        str(current_backend.kind or "").strip().lower()
        if current_backend is not None
        else "",
        *stat_signature,
    )


def _cached_backup_description(
    cache_key: tuple[object, ...] | None,
) -> dict[str, Any] | None:
    if cache_key is None:
        return None
    cached = _backup_description_cache.get(cache_key)
    if cached is None:
        return None
    expires_at, payload = cached
    if time.monotonic() >= expires_at:
        _backup_description_cache.pop(cache_key, None)
        return None
    return copy.deepcopy(payload)


def _store_backup_description(
    cache_key: tuple[object, ...] | None,
    payload: dict[str, Any],
) -> None:
    if cache_key is None:
        return
    if len(_backup_description_cache) >= BACKUP_DESCRIPTION_CACHE_MAX:
        oldest_key = min(
            _backup_description_cache,
            key=lambda key: _backup_description_cache[key][0],
        )
        _backup_description_cache.pop(oldest_key, None)
    _backup_description_cache[cache_key] = (
        time.monotonic() + BACKUP_DESCRIPTION_CACHE_TTL_SEC,
        copy.deepcopy(payload),
    )


async def describe_backend_backup(
    backup: BackendBackup | None,
    *,
    current_backend: Backend | None = None,
) -> dict[str, Any]:
    if backup is None or backup.status != "success" or not backup.bundle_path:
        return {
            "declared_mounts": [],
            "covered_paths": [],
            "covered_paths_summary": "metadata only",
            "verification_status": "unavailable",
            "restore_readiness": "high_risk",
            "restore_readiness_label": "high risk",
            "risk_flags": ["no_successful_bundle"],
            "risk_summary": "no successful bundle is available",
        }
    cache_key = _backup_description_cache_key(backup, current_backend)
    cached = _cached_backup_description(cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()
    tasks = _description_tasks.setdefault(loop, {})
    task = tasks.get(cache_key)
    if task is None:
        snapshot = BackendBackup(
            bundle_path=backup.bundle_path, bundle_sha256=backup.bundle_sha256
        )
        backend = (
            Backend(kind=current_backend.kind) if current_backend is not None else None
        )
        task = asyncio.create_task(
            _describe_backup_uncached(snapshot, backend, cache_key)
        )
        tasks[cache_key] = task
        task.add_done_callback(lambda completed: tasks.pop(cache_key, None))
    return copy.deepcopy(await asyncio.shield(task))


async def drain_backup_descriptions() -> None:
    tasks = _description_tasks.pop(asyncio.get_running_loop(), {})
    if tasks:
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def _describe_backup_uncached(
    backup, current_backend, cache_key
) -> dict[str, Any]:
    payload = await _description_workers.run(
        _inspect_backup_description, backup, current_backend
    )
    if payload["verification_status"] == "verified":
        _store_backup_description(cache_key, payload)
    return payload


def _inspect_backup_description(backup, current_backend) -> dict[str, Any]:
    try:
        verification = _verify_bundle_contents(
            Path(backup.bundle_path),
            expected_sha256=str(backup.bundle_sha256 or ""),
        )
        payload = {
            "declared_mounts": verification["declared_mounts"],
            "covered_paths": verification["covered_paths"],
            "covered_paths_summary": verification["covered_paths_summary"],
            "verification_status": "verified",
            **_bundle_restore_signals(
                verification["metadata"], current_backend=current_backend
            ),
        }
        return payload
    except Exception:
        metadata: dict[str, Any]
        try:
            metadata = _load_bundle_metadata(Path(backup.bundle_path))
        except Exception:
            metadata = {}
        fallback_coverage = (
            _bundle_coverage_payload(metadata)
            if metadata
            else {
                "declared_mounts": [],
                "covered_paths": [],
                "covered_paths_summary": "unavailable",
            }
        )
        fallback_trust = (
            _bundle_restore_signals(metadata, current_backend=current_backend)
            if metadata
            else {
                "bundle_backend_name": None,
                "bundle_backend_kind": "unknown",
                "restore_readiness": "high_risk",
                "restore_readiness_label": "high risk",
                "risk_flags": ["verification_failed"],
                "risk_summary": "bundle verification failed",
            }
        )
        fallback_trust = {
            **fallback_trust,
            "restore_readiness": "high_risk",
            "restore_readiness_label": "high risk",
            "risk_flags": list(
                dict.fromkeys(
                    [*fallback_trust.get("risk_flags", []), "verification_failed"]
                )
            ),
            "risk_summary": "bundle verification failed; restore readiness is unverified",
        }
        payload = {
            **fallback_coverage,
            **fallback_trust,
            "verification_status": "failed",
        }
        return payload
