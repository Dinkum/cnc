import app.ui.cluster as ui_cluster
import app.ui.dashboard.data as ui_dashboard_data
import app.ui.diagnostics as ui_diagnostics
import app.ui.http as ui_http
import app.ui.outputs.context as ui_outputs_context
import app.ui.outputs.data as ui_outputs_data

from .support import (
    Backend,
    ControlEvent,
    Input,
    Path,
    Settings,
    SimpleNamespace,
    _make_session,
    _request,
    asyncio,
    datetime,
    json,
    time,
    timedelta,
    ui_pages,
    ui_reads,
)


async def test_output_detail_uses_request_first_template_response(monkeypatch) -> None:
    captured: dict[str, object] = {}
    settings = Settings()
    context_kwargs: dict[str, object] = {}

    async def fake_output_context(
        _session, _settings, backend_id: int, **kwargs
    ) -> dict[str, object]:
        context_kwargs.update(kwargs)
        return {
            "selected_backend": SimpleNamespace(id=backend_id, name="web"),
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "flash_error": None,
            "flash_success": None,
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_pages, "output_page_context", fake_output_context)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    request = _request()
    response = await ui_pages.output_detail(
        7, request, settings=settings, session=object()
    )

    assert response.status_code == 200
    assert captured["request"] is request
    assert captured["name"] == "output_detail.html"
    assert captured["context"]["request"] is request
    assert captured["context"]["selected_backend"].id == 7
    assert context_kwargs["prefer_cached_runtime"] is True


async def test_missing_output_hydration_endpoints_return_404(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        responses = [
            await ui_reads.output_backup_signals(
                999, settings=settings, session=session
            ),
            await ui_reads.output_runtime_signals(
                999, settings=settings, session=session
            ),
            await ui_reads.output_metric_history(
                999, settings=settings, session=session
            ),
        ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert all(
        json.loads(response.body)["error"].startswith("output not found")
        for response in responses
    )


async def test_missing_output_detail_returns_fast_404_without_dashboard_context(
    monkeypatch,
) -> None:
    async def fake_output_context(*_args, **_kwargs) -> dict[str, object]:
        raise LookupError("backend not found")

    async def fake_dashboard_context(*_args, **_kwargs) -> dict[str, object]:
        raise AssertionError("missing output detail must not build dashboard context")

    monkeypatch.setattr(ui_pages, "output_page_context", fake_output_context)
    monkeypatch.setattr(ui_pages, "dashboard_context", fake_dashboard_context)

    response = await ui_pages.output_detail(
        20, _request(path="/outputs/20"), settings=Settings(), session=object()
    )

    body = response.body.decode()
    assert response.status_code == 404
    assert "Output not found" in body
    assert "/?tab=outputs" in body


async def test_output_page_context_skips_live_status_collection_and_keeps_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
    )

    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("output page should not collect live dashboard status")

    monkeypatch.setattr(ui_dashboard_data, "collect_status", fail_collect_status)

    def fake_collect_app_backend_diagnostics(_backend, _settings):
        return {
            "diagnosis": "app_service_down",
            "sandbox_status": "ready",
            "guest_status": "ready",
            "app_handoff_status": "down",
            "app_handoff_error": "connection refused",
            "issues": ["private_unreachable", "loopback_unreachable"],
            "container_state": {"ActiveState": "running", "SubState": "running"},
        }

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        fake_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        fake_collect_app_backend_diagnostics,
    )

    async with maker() as session:
        input_item = Input(kind="domain", hostname="web.example.com", enabled=True)
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [input_item]
        session.add_all([input_item, backend])
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, backend.id
        )

    assert context["selected_backend"].id == 1
    assert [item.hostname for item in context["inputs"]] == ["web.example.com"]
    assert (
        context["selected_output_detail"]["attached_inputs_summary"]
        == "domain: web.example.com"
    )
    assert context["attached_input_rows"] == ["domain: web.example.com"]
    assert context["selected_output_detail"]["runtime_health_label"] == "UNHEALTHY"
    assert context["selected_output_detail"]["runtime_health_summary"] == "UNHEALTHY"
    assert context["selected_output_detail"]["runtime_health_tone"] == "error"
    assert (
        context["selected_output_detail"]["configured_health_target"]
        == "http / @ web.example.com"
    )
    output_info_rows = {row[0]: row[1] for row in context["output_info_rows"]}
    assert "runtime owner" not in output_info_rows
    assert "health host" not in output_info_rows
    assert output_info_rows["resource size"] == "auto (small)"
    memory_high_row = output_info_rows["memory target"]
    memory_max_row = output_info_rows["memory cap"]
    cpu_limit_row = output_info_rows["cpu limit"]
    assert memory_high_row != context["selected_output_detail"]["resource_memory_high"]
    assert memory_max_row != context["selected_output_detail"]["resource_memory_max"]
    assert "iB" in memory_high_row
    assert "iB" in memory_max_row
    assert cpu_limit_row.endswith("% of total CPU")
    assert "cores" not in cpu_limit_row
    assert (
        context["runtime_alert"]["summary"] == "configured health endpoint unreachable"
    )
    assert context["backup_signals_pending"] is True


