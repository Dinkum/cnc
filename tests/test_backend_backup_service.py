import io
import json
from pathlib import Path
import tarfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.logger import configure_logging, flush_logging_pipeline_async
from app.models.entities import Backend, BackendBackup, Input, Operation
from app.schemas.apply import ApplyResponse
from app.services.app_containers import write_app_control_assets
from app.services import backend_backup_service as backups
from app.services.operations import create_operation
from app.services.sandbox_profiles import (
    app_sandbox_dir,
    app_sandbox_profile_state_path,
)
from app.services.commands import CommandResult
from app.services.validators import ValidationError


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _set_container_exists(monkeypatch: pytest.MonkeyPatch, exists: bool) -> None:
    monkeypatch.setattr(
        backups, "container_exists", lambda _container, _timeout_sec: exists
    )
    # These fixtures expose an ordinary, unmounted host-root guest tree.
    # Active idmapped ownership is exercised by the guest metadata tests.
    monkeypatch.setattr(
        backups.GuestArchiveView,
        "capture",
        classmethod(lambda cls, container, runner=None: cls(container, "fixture")),
    )


def _stub_notify(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(backups, "send_pushover_notification_async", fake_notify)
    return sent


def _backup_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        **overrides,
    )


@pytest.fixture(autouse=True)
def _stub_restore_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_apply(*_args, **_kwargs) -> ApplyResponse:
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={"app_healthcheck_status": "healthy"},
            run_id=700,
        )

    monkeypatch.setattr(backups, "run_apply", fake_run_apply)


def _seed_app_guest_state(backend: Backend, settings: Settings) -> None:
    write_app_control_assets(backend, settings)


