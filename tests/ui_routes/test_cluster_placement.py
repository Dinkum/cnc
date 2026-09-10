import app.services.operations as app_services_operations
import app.ui.cluster as ui_cluster
import app.ui.dashboard.data as ui_dashboard_data
import app.ui.http as ui_http
import app.ui.outputs.context as ui_outputs_context
import app.ui.outputs.data as ui_outputs_data

from .support import (
    ApplyResponse,
    Backend,
    BackgroundTasks,
    ClusterNode,
    Operation,
    Path,
    Settings,
    SimpleNamespace,
    _healthy_status,
    _make_session,
    _mark_replica_ready,
    _PostedRequest,
    _request,
    json,
    output_save_operations,
    select,
    ui_backup,
    ui_output,
    ui_pages,
    ui_settings,
)


async def test_dashboard_non_settings_tabs_skip_cluster_nodes_when_multi_node_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        multi_node_enabled=True,
    )

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=0)

    async def fail_cluster_nodes(*_args, **_kwargs):
        raise AssertionError(
            "non-settings dashboard tabs should not build node summaries"
        )

    monkeypatch.setattr(ui_dashboard_data, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_cluster, "cluster_nodes", fail_cluster_nodes)

    async with maker() as session:
        for tab in ("home", "outputs"):
            response = await ui_pages.dashboard(
                _request(path=f"/?tab={tab}"), settings=settings, session=session
            )
            assert response.status_code == 200


async def test_update_node_settings_form_persists_multi_node_toggle(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("MULTI_NODE_ENABLED", raising=False)
    settings = Settings(managed_env_file_path=env_path)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)

    try:
        response = await ui_settings.update_node_settings_form(
            _PostedRequest(path="/ui/settings/nodes"),
            csrf_token="token",
            multi_node_enabled=True,
            settings=settings,
            session=object(),
        )

        next_settings = Settings(managed_env_file_path=env_path)

        assert response.status_code == 303
        assert response.headers["location"] == "/?tab=settings"
        assert next_settings.multi_node_enabled is True
        assert "MULTI_NODE_ENABLED=true" in env_path.read_text(encoding="utf-8")
        assert ui_http.FLASH_SUCCESS_COOKIE in response.headers["set-cookie"]
    finally:
        monkeypatch.delenv("MULTI_NODE_ENABLED", raising=False)


async def test_create_node_join_command_returns_expiring_command(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
        cluster_join_tailnet_base_url="https://leader.tailnet.ts.net",
        github_readonly_pat="github_pat_test",
    )
    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        response = await ui_settings.create_node_join_command(
            _PostedRequest(path="/ui/settings/nodes/join-command"),
            x_csrf_token="token",
            settings=settings,
            session=session,
        )

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert "https://leader.tailnet.ts.net/join/install.sh" in payload["command"]
    assert "CNC_JOIN_TOKEN=" in payload["command"]
    assert "GITHUB_READONLY_PAT" not in payload["command"]
    assert payload["ttl_sec"] == 900


async def test_remove_cluster_node_endpoint_severs_local_trust(monkeypatch) -> None:
    settings = Settings(multi_node_enabled=True)
    calls: dict[str, object] = {}

    async def fake_remove_node(session, received_settings, *, node_uid, request_host):
        calls.update(
            {
                "session": session,
                "settings": received_settings,
                "node_uid": node_uid,
                "request_host": request_host,
            }
        )
        return SimpleNamespace(node_uid=node_uid, state="removed")

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_settings, "remove_node", fake_remove_node)

    response = await ui_settings.remove_cluster_node(
        "node-a",
        _PostedRequest(
            path="/ui/settings/nodes/node-a/remove",
            headers=[(b"host", b"admin.example")],
        ),
        x_csrf_token="token",
        settings=settings,
        session=object(),
    )

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload == {"node_uid": "node-a", "state": "removed"}
    assert calls["node_uid"] == "node-a"
    assert calls["request_host"] == "admin.example"