async def test_output_page_context_blocks_inputs_attached_to_other_enabled_outputs(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
    )

    async with maker() as session:
        selected_input = Input(
            kind="tailnet_path", hostname="/sample-current", enabled=True
        )
        blocked_input = Input(
            kind="tailnet_path", hostname="/sample-other", enabled=True
        )
        free_input = Input(kind="domain", hostname="free.localtest", enabled=True)
        selected_backend = Backend(
            name="sample-current", kind="app", port=12000, enabled=True
        )
        other_backend = Backend(
            name="sample-other", kind="app", port=12001, enabled=True
        )
        selected_backend.inputs = [selected_input]
        other_backend.inputs = [blocked_input]
        session.add_all(
            [selected_input, blocked_input, free_input, selected_backend, other_backend]
        )
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session,
            settings,
            selected_backend.id,
            prefer_cached_runtime=True,
        )
        attach_options_payload = await ui_reads.output_input_attach_options(
            selected_backend.id,
            session=session,
        )

    options = {item["value"]: item for item in context["input_attach_options"]}
    assert options["/sample-current"]["attached"] is True
    assert options["/sample-current"]["attachable"] is True
    assert "/sample-other" not in options
    assert context["input_attach_visible_count"] == 0
    assert context["input_attach_lazy_available"] is True
    assert context["input_attach_options_url"] == "/api/backends/1/input-attach-options"

    options = {item["value"]: item for item in attach_options_payload["options"]}
    assert options["/sample-current"]["attached"] is True
    assert options["/sample-current"]["attachable"] is True
    assert options["/sample-other"]["attached"] is False
    assert options["/sample-other"]["attachable"] is False
    assert (
        options["/sample-other"]["unavailable_reason"]
        == "already attached to sample-other"
    )
    assert options["free.localtest"]["attachable"] is True
    assert attach_options_payload["visible_count"] == 2


async def test_output_page_context_shield_omits_app_only_runtime_fields(
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

    async def fail_cluster_nodes(*_args, **_kwargs):
        raise AssertionError("shield output detail should not load transfer nodes")

    monkeypatch.setattr(ui_cluster, "cluster_nodes_for_context", fail_cluster_nodes)
    monkeypatch.setattr(
        ui_dashboard_data, "cluster_nodes_for_context", fail_cluster_nodes
    )
    monkeypatch.setattr(
        ui_outputs_data, "cluster_nodes_for_context", fail_cluster_nodes
    )

    async with maker() as session:
        backend = Backend(name="shield", kind="shield", enabled=True, volumes_json="[]")
        session.add(backend)
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, backend.id
        )

    output_info_rows = {row[0]: row[1] for row in context["output_info_rows"]}
    signal_labels = {item["label"] for item in context["output_signal_cards"]}
    assert output_info_rows["kind"] == "shield"
    assert output_info_rows["runtime"] == "cnc-shield.service"
    assert output_info_rows["loopback publish"] == "127.0.0.1:1026:1026/tcp"
    assert output_info_rows["public access"] == "protected app routes only"
    assert "resource size" not in output_info_rows
    assert "handoff port" not in output_info_rows
    assert "internal app port" not in output_info_rows
    assert "memory target" not in output_info_rows
    assert "memory cap" not in output_info_rows
    assert "cpu limit" not in output_info_rows
    assert "health check" not in signal_labels
    assert context["multi_node_transfer_enabled"] is False
    assert context["cluster_nodes"] == []
    assert context["multi_node_placement"] is None
    assert context["selected_output_detail"]["target"] == "protected app routes only"