@pytest.mark.asyncio
async def test_fail_interrupted_backend_backups_marks_running_rows_failed(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _backup_settings(tmp_path)
    interrupted_bundle = tmp_path / "backend-backups" / "web" / "interrupted.tar"
    interrupted_bundle.parent.mkdir(parents=True)
    interrupted_bundle.write_text("partial", encoding="utf-8")
    interrupted_temp = interrupted_bundle.with_name(
        f".{interrupted_bundle.name}.12345.tmp"
    )
    interrupted_temp.write_text("temp partial", encoding="utf-8")

    async with maker() as session:
        session.add_all(
            [
                BackendBackup(
                    status="running",
                    scope="metadata_only",
                    notes="backup queued",
                    bundle_path=str(interrupted_bundle),
                ),
                BackendBackup(
                    status="success", scope="metadata_only", notes="verified"
                ),
            ]
        )
        await session.commit()

        count = await backups.fail_interrupted_backend_backups_in_session(
            session, settings
        )
        rows = (
            (
                await session.execute(
                    select(BackendBackup).order_by(BackendBackup.id.asc())
                )
            )
            .scalars()
            .all()
        )

    assert count == 1
    assert rows[0].status == "error"
    assert rows[0].error == backups.BACKUP_RESTART_ERROR
    assert rows[0].notes == "backup queued; interrupted by CNC restart"
    assert not interrupted_bundle.exists()
    assert not interrupted_temp.exists()
    assert rows[1].status == "success"
    assert rows[1].error is None


@pytest.mark.asyncio
async def test_interrupted_backup_cleanup_refuses_bundle_path_outside_backup_root(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _backup_settings(tmp_path)
    outside_bundle = tmp_path / "outside.tar"
    outside_bundle.write_text("do not delete", encoding="utf-8")

    async with maker() as session:
        session.add(
            BackendBackup(
                status="running",
                scope="metadata_only",
                notes="backup queued",
                bundle_path=str(outside_bundle),
            )
        )
        await session.commit()

        count = await backups.fail_interrupted_backend_backups_in_session(
            session, settings
        )
        row = (await session.execute(select(BackendBackup))).scalar_one()

    assert count == 1
    assert row.status == "error"
    assert outside_bundle.exists()


@pytest.mark.asyncio
async def test_create_backend_backup_does_not_mark_operation_success_before_record_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _backup_settings(tmp_path)

    def fake_write_bundle(*, bundle_path: Path, **_kwargs):
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text("verified bundle", encoding="utf-8")
        return {
            "scope": "metadata_only",
            "bundle_path": str(bundle_path),
            "bundle_sha256": "sha",
            "size_bytes": bundle_path.stat().st_size,
            "notes": "metadata only",
        }

    monkeypatch.setattr(backups, "_write_backup_bundle", fake_write_bundle)
    monkeypatch.setattr(
        backups, "_verify_bundle_contents", lambda *_args, **_kwargs: {}
    )

    async def fake_notify(*_args, **_kwargs):
        return False

    monkeypatch.setattr(backups, "send_pushover_notification_async", fake_notify)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)

        real_commit = session.commit
        commit_count = 0

        async def fail_success_record_commit():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 3:
                raise RuntimeError("sqlite commit failed")
            await real_commit()

        monkeypatch.setattr(session, "commit", fail_success_record_commit)
        record = await backups.create_backend_backup(session, backend, settings)
        bundle_path = Path(record.bundle_path or "")
        operation = (await session.execute(select(Operation))).scalar_one()

    assert record.status == "error"
    assert record.error == "sqlite commit failed"
    assert not bundle_path.exists()
    assert operation.status == "failed"
    assert operation.error == "sqlite commit failed"


def test_extract_bundle_entries_rejects_hostile_mount_member(tmp_path: Path) -> None:
    bundle_path = tmp_path / "hostile.tar"
    destination = tmp_path / "restore"
    link = tarfile.TarInfo("mounts/0/passwd")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with tarfile.open(bundle_path, "w") as archive:
        archive.addfile(link)

    metadata = {"mount_entries": [{"exists": True, "archive_prefix": "mounts/0"}]}

    with pytest.raises(RuntimeError, match="tar symlink escapes destination"):
        backups._extract_bundle_entries(bundle_path, destination, metadata)

    assert not (destination / "mounts/0/passwd").exists()


def _write_bundle_for_verification(
    bundle_path: Path,
    *,
    metadata: dict[str, object],
    members: list[tarfile.TarInfo | tuple[str, bytes]],
) -> None:
    metadata_bytes = json.dumps(metadata).encode("utf-8")
    with tarfile.open(bundle_path, "w") as archive:
        metadata_info = tarfile.TarInfo(backups.BUNDLE_METADATA_NAME)
        metadata_info.size = len(metadata_bytes)
        metadata_info.mode = 0o600
        archive.addfile(metadata_info, io.BytesIO(metadata_bytes))
        for member in members:
            if isinstance(member, tuple):
                name, payload = member
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
                continue
            archive.addfile(member)


def _verification_metadata(
    tmp_path: Path,
    *,
    link_policy: str = "strict",
) -> dict[str, object]:
    return {
        "bundle_format_version": 1,
        "backend": {
            "name": "web",
            "kind": "static",
            "static_root": str(tmp_path / "site"),
            "enabled": True,
        },
        "mount_entries": [
            {
                "exists": True,
                "archive_prefix": "mounts/0",
                "host_path": str(tmp_path / "site"),
                "link_policy": link_policy,
            }
        ],
    }


def _tar_member(
    name: str,
    member_type: bytes,
    *,
    linkname: str = "",
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.linkname = linkname
    return member


@pytest.mark.parametrize(
    ("member", "match"),
    [
        (
            _tar_member("mounts/0/passwd", tarfile.SYMTYPE, linkname="/etc/passwd"),
            "tar symlink escapes destination",
        ),
        (
            _tar_member("mounts/0/hardlink", tarfile.LNKTYPE, linkname="/etc/passwd"),
            "unsafe tar hardlink target",
        ),
        (
            _tar_member("mounts/0/device", tarfile.CHRTYPE),
            "tar devices are not allowed",
        ),
        (
            _tar_member("mounts/0/fifo", tarfile.FIFOTYPE),
            "tar devices are not allowed",
        ),
        (
            _tar_member("mounts/0/unknown", b"Z"),
            "unsupported tar member type",
        ),
    ],
)
def test_verify_bundle_contents_rejects_restore_unsafe_mount_members(
    tmp_path: Path, member: tarfile.TarInfo, match: str
) -> None:
    bundle_path = tmp_path / "hostile.tar"
    _write_bundle_for_verification(
        bundle_path,
        metadata=_verification_metadata(tmp_path),
        members=[member],
    )

    with pytest.raises(RuntimeError, match=match):
        backups._verify_bundle_contents(bundle_path)


def test_verify_bundle_contents_allows_guest_absolute_symlink_policy(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "guest.tar"
    symlink = _tar_member(
        "mounts/0/rootfs/bin/python3",
        tarfile.SYMTYPE,
        linkname="/usr/bin/python3",
    )
    _write_bundle_for_verification(
        bundle_path,
        metadata=_verification_metadata(tmp_path, link_policy="guest"),
        members=[
            ("mounts/0/rootfs/usr/bin/python3", b"#!/usr/bin/python3\n"),
            symlink,
        ],
    )

    payload = backups._verify_bundle_contents(bundle_path)

    assert payload["scope"] == "mounted_data"
    assert payload["mount_entries_archived"] == 1


@pytest.mark.asyncio
async def test_create_backend_backup_writes_single_bundle(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "app.txt").write_text("hello", encoding="utf-8")
    venv_bin = data_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "uvicorn").write_text("#!/srv/web/.venv/bin/python\n", encoding="utf-8")
    (venv_bin / "python").symlink_to("python3")
    (venv_bin / "python3").symlink_to("/usr/bin/python3")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json='{"PORT":"8337"}',
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        progress_events: list[tuple[int, str]] = []
        backup = await backups.create_backend_backup(
            session,
            backend,
            settings,
            progress_callback=lambda progress, message: progress_events.append(
                (progress, message)
            ),
        )

    assert backup.status == "success"
    assert backup.scope == "mounted_data"
    assert backup.bundle_path is not None
    assert backup.bundle_sha256
    assert backup.notes == "bundle with metadata plus 2 mounted path(s); verified"
    bundle_path = Path(backup.bundle_path)
    assert bundle_path.exists()
    with tarfile.open(bundle_path, "r:gz") as archive:
        metadata = json.loads(
            archive.extractfile("metadata.json").read().decode("utf-8")
        )  # type: ignore[union-attr]
        member_names = [member.name for member in archive.getmembers()]
    assert metadata["backend"]["name"] == "web"
    assert metadata["inputs"][0]["hostname"] == "web.example.com"
    assert metadata["resolved_host_paths"] == [
        str(app_sandbox_dir(settings, "web")),
        str(data_dir),
    ]
    assert metadata["mount_entries"][1]["link_policy"] == "guest"
    assert any(name.startswith("mounts/0") for name in member_names)
    assert "mounts/1/.venv/bin/python" in member_names
    assert "mounts/1/.venv/bin/python3" in member_names
    progress_messages = [message for _, message in progress_events]
    assert "Gathering app data." in progress_messages
    assert "Collecting mounted paths." in progress_messages
    assert any(
        message.startswith(f"Exporting path {app_sandbox_dir(settings, 'web')} (")
        for message in progress_messages
    )
    assert any(
        message.startswith(f"Exporting path {data_dir} (")
        for message in progress_messages
    )
    assert any(
        message.startswith(f"Exporting path {data_dir}: ") and " of " in message
        for message in progress_messages
    )
    assert any(
        message.startswith(f"Exported path {data_dir} (")
        for message in progress_messages
    )
    assert any(
        message.startswith("Compressing backup bundle from ")
        for message in progress_messages
    )
    assert any(
        message.startswith("Compressed backup to ") for message in progress_messages
    )
    assert "Verifying backup." in progress_messages
    assert "Backup verified." in progress_messages


@pytest.mark.asyncio
async def test_create_backend_backup_reuses_supplied_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        operation = await create_operation(
            settings,
            kind="backup_backend",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details={"operation_role": "ui_backup"},
        )
        backup = await backups.create_backend_backup(
            session,
            backend,
            settings,
            operation=operation,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    assert backup.status == "success"
    assert backup.operation_id == operation.id
    assert [item.kind for item in operations] == ["backup_backend"]


def test_extract_bundle_entries_preserves_guest_mount_venv_symlinks(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "venv.tar"
    destination = tmp_path / "restore"
    relative_link = tarfile.TarInfo("mounts/1/.venv/bin/python")
    relative_link.type = tarfile.SYMTYPE
    relative_link.linkname = "python3"
    absolute_link = tarfile.TarInfo("mounts/1/.venv/bin/python3")
    absolute_link.type = tarfile.SYMTYPE
    absolute_link.linkname = "/usr/bin/python3"
    with tarfile.open(bundle_path, "w") as archive:
        archive.addfile(relative_link)
        archive.addfile(absolute_link)

    metadata = {
        "mount_entries": [
            {
                "exists": True,
                "archive_prefix": "mounts/1",
                "link_policy": "guest",
            }
        ]
    }

    backups._extract_bundle_entries(bundle_path, destination, metadata)

    assert (destination / "mounts/1/.venv/bin/python").readlink() == Path("python3")
    assert (destination / "mounts/1/.venv/bin/python3").readlink() == Path(
        "/usr/bin/python3"
    )


def test_restore_mount_entry_validation_rejects_metadata_host_path_rewrite(
    tmp_path: Path,
) -> None:
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    metadata = {
        "backend": {
            "name": "web",
            "kind": "app",
            "port": 12000,
            "sandbox_profile": "ubuntu-24.04-systemd",
            "handoff_port": 8337,
            "volumes_json": json.dumps([f"{data_dir}:/srv/app/data"]),
            "enabled": True,
        },
        "mount_entries": [
            {
                "host_path": "/var/lib/cnc/current",
                "declared": str(app_sandbox_dir(settings, "web")),
                "target_path": None,
                "exists": True,
                "archive_prefix": "mounts/0",
                "link_policy": "guest",
            }
        ],
    }

    with pytest.raises(RuntimeError, match="restore path does not match"):
        backups._validate_restore_mount_entries(metadata, settings, backend_name="web")


@pytest.mark.asyncio
async def test_create_backend_backup_supports_uncompressed_tar_format(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _backup_settings(
        tmp_path,
        backend_backup_archive_format="tar",
        backend_backup_retention_per_backend=0,
    )
    data_dir = tmp_path / "site-data"
    data_dir.mkdir()
    (data_dir / "index.html").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="docs",
            kind="static",
            static_root=str(data_dir),
            internal_port=8000,
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)

        backup = await backups.create_backend_backup(session, backend, settings)
        payload = await backups.verify_backend_backup(session, backend, backup)

    assert backup.status == "success"
    assert backup.bundle_path is not None
    assert backup.bundle_path.endswith(".tar")
    assert payload["scope"] == "mounted_data"
    assert payload["mount_entries_archived"] == 1


@pytest.mark.asyncio
async def test_create_backend_backup_sends_notification_on_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    sent = _stub_notify(monkeypatch)
    settings = _backup_settings(tmp_path)

    monkeypatch.setattr(
        backups,
        "_write_backup_bundle",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)

    assert backup.status == "error"
    assert sent
    assert sent[0]["title"] == "CNC backup failed: web"


@pytest.mark.asyncio
async def test_restore_backend_backup_sends_completion_notification(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    sent = _stub_notify(monkeypatch)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        progress_events: list[tuple[int, str]] = []
        await backups.restore_backend_backup(
            session,
            backend,
            backup,
            settings,
            progress_callback=lambda progress, message: progress_events.append(
                (progress, message)
            ),
        )

    assert any(item["title"] == "CNC restore completed: web" for item in sent)
    progress_messages = [message for _, message in progress_events]
    assert progress_messages[:2] == [
        "Verifying backup bundle.",
        "Validating restore payload.",
    ]
    assert "Restoring mounted paths." in progress_messages
    assert "Restoring backend config." in progress_messages
    assert "Writing app control assets." in progress_messages
    assert progress_messages[-2:] == ["Committing restore.", "Refreshing output page."]


@pytest.mark.asyncio
async def test_create_backend_backup_includes_container_snapshot_when_app_container_exists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, True)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "app.txt").write_text("hello", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 0, "", "")
        if command[:2] == ["podman", "commit"]:
            return CommandResult(command, 0, "", "")
        if command[:3] == ["podman", "save", "-o"]:
            Path(command[3]).write_bytes(b"snapshot")
            return CommandResult(command, 0, "", "")
        if command[:3] == ["podman", "image", "rm"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json='{"PORT":"8337"}',
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)

    assert backup.status == "success"
    assert backup.scope == "full_state"
    bundle_path = Path(str(backup.bundle_path))
    with tarfile.open(bundle_path, "r:gz") as archive:
        metadata = json.loads(
            archive.extractfile("metadata.json").read().decode("utf-8")
        )  # type: ignore[union-attr]
        member_names = [member.name for member in archive.getmembers()]
    assert metadata["container_snapshot"]["included"] is True
    assert "container-image.tar" in member_names


@pytest.mark.asyncio
async def test_create_backend_backup_skips_unsupported_exploded_rootfs_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, True)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "app.txt").write_text("hello", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 0, "", "")
        if command[:2] == ["podman", "commit"]:
            return CommandResult(
                command,
                125,
                "",
                "Error: cannot commit a container that uses an exploded rootfs",
            )
        if command[:3] == ["podman", "image", "rm"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json='{"PORT":"8337"}',
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        payload = await backups.verify_backend_backup(session, backend, backup)

    assert backup.status == "success"
    assert backup.scope == "mounted_data"
    assert payload["container_snapshot_included"] is False
    assert payload["covered_paths"] == [
        str(app_sandbox_dir(settings, "web")),
        str(data_dir),
    ]


@pytest.mark.asyncio
async def test_create_backend_backup_prunes_older_successful_bundles(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path, backend_backup_retention_per_backend=2)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    bundle_paths: list[Path] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        for index in range(3):
            (data_dir / "state.txt").write_text(f"state-{index}", encoding="utf-8")
            backup = await backups.create_backend_backup(session, backend, settings)
            bundle_paths.append(Path(str(backup.bundle_path)))

        backups_after = await backups.list_backend_backups(
            session, backend.id, limit=10
        )

    assert [item.status for item in backups_after] == ["success", "success"]
    assert [item.id for item in backups_after] == [3, 2]
    assert not bundle_paths[0].exists()
    assert bundle_paths[1].exists()
    assert bundle_paths[2].exists()


@pytest.mark.asyncio
async def test_verify_backend_backup_reports_bundle_details(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        payload = await backups.verify_backend_backup(session, backend, backup)

    assert payload["ok"] is True
    assert payload["backend"] == "web"
    assert payload["backup_id"] == backup.id
    assert payload["scope"] == "mounted_data"
    assert payload["mount_entries_archived"] == 2
    assert payload["mount_entries_total"] == 2
    assert payload["container_snapshot_included"] is False
    assert payload["covered_paths"] == [
        str(app_sandbox_dir(settings, "web")),
        str(data_dir),
    ]
    assert payload["covered_paths_summary"] == (
        f"{app_sandbox_dir(settings, 'web')}, {data_dir}"
    )
    assert payload["verification_status"] == "verified"
    assert payload["restore_readiness"] == "review"
    assert payload["risk_flags"] == ["app_without_container_snapshot"]


@pytest.mark.asyncio
async def test_verify_backend_backup_detects_checksum_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        assert backup.bundle_path is not None
        Path(backup.bundle_path).write_bytes(b"corrupted")

        with pytest.raises(RuntimeError, match="backup bundle checksum mismatch"):
            await backups.verify_backend_backup(session, backend, backup)


@pytest.mark.asyncio
async def test_restore_latest_backend_backup_restores_mounts_and_backend_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        await backups.create_backend_backup(session, backend, settings)

        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()
        for item in list(backend.inputs):
            await session.delete(item)
        backend.inputs = []
        await session.commit()

        result = await backups.restore_latest_backend_backup(session, backend, settings)
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()

    assert (data_dir / "state.txt").read_text(encoding="utf-8") == "original"
    assert [item.hostname for item in backend.inputs] == ["web.example.com"]
    assert result["restored_paths"] == [
        str(app_sandbox_dir(settings, "web")),
        str(data_dir),
    ]
    assert "ubuntu-24.04-systemd" in app_sandbox_profile_state_path(
        settings, "web"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restore_latest_backend_backup_converges_before_reporting_success(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")
    convergence_observed: dict[str, object] = {}

    async def fake_run_apply(session, _settings, **kwargs) -> ApplyResponse:
        restored_backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == "web")
            )
        ).scalar_one()
        convergence_observed["state"] = (data_dir / "state.txt").read_text(
            encoding="utf-8"
        )
        convergence_observed["inputs"] = [
            item.hostname for item in restored_backend.inputs
        ]
        convergence_observed["commit_on_success"] = kwargs.get("commit_on_success")
        convergence_observed["operation_handle"] = kwargs.get("operation_handle")
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={"app_healthcheck_status": "healthy", "desired_state_hash": "abc"},
            run_id=701,
        )

    monkeypatch.setattr(backups, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)
        await backups.create_backend_backup(session, backend, settings)

        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()
        for item in list(backend.inputs):
            await session.delete(item)
        backend.inputs = []
        await session.commit()

        result = await backups.restore_latest_backend_backup(session, backend, settings)

    assert convergence_observed["state"] == "original"
    assert convergence_observed["inputs"] == ["web.example.com"]
    assert convergence_observed["commit_on_success"] is False
    assert convergence_observed["operation_handle"] is not None
    assert result["convergence"]["apply_run_id"] == 701
    assert result["convergence"]["desired_state_hash"] == "abc"


@pytest.mark.asyncio
async def test_restore_latest_backend_backup_rolls_back_when_convergence_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    async def fake_run_apply(*_args, **_kwargs) -> ApplyResponse:
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={"phase": "nginx_reload", "error": "reload failed"},
            run_id=702,
        )

    monkeypatch.setattr(backups, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)
        await backups.create_backend_backup(session, backend, settings)

        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()
        for item in list(backend.inputs):
            await session.delete(item)
        backend.inputs = []
        await session.commit()
        backend_id = backend.id

        with pytest.raises(
            RuntimeError,
            match="restore convergence failed during nginx_reload: reload failed",
        ):
            await backups.restore_latest_backend_backup(session, backend, settings)

        restored_backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend_id)
            )
        ).scalar_one()

    assert (data_dir / "state.txt").read_text(encoding="utf-8") == "mutated"
    assert restored_backend.inputs == []


