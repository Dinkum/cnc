from .support import (
    Path,
    Settings,
    SimpleNamespace,
    _PostedRequest,
    _dashboard_client_source,
    _make_session,
    _request,
    app_main,
    asyncio,
    mask_secret,
    os,
    ui_pages,
    ui_settings,
    ui_shared,
)


async def test_dashboard_settings_tab_uses_minimal_status_when_cache_empty(
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

    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("settings tab should not block on live status collection")

    async def fail_cluster_nodes(*_args, **_kwargs):
        raise AssertionError("disabled multi-node mode should not build node summaries")

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: None)
    monkeypatch.setattr(ui_shared, "_cluster_nodes", fail_cluster_nodes)

    async with maker() as session:
        response = await ui_pages.dashboard(
            _request(path="/?tab=settings"), settings=settings, session=session
        )

    body = response.body.decode()
    assert response.status_code == 200
    assert 'data-initial-tab="settings"' in body
    assert '<section class="panel" data-panel="settings" data-loaded="1">' in body
    assert "data-update-current-version" in body
    assert "not checked" in body


async def test_update_form_returns_conflict_when_no_new_version_is_available(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    settings = Settings()

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"flash_error": None, "flash_success": None, "active_tab": "home"}

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    async def fake_run_update(_session, _settings):
        raise ui_settings.UpdateRejectedError(
            "already on latest version (0.1.27)",
            details={
                "update_version": {
                    "current_version": "0.1.27",
                    "available_version": "0.1.27",
                    "has_update": False,
                }
            },
        )

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_settings, "run_update", fake_run_update)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _PostedRequest(path="/ui/update")
    response = await ui_settings.update_form(
        request, csrf_token="token", settings=settings, session=object()
    )

    assert response.status_code == 409
    assert captured["name"] == "index.html"
    assert str(captured["context"]["flash_error"]).startswith(
        "already on latest version (0.1.27)"
    )
    assert " - Error CNC-06001-" in str(captured["context"]["flash_error"])
    assert captured["context"]["active_tab"] == "settings"


async def test_update_beta_settings_form_persists_routing_toggle(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("BETA_ROUTING", raising=False)
    settings = Settings(managed_env_file_path=env_path)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)

    try:
        response = await ui_settings.update_beta_settings_form(
            _PostedRequest(path="/ui/settings/beta"),
            csrf_token="token",
            beta_routing=True,
            settings=settings,
            session=object(),
        )

        next_settings = Settings(managed_env_file_path=env_path)

        assert response.status_code == 303
        assert response.headers["location"] == "/?tab=settings"
        assert next_settings.beta_routing is True
        assert "BETA_ROUTING=true" in env_path.read_text(encoding="utf-8")
        assert ui_shared.FLASH_SUCCESS_COOKIE in response.headers["set-cookie"]
    finally:
        monkeypatch.delenv("BETA_ROUTING", raising=False)


async def test_settings_nodes_ui_renders_cluster_modals_when_enabled(
    monkeypatch, tmp_path: Path
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

    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("settings tab should not block on live status collection")

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: None)

    async with maker() as session:
        response = await ui_pages.dashboard(
            _request(path="/?tab=settings"), settings=settings, session=session
        )

    body = response.body.decode()
    assert "<h2>Nodes</h2>" in body
    assert 'action="/ui/settings/nodes"' in body
    assert "data-node-add-open" in body
    assert 'id="node-add-modal"' in body
    assert 'id="node-details-modal"' in body
    assert "Tailscale IP" in body
    assert "Tailscale URL" in body
    client_source = _dashboard_client_source()
    assert "/ui/settings/nodes/data" in client_source
    assert "Removing a node only severs CNC trust and join authentication." in body
    assert "/ui/settings/nodes/join-command" in client_source
    assert "Creating 15-minute join command..." in client_source
    assert "curl -fsSL https://tailscale.com/install.sh" in body