async def test_output_page_context_prefers_cached_runtime_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
    )

    monkeypatch.setattr(
        ui_outputs_data,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "ok": True,
                    "metrics": {
                        "cpu_percent": 12.5,
                        "memory_percent": 31.25,
                        "network_total_bps": 2048.0,
                    },
                    "data": {
                        "RuntimeDiagnosis": "healthy",
                        "SandboxStatus": "ready",
                        "GuestStatus": "ready",
                        "AppHandoffStatus": "up",
                        "RuntimeIssues": "-",
                        "PrivateAddress": "10.89.1.6",
                        "PrivateReachable": "yes",
                        "ProxyReachable": "yes",
                        "ActiveState": "active",
                        "SubState": "running",
                    },
                }
            ]
        },
    )

    def fail_collect_app_backend_diagnostics(_backend, _settings):
        raise AssertionError(
            "should not run direct runtime diagnostics when cached runtime is preferred"
        )

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
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
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, backend.id, prefer_cached_runtime=True
        )

    assert context["selected_output_detail"]["runtime_health_label"] == "HEALTHY"
    assert context["selected_output_detail"]["runtime_health_summary"] == "HEALTHY"
    assert context["selected_output_live_metrics"] == {
        "cpu_percent": 12.5,
        "memory_percent": 31.25,
        "network_total_bps": 2048.0,
    }


async def test_output_page_context_defers_unknown_cached_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_outputs_data, "peek_cached_status", lambda: None)

    def fail_collect_app_backend_diagnostics(_backend, _settings):
        raise AssertionError(
            "initial output render should not probe live runtime when cached runtime is unavailable"
        )

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, backend.id, prefer_cached_runtime=True
        )

    detail = context["selected_output_detail"]
    assert detail["runtime_health_label"] == ""
    assert detail["runtime_health_summary"] == ""
    assert "status" not in {item["label"] for item in context["output_signal_cards"]}
    assert "runtime state" not in {
        item["label"] for item in context["output_signal_cards"]
    }