@pytest.mark.asyncio
async def test_restore_latest_backend_backup_uses_quadlet_lifecycle_when_available(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _backup_settings(tmp_path, app_quadlet_dir=tmp_path / "quadlets")
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)
    _set_container_exists(monkeypatch, False)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)
        await backups.create_backend_backup(session, backend, settings)

        settings.app_quadlet_dir.mkdir(parents=True)
        backups.quadlet_container_path(settings, "web").write_text(
            "[Container]\n", encoding="utf-8"
        )
        _set_container_exists(monkeypatch, True)
        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")

        await backups.restore_latest_backend_backup(session, backend, settings)

    systemctl_commands = [
        command
        for command in commands
        if command[:2] in (["systemctl", "stop"], ["systemctl", "start"])
    ]
    assert [
        ["systemctl", "stop", "cnc-app-web.service"],
        ["systemctl", "start", "cnc-app-web.service"],
    ] == systemctl_commands
    assert ["podman", "stop", "cnc-web"] not in commands
    assert ["podman", "start", "cnc-web"] not in commands


@pytest.mark.asyncio
async def test_restore_latest_backend_backup_rolls_back_filesystem_and_control_assets_on_commit_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)
        await backups.create_backend_backup(session, backend, settings)

        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()
        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        await session.commit()
        write_app_control_assets(backend, settings)

        original_commit = session.commit

        async def failing_commit() -> None:
            raise RuntimeError("db commit failed")

        monkeypatch.setattr(session, "commit", failing_commit)
        backend_id = backend.id

        with pytest.raises(RuntimeError, match="db commit failed"):
            await backups.restore_latest_backend_backup(session, backend, settings)

        monkeypatch.setattr(session, "commit", original_commit)
        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend_id)
            )
        ).scalar_one()

    assert (data_dir / "state.txt").read_text(encoding="utf-8") == "mutated"
    assert "ubuntu-24.04-systemd" in app_sandbox_profile_state_path(
        settings, "web"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restore_backend_backup_can_target_selected_backup(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("v1", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        first = await backups.create_backend_backup(session, backend, settings)

        (data_dir / "state.txt").write_text("v2", encoding="utf-8")
        second = await backups.create_backend_backup(session, backend, settings)

        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        backend = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one()
        result = await backups.restore_backend_backup(
            session,
            backend,
            first,
            settings,
        )

    assert isinstance(first, BackendBackup)
    assert isinstance(second, BackendBackup)
    assert first.id != second.id
    assert result["backup"].id == first.id
    assert (data_dir / "state.txt").read_text(encoding="utf-8") == "v1"


@pytest.mark.asyncio
async def test_import_backend_backup_bundle_restores_from_uploaded_bundle(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    source_data_dir = tmp_path / "web-data"
    source_data_dir.mkdir()
    (source_data_dir / "state.txt").write_text("exported", encoding="utf-8")

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 1, "", "missing")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{source_data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        original_backup = await backups.create_backend_backup(
            session, backend, settings
        )
        exported_bundle = Path(original_backup.bundle_path or "")
        assert exported_bundle.exists()

        backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend.id)
            )
        ).scalar_one()
        (source_data_dir / "state.txt").write_text("mutated", encoding="utf-8")
        operation = await create_operation(
            settings,
            kind="import_backend_backup",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details={"operation_role": "ui_backup_import"},
        )
        await session.commit()

        imported = await backups.import_backend_backup_bundle(
            session,
            backend,
            exported_bundle,
            original_name="web-export.tar.gz",
            settings=settings,
            operation=operation,
        )
        result = await backups.restore_backend_backup(
            session, backend, imported, settings
        )
        operations = (await session.execute(select(Operation))).scalars().all()

    assert imported.status == "success"
    assert imported.operation_id == operation.id
    assert [item.kind for item in operations].count("backup_backend") == 1
    assert [item.kind for item in operations].count("import_backend_backup") == 1
    assert imported.bundle_path is not None
    assert result["backup"].id == imported.id
    assert (source_data_dir / "state.txt").read_text(encoding="utf-8") == "exported"


@pytest.mark.asyncio
async def test_import_backend_backup_bundle_reuses_operation_id(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        original_backup = await backups.create_backend_backup(
            session, backend, settings
        )
        exported_bundle = Path(original_backup.bundle_path or "")
        operation = await create_operation(
            settings,
            kind="import_backend_backup",
            actor="ui",
            backend_id=backend.id,
            phase="queued",
            details={"operation_role": "ui_backup_import"},
        )
        imported = await backups.import_backend_backup_bundle(
            session,
            backend,
            exported_bundle,
            original_name="web-export.tar.gz",
            settings=settings,
            operation_id=operation.id,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    assert imported.status == "success"
    assert imported.operation_id == operation.id
    assert [item.kind for item in operations].count("import_backend_backup") == 1


@pytest.mark.asyncio
async def test_restore_backend_backup_recreates_app_from_snapshot_image(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, True)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")
    sync_commands: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        sync_commands.append(command)
        if command[:3] == ["podman", "container", "exists"]:
            return CommandResult(command, 0, "", "")
        if command[:2] == ["podman", "commit"]:
            return CommandResult(command, 0, "", "")
        if command[:3] == ["podman", "save", "-o"]:
            Path(command[3]).write_bytes(b"snapshot")
            return CommandResult(command, 0, "", "")
        if command[:3] == ["podman", "load", "-i"]:
            return CommandResult(
                command,
                0,
                "Loaded image(s): localhost/cnc-backup-snapshot-web:test",
                "",
            )
        if command[:2] == ["podman", "tag"]:
            return CommandResult(command, 0, "", "")
        if command[:3] == ["podman", "image", "rm"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(backups, "run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        backend = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one()
        result = await backups.restore_backend_backup(
            session, backend, backup, settings
        )

    assert result["backup"].id == backup.id
    assert any(command[:3] == ["podman", "load", "-i"] for command in sync_commands)
    assert any(command[:2] == ["podman", "tag"] for command in sync_commands)


@pytest.mark.asyncio
async def test_restore_backend_bundle_as_new_backend_recreates_deleted_output(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            memory_high_override="512M",
            memory_max_override="768M",
            cpu_quota_override="100%",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        exported_bundle = Path(str(backup.bundle_path))

        await session.delete(backend)
        await session.commit()
        (data_dir / "state.txt").write_text("mutated", encoding="utf-8")

        result = await backups.restore_backend_bundle_as_new_backend(
            session,
            exported_bundle,
            original_name=exported_bundle.name,
            settings=settings,
        )

        restored_backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == "web")
            )
        ).scalar_one()
        backend_backups = await backups.list_backend_backups(
            session, restored_backend.id, limit=10
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    assert result["backend"].name == "web"
    assert result["backup"].status == "success"
    assert (data_dir / "state.txt").read_text(encoding="utf-8") == "original"
    assert restored_backend.memory_high_override == "512M"
    assert restored_backend.memory_max_override == "768M"
    assert restored_backend.cpu_quota_override == "100%"
    assert [item.hostname for item in restored_backend.inputs] == ["web.example.com"]
    assert any(item.id == result["backup"].id for item in backend_backups)
    restore_operations = [item for item in operations if item.kind == "restore_backend"]
    assert [item.kind for item in operations].count("import_backend_backup") == 0
    assert len(restore_operations) == 1
    assert result["backup"].operation_id == restore_operations[0].id
    restore_details = json.loads(restore_operations[0].details_json or "{}")
    assert restore_details["backup_id"] == result["backup"].id
    assert restore_details["convergence"]["apply_run_id"] == 700


@pytest.mark.asyncio
async def test_restore_backend_bundle_as_new_backend_rejects_renamed_storage_paths(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)
        backup = await backups.create_backend_backup(session, backend, settings)
        exported_bundle = Path(str(backup.bundle_path))
        await session.delete(backend)
        await session.commit()

        with pytest.raises(RuntimeError, match="cannot change the backend name"):
            await backups.restore_backend_bundle_as_new_backend(
                session,
                exported_bundle,
                original_name=exported_bundle.name,
                settings=settings,
                backend_name="web-copy",
            )


@pytest.mark.asyncio
async def test_clone_backend_as_new_backend_copies_mounts_without_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        progress_events: list[tuple[int, str]] = []
        result = await backups.clone_backend_as_new_backend(
            session,
            backend,
            settings,
            backend_name="web-copy",
            target_port=12001,
            progress_callback=lambda progress, message: progress_events.append(
                (progress, message)
            ),
        )
        cloned_backend = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == "web-copy")
            )
        ).scalar_one()

    cloned_source = Path(json.loads(cloned_backend.volumes_json)[0].split(":", 1)[0])
    cloned_sandbox = app_sandbox_dir(settings, "web-copy")
    assert result["backend"].name == "web-copy"
    assert result["restored_paths"] == [str(cloned_sandbox), str(cloned_source)]
    assert cloned_backend.enabled is False
    assert cloned_backend.port == 12001
    assert cloned_backend.healthcheck_host_header in {None, ""}
    assert cloned_backend.inputs == []
    assert cloned_source != data_dir
    assert (cloned_source / "state.txt").read_text(encoding="utf-8") == "original"
    assert (settings.app_control_dir / "web-copy" / "spec.json").exists()
    assert result["clone_contract"]["source_backend"] == "web"
    assert result["clone_contract"]["target_backend"] == "web-copy"
    assert result["clone_contract"]["target_port"] == 12001
    assert result["clone_contract"]["metadata_rewrites"]["inputs"] == []
    assert result["clone_contract"]["healthcheck"]["warnings"] == [
        "source_domain_healthcheck_host_header_cleared"
    ]
    assert {item["name"] for item in result["post_clone_checks"]} >= {
        "backend_metadata_rewritten",
        "route_inputs_stripped",
        "restored_paths_exist",
        "healthcheck_host_header_valid",
        "app_control_assets_written",
    }
    assert [message for _, message in progress_events] == [
        "Capturing source state.",
        "Verifying clone bundle.",
        "Writing cloned output.",
        "Restoring guest and data.",
        "Writing app control assets.",
        "Running clone checks.",
        "Finalizing clone.",
    ]


def test_clone_metadata_rejects_invalid_healthcheck_host_header() -> None:
    metadata = {
        "backend": {
            "name": "web",
            "kind": "app",
            "port": 12000,
            "sandbox_profile": "ubuntu-24.04-systemd",
            "handoff_port": 8337,
            "healthcheck_mode": "http",
            "healthcheck_path": "/health",
            "healthcheck_host_header": "bad host",
            "resource_mode": "auto",
            "resource_size": "small",
            "volumes_json": "[]",
            "enabled": True,
        },
        "inputs": [],
        "mount_entries": [],
        "guest_contract": {"version": 1},
    }

    with pytest.raises(ValidationError, match="hostname"):
        backups._clone_bundle_metadata_for_new_backend(
            metadata,
            source_backend_name="web",
            target_backend_name="web-copy",
            target_port=12001,
        )


@pytest.mark.asyncio
async def test_restore_backend_bundle_as_new_backend_logs_import_and_restore(
    monkeypatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=1_000_000,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(
        tmp_path,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("original", encoding="utf-8")
    sent = _stub_notify(monkeypatch)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps([f"{data_dir}:/srv/app/data"]),
            enabled=True,
        )
        backend.inputs = [Input(hostname="web.example.com", enabled=True)]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        exported_bundle = Path(str(backup.bundle_path))
        await session.delete(backend)
        await session.commit()

        await backups.restore_backend_bundle_as_new_backend(
            session,
            exported_bundle,
            original_name=exported_bundle.name,
            settings=settings,
        )

    assert any(item.get("event") == "backend_restored" for item in sent)
    await flush_logging_pipeline_async()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(
        "backup.import.succeeded" in line and "backend: web" in line for line in lines
    )
    assert any(
        "restore.succeeded" in line
        and "backend: web" in line
        and "notified: false" in line
        for line in lines
    )
    assert any(
        "restore.bundle.succeeded" in line and "backend: web" in line for line in lines
    )


@pytest.mark.asyncio
async def test_describe_backend_backup_reports_covered_paths(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "docs-data"
    data_dir.mkdir()
    (data_dir / "index.html").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="docs",
            kind="static",
            static_root=str(data_dir),
            internal_port=8000,
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)

        backup = await backups.create_backend_backup(session, backend, settings)
        payload = await backups.describe_backend_backup(backup)

    assert payload["covered_paths"] == [str(data_dir)]
    assert payload["covered_paths_summary"] == str(data_dir)
    assert payload["verification_status"] == "verified"
    assert payload["restore_readiness"] == "ready"
    assert payload["risk_flags"] == []


@pytest.mark.asyncio
async def test_describe_backend_backup_reuses_recent_verification(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "docs-data"
    data_dir.mkdir()
    (data_dir / "index.html").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="docs",
            kind="static",
            static_root=str(data_dir),
            internal_port=8000,
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)

        backup = await backups.create_backend_backup(session, backend, settings)

        backups._backup_description_cache.clear()
        original_verify = backups._verify_bundle_contents
        verify_calls = 0

        def counting_verify(*args, **kwargs):
            nonlocal verify_calls
            verify_calls += 1
            return original_verify(*args, **kwargs)

        monkeypatch.setattr(backups, "_verify_bundle_contents", counting_verify)

        first = await backups.describe_backend_backup(backup, current_backend=backend)
        second = await backups.describe_backend_backup(backup, current_backend=backend)

    assert first == second
    assert first["verification_status"] == "verified"
    assert verify_calls == 1


@pytest.mark.asyncio
async def test_describe_backend_backup_flags_partial_mount_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    _set_container_exists(monkeypatch, False)
    settings = _backup_settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    missing_dir = tmp_path / "missing-data"
    (data_dir / "state.txt").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json=json.dumps(
                [f"{data_dir}:/srv/app/data", f"{missing_dir}:/srv/app/missing"]
            ),
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        _seed_app_guest_state(backend, settings)

        backup = await backups.create_backend_backup(session, backend, settings)
        payload = await backups.describe_backend_backup(backup, current_backend=backend)

    assert payload["verification_status"] == "verified"
    assert payload["restore_readiness"] == "review"
    assert "partial_mount_coverage" in payload["risk_flags"]


def test_volume_ownership_restore_requires_a_private_target_parent(tmp_path):
    import os

    bundle = tmp_path / "volume.tar"
    with tarfile.open(bundle, "w") as archive:
        entry = tarfile.TarInfo("mounts/0/file")
        entry.uid, entry.gid, entry.size = os.getuid(), os.getgid(), 1
        archive.addfile(entry, io.BytesIO(b"x"))
    target = tmp_path / "shared-target"
    metadata = {
        "mount_entries": [
            {
                "archive_prefix": "mounts/0",
                "host_path": str(target),
                "target_path": "/data",
                "exists": True,
                "link_policy": "strict",
                "ownership_policy": "canonical_guest",
            }
        ]
    }
    transaction = backups._RestoreBundleTransaction(metadata, bundle)
    with pytest.raises(ValueError, match="private directory"):
        transaction.apply()
    assert not target.exists()
