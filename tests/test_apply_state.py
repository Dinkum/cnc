import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, ClusterNode, Input
from app.services.apply_core import ApplyFailed
from app.services.apply_state import build_desired_state
from app.services.replica_readiness import (
    record_replica_readiness,
    replica_target_url,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _settings(tmp_path: Path) -> Settings:
    resolv_path = tmp_path / "resolv.conf"
    resolv_path.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_container_resolv_conf_path=resolv_path,
        port_range_start=12000,
        port_range_end=12999,
    )


async def _mark_replica_ready(
    session,
    settings: Settings,
    *,
    backend: Backend,
    node: ClusterNode,
    setup_mode: str = "clone",
) -> None:
    await record_replica_readiness(
        session,
        settings,
        backend=backend,
        node_uid=node.node_uid,
        setup_mode=setup_mode,
        target_url=replica_target_url(backend, node, node.node_uid),
        healthcheck_result=f"tcp reachable at {node.tailnet_ip}:{backend.port}",
    )
    await session.commit()


@pytest.mark.asyncio
async def test_build_desired_state_includes_declarative_runtime_graph(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            internal_port=8337,
            sandbox_profile="ubuntu-24.04-systemd",
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            volumes_json=json.dumps([f"{tmp_path / 'web-data'}:/srv/web"]),
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        session.add(backend)
        await session.commit()

        desired = await build_desired_state(session, settings)

    graph = desired.runtime_graph
    assert graph.known_app_backend_names == {"web"}
    assert graph.enabled_app_backend_names == {"web"}
    node = graph.app_backends["web"]
    assert node.container == "cnc-app-web"
    assert node.network == "cnc-net-web"
    assert node.port == 12001
    assert node.handoff_port == 8337
    assert node.healthcheck_mode == "http"
    assert node.healthcheck_path == "/health"
    assert node.healthcheck_host_header == "web.example.com"
    assert node.volumes == (f"{tmp_path / 'web-data'}:/srv/web",)
    assert graph.as_dict()["app_backends"]["web"]["publish"] == {
        "host_ip": "127.0.0.1",
        "host_port": 12001,
        "container_port": 8337,
        "protocol": "tcp",
    }


@pytest.mark.asyncio
async def test_build_desired_state_skips_flush_when_ports_are_already_allocated(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            internal_port=8337,
            sandbox_profile="ubuntu-24.04-systemd",
            enabled=True,
            resource_mode="auto",
            resource_size="small",
        )
        session.add(backend)
        await session.commit()
        backend.resource_size = "medium"
        flushes = 0

        async def count_flush(*_args, **_kwargs):
            nonlocal flushes
            flushes += 1

        monkeypatch.setattr(session, "flush", count_flush)
        with session.no_autoflush:
            desired = await build_desired_state(session, settings)

    assert flushes == 0
    assert desired.runtime_graph.app_backends["web"].resource_size == "medium"


@pytest.mark.asyncio
async def test_build_desired_state_includes_inter_app_interfaces(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        source = Backend(
            id=1,
            name="web",
            kind="app",
            port=12001,
            handoff_port=8337,
            sandbox_profile="ubuntu-24.04-systemd",
            inter_app_interfaces_json=json.dumps(
                [{"name": "cnc0", "target_backend_id": 2, "status": "accepted"}]
            ),
            placement_mode="failover",
            placement_active_node_uid="local",
            placement_node_uids_json='["local","node-a"]',
            volumes_json="[]",
            enabled=True,
        )
        target = Backend(
            id=2,
            name="api",
            kind="app",
            port=12002,
            handoff_port=9000,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_mode="failover",
            placement_active_node_uid="local",
            placement_node_uids_json='["local","node-a"]',
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.10",
        )
        session.add_all([source, target, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=source, node=node)
        await _mark_replica_ready(session, settings, backend=target, node=node)

        desired = await build_desired_state(session, settings)

    graph = desired.runtime_graph
    assert [item.network for item in graph.inter_app_interfaces] == ["cnc-if-web-cnc0"]
    assert graph.app_backends["web"].inter_app_interfaces == (
        {
            "name": "cnc0",
            "target_backend": "api",
            "target_handoff_port": 9000,
            "env_key": "CNC_INTERFACE_CNC0_URL",
            "url": "http://cnc0:9000",
            "network": "cnc-if-web-cnc0",
            "direction": "out",
        },
    )
    assert graph.app_backends["api"].interface_networks == (
        {
            "network": "cnc-if-web-cnc0",
            "source_backend": "web",
            "target_backend": "api",
            "alias": "cnc0",
            "role": "target",
            "direction": "out",
        },
    )


@pytest.mark.asyncio
async def test_build_desired_state_waits_for_interface_confirmation(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        source = Backend(
            id=1,
            name="web",
            kind="app",
            port=12001,
            handoff_port=8337,
            sandbox_profile="ubuntu-24.04-systemd",
            inter_app_interfaces_json=json.dumps(
                [{"name": "cnc0", "target_backend_id": 2, "status": "pending"}]
            ),
            placement_mode="failover",
            placement_active_node_uid="local",
            placement_node_uids_json='["local","node-a"]',
            volumes_json="[]",
            enabled=True,
        )
        target = Backend(
            id=2,
            name="api",
            kind="app",
            port=12002,
            handoff_port=9000,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_mode="failover",
            placement_active_node_uid="local",
            placement_node_uids_json='["local","node-a"]',
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="node-a",
            role="follower",
            state="healthy",
            wireguard_ip="",
            wireguard_public_key="",
            tailnet_ip="100.64.0.10",
        )
        session.add_all([source, target, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=source, node=node)
        await _mark_replica_ready(session, settings, backend=target, node=node)

        desired = await build_desired_state(session, settings)

    assert desired.runtime_graph.inter_app_interfaces == ()
    assert desired.runtime_graph.app_backends["web"].inter_app_interfaces == ()
    assert desired.runtime_graph.app_backends["api"].interface_networks == ()


@pytest.mark.asyncio
async def test_build_desired_state_routes_shield_input_to_shield_runtime(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        backend = Backend(
            name="shield",
            kind="shield",
            enabled=True,
            volumes_json="[]",
        )
        backend.inputs = [
            Input(kind="shield", hostname="shield.example.com", enabled=True)
        ]
        session.add(backend)
        await session.commit()

        desired = await build_desired_state(session, settings)

    assert desired.shield_required is True
    assert "cnc-host-shield-example-com.conf" in desired.nginx_files
    shield_nginx = desired.nginx_files["cnc-host-shield-example-com.conf"]
    assert "return 404;" in shield_nginx
    assert "proxy_pass" not in shield_nginx
    assert desired.route_contracts == [
        {
            "input_kind": "shield",
            "input_value": "shield.example.com",
            "enabled": True,
            "backend_names": ["shield"],
            "target": "shield.example.com -> Shield access gate",
        }
    ]


@pytest.mark.asyncio
async def test_build_desired_state_uses_input_shield_code_before_output_code(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        shield_backend = Backend(
            name="shield",
            kind="shield",
            enabled=True,
            volumes_json="[]",
        )
        shield_backend.inputs = [
            Input(kind="shield", hostname="shield.example.com", enabled=True)
        ]
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            shield_enabled=True,
            shield_code_hash="hmac-sha256:v1:output",
            volumes_json="[]",
            enabled=True,
        )
        input_a = Input(
            kind="domain",
            hostname="a.example.com",
            enabled=True,
            shield_enabled=True,
            shield_code_hash="hmac-sha256:v1:input-a",
        )
        input_b = Input(kind="domain", hostname="b.example.com", enabled=True)
        input_a.backends = [backend]
        input_b.backends = [backend]
        session.add_all([shield_backend, backend, input_a, input_b])
        await session.commit()

        desired = await build_desired_state(session, settings)

    input_a_key = f"input:{input_a.id}"
    assert desired.shield_required is True
    assert desired.shield_output_code_hashes[input_a_key] == "hmac-sha256:v1:input-a"
    assert desired.shield_output_code_hashes["web"] == "hmac-sha256:v1:output"
    input_a_nginx = desired.nginx_files["cnc-host-a-example-com.conf"]
    input_b_nginx = desired.nginx_files["cnc-host-b-example-com.conf"]
    assert f"proxy_set_header X-Shield-Output {input_a_key};" in input_a_nginx
    assert "proxy_set_header X-Shield-Output web;" not in input_a_nginx
    assert "proxy_set_header X-Shield-Output web;" in input_b_nginx


@pytest.mark.asyncio
async def test_build_desired_state_renders_follower_ingress_targets(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(
        update={"multi_node_enabled": True, "nginx_cloudflare_only": True}
    )
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )
    monkeypatch.setattr(
        "app.services.apply_state.current_tailscale_admin_hostnames",
        lambda _settings: ("leader.example.ts.net",),
    )

    async with maker() as session:
        leader_backend = Backend(
            name="leader-app",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        follower_backend = Backend(
            name="follower-app",
            kind="app",
            port=12002,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            volumes_json="[]",
            enabled=True,
        )
        leader_input = Input(kind="domain", hostname="leader.example.com", enabled=True)
        follower_input = Input(
            kind="domain", hostname="follower.example.com", enabled=True
        )
        leader_input.backends = [leader_backend]
        follower_input.backends = [follower_backend]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all(
            [leader_backend, follower_backend, leader_input, follower_input, node]
        )
        await session.commit()
        await _mark_replica_ready(
            session, settings, backend=follower_backend, node=node
        )

        desired = await build_desired_state(session, settings)

    assert desired.runtime_graph.known_app_backend_names == {"leader-app"}
    assert desired.cluster_nodes == (
        {
            "node_uid": "node-a",
            "name": "follower-a",
            "tailnet_ip": "100.64.0.19",
            "state": "healthy",
        },
    )
    follower_app_files = desired.cluster_node_app_nginx_files["node-a"]
    follower_admin_files = desired.cluster_node_admin_nginx_files["node-a"]
    assert "cnc-admin-proxy.conf" not in follower_app_files
    admin_proxy = follower_admin_files["cnc-admin-proxy.conf"]
    leader_route = follower_app_files["cnc-host-leader-example-com.conf"]
    follower_route = follower_app_files["cnc-host-follower-example-com.conf"]
    assert "listen 127.0.0.1:9091;" in admin_proxy
    assert "proxy_pass https://leader.example.ts.net;" in admin_proxy
    assert (
        "server_name 100.64.0.19 follower-a follower-a.example.ts.net;" in admin_proxy
    )
    assert "allow 100.64.0.0/10;" in admin_proxy
    assert "server 100.64.0.10:80;" in leader_route
    assert "server 100.64.0.19:12002;" in follower_route
    assert (
        "allow 100.64.0.19/32;"
        in desired.nginx_files["cnc-host-leader-example-com.conf"]
    )


@pytest.mark.asyncio
async def test_build_desired_state_renders_failover_backup_upstreams(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_mode="failover",
            placement_active_node_uid="local",
            placement_node_uids_json=json.dumps(["local", "node-a"]),
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=backend, node=node)

        desired = await build_desired_state(session, settings)

    nginx = desired.nginx_files["cnc-host-web-example-com.conf"]
    assert "server 127.0.0.1:12001;" in nginx
    assert "server 100.64.0.19:12001 backup;" in nginx


@pytest.mark.asyncio
async def test_build_desired_state_live_replicas_keep_local_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            placement_mode="live",
            placement_active_node_uid="node-a",
            placement_node_uids_json=json.dumps(["local", "node-a"]),
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=backend, node=node)

        desired = await build_desired_state(session, settings)

    nginx = desired.nginx_files["cnc-host-web-example-com.conf"]
    assert desired.runtime_graph.known_app_backend_names == {"web"}
    assert "server 127.0.0.1:12001;" in nginx
    assert "server 100.64.0.19:12001;" in nginx


@pytest.mark.asyncio
async def test_build_desired_state_rejects_remote_placement_without_readiness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()

        with pytest.raises(ApplyFailed) as exc_info:
            await build_desired_state(session, settings)

    assert exc_info.value.phase == "validate_replica_readiness"
    assert "replica readiness is missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_build_desired_state_rejects_stale_replica_readiness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            handoff_port=8337,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=backend, node=node)
        backend.handoff_port = 9000
        await session.commit()

        with pytest.raises(ApplyFailed) as exc_info:
            await build_desired_state(session, settings)

    assert exc_info.value.phase == "validate_replica_readiness"
    assert "stale for this runtime contract" in str(exc_info.value)


@pytest.mark.asyncio
async def test_build_desired_state_rejects_unhealthy_ready_replica(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": True})
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip",
        lambda _settings: "100.64.0.10",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=backend, node=node)
        node.state = "offline"
        await session.commit()

        with pytest.raises(ApplyFailed) as exc_info:
            await build_desired_state(session, settings)

    assert exc_info.value.phase == "validate_replica_readiness"
    assert "node state is offline" in str(exc_info.value)


@pytest.mark.asyncio
async def test_build_desired_state_ignores_replica_members_when_multi_node_disabled(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(update={"multi_node_enabled": False})

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid="node-a",
            placement_mode="live",
            placement_active_node_uid="node-a",
            placement_node_uids_json=json.dumps(["local", "node-a"]),
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()

        desired = await build_desired_state(session, settings)

    nginx = desired.nginx_files["cnc-host-web-example-com.conf"]
    assert desired.runtime_graph.known_app_backend_names == set()
    assert "server 100.64.0.19:12001;" in nginx
    assert "server 127.0.0.1:12001;" not in nginx