async def test_update_beta_settings_form_persists_shield_toggle(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("SHIELD_ENABLED", raising=False)
    settings = Settings(managed_env_file_path=env_path)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)

    try:
        response = await ui_settings.update_beta_settings_form(
            _PostedRequest(path="/ui/settings/beta"),
            csrf_token="token",
            beta_routing=False,
            shield_enabled=True,
            netdata_enabled=False,
            settings=settings,
            session=object(),
        )

        next_settings = Settings(managed_env_file_path=env_path)

        assert response.status_code == 303
        assert response.headers["location"] == "/?tab=settings"
        assert next_settings.shield_enabled is True
        assert "SHIELD_ENABLED=true" in env_path.read_text(encoding="utf-8")
        assert ui_shared.FLASH_SUCCESS_COOKIE in response.headers["set-cookie"]
    finally:
        monkeypatch.delenv("SHIELD_ENABLED", raising=False)


async def test_update_beta_settings_form_persists_netdata_toggle(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("NETDATA_ENABLED", raising=False)
    settings = Settings(managed_env_file_path=env_path)
    apply_calls: list[Settings] = []

    async def fake_run_apply(_session, received_settings, **kwargs):
        apply_calls.append(received_settings)
        assert kwargs["operation_kind"] == "ui.settings.netdata"
        return SimpleNamespace(status="success", details={}, run_id=1)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_settings, "run_apply", fake_run_apply)

    try:
        response = await ui_settings.update_beta_settings_form(
            _PostedRequest(path="/ui/settings/beta"),
            csrf_token="token",
            beta_routing=False,
            shield_enabled=False,
            netdata_enabled=True,
            settings=settings,
            session=object(),
        )

        next_settings = Settings(managed_env_file_path=env_path)

        assert response.status_code == 303
        assert response.headers["location"] == "/?tab=settings"
        assert next_settings.netdata_enabled is True
        assert "NETDATA_ENABLED=true" in env_path.read_text(encoding="utf-8")
        assert len(apply_calls) == 1
        assert apply_calls[0].netdata_enabled is True
    finally:
        monkeypatch.delenv("NETDATA_ENABLED", raising=False)


async def test_update_beta_settings_form_reports_only_failure_when_apply_fails(
    monkeypatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("NETDATA_ENABLED", raising=False)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        managed_env_file_path=env_path,
    )

    async def fake_preflight(*_args, **_kwargs):
        return None

    async def fake_run_apply(*_args, **_kwargs):
        return SimpleNamespace(
            status="error",
            message="apply failed",
            details={"error": "netdata unit failed"},
            run_id=7,
        )

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_settings, "_host_mutation_preflight_message", fake_preflight)
    monkeypatch.setattr(ui_settings, "run_apply", fake_run_apply)

    try:
        response = await ui_settings.update_beta_settings_form(
            _PostedRequest(path="/ui/settings/beta"),
            csrf_token="token",
            beta_routing=False,
            shield_enabled=False,
            netdata_enabled=True,
            settings=settings,
            session=object(),
        )
    finally:
        monkeypatch.delenv("NETDATA_ENABLED", raising=False)

    cookie_headers = [
        value.decode() for key, value in response.raw_headers if key == b"set-cookie"
    ]
    assert response.status_code == 303
    success_headers = [
        value
        for value in cookie_headers
        if value.startswith(f"{ui_shared.FLASH_SUCCESS_COOKIE}=")
    ]
    assert success_headers and all("Max-Age=0" in value for value in success_headers)
    assert any(
        value.startswith(f"{ui_shared.FLASH_ERROR_COOKIE}=")
        and "Netdata setting could not be applied" in value
        and "Max-Age=60" in value
        for value in cookie_headers
    )


async def test_update_notification_settings_form_tests_without_saving(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"flash_error": None, "flash_success": None, "active_tab": "home"}

    async def fake_send_notification(*_args, **_kwargs) -> bool:
        return True

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(
        ui_settings, "send_pushover_notification_async", fake_send_notification
    )
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _PostedRequest(path="/ui/settings/notifications")
    response = await ui_settings.update_notification_settings_form(
        request,
        csrf_token="token",
        pushover_app_token="apptoken",
        pushover_user_key="userkey",
        action="test",
        settings=settings,
        session=object(),
    )

    assert response.status_code == 200
    assert captured["name"] == "index.html"
    assert captured["context"]["flash_success"] == "Pushover test alert sent."
    assert not settings.managed_env_file_path.exists()


