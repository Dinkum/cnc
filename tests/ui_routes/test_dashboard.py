from .support import (
    Backend,
    HTMLResponse,
    Input,
    Path,
    Settings,
    SimpleNamespace,
    _dashboard_client_source,
    _healthy_status,
    _make_session,
    _request,
    ui_pages,
    ui_shared,
)


async def test_dashboard_uses_request_first_template_response(monkeypatch) -> None:
    captured: dict[str, object] = {}
    settings = Settings()

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"flash_error": None, "flash_success": None}

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_pages, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _request()
    response = await ui_pages.dashboard(request, settings=settings, session=object())

    assert response.status_code == 200
    assert captured["request"] is request
    assert captured["name"] == "index.html"
    context = captured["context"]
    assert isinstance(context, dict)
    assert context["active_tab"] == "home"
    assert context["flash_error"] is None
    assert context["flash_success"] is None
    assert context["request"] is request
    assert context["asset_version"] == ui_shared._app_version()
    assert (
        context["create_output_progress_steps"]
        == ui_shared.create_backend_progress_steps()
    )
    assert context["input_progress_pipelines"] == ui_shared.input_progress_pipelines()
    assert (
        context["operation_progress_pipelines"]
        == ui_shared.operation_progress_pipelines()
    )


async def test_dashboard_prefers_cached_status_for_routing_tab(monkeypatch) -> None:
    captured: dict[str, object] = {}
    settings = Settings()

    async def fake_dashboard_context(
        _session, _settings, **kwargs
    ) -> dict[str, object]:
        captured.update(kwargs)
        return {"active_tab": "home", "flash_error": None, "flash_success": None}

    def fake_template_response(request, name, context, status_code=200):
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_pages, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _request(path="/?tab=routing")
    response = await ui_pages.dashboard(request, settings=settings, session=object())

    assert response.status_code == 200
    assert captured["prefer_cached_status"] is True
    assert captured["active_tab"] == "home"
    assert captured["defer_status"] is False


async def test_dashboard_can_defer_status_for_fast_output_return(monkeypatch) -> None:
    captured: dict[str, object] = {}
    settings = Settings()

    async def fake_dashboard_context(
        _session, _settings, **kwargs
    ) -> dict[str, object]:
        captured.update(kwargs)
        return {"active_tab": "outputs", "flash_error": None, "flash_success": None}

    def fake_template_response(request, name, context, status_code=200):
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_pages, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _request(path="/?tab=outputs&defer_status=1")
    response = await ui_pages.dashboard(request, settings=settings, session=object())

    assert response.status_code == 200
    assert captured["prefer_cached_status"] is True
    assert captured["active_tab"] == "outputs"
    assert captured["defer_status"] is True


async def test_deferred_dashboard_status_skips_collection(monkeypatch) -> None:
    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("deferred dashboard render must not collect status")

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: None)

    payload = await ui_shared._dashboard_status(
        object(),
        Settings(),
        scope=ui_shared._DashboardScope("outputs"),
        prefer_cached_status=True,
        defer_status=True,
    )

    assert payload["services"] == []
    assert payload["update_version"]["current_version"]


async def test_home_dashboard_cold_status_never_blocks_on_collection(
    monkeypatch,
) -> None:
    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("Home first paint must not run a cold host status sweep")

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: None)

    payload = await ui_shared._dashboard_status(
        object(),
        Settings(),
        scope=ui_shared._DashboardScope("home"),
        prefer_cached_status=True,
    )

    assert payload["app_network_isolation"] == {"checked": False}
    assert payload["dashboard_overview"] == {}


