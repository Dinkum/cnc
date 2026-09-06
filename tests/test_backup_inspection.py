import asyncio
import io
import json
from pathlib import Path
import tarfile
import threading

import pytest

from app.models.entities import BackendBackup
from app.services import backend_backup_service as backups


def make_bundle(path: Path, *, unsafe: bool = False) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in ("first", "last"):
            metadata = {
                "bundle_format_version": 1,
                "backend": {"kind": "static", "name": name},
                "mount_entries": [
                    {
                        "exists": True,
                        "archive_prefix": "mounts/0",
                        "path": "/srv/example",
                    }
                ],
            }
            value = json.dumps(metadata).encode()
            member = tarfile.TarInfo(backups.BUNDLE_METADATA_NAME)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        value = b"payload"
        member = tarfile.TarInfo("../unsafe" if unsafe else "mounts/0/file")
        member.size = len(value)
        archive.addfile(member, io.BytesIO(value))


def test_verification_uses_one_archive_and_last_metadata(tmp_path, monkeypatch):
    path = tmp_path / "backup.tar.gz"
    make_bundle(path)
    opens = []
    original = tarfile.open

    def tracked(*args, **kwargs):
        opens.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(backups.tarfile, "open", tracked)
    result = backups._verify_bundle_contents(path)
    assert result["metadata"]["backend"]["name"] == "last"
    assert len(opens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checksum", "unsafe"])
async def test_failed_integrity_never_reports_ready(tmp_path, failure):
    path = tmp_path / "backup.tar.gz"
    make_bundle(path, unsafe=failure == "unsafe")
    backup = BackendBackup(
        id=1,
        status="success",
        bundle_path=str(path),
        bundle_sha256="wrong" if failure == "checksum" else "",
    )
    result = await backups.describe_backend_backup(backup)
    assert result["verification_status"] == "failed"
    assert result["restore_readiness"] == "high_risk"
    assert "verification_failed" in result["risk_flags"]
    if failure == "checksum":
        backup.bundle_sha256 = ""
        assert (await backups.describe_backend_backup(backup))[
            "verification_status"
        ] == "verified"


@pytest.mark.asyncio
async def test_description_shares_work_survives_cancel_and_copies_results(
    tmp_path, monkeypatch
):
    path = tmp_path / "backup.tar.gz"
    make_bundle(path)
    backup = BackendBackup(id=2, status="success", bundle_path=str(path))
    started, release = threading.Event(), threading.Event()
    calls = []
    original = backups._verify_bundle_contents

    def verify(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(backups, "_verify_bundle_contents", verify)
    tasks = [
        asyncio.create_task(backups.describe_backend_backup(backup)) for _ in range(4)
    ]
    try:
        assert await asyncio.to_thread(started.wait, 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert len(calls) == 1
    finally:
        release.set()
    results = await asyncio.gather(*tasks[1:])
    assert all(result == results[0] for result in results)
    results[0]["covered_paths"].append("changed")
    assert "changed" not in results[1]["covered_paths"]
    assert (
        "changed"
        not in (await backups.describe_backend_backup(backup))["covered_paths"]
    )
    assert len(calls) == 1
    await backups.drain_backup_descriptions()


@pytest.mark.asyncio
async def test_description_retries_transient_verifier_failure(tmp_path, monkeypatch):
    path = tmp_path / "backup.tar.gz"
    make_bundle(path)
    backup = BackendBackup(id=3, status="success", bundle_path=str(path))
    original = backups._verify_bundle_contents
    calls = 0

    def verify(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary read failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(backups, "_verify_bundle_contents", verify)
    assert (await backups.describe_backend_backup(backup))[
        "verification_status"
    ] == "failed"
    assert (await backups.describe_backend_backup(backup))[
        "verification_status"
    ] == "verified"
    assert calls == 2