async def test_update_notification_settings_form_preserves_masked_values(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    env_path = tmp_path / "cnc.env"
    env_path.write_text(
        "PUSHOVER_APP_TOKEN=fixture-app-token\nPUSHOVER_USER_KEY=fixture-user-key\n",
        encoding="utf-8",
    )
    settings = Settings(
        managed_env_file_path=env_path,
        pushover_app_token="fixture-app-token",
        pushover_user_key="fixture-user-key",
    )

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {
            "flash_error": None,
            "flash_success": None,
            "active_tab": "home",
            "notification_settings": ui_shared._notification_settings_summary(
                _settings
            ),
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    response = await ui_settings.update_notification_settings_form(
        _PostedRequest(path="/ui/settings/notifications"),
        csrf_token="token",
        pushover_app_token=mask_secret(settings.pushover_app_token),
        pushover_user_key=mask_secret(settings.pushover_user_key),
        action="save",
        settings=settings,
        session=object(),
    )

    assert response.status_code == 200
    assert captured["context"]["flash_success"] == "Pushover settings saved."
    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "PUSHOVER_APP_TOKEN=fixture-app-token",
        "PUSHOVER_USER_KEY=fixture-user-key",
    ]
    context_settings = captured["context"]["notification_settings"]
    assert context_settings["app_token_masked"] == mask_secret(
        settings.pushover_app_token
    )
    assert context_settings["user_key_masked"] == mask_secret(
        settings.pushover_user_key
    )


def test_notification_settings_summary_displays_masked_configured_values() -> None:
    settings = Settings(
        pushover_app_token="fixture-app-token",
        pushover_user_key="fixture-user-key",
    )

    summary = ui_shared._notification_settings_summary(settings)

    assert summary["app_token_display"] == mask_secret(settings.pushover_app_token)
    assert summary["user_key_display"] == mask_secret(settings.pushover_user_key)
    assert summary["app_token_display"] != ""
    assert summary["user_key_display"] != ""


def test_notification_settings_summary_leaves_unconfigured_inputs_empty(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    summary = ui_shared._notification_settings_summary(Settings())

    assert summary["app_token_masked"] == "off"
    assert summary["user_key_masked"] == "off"
    assert summary["app_token_display"] == ""
    assert summary["user_key_display"] == ""


async def test_notification_settings_save_is_visible_to_unhandled_exception_handler(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    sent_settings: list[Settings] = []
    env_path = tmp_path / "cnc.env"
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    settings = Settings(managed_env_file_path=env_path)

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {
            "flash_error": None,
            "flash_success": None,
            "active_tab": "home",
            "notification_settings": ui_shared._notification_settings_summary(
                _settings
            ),
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    async def fake_notify(settings: Settings, **_kwargs) -> bool:
        sent_settings.append(settings)
        return True

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)
    monkeypatch.setattr(app_main, "send_pushover_notification_async", fake_notify)

    try:
        response = await ui_settings.update_notification_settings_form(
            _PostedRequest(path="/ui/settings/notifications"),
            csrf_token="token",
            pushover_app_token="apptoken",
            pushover_user_key="userkey",
            action="save",
            settings=settings,
            session=object(),
        )

        assert response.status_code == 200
        assert captured["context"]["flash_success"] == "Pushover settings saved."
        assert os.environ["PUSHOVER_APP_TOKEN"] == "apptoken"
        assert os.environ["PUSHOVER_USER_KEY"] == "userkey"
        assert captured["context"]["notification_settings"]["configured"] is True

        error_response = await app_main.unhandled_exception_handler(
            _request(path="/api/status", headers=[(b"accept", b"application/json")]),
            RuntimeError("boom"),
        )
        await asyncio.sleep(0)

        assert error_response.status_code == 500
        assert len(sent_settings) == 1
        assert sent_settings[0].pushover_app_token == "apptoken"
        assert sent_settings[0].pushover_user_key == "userkey"
    finally:
        app_main.get_settings.cache_clear()
