from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ClusterJoinToken, ClusterNodeLatencySample
from app.services import cluster_nodes


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_join_command_creates_one_use_token(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        cluster_join_tailnet_base_url="https://leader.tailnet.ts.net",
        github_readonly_pat="github_pat_test",
    )

    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="http://127.0.0.1:19090",
        )

    assert "https://leader.tailnet.ts.net/join/install.sh" in join.command
    assert "CNC_JOIN_TOKEN=" in join.command
    assert "GITHUB_READONLY_PAT" not in join.command
    assert join.base_url == "https://leader.tailnet.ts.net"


@pytest.mark.asyncio
async def test_join_command_uses_current_tailscale_admin_host(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings()
    monkeypatch.setattr(
        cluster_nodes,
        "current_tailscale_admin_hostnames",
        lambda _settings: ("leader.tailnet.ts.net",),
    )

    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="http://127.0.0.1:19090",
        )

    assert join.base_url == "https://leader.tailnet.ts.net"


@pytest.mark.asyncio
async def test_join_command_rejects_public_base_url(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings()

    async with maker() as session:
        with pytest.raises(ValueError, match="Tailscale admin URL"):
            await cluster_nodes.create_join_command(
                session,
                settings,
                request_base_url="https://public.example.com",
            )


@pytest.mark.asyncio
async def test_register_and_confirm_node_records_tailnet_peer(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(cluster_join_tailnet_base_url="https://leader.tailnet.ts.net")
    monkeypatch.setattr(
        cluster_nodes, "_leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )

    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="https://leader.tailnet.ts.net",
        )
        registered = await cluster_nodes.register_node(
            session,
            settings,
            token=join.token,
            request_host="leader.tailnet.ts.net",
            payload={
                "node_uid": "node-a",
                "name": "node-a.example",
                "tailnet_ip": "100.64.0.19",
                "ram_bytes": 1024,
                "cpu_count": 2,
                "disk_bytes": 4096,
                "version": "bootstrap",
            },
        )
        confirmed = await cluster_nodes.confirm_node(
            session,
            token=join.token,
            payload={
                "node_uid": "node-a",
                "tailnet_ip": "100.64.0.19",
                "tailnet_ok": True,
                "latency_ms": 3.4,
                "version": "v0.1.67 (05a7cf3)",
            },
        )
        summaries = await cluster_nodes.cluster_latency_summaries(
            session,
            ["node-a"],
            since=confirmed.last_seen_at,
        )

    assert registered.node.tailnet_ip == "100.64.0.19"
    assert registered.leader_tailnet_ip == "100.64.0.10"
    assert confirmed.state == "healthy"
    assert confirmed.latency_ms == 3.4
    assert confirmed.version == "v0.1.67 (05a7cf3)"
    assert summaries["node-a"].avg_ms == 3.4
    assert summaries["node-a"].p95_ms == 3.4
    assert summaries["node-a"].p99_ms == 3.4


@pytest.mark.asyncio
async def test_latency_summary_reports_nearest_rank_percentiles(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")

    async with maker() as session:
        for latency_ms in [1.0, 2.0, 3.0, 100.0]:
            session.add(
                ClusterNodeLatencySample(
                    node_uid="node-a",
                    latency_ms=latency_ms,
                    recorded_at=cluster_nodes.datetime.now(cluster_nodes.UTC),
                )
            )
        await session.commit()
        summaries = await cluster_nodes.cluster_latency_summaries(
            session,
            ["node-a"],
            since=cluster_nodes.datetime.now(cluster_nodes.UTC)
            - cluster_nodes.timedelta(minutes=1),
        )

    assert round(summaries["node-a"].avg_ms, 2) == 26.5
    assert summaries["node-a"].p95_ms == 100.0
    assert summaries["node-a"].p99_ms == 100.0


@pytest.mark.asyncio
async def test_remove_node_severs_local_trust(monkeypatch, tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(cluster_join_tailnet_base_url="https://leader.tailnet.ts.net")
    monkeypatch.setattr(
        cluster_nodes, "_leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )

    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="https://leader.tailnet.ts.net",
        )
        await cluster_nodes.register_node(
            session,
            settings,
            token=join.token,
            request_host="leader.tailnet.ts.net",
            payload={
                "node_uid": "node-a",
                "name": "node-a.example",
                "tailnet_ip": "100.64.0.19",
            },
        )
        removed = await cluster_nodes.remove_node(
            session,
            settings,
            node_uid="node-a",
            request_host="leader.tailnet.ts.net",
        )
        nodes = await cluster_nodes.list_cluster_nodes(session)

    assert removed.state == "removed"
    assert removed.removed_at is not None
    assert nodes == []


@pytest.mark.asyncio
async def test_join_token_cannot_be_registered_twice(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(cluster_join_tailnet_base_url="https://leader.tailnet.ts.net")
    monkeypatch.setattr(
        cluster_nodes, "_leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )

    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="https://leader.tailnet.ts.net",
        )
        await cluster_nodes.register_node(
            session,
            settings,
            token=join.token,
            request_host="leader.tailnet.ts.net",
            payload={
                "node_uid": "node-a",
                "name": "node-a.example",
                "tailnet_ip": "100.64.0.19",
            },
        )
        with pytest.raises(ValueError, match="already been used"):
            await cluster_nodes.register_node(
                session,
                settings,
                token=join.token,
                request_host="leader.tailnet.ts.net",
                payload={
                    "node_uid": "node-b",
                    "name": "node-b.example",
                    "tailnet_ip": "100.64.0.20",
                },
            )


@pytest.mark.asyncio
async def test_join_token_is_claimed_atomically_by_one_concurrent_registration(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(cluster_join_tailnet_base_url="https://leader.tailnet.ts.net")
    monkeypatch.setattr(
        cluster_nodes, "_leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )
    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="https://leader.tailnet.ts.net",
        )

    async def register(node_uid: str, tailnet_ip: str):
        async with maker() as session:
            return await cluster_nodes.register_node(
                session,
                settings,
                token=join.token,
                request_host="leader.tailnet.ts.net",
                payload={
                    "node_uid": node_uid,
                    "name": f"{node_uid}.example",
                    "tailnet_ip": tailnet_ip,
                },
            )

    results = await asyncio.gather(
        register("node-a", "100.64.0.19"),
        register("node-b", "100.64.0.20"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 1
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(failures) == 1
    assert "already been used" in str(failures[0])


@pytest.mark.asyncio
async def test_failed_registration_rolls_back_atomic_token_claim(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(cluster_join_tailnet_base_url="https://leader.tailnet.ts.net")
    monkeypatch.setattr(
        cluster_nodes, "_leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )
    async with maker() as session:
        join = await cluster_nodes.create_join_command(
            session,
            settings,
            request_base_url="https://leader.tailnet.ts.net",
        )

    async with maker() as session:

        async def fail_flush() -> None:
            raise RuntimeError("enrollment failed")

        monkeypatch.setattr(session, "flush", fail_flush)
        with pytest.raises(RuntimeError, match="enrollment failed"):
            await cluster_nodes.register_node(
                session,
                settings,
                token=join.token,
                request_host="leader.tailnet.ts.net",
                payload={
                    "node_uid": "node-a",
                    "name": "node-a.example",
                    "tailnet_ip": "100.64.0.19",
                },
            )
        await session.rollback()

    async with maker() as session:
        token_row = (await session.execute(select(ClusterJoinToken))).scalar_one()
        assert token_row.used_at is None
        assert token_row.node_uid is None


def test_join_install_script_uses_tailscale_heartbeat_and_confirm_endpoint() -> None:
    script = cluster_nodes.install_script("v0.1.67 (05a7cf3)")

    assert "apt-get -qq install -y" in script
    assert "tailscale ip -4" in script
    assert "wireguard" not in script.lower()
    assert "/join/register" in script
    assert "/join/confirm" in script
    assert "cnc-node-heartbeat.timer" in script
    assert "cnc_version='v0.1.67 (05a7cf3)'" in script
    assert "tailnet_ok" in script
    assert 'split($1, parts, "/"); print parts[2]' in script
