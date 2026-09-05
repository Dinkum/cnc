from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, BackendReplicaReadiness, ClusterNode
from app.services import cluster_transfer
from app.services.commands import CommandResult


async def _make_session(db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_scp_checked_accepts_new_cluster_host_keys(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_transfer, "run_command", fake_run_command)

    cluster_transfer._scp_checked(
        "/tmp/source.tar.gz",
        "root@100.64.0.19:/tmp/source.tar.gz",
        Settings(command_timeout_apply_sec=42),
    )

    assert commands == [
        [
            "scp",
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
            "/tmp/source.tar.gz",
            "root@100.64.0.19:/tmp/source.tar.gz",
        ]
    ]


def test_remote_restore_starts_generated_quadlet_units_without_enable() -> None:
    backend = Backend(
        name="demo",
        kind="app",
        port=12003,
        handoff_port=3000,
        sandbox_profile="ubuntu-24.04-systemd",
        volumes_json="[]",
    )
    node = ClusterNode(
        node_uid="node-a",
        name="node-a",
        tailnet_ip="100.64.0.19",
    )

    script = cluster_transfer._remote_restore_script(
        backend,
        Settings(),
        node,
        "/var/lib/cnc/cluster-transfers/demo.tar.gz",
        rollback=cluster_transfer.TargetRollbackState(
            target_uid="node-a",
            target_path="/var/lib/cnc/sandboxes/demo",
            rollback_path="/var/lib/cnc/sandboxes/.cnc-transfer-demo-rollback",
            unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-transfer-demo-units",
        ),
    )

    assert "systemctl start cnc-net-demo-network.service" in script
    assert "systemctl start cnc-app-demo.service" in script
    assert "systemctl enable --now" not in script
    assert "restore_previous_target()" in script
    assert 'if [ "$status" -ne 0 ]; then' in script
    assert "activated=1\ncat > /etc/containers/systemd/cnc-net-demo.network" in script
    assert 'rm -rf "$rollback_dir"\ncat >' not in script


def test_remote_fresh_setup_keeps_previous_sandbox_until_success() -> None:
    backend = Backend(
        name="demo",
        kind="app",
        port=12003,
        handoff_port=3000,
        sandbox_profile="ubuntu-24.04-systemd",
        volumes_json="[]",
    )
    node = ClusterNode(
        node_uid="node-a",
        name="node-a",
        tailnet_ip="100.64.0.19",
    )

    script = cluster_transfer._remote_fresh_setup_script(
        backend,
        Settings(),
        node,
        rollback=cluster_transfer.TargetRollbackState(
            target_uid="node-a",
            target_path="/var/lib/cnc/sandboxes/demo",
            rollback_path="/var/lib/cnc/sandboxes/.cnc-fresh-demo-rollback",
            unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-fresh-demo-units",
        ),
    )

    assert 'stage_sandbox="$stage_dir/demo"' in script
    assert 'mv "$target_sandbox" "$rollback_dir"' in script
    assert "restore_previous_target()" in script
    assert "activated=1\ncat > /etc/containers/systemd/cnc-net-demo.network" in script
    assert 'rm -rf "$rollback_dir"\ncat >' not in script


def test_remote_cleanup_removes_sandbox_and_quadlet_assets(monkeypatch) -> None:
    backend = Backend(name="demo", kind="app")
    node = ClusterNode(
        node_uid="node-a",
        name="node-a",
        tailnet_ip="100.64.0.19",
    )
    scripts: list[str] = []

    def fake_ssh_checked(_node, _settings, script: str) -> None:
        scripts.append(script)

    monkeypatch.setattr(cluster_transfer, "_ssh_checked", fake_ssh_checked)

    cluster_transfer._cleanup_remote_restored_target(backend, Settings(), node)

    script = scripts[0]
    assert "systemctl stop cnc-app-demo.service" in script
    assert "systemctl disable cnc-app-demo.service" in script
    assert "podman rm -f cnc-app-demo" in script
    assert "rm -rf /var/lib/cnc/sandboxes/demo" in script
    assert "rm -f /etc/containers/systemd/cnc-net-demo.network" in script
    assert "rm -f /etc/containers/systemd/cnc-app-demo.container" in script
    assert "systemctl daemon-reload" in script


def test_verify_tcp_waits_for_late_port(monkeypatch) -> None:
    attempts = []
    now = {"value": 100.0}

    def fake_monotonic() -> float:
        return now["value"]

    def fake_sleep(seconds: int) -> None:
        now["value"] += seconds

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        attempts.append(command)
        if len(attempts) < 3:
            return CommandResult(
                command=command, returncode=1, stdout="", stderr="Connection refused"
            )
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_transfer.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(cluster_transfer.time, "sleep", fake_sleep)
    monkeypatch.setattr(cluster_transfer, "run_command", fake_run_command)

    cluster_transfer._verify_tcp(
        "100.64.0.19", 12003, Settings(command_timeout_apply_sec=15)
    )

    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_remote_to_leader_transfer_verifies_leader_before_remote_source_cleanup(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    events: list[str] = []
    apply_autoflush: list[bool] = []
    apply_entity_placements: list[str | None] = []
    apply_database_placements: list[str | None] = []

    class _ApplySuccess:
        status = "success"
        message = "ok"

    async def fake_run_apply_success(_session, _settings, **_kwargs):
        events.append("apply")
        apply_autoflush.append(_session.autoflush)
        entity = (
            await _session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()
        apply_entity_placements.append(entity.placement_node_uid)
        apply_database_placements.append(
            (
                await _session.execute(
                    select(Backend.placement_node_uid).where(Backend.id == backend_id)
                )
            ).scalar_one()
        )
        return _ApplySuccess()

    def fake_archive_remote(*_args, **_kwargs):
        events.append("archive_remote")

    def fake_restore_local(*_args, **_kwargs):
        events.append("restore_local")

    def fake_verify_local(*_args, **_kwargs):
        events.append("verify_local")
        return "tcp reachable at 127.0.0.1:12003"

    def fake_cleanup_remote(*_args, **_kwargs):
        events.append("cleanup_remote")

    def fake_start_local(*_args, **_kwargs):
        events.append("start_local")

    monkeypatch.setattr(cluster_transfer, "run_apply", fake_run_apply_success)
    monkeypatch.setattr(
        cluster_transfer, "_archive_remote_backend", fake_archive_remote
    )
    monkeypatch.setattr(cluster_transfer, "_restore_local_backend", fake_restore_local)
    monkeypatch.setattr(
        cluster_transfer, "_start_local_backend_replica", fake_start_local
    )
    monkeypatch.setattr(cluster_transfer, "_verify_local_backend", fake_verify_local)
    monkeypatch.setattr(
        cluster_transfer, "_cleanup_remote_restored_target", fake_cleanup_remote
    )

    async with maker() as session:
        source = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
            placement_node_uid="node-a",
        )
        session.add_all([source, backend])
        await session.commit()
        backend_id = backend.id

        await cluster_transfer.transfer_backend(
            session,
            settings,
            backend_id=1,
            target_node_uid=cluster_transfer.LOCAL_NODE_UID,
        )

    assert (
        events.index("start_local")
        < events.index("verify_local")
        < events.index("apply")
    )
    assert events.index("verify_local") < events.index("cleanup_remote")
    assert apply_autoflush == [False]
    assert apply_entity_placements == [None]
    assert apply_database_placements == ["node-a"]


@pytest.mark.asyncio
async def test_remote_to_leader_transfer_keeps_remote_source_when_leader_verify_fails(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    events: list[str] = []

    class _ApplySuccess:
        status = "success"
        message = "ok"

    async def fake_run_apply_success(_session, _settings, **_kwargs):
        return _ApplySuccess()

    def fake_noop(*_args, **_kwargs):
        return None

    def fake_verify_local(*_args, **_kwargs):
        events.append("verify_local")
        raise RuntimeError("leader port unavailable")

    def fake_cleanup_remote(*_args, **_kwargs):
        events.append("cleanup_remote")

    def fake_start_local(*_args, **_kwargs):
        events.append("start_local")

    monkeypatch.setattr(cluster_transfer, "run_apply", fake_run_apply_success)
    monkeypatch.setattr(cluster_transfer, "_archive_remote_backend", fake_noop)
    monkeypatch.setattr(cluster_transfer, "_restore_local_backend", fake_noop)
    monkeypatch.setattr(
        cluster_transfer, "_start_local_backend_replica", fake_start_local
    )
    monkeypatch.setattr(cluster_transfer, "_verify_local_backend", fake_verify_local)
    monkeypatch.setattr(
        cluster_transfer, "_cleanup_remote_restored_target", fake_cleanup_remote
    )

    async with maker() as session:
        source = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
            placement_node_uid="node-a",
        )
        session.add_all([source, backend])
        await session.commit()

        with pytest.raises(RuntimeError, match="leader port unavailable"):
            await cluster_transfer.transfer_backend(
                session,
                settings,
                backend_id=1,
                target_node_uid=cluster_transfer.LOCAL_NODE_UID,
            )

    assert events == ["start_local", "verify_local"]


@pytest.mark.asyncio
async def test_leader_to_remote_transfer_records_readiness_before_apply(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    events: list[str] = []

    class _ApplySuccess:
        status = "success"
        message = "ok"

    async def fake_run_apply_success(session, _settings, **_kwargs):
        events.append("apply")
        readiness = (
            await session.execute(select(BackendReplicaReadiness))
        ).scalar_one()
        stored_backend = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()
        assert readiness.node_uid == "node-a"
        assert readiness.target_url == "http://100.64.0.19:12003"
        assert stored_backend.placement_node_uid == "node-a"
        assert stored_backend.placement_mode == "single"
        assert stored_backend.placement_active_node_uid == "node-a"
        assert stored_backend.placement_node_uids_json == "[]"
        return _ApplySuccess()

    def fake_archive_local(*_args, **_kwargs):
        events.append("archive_local")

    def fake_restore_remote(*_args, **_kwargs):
        events.append("restore_remote")
        return cluster_transfer.TargetRollbackState(
            target_uid="node-a",
            target_path="/var/lib/cnc/sandboxes/demo",
            rollback_path="/var/lib/cnc/sandboxes/.cnc-transfer-demo-rollback",
            unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-transfer-demo-units",
        )

    def fake_verify_remote(*_args, **_kwargs):
        events.append("verify_remote")
        return "tcp reachable at 100.64.0.19:12003"

    async def fake_discard(*_args, **_kwargs):
        events.append("discard_rollback")

    monkeypatch.setattr(cluster_transfer, "run_apply", fake_run_apply_success)
    monkeypatch.setattr(cluster_transfer, "_archive_local_backend", fake_archive_local)
    monkeypatch.setattr(
        cluster_transfer, "_restore_remote_backend", fake_restore_remote
    )
    monkeypatch.setattr(cluster_transfer, "_verify_remote_backend", fake_verify_remote)
    monkeypatch.setattr(cluster_transfer, "_discard_target_rollback", fake_discard)

    async with maker() as session:
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
            placement_mode="live",
            placement_active_node_uid=cluster_transfer.LOCAL_NODE_UID,
            placement_node_uids_json='["local","node-a"]',
        )
        session.add_all([node, backend])
        await session.commit()
        backend_id = backend.id

        result = await cluster_transfer.transfer_backend(
            session,
            settings,
            backend_id=backend.id,
            target_node_uid="node-a",
        )

        readiness = (
            await session.execute(select(BackendReplicaReadiness))
        ).scalar_one()

    assert events == [
        "archive_local",
        "restore_remote",
        "verify_remote",
        "apply",
        "discard_rollback",
    ]
    assert result.target_node_uid == "node-a"
    assert readiness.setup_mode == cluster_transfer.REPLICA_SETUP_MODE_TRANSFER
    assert readiness.healthcheck_result == "tcp reachable at 100.64.0.19:12003"


@pytest.mark.asyncio
async def test_transfer_cleans_prior_remote_replicas_and_invalidates_readiness(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    cleaned_nodes: list[str] = []

    class _ApplySuccess:
        status = "success"
        message = "ok"

    async def fake_run_apply_success(*_args, **_kwargs):
        return _ApplySuccess()

    def fake_archive_local(*_args, **_kwargs):
        return None

    def fake_restore_remote(*_args, **_kwargs):
        return cluster_transfer.TargetRollbackState(
            target_uid="node-c",
            target_path="/var/lib/cnc/sandboxes/demo",
            rollback_path="/var/lib/cnc/sandboxes/.cnc-transfer-demo-rollback",
            unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-transfer-demo-units",
        )

    def fake_verify_remote(*_args, **_kwargs):
        return "tcp reachable at 100.64.0.21:12003"

    def fake_cleanup_remote(_backend, _settings, node):
        cleaned_nodes.append(node.node_uid)

    async def fake_discard(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cluster_transfer, "run_apply", fake_run_apply_success)
    monkeypatch.setattr(cluster_transfer, "_archive_local_backend", fake_archive_local)
    monkeypatch.setattr(
        cluster_transfer, "_restore_remote_backend", fake_restore_remote
    )
    monkeypatch.setattr(cluster_transfer, "_verify_remote_backend", fake_verify_remote)
    monkeypatch.setattr(
        cluster_transfer, "_cleanup_remote_restored_target", fake_cleanup_remote
    )
    monkeypatch.setattr(cluster_transfer, "_discard_target_rollback", fake_discard)

    async with maker() as session:
        nodes = [
            ClusterNode(
                node_uid=node_uid,
                name=node_uid,
                role="follower",
                state="healthy",
                wireguard_ip="",
                wireguard_public_key="",
                tailnet_ip=tailnet_ip,
            )
            for node_uid, tailnet_ip in (
                ("node-a", "100.64.0.19"),
                ("node-b", "100.64.0.20"),
                ("node-c", "100.64.0.21"),
            )
        ]
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
            placement_mode="live",
            placement_active_node_uid=cluster_transfer.LOCAL_NODE_UID,
            placement_node_uids_json='["local","node-a","node-missing","node-b"]',
        )
        session.add_all([*nodes, backend])
        await session.flush()
        verified_at = datetime.now(UTC)
        session.add_all(
            [
                BackendReplicaReadiness(
                    backend_id=backend.id,
                    node_uid=node_uid,
                    setup_mode="clone",
                    runtime_contract_hash="old",
                    target_url=f"http://{node_uid}:12003",
                    healthcheck_result="previously reachable",
                    last_verified_at=verified_at,
                )
                for node_uid in ("node-a", "node-missing", "node-b")
            ]
        )
        await session.commit()

        result = await cluster_transfer.transfer_backend(
            session,
            settings,
            backend_id=backend.id,
            target_node_uid="node-c",
        )

        readiness_rows = (
            (
                await session.execute(
                    select(BackendReplicaReadiness.node_uid).order_by(
                        BackendReplicaReadiness.node_uid.asc()
                    )
                )
            )
            .scalars()
            .all()
        )

    assert cleaned_nodes == ["node-a", "node-b"]
    assert readiness_rows == ["node-c", "node-missing"]
    assert any(
        "source cleanup warning on node-missing: node is not linked" in message
        for message in result.messages
    )


@pytest.mark.asyncio
async def test_setup_backend_replica_records_remote_readiness(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )

    def fake_archive_local(*_args, **_kwargs):
        return None

    def fake_restore_remote(*_args, **_kwargs):
        return None

    def fake_verify_remote(*_args, **_kwargs):
        return "tcp reachable at 100.64.0.19:12003"

    monkeypatch.setattr(cluster_transfer, "_archive_local_backend", fake_archive_local)
    monkeypatch.setattr(
        cluster_transfer, "_restore_remote_backend", fake_restore_remote
    )
    monkeypatch.setattr(cluster_transfer, "_verify_remote_backend", fake_verify_remote)

    async with maker() as session:
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([node, backend])
        await session.commit()

        result = await cluster_transfer.setup_backend_replica(
            session,
            settings,
            backend_id=backend.id,
            target_node_uid="node-a",
            setup_mode=cluster_transfer.REPLICA_SETUP_MODE_CLONE,
        )

        readiness = (
            await session.execute(select(BackendReplicaReadiness))
        ).scalar_one()

    assert result.target_node_uid == "node-a"
    assert readiness.backend_id == backend.id
    assert readiness.node_uid == "node-a"
    assert readiness.setup_mode == cluster_transfer.REPLICA_SETUP_MODE_CLONE
    assert readiness.target_url == "http://100.64.0.19:12003"
    assert readiness.healthcheck_result == "tcp reachable at 100.64.0.19:12003"
    assert readiness.runtime_contract_hash


@pytest.mark.asyncio
async def test_leader_to_remote_transfer_restores_target_rollback_when_verify_fails(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    events: list[str] = []
    rollback = cluster_transfer.TargetRollbackState(
        target_uid="node-a",
        target_path="/var/lib/cnc/sandboxes/demo",
        rollback_path="/var/lib/cnc/sandboxes/.cnc-transfer-demo-rollback",
        unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-transfer-demo-units",
    )

    async def fake_run_apply_success(*_args, **_kwargs):
        events.append("apply")

    def fake_archive_local(*_args, **_kwargs):
        events.append("archive_local")

    def fake_restore_remote(*_args, **_kwargs):
        events.append("restore_remote")
        return rollback

    def fake_verify_remote(*_args, **_kwargs):
        events.append("verify_remote")
        raise RuntimeError("target port unavailable")

    async def fake_restore_rollback(*_args, **_kwargs):
        events.append("restore_rollback")
        return ["remote target rollback restored on node-a"]

    monkeypatch.setattr(cluster_transfer, "run_apply", fake_run_apply_success)
    monkeypatch.setattr(cluster_transfer, "_archive_local_backend", fake_archive_local)
    monkeypatch.setattr(
        cluster_transfer, "_restore_remote_backend", fake_restore_remote
    )
    monkeypatch.setattr(cluster_transfer, "_verify_remote_backend", fake_verify_remote)
    monkeypatch.setattr(
        cluster_transfer, "_restore_target_rollback", fake_restore_rollback
    )

    async with maker() as session:
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([node, backend])
        await session.commit()

        with pytest.raises(RuntimeError, match="restored target rollback"):
            await cluster_transfer.transfer_backend(
                session,
                settings,
                backend_id=backend.id,
                target_node_uid="node-a",
            )

    assert events == [
        "archive_local",
        "restore_remote",
        "verify_remote",
        "restore_rollback",
    ]


@pytest.mark.asyncio
async def test_fresh_replica_setup_restores_target_rollback_when_verify_fails(
    monkeypatch, tmp_path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    events: list[str] = []
    rollback = cluster_transfer.TargetRollbackState(
        target_uid="node-a",
        target_path="/var/lib/cnc/sandboxes/demo",
        rollback_path="/var/lib/cnc/sandboxes/.cnc-fresh-demo-rollback",
        unit_backup_path="/var/lib/cnc/cluster-transfers/.cnc-fresh-demo-units",
    )

    def fake_setup_fresh(*_args, **_kwargs):
        events.append("setup_fresh")
        return rollback

    def fake_verify_remote(*_args, **_kwargs):
        events.append("verify_remote")
        raise RuntimeError("fresh target unavailable")

    async def fake_restore_rollback(*_args, **_kwargs):
        events.append("restore_rollback")
        return ["remote target rollback restored on node-a"]

    monkeypatch.setattr(
        cluster_transfer, "_setup_remote_fresh_backend", fake_setup_fresh
    )
    monkeypatch.setattr(cluster_transfer, "_verify_remote_backend", fake_verify_remote)
    monkeypatch.setattr(
        cluster_transfer, "_restore_target_rollback", fake_restore_rollback
    )

    async with maker() as session:
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.19",
        )
        backend = Backend(
            name="demo",
            kind="app",
            port=12003,
            handoff_port=3000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([node, backend])
        await session.commit()

        with pytest.raises(RuntimeError, match="replica target rollback"):
            await cluster_transfer.setup_backend_replica(
                session,
                settings,
                backend_id=backend.id,
                target_node_uid="node-a",
                setup_mode=cluster_transfer.REPLICA_SETUP_MODE_FRESH,
            )

        readiness = (await session.execute(select(BackendReplicaReadiness))).all()

    assert readiness == []
    assert events == ["setup_fresh", "verify_remote", "restore_rollback"]
