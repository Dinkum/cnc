from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, BackendBackup, ClusterNode
from app.services import cluster_backups
from app.services.commands import CommandResult


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_mirror_backend_backup_to_cluster_copies_and_verifies(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    bundle_path = tmp_path / "backup.tar.gz"
    bundle_path.write_text("backup", encoding="utf-8")
    settings = Settings(
        multi_node_enabled=True,
        command_timeout_apply_sec=42,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
    )
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_backups, "run_command", fake_run_command)

    async with maker() as session:
        backend = Backend(name="static site", kind="static", enabled=True)
        backup = BackendBackup(
            backend_id=1,
            status="success",
            bundle_path=str(bundle_path),
            bundle_sha256="sha256",
        )
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([node])
        await session.commit()

        result = await cluster_backups.mirror_backend_backup_to_cluster(
            session, backend, backup, settings
        )

    assert result["mirrored"][0]["node_uid"] == "node-a"
    assert commands[0][:15] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/tmp/cnc-ssh-known-hosts",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=5m",
        "-o",
        "ControlPath=/tmp/cnc-ssh-%C",
    ]
    assert commands[1][0] == "scp"
    assert (
        "root@100.64.0.19:/var/lib/cnc/backend-backup-mirrors/static-site/backup.tar.gz"
        in commands[1]
    )
    assert (
        "sha256sum /var/lib/cnc/backend-backup-mirrors/static-site/backup.tar.gz"
        in commands[2][-1]
    )