async def test_dashboard_renders_remote_update_version_and_disables_button_when_up_to_date(
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
    )

    cached_status = _healthy_status(backend_count=0)
    cached_status["update_version"] = {
        "current_version": "0.1.27",
        "available_version": "0.1.27",
        "has_update": False,
        "repo": "Dinkum/cnc",
        "ref": "main",
        "error": None,
    }

    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError(
            "settings tab should render from cached status without live collection"
        )

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: cached_status)

    async with maker() as session:
        session.add(Input(kind="domain", hostname="web.example.com", enabled=True))
        await session.commit()
        response = await ui_pages.dashboard(
            _request(path="/?tab=settings"), settings=settings, session=session
        )

    body = response.body.decode()
    client_source = _dashboard_client_source()
    assert response.status_code == 200
    assert "data-update-current-version" in body
    assert ">0.1.27<" in body
    assert "data-update-available-version" in body
    assert "Already up to date" not in body
    assert "Already on the latest version." not in body
    assert '<span class="label">value</span>' not in body
    assert '<span class="label">route state</span>' not in body
    assert (
        "const submitUrl = new URL(actionPath || form.action, window.location.href).toString();"
        in client_source
    )
    assert "data-output-load-sync" not in body
    assert "const clearBanners = ({ successOnly = false } = {}) =>" in client_source
    assert (
        'if (tone === "success") clearBanners({ successOnly: true });' in client_source
    )
    assert "routing:" not in body
    assert (
        'const STATUS_POLL_TABS = new Set(["home", "outputs", "settings"]);'
        in client_source
    )
    assert "const renderHomeFromStatus = (payload) =>" in client_source
    assert "const submitUrl" not in body
    assert f"/static/js/dashboard.js?v={ui_shared._app_version()}" in body
    assert "<h2>Nodes</h2>" in body
    assert 'action="/ui/settings/nodes"' in body
    assert 'name="multi_node_enabled" type="checkbox"' in body
    assert '<button type="button" class="ghost" data-node-add-open>' not in body
    assert '<span class="beta-feature-name">Routing page</span>' in body
    assert "Preset app size used by auto sizing" in body
    assert (
        "Soft memory target. The host starts applying pressure here before the hard cap."
        in body
    )
    assert "Hard memory ceiling for this app size." in body
    assert "Share of total host CPU capacity assigned to this app size" in body
    assert ".resource-size-th" in Path("app/static/css/app.css").read_text(
        encoding="utf-8"
    )
    assert "data-update-form hidden" in body
    assert "latest update run" not in body
    assert (
        "Update completed. Waiting for admin service restart to refresh the version card..."
        in client_source
    )
    assert (
        "Waiting for the admin service to come back after update restart..."
        in client_source
    )
    assert "window.setTimeout(pollServerLoadStatus, 0)" not in client_source
    assert "scheduleServerLoadPoll();" in client_source
    assert 'if (activeTab === "settings" && updateFlowActive())' in client_source
    assert "let hostMetricsShell = null;" in client_source
    assert "refreshHostMetricRefs();" in client_source
    assert "refreshVisibleTabStatus();" in client_source
    assert (
        "const hasLastUpdate = Boolean(lastUpdate?.status || lastUpdate?.created_at || lastUpdate?.message);"
        in client_source
    )


async def test_dashboard_non_routing_tabs_do_not_embed_route_graph_rows(
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
        beta_routing=True,
    )

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=0)

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            enabled=True,
        )
        input_row = Input(kind="domain", hostname="web.example.com", enabled=True)
        input_row.backends.append(backend)
        session.add(input_row)
        await session.commit()

        response = await ui_pages.dashboard(
            _request(path="/?tab=home"), settings=settings, session=session
        )

    body = response.body.decode()
    assert response.status_code == 200
    assert "<strong data-home-routes>1/1</strong>" in body
    assert "data-routes='[]'" in body


async def test_dashboard_server_renders_active_outputs_tab_without_panel_flash(
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
        beta_routing=True,
    )

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=0)

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        response = await ui_pages.dashboard(
            _request(path="/?tab=outputs"), settings=settings, session=session
        )

    body = response.body.decode()
    assert response.status_code == 200
    assert 'data-initial-tab="outputs"' in body
    assert (
        '<button class="tab is-active" type="button" data-tab="outputs" aria-selected="true">outputs</button>'
        in body
    )
    assert (
        '<button class="tab" type="button" data-tab="routing" aria-selected="false">routing</button>'
        in body
    )
    assert '<section class="panel" data-panel="home" data-loaded="0" hidden>' in body
    assert '<section class="panel" data-panel="routing" data-loaded="0" hidden>' in body
    assert '<section class="panel" data-panel="outputs" data-loaded="1">' in body