async def test_output_page_context_includes_minimal_multi_node_placement(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_outputs_data, "peek_cached_status", lambda: {"services": []})

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        target_backend = Backend(
            name="api",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="remote-node",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, target_backend, node])
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session,
            settings,
            backend.id,
            prefer_cached_runtime=True,
        )

    placement = context["multi_node_placement"]
    assert placement["enabled"] is False
    assert placement["mode"] == "failover"
    assert placement["active_node_uid"] == "local"
    assert [node["display_role"] for node in placement["nodes"]] == [
        "primary",
        "follower",
    ]
    assert all(node["selected"] is False for node in placement["nodes"])
    assert [node["setup_ready"] for node in placement["nodes"]] == [True, False]
    assert placement["nodes"][1]["unavailable_reason"] == "replica readiness is missing"
    assert placement["interface_targets"] == [{"id": target_backend.id, "name": "api"}]
    assert placement["interfaces"] == []
    assert placement["inbound_interfaces"] == []
    assert placement["interface_statuses"] == [
        {"value": "pending", "label": "pending"},
        {"value": "accepted", "label": "confirm"},
        {"value": "rejected", "label": "reject"},
    ]


async def test_setup_backend_replica_form_queues_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="remote-node",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()

        response = await ui_backup.setup_backend_replica_form(
            backend.id,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/replica-setup",
                headers=[(b"accept", b"application/json")],
                fields={
                    "target_node": ["node-a"],
                    "setup_mode": ["clone"],
                },
            ),
            BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            target_node="node-a",
            setup_mode="clone",
            session=session,
        )

        operations = list((await session.execute(select(Operation))).scalars())

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert payload["operation_status"] == "queued"
    assert payload["message"] == "Replica setup started."
    assert operations[0].kind == "setup_backend_replica"
    assert operations[0].backend_id == backend.id
    assert json.loads(operations[0].details_json)["setup_mode"] == "clone"