async def test_output_page_context_includes_relevant_recent_events(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(
        ui_outputs_data,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "ok": True,
                    "data": {
                        "RuntimeDiagnosis": "healthy",
                        "SandboxStatus": "ready",
                        "GuestStatus": "ready",
                        "AppHandoffStatus": "up",
                        "RuntimeIssues": "-",
                        "PrivateAddress": "10.89.1.6",
                        "PrivateReachable": "yes",
                        "ProxyReachable": "yes",
                        "ActiveState": "active",
                        "SubState": "running",
                    },
                }
            ]
        },
    )

    backend_created_at = datetime(2026, 5, 1, 14, 0, 0)
    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            memory_high_override="512M",
            memory_max_override="1G",
            enabled=True,
            created_at=backend_created_at,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                ControlEvent(
                    kind="update_completed",
                    source="update",
                    summary="Old host update completed",
                    severity="success",
                    scope="host",
                    affects_all=True,
                    created_at=backend_created_at - timedelta(hours=2),
                ),
                ControlEvent(
                    kind="apply_completed",
                    source="apply",
                    summary="Output created",
                    severity="success",
                    scope="host",
                    affects_all=True,
                    subevents_json='[{"label":"run","value":"#7"},{"label":"output","value":"web"},{"label":"status","value":"success"}]',
                    details_json='{"run_id":7,"status":"success","outputs_total":1,"outputs_created":1,"bundle_path":"/tmp/nope","size_bytes":1048576}',
                    created_at=backend_created_at + timedelta(minutes=1),
                ),
                ControlEvent(
                    kind="backend_fix_recovered",
                    source="fix",
                    summary="Backend fix recovered",
                    severity="success",
                    scope="backend",
                    backend_name="web",
                    subevents_json='[{"label":"after","value":"healthy"}]',
                    created_at=backend_created_at + timedelta(minutes=2),
                ),
                ControlEvent(
                    kind="hardening_phase1_complete",
                    source="hardening",
                    summary="Security baseline complete",
                    severity="success",
                    scope="backend",
                    backend_name="web",
                    subevents_json='[{"label":"run","value":"#12"},{"label":"recommended","value":"9"},{"label":"not recommended","value":"34"}]',
                    details_json='{"run_id":12,"status":"success"}',
                    created_at=backend_created_at + timedelta(minutes=2, seconds=30),
                ),
                ControlEvent(
                    kind="resource_resized",
                    source="auto_size",
                    summary="Resource size changed",
                    severity="info",
                    scope="host",
                    related_backends_json='["web"]',
                    created_at=backend_created_at + timedelta(minutes=3),
                ),
                ControlEvent(
                    kind="backup_created",
                    source="backup",
                    summary="Backup created",
                    severity="success",
                    scope="backend",
                    backend_name="smoke",
                    created_at=backend_created_at + timedelta(minutes=4),
                ),
            ]
        )
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, backend.id, prefer_cached_runtime=True
        )

    summaries = [item["summary"] for item in context["recent_event_rows"]]
    assert "Output created" in summaries
    assert "Backend fix recovered" in summaries
    assert "Security baseline complete" in summaries
    assert "Resource size changed" in summaries
    assert "Backup created" not in summaries
    assert "Old host update completed" not in summaries
    apply_row = next(
        item
        for item in context["recent_event_rows"]
        if item["summary"] == "Output created"
    )
    assert apply_row["severity"] == "success"
    assert apply_row["pairs"] == [
        {"label": "run", "value": "#7"},
        {"label": "output", "value": "web"},
        {"label": "outputs total", "value": "1"},
        {"label": "outputs created", "value": "1"},
        {"label": "size bytes", "value": "1.0 MiB"},
    ]
    hardening_row = next(
        item
        for item in context["recent_event_rows"]
        if item["summary"] == "Security baseline complete"
    )
    assert hardening_row["pairs"] == [
        {"label": "run", "value": "#12"},
        {"label": "recommended", "value": "9"},
        {"label": "not recommended", "value": "34"},
    ]
    assert "source" not in apply_row
    assert '"status": "success"' in apply_row["details"]