async def test_dashboard_context_builds_runtime_cards_for_app(
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

    async def fake_collect_status(_session, _settings, **_kwargs):
        return {
            "services": [
                {
                    "service": "cnc-app-web",
                    "ok": True,
                    "data": {
                        "ActiveState": "active",
                        "SubState": "running",
                        "BootstrapState": "succeeded",
                        "BootstrapMarker": "present",
                        "PrivateReachable": "yes",
                        "ProxyReachable": "yes",
                        "PrivateAddress": "10.89.1.6",
                        "DnsServers": "1.1.1.1, 1.0.0.1",
                    },
                    "metrics": {
                        "cpu_percent": 18.4,
                        "cpu_percent_of_host": 18.4,
                        "memory_percent": 42.0,
                        "memory_current_bytes": 134217728,
                        "memory_max_bytes": 319815680,
                    },
                    "action_hints": [],
                },
            ],
            "nginx": {
                "ok": True,
                "data": {"ActiveState": "active", "SubState": "running"},
            },
            "resource_profile": {
                "mode": "auto",
                "backend_count": 1,
                "host_cpu_count": 2,
                "host_memory_bytes": 1024,
            },
            "resource_profile_drift": {"changed": False},
            "last_apply": {},
            "last_update": {},
        }

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        session.add_all(
            [
                Backend(
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
                    memory_high_override="512M",
                    memory_max_override="768M",
                    cpu_quota_override="100%",
                    enabled=True,
                ),
            ]
        )
        await session.commit()

        context = await ui_shared._dashboard_context(session, settings)

    runtime_cards = context["runtime_cards"]
    assert [card["name"] for card in runtime_cards] == ["web"]
    assert context["output_details"][1]["cpu_percent"] == 18.4
    assert context["output_details"][1]["memory_percent"] == 42.0
    assert context["output_details"][1]["status_value"] == "healthy"
    assert context["output_details"][1]["status_tone"] == "success"
    assert context["output_details"][1]["resource_mode"] == "manual"
    assert context["output_details"][1]["resource_memory_high"] == "512M"
    assert context["output_details"][1]["resource_memory_max"] == "768M"
    assert context["output_details"][1]["resource_cpu_quota"] == "100%"


async def test_dashboard_context_tolerates_missing_service_row_for_app(
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

    async def fake_collect_status(_session, _settings, **_kwargs):
        return {
            "services": [],
            "nginx": {
                "ok": True,
                "data": {"ActiveState": "active", "SubState": "running"},
            },
            "resource_profile": {
                "mode": "auto",
                "backend_count": 1,
                "host_cpu_count": 2,
                "host_memory_bytes": 1024,
            },
            "resource_profile_drift": {"changed": False},
            "last_apply": {},
            "last_update": {},
        }

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        session.add(
            Backend(
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
        )
        await session.commit()

        context = await ui_shared._dashboard_context(session, settings)

    assert context["runtime_cards"] == []
    assert context["output_details"][1]["status_value"] == "unhealthy"
    assert context["output_details"][1]["status_tone"] == "error"
    assert context["output_details"][1]["service_state"] == "- / -"


async def test_dashboard_context_marks_explicit_failed_app_backend_unhealthy(
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

    async def fake_collect_status(_session, _settings, **_kwargs):
        return {
            "services": [
                {
                    "service": "cnc-app-web",
                    "ok": False,
                    "data": {
                        "ActiveState": "failed",
                        "SubState": "failed",
                    },
                },
            ],
            "nginx": {
                "ok": True,
                "data": {"ActiveState": "active", "SubState": "running"},
            },
            "resource_profile": {
                "mode": "auto",
                "backend_count": 1,
                "host_cpu_count": 2,
                "host_memory_bytes": 1024,
            },
            "resource_profile_drift": {"changed": False},
            "last_apply": {},
            "last_update": {},
        }

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        session.add(
            Backend(
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
        )
        await session.commit()

        context = await ui_shared._dashboard_context(session, settings)

    assert context["output_details"][1]["status_value"] == "unhealthy"
    assert context["output_details"][1]["status_tone"] == "error"
    assert context["overview"]["outputs_unhealthy"] == 1


async def test_dashboard_context_marks_disabled_app_backend_inactive(
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

    async def fake_collect_status(_session, _settings, **_kwargs):
        return {
            "services": [],
            "nginx": {
                "ok": True,
                "data": {"ActiveState": "active", "SubState": "running"},
            },
            "resource_profile": {
                "mode": "auto",
                "backend_count": 1,
                "host_cpu_count": 2,
                "host_memory_bytes": 1024,
            },
            "resource_profile_drift": {"changed": False},
            "last_apply": {},
            "last_update": {},
        }

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        session.add(
            Backend(
                name="smokeapp",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/smokeapp",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                enabled=False,
            )
        )
        await session.commit()

        context = await ui_shared._dashboard_context(session, settings)

    assert context["output_details"][1]["status_value"] == "not enabled"
    assert context["output_details"][1]["status_tone"] == "inactive"


def test_dashboard_output_create_form_has_compact_controls() -> None:
    body = "\n".join(
        (
            Path("app/templates/index.html").read_text(encoding="utf-8"),
            _dashboard_client_source(),
        )
    )
    css = Path("app/static/css/app.css").read_text(encoding="utf-8")

    assert 'data-editor-close="backend-editor"' in body
    assert "Resource Mode" not in body
    assert 'name="resource_size" data-resource-size-control' in body
    assert '<option value="auto" selected>auto</option>' in body
    assert '<option value="custom">custom</option>' in body
    assert "data-custom-resource-limit hidden" in body
    assert "output-create-attach" in body
    assert "output-create-controls" in body
    assert "output-create-enabled" in body
    assert 'id="backend-output-option-template"' in body
    assert 'data-output-lazy-options="backends"' in body
    assert "materializeOutputList" in body
    assert ".output-create-controls" in css
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in css
    assert ".output-create-enabled" in css
    assert "justify-self: center;" in css
    assert "transform: translate(-50%, -50%) rotate(40deg);" in css
    assert ".editor-close-icon" in css
    assert "min-width: 0;" in css
    assert ".floating-save-card.success" in css
    assert "rgba(135, 242, 184, 0.42)" in css