async def test_update_backend_placement_form_saves_failover(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings, **_kwargs):
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=42,
        )

    monkeypatch.setattr(ui_backup, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        target_backend = Backend(
            name="api",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="remote-node",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, target_backend, node])
        await session.commit()
        await _mark_replica_ready(session, settings, backend=backend, node=node)

        response = await ui_backup.update_backend_placement_form(
            backend.id,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/placement",
                headers=[(b"accept", b"application/json")],
                fields={
                    "placement_enabled": ["true"],
                    "placement_mode": ["failover"],
                    "placement_active_node": ["node-a"],
                    "placement_node_uids": ["local", "node-a"],
                    "inter_app_interface_names": ["cnc0"],
                    "inter_app_interface_targets": [str(target_backend.id)],
                    "inter_app_interface_directions": ["out"],
                },
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one()

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["placement"] == {
        "enabled": True,
        "mode": "failover",
        "active_node_uid": "node-a",
        "selected_node_uids": ["local", "node-a"],
        "inter_app_interfaces": [
            {
                "name": "cnc0",
                "target_backend_id": target_backend.id,
                "status": "pending",
                "direction": "out",
            }
        ],
    }
    assert stored.placement_node_uid == "node-a"
    assert stored.placement_mode == "failover"
    assert stored.placement_active_node_uid == "node-a"
    assert json.loads(stored.placement_node_uids_json) == ["local", "node-a"]
    assert json.loads(stored.inter_app_interfaces_json) == [
        {
            "name": "cnc0",
            "target_backend_id": target_backend.id,
            "status": "pending",
            "direction": "out",
        }
    ]


async def test_update_backend_placement_form_rejects_active_host_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_active_host_mutation_blocker(_settings):
        return app_services_operations.HostMutationBlocker(
            id=7,
            kind="transfer_backend",
            status="running",
            phase="restore_target",
        )

    monkeypatch.setattr(
        ui_backup, "active_host_mutation_blocker", fake_active_host_mutation_blocker
    )

    async def fail_run_apply(*_args, **_kwargs):
        raise AssertionError("placement blocker should fail before apply")

    monkeypatch.setattr(ui_backup, "run_apply", fail_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        response = await ui_backup.update_backend_placement_form(
            backend.id,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/placement",
                headers=[(b"accept", b"application/json")],
                fields={
                    "placement_enabled": ["true"],
                    "placement_mode": ["failover"],
                    "placement_active_node": ["local"],
                    "placement_node_uids": ["local"],
                },
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )

    payload = json.loads(response.body)
    assert response.status_code == 409
    assert "Transfer Backend is already running" in payload["flash_error"]
    assert "Multi-node placement can't start" in payload["flash_error"]


async def test_update_backend_placement_form_rejects_missing_replica_readiness(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fail_run_apply(*_args, **_kwargs):
        raise AssertionError("placement validation should fail before apply")

    monkeypatch.setattr(ui_backup, "run_apply", fail_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            placement_node_uid=None,
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="remote-node",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, node])
        await session.commit()
        backend_id = backend.id

        response = await ui_backup.update_backend_placement_form(
            backend_id,
            _PostedRequest(
                path=f"/ui/backends/{backend_id}/placement",
                headers=[(b"accept", b"application/json")],
                fields={
                    "placement_enabled": ["true"],
                    "placement_mode": ["failover"],
                    "placement_active_node": ["node-a"],
                    "placement_node_uids": ["local", "node-a"],
                },
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()

    payload = json.loads(response.body)
    assert response.status_code == 400
    assert "replica readiness is missing" in payload["flash_error"]
    assert stored.placement_node_uid is None
    assert stored.placement_mode == "single"
    assert json.loads(stored.placement_node_uids_json) == []


async def test_update_backend_inbound_interface_form_confirms_interface(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_commit_and_apply(session, *_args, **_kwargs):
        await session.commit()
        return SimpleNamespace(
            apply_response=ApplyResponse(
                status="success",
                message="apply completed",
                details={},
                run_id=42,
            )
        )

    monkeypatch.setattr(
        output_save_operations, "commit_and_apply", fake_commit_and_apply
    )

    async with maker() as session:
        source_backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            inter_app_interfaces_json="[]",
            volumes_json="[]",
            enabled=True,
        )
        target_backend = Backend(
            name="api",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        node = ClusterNode(
            node_uid="node-a",
            name="remote-node",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([source_backend, target_backend, node])
        await session.flush()
        source_backend.inter_app_interfaces_json = json.dumps(
            [
                {
                    "name": "cnc0",
                    "target_backend_id": target_backend.id,
                    "status": "pending",
                    "direction": "out",
                }
            ]
        )
        await session.commit()
        source_backend_id = source_backend.id
        target_backend_id = target_backend.id

        background_tasks = BackgroundTasks()
        response = await ui_output.update_backend_inbound_interface_form(
            target_backend_id,
            _PostedRequest(
                path=f"/ui/backends/{target_backend_id}/interfaces/inbound",
                headers=[(b"accept", b"application/json")],
                fields={
                    "source_backend_id": [str(source_backend_id)],
                    "interface_name": ["cnc0"],
                    "status": ["accepted"],
                    "direction": ["out"],
                },
            ),
            background_tasks,
            settings=settings,
            csrf_token="token",
            source_backend_id=source_backend_id,
            interface_name="cnc0",
            status="accepted",
            direction="out",
            session=session,
        )
        await background_tasks()
        session.expire_all()

        stored_source = (
            await session.execute(
                select(Backend).where(Backend.id == source_backend_id)
            )
        ).scalar_one()

    assert response.status_code == 202
    payload = json.loads(response.body)
    assert isinstance(payload["operation_id"], int)
    assert json.loads(stored_source.inter_app_interfaces_json) == [
        {
            "name": "cnc0",
            "target_backend_id": target_backend_id,
            "status": "accepted",
            "direction": "out",
        }
    ]


async def test_update_backend_placement_form_rejects_unknown_node(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        response = await ui_backup.update_backend_placement_form(
            backend.id,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/placement",
                headers=[(b"accept", b"application/json")],
                fields={
                    "placement_enabled": ["true"],
                    "placement_mode": ["live"],
                    "placement_active_node": ["missing-node"],
                    "placement_node_uids": ["local", "missing-node"],
                },
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )

    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["flash_error"].startswith("unknown placement node: missing-node")
    assert " - Error CNC-01001-" in payload["flash_error"]