async def test_output_runtime_signals_returns_live_backend_payload(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    def fake_collect_app_backend_diagnostics(_backend, _settings):
        return {
            "backend": "web",
            "diagnosis": "healthy",
            "sandbox_status": "ready",
            "guest_status": "ready",
            "app_handoff_status": "up",
            "container_state": {"ActiveState": "active", "SubState": "running"},
            "issues": [],
        }

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        fake_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        fake_collect_app_backend_diagnostics,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        payload = await ui_reads.output_runtime_signals(
            1, settings=settings, session=session
        )

    assert payload["runtime_health_label"] == "HEALTHY"
    assert payload["runtime_health_summary"] == "HEALTHY"
    assert payload["runtime_alert"] is None
    assert payload["output_signal_cards"][0]["label"] == "status"
    assert payload["output_signal_cards"][0]["value"] == "HEALTHY"
    assert payload["output_signal_cards"][0]["checks"] == [
        {"label": "health check", "ok": True},
        {"label": "container", "ok": True},
    ]
    labels = [item["label"] for item in payload["output_signal_cards"]]
    assert labels == [
        "status",
        "target",
        "loopback publish",
        "guest profile",
        "health check",
    ]
    assert (
        payload["output_signal_cards"][-1]["value"] == "http /health @ web.example.com"
    )


async def test_output_detail_keeps_input_picker_available_for_disabled_outputs(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
    )

    async with maker() as session:
        attached = Input(kind="domain", hostname="smoke.localtest", enabled=True)
        unattached = Input(kind="domain", hostname="dev.localtest", enabled=True)
        backend = Backend(
            name="app-dev",
            kind="app",
            port=12001,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=False,
        )
        backend.inputs = [attached]
        session.add_all([attached, unattached, backend])
        await session.commit()

        response = await ui_pages.output_detail(
            backend.id,
            _request(
                path=f"/outputs/{backend.id}",
                headers=[(b"host", b"cnc-admin.example.ts.net")],
            ),
            settings=settings,
            session=session,
        )
        attach_options_payload = await ui_reads.output_input_attach_options(
            backend.id,
            session=session,
        )

    body = response.body.decode()
    assert response.status_code == 200
    assert 'data-output-name="smoke.localtest"' in body
    assert 'data-output-name="dev.localtest"' not in body
    assert (
        'data-output-attach-options-url="/api/backends/1/input-attach-options"' in body
    )
    assert any(
        item["value"] == "dev.localtest" for item in attach_options_payload["options"]
    )
    assert "data-output-add-toggle disabled" not in body
    output_script = Path("app/static/js/output-detail.js").read_text(encoding="utf-8")
    assert "event.stopPropagation();" in output_script


async def test_output_runtime_signals_skip_live_probe_for_disabled_outputs(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    def fail_collect_app_backend_diagnostics(_backend, _settings):
        raise AssertionError(
            "disabled output detail refresh should not run live diagnostics"
        )

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        fail_collect_app_backend_diagnostics,
    )

    async with maker() as session:
        backend = Backend(
            name="app-dev",
            kind="app",
            port=12001,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=False,
        )
        session.add(backend)
        await session.commit()

        payload = await ui_reads.output_runtime_signals(
            backend.id, settings=settings, session=session
        )

    assert payload["runtime_health_label"] == "NOT ENABLED"
    assert payload["runtime_health_tone"] == "inactive"
    assert payload["runtime_alert"] is None


async def test_output_runtime_signals_have_short_live_probe_budget(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(ui_diagnostics, "UI_RUNTIME_SIGNAL_TIMEOUT_SEC", 0.01)
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.clear()

    def slow_collect_app_backend_diagnostics(_backend, _settings):
        time.sleep(0.2)
        return {"diagnosis": "healthy"}

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        slow_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        slow_collect_app_backend_diagnostics,
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
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        started_at = time.monotonic()
        response = await ui_reads.output_runtime_signals(
            backend.id, settings=settings, session=session
        )
        elapsed = time.monotonic() - started_at

    assert elapsed < 0.15
    assert response.status_code == 202
    payload = json.loads(response.body)
    assert payload["pending"] is True
    assert payload["runtime_health_label"] == "Loading"
    assert payload["retry_after_ms"] == 750
    await asyncio.sleep(0.25)
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.clear()


async def test_output_runtime_signals_coalesce_slow_live_probe(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(ui_diagnostics, "UI_RUNTIME_SIGNAL_TIMEOUT_SEC", 0.01)
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.clear()
    call_count = 0

    def slow_collect_app_backend_diagnostics(_backend, _settings):
        nonlocal call_count
        call_count += 1
        time.sleep(0.2)
        return {"diagnosis": "healthy"}

    monkeypatch.setattr(
        ui_diagnostics,
        "collect_app_backend_diagnostics",
        slow_collect_app_backend_diagnostics,
    )
    monkeypatch.setattr(
        ui_outputs_data,
        "collect_app_backend_diagnostics",
        slow_collect_app_backend_diagnostics,
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
            update_command="git pull --ff-only",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        first = await ui_reads.output_runtime_signals(
            backend.id, settings=settings, session=session
        )
        second = await ui_reads.output_runtime_signals(
            backend.id, settings=settings, session=session
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert json.loads(first.body)["pending"] is True
    assert json.loads(second.body)["retry_after_ms"] == 750
    assert call_count == 1
    await asyncio.sleep(0.25)
    assert ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS == {}
    assert (backend.id, backend.name) in ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.clear()


def test_runtime_diagnostics_cache_prune_expires_and_caps(monkeypatch) -> None:
    monkeypatch.setattr(ui_diagnostics, "UI_RUNTIME_SIGNAL_CACHE_TTL_SEC", 10)
    monkeypatch.setattr(ui_diagnostics, "UI_RUNTIME_SIGNAL_CACHE_MAX_ENTRIES", 3)
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.clear()

    for index in range(5):
        ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE[(index, f"backend-{index}")] = (
            100.0 + index,
            {"diagnosis": "healthy"},
        )

    ui_diagnostics._prune_runtime_diagnostics_cache(now=106.0)

    assert list(ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE) == [
        (2, "backend-2"),
        (3, "backend-3"),
        (4, "backend-4"),
    ]

    ui_diagnostics._prune_runtime_diagnostics_cache(now=115.0)

    assert list(ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE) == []
