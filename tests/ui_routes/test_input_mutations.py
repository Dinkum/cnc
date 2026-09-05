from .support import (
    ApplyResponse,
    Backend,
    Input,
    Operation,
    Path,
    Settings,
    SimpleNamespace,
    _FormRequest,
    _PostedRequest,
    _cookie_values,
    _healthy_status,
    _make_session,
    _request,
    create_operation,
    json,
    make_code_hash,
    select,
    selectinload,
    input_operations,
    output_save_operations,
    ui_input,
    ui_pages,
    ui_shared,
)


async def test_dashboard_output_create_picker_blocks_single_output_inputs(
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
        return _healthy_status(backend_count=1)

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)

    async with maker() as session:
        attached = Input(kind="tailnet_path", hostname="/sample-used", enabled=True)
        free = Input(kind="domain", hostname="free.example.com", enabled=True)
        backend = Backend(
            name="sample-robust-a",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/app",
            install_command="true",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [attached]
        session.add_all([backend, free])
        await session.commit()

        response = await ui_pages.dashboard(
            _request(path="/?tab=outputs"), settings=settings, session=session
        )

    body = response.body.decode()
    assert response.status_code == 200
    assert '<span class="summary-pill">1 available</span>' in body
    assert 'data-output-name="/sample-used"' in body
    assert (
        'data-output-name="/sample-used"\n                          data-output-kind="tailnet path"\n                          data-output-enabled=""\n                          disabled title="already attached to sample-robust-a"'
        in body
    )
    assert 'data-output-name="free.example.com"' in body
    free_option = body[body.index('data-output-name="free.example.com"') :][:240]
    assert 'data-output-kind="domain"' in free_option
    assert "disabled" not in free_option.split(">")[0]


async def test_create_input_form_surfaces_hostname_hint_on_validation_error(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
    )

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        request = _FormRequest()
        response = await ui_input.create_input_form(
            request,
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="bad_label.example.com",
            enabled=True,
            session=session,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/?tab=inputs")
    flash_error = _cookie_values(response)["cnc_flash_error"]
    assert flash_error.startswith(
        "hostname label has invalid characters: bad_label. "
        "Use a lowercase hostname like api.example.com. Letters, digits, hyphens, and dots only."
    )
    assert " - Error CNC-01001-" in flash_error


async def test_create_input_form_conflict_uses_operator_error_fields(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        session.add(
            Input(
                kind="tailnet_path", hostname="/sample-robust-narrative", enabled=True
            )
        )
        await session.commit()

        response = await ui_input.create_input_form(
            _PostedRequest(path="/ui/inputs"),
            settings=settings,
            csrf_token="token",
            kind="tailnet_path",
            value="/sample-robust-narrative",
            enabled=True,
            session=session,
        )

    error = _cookie_values(response)["cnc_flash_error"]
    assert response.status_code == 303
    assert response.headers["location"].endswith("/?tab=inputs")
    assert error.startswith("Input create failed - Error CNC-08006-")
    assert "/sample-robust-narrative already exists. Use a unique input value." in error
    assert "sqlite3.IntegrityError" not in error
    assert "INSERT INTO" not in error


async def test_create_input_form_renders_dashboard_after_success(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=1)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=42,
        )

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

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

        response = await ui_input.create_input_form(
            _PostedRequest(path="/ui/inputs", fields={"backend_ids": ["1"]}),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="Api.Example.Com",
            enabled=True,
            session=session,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/?tab=inputs")
    cookies = _cookie_values(response)
    assert cookies["cnc_flash_success"] == "Input saved. Host updated."


async def test_create_input_form_returns_dashboard_html_for_mounted_refresh(
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
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=1)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=42
        )

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code, raw_headers=[])

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        response = await ui_input.create_input_form(
            _PostedRequest(
                path="/ui/inputs", headers=[(b"x-cnc-dashboard-refresh", b"1")]
            ),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="api.example.com",
            enabled=True,
            session=session,
        )

    assert response.status_code == 201
    assert captured["name"] == "index.html"
    assert captured["context"]["active_tab"] == "inputs"
    assert captured["context"]["flash_success"] == "Input saved. Host updated."


async def test_create_input_form_returns_operation_for_async_dashboard_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        response = await ui_input.create_input_form(
            _PostedRequest(
                path="/ui/inputs",
                headers=[
                    (b"accept", b"application/json, text/html"),
                    (b"x-cnc-dashboard-refresh", b"1"),
                ],
            ),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="api.example.com",
            enabled=True,
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )
        inputs = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert response.background is not None
    assert len(response.background.tasks) == 1
    assert payload["operation_id"] == operations[0].id
    assert payload["operation_status"] == "queued"
    assert payload["message"] == "Input create started."
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.input.create", "queued", "Validate input")]
    assert inputs == []


async def test_create_input_form_blocks_mounted_refresh_during_active_host_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=1)

    async def fail_run_apply(_session, _settings):
        raise AssertionError("blocked input create should not start apply")

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code, raw_headers=[])

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fail_run_apply)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="Create output",
                actor="ui",
            )
        )
        await session.commit()

        response = await ui_input.create_input_form(
            _PostedRequest(
                path="/ui/inputs", headers=[(b"x-cnc-dashboard-refresh", b"1")]
            ),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="api.example.com",
            enabled=True,
            session=session,
        )

    assert response.status_code == 409
    assert captured["context"]["active_tab"] == "inputs"
    assert str(captured["context"]["flash_error"]).startswith(
        "Output create is already running (Create output). Input create can't start until it finishes. Try again later."
    )
    assert " - Error CNC-08013-" in str(captured["context"]["flash_error"])


async def test_create_input_form_rolls_back_input_when_apply_fails(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=1)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "sync_files",
                "error": "failed to stage managed nginx files",
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "INPUT001",
            },
            run_id=36,
        )

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

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

        response = await ui_input.create_input_form(
            _PostedRequest(path="/ui/inputs", fields={"backend_ids": ["1"]}),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="api.example.com",
            enabled=True,
            session=session,
        )

        stored_inputs = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    assert response.status_code == 303
    assert stored_inputs == []
    assert response.headers["location"].endswith("/?tab=inputs")
    cookies = _cookie_values(response)
    assert cookies["cnc_flash_error"] == (
        "Save failed (Error CNC-02099-INPUT001). Existing config is still active. "
        "Reason: sync files failed: failed to stage managed nginx files"
    )


async def test_update_input_form_deletes_input_after_successful_apply(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=0)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=44
        )

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

    async with maker() as session:
        session.add(Input(kind="tailnet_path", hostname="/app1", enabled=True))
        await session.commit()

        response = await ui_input.update_input_form(
            1,
            _PostedRequest(path="/ui/inputs/1"),
            settings=settings,
            csrf_token="token",
            kind="tailnet_path",
            value="/app1",
            enabled=True,
            action="delete",
            session=session,
        )

        stored_inputs = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    assert response.status_code == 303
    assert not stored_inputs
    assert response.headers["location"].endswith("/?tab=inputs")
    cookies = _cookie_values(response)
    assert cookies["cnc_flash_success"] == "Input deleted. Host updated."


async def test_delete_input_form_deletes_input_after_successful_apply(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=44
        )

    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

    async with maker() as session:
        session.add(Input(kind="domain", hostname="delete.example.com", enabled=True))
        await session.commit()

        response = await ui_input.delete_input_form(
            1,
            _PostedRequest(path="/ui/inputs/1/delete"),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored_inputs = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    assert response.status_code == 303
    assert stored_inputs == []
    assert response.headers["location"].endswith("/?tab=inputs")
    assert (
        _cookie_values(response)["cnc_flash_success"] == "Input deleted. Host updated."
    )


async def test_update_input_form_returns_operation_for_async_save(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        session.add(Input(kind="domain", hostname="save.example.com", enabled=True))
        await session.commit()

        response = await ui_input.update_input_form(
            1,
            _PostedRequest(
                path="/ui/inputs/1",
                headers=[(b"accept", b"application/json, text/html")],
            ),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="save.example.com",
            enabled=False,
            shield_enabled=False,
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )
        stored = (
            await session.execute(select(Input).where(Input.id == 1))
        ).scalar_one()

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert response.background is not None
    assert len(response.background.tasks) == 1
    assert payload["operation_id"] == operations[0].id
    assert payload["message"] == "Input save started."
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.input.update", "queued", "Validate input")]
    assert stored.enabled is True


async def test_update_input_operation_persists_input_shield_code_for_async_save(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        operation_progress_dir=tmp_path / "operations",
        shield_secret_key="secret",
    )

    async def fake_run_apply(_session, _settings, **_kwargs):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=48
        )

    monkeypatch.setattr(input_operations, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(kind="domain", hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()
        input_id = item.id
        backend_id = backend.id

    operation = await create_operation(
        settings,
        kind="ui.input.update",
        actor="ui",
        phase="Validate input",
    )
    await input_operations._run_update_input_operation(
        settings,
        operation.id,
        input_id,
        {
            "backend_ids": [str(backend_id)],
            "enabled": True,
            "shield_enabled": True,
            "shield_access_code": "route key",
        },
    )

    async with maker() as session:
        stored = (
            await session.execute(select(Input).where(Input.id == input_id))
        ).scalar_one()
        saved_operation = (
            await session.execute(select(Operation).where(Operation.id == operation.id))
        ).scalar_one()

    assert stored.shield_enabled is True
    assert stored.shield_code_hash == make_code_hash(
        settings.shield_secret_key, "route key"
    )
    assert stored.shield_access_code == "route key"
    assert saved_operation.status == "success"


async def test_delete_input_form_returns_operation_for_async_delete(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        session.add(Input(kind="domain", hostname="delete.example.com", enabled=True))
        await session.commit()

        response = await ui_input.delete_input_form(
            1,
            _PostedRequest(
                path="/ui/inputs/1/delete",
                headers=[(b"accept", b"application/json, text/html")],
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )
        stored = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    payload = json.loads(response.body)
    assert response.status_code == 202
    assert response.background is not None
    assert len(response.background.tasks) == 1
    assert payload["operation_id"] == operations[0].id
    assert payload["message"] == "Input delete started."
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.input.delete", "queued", "Validate input")]
    assert len(stored) == 1


async def test_update_input_form_keeps_type_and_value_immutable(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=46
        )

    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

    async with maker() as session:
        session.add(Input(kind="tailnet_path", hostname="/app1", enabled=True))
        await session.commit()

        response = await ui_input.update_input_form(
            1,
            _PostedRequest(path="/ui/inputs/1", fields={"backend_ids": []}),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="example.com",
            enabled=False,
            session=session,
        )

        stored = (
            await session.execute(select(Input).where(Input.id == 1))
        ).scalar_one()

    assert response.status_code == 303
    assert stored.kind == "tailnet_path"
    assert stored.hostname == "/app1"
    assert stored.enabled is False


async def test_update_input_form_persists_input_shield_code(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        shield_secret_key="secret",
    )

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=47
        )

    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(kind="domain", hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        response = await ui_input.update_input_form(
            item.id,
            _PostedRequest(
                path=f"/ui/inputs/{item.id}", fields={"backend_ids": [str(backend.id)]}
            ),
            settings=settings,
            csrf_token="token",
            kind="domain",
            value="web.example.com",
            enabled=True,
            shield_enabled=True,
            shield_access_code="route key",
            session=session,
        )

        stored = (
            await session.execute(select(Input).where(Input.id == item.id))
        ).scalar_one()

    assert response.status_code == 303
    assert stored.shield_enabled is True
    assert stored.shield_code_hash == make_code_hash(
        settings.shield_secret_key, "route key"
    )
    assert stored.shield_access_code == "route key"
    assert _cookie_values(response)["cnc_flash_success"] == "Input saved. Host updated."


async def test_update_input_form_rolls_back_deleted_input_when_apply_fails(
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

    monkeypatch.setattr(ui_input, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=0)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "sync_files",
                "error": "failed to stage managed nginx files",
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "INPUT002",
            },
            run_id=45,
        )

    monkeypatch.setattr(ui_shared, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_input, "run_apply", fake_run_apply)

    async with maker() as session:
        session.add(Input(kind="tailnet_path", hostname="/app1", enabled=True))
        await session.commit()

        response = await ui_input.update_input_form(
            1,
            _PostedRequest(path="/ui/inputs/1"),
            settings=settings,
            csrf_token="token",
            kind="tailnet_path",
            value="/app1",
            enabled=True,
            action="delete",
            session=session,
        )

        stored_inputs = (
            (await session.execute(select(Input).order_by(Input.id.asc())))
            .scalars()
            .all()
        )

    assert response.status_code == 303
    assert len(stored_inputs) == 1
    assert stored_inputs[0].hostname == "/app1"
    assert response.headers["location"].endswith("/?tab=inputs")
    cookies = _cookie_values(response)
    assert cookies["cnc_flash_error"] == (
        "Save failed (Error CNC-02099-INPUT002). Existing config is still active. "
        "Reason: sync files failed: failed to stage managed nginx files"
    )


async def test_async_attached_inputs_apply_failure_uses_requested_ids(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
    )

    async def fake_run_apply(*_args, **_kwargs):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "nginx_validate",
                "error": "bad candidate",
                "failure_mode": "clean",
                "manual_review_required": False,
                "error_code": "CNC-02001",
                "error_name": "APPLY_NGINX_CONFIG_INVALID",
                "error_inst": "INPUTS01",
            },
            run_id=54,
        )

    monkeypatch.setattr(output_save_operations, "run_apply", fake_run_apply)

    async with maker() as session:
        input_a = Input(kind="domain", hostname="a.example.com", enabled=True)
        input_b = Input(kind="domain", hostname="b.example.com", enabled=True)
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [input_a]
        operation = Operation(
            kind="ui.backend.inputs",
            status="queued",
            phase="queued",
            actor="ui",
            details_json="{}",
        )
        session.add_all([input_a, input_b, backend, operation])
        await session.commit()
        backend_id = backend.id
        input_a_id = input_a.id
        input_b_id = input_b.id
        operation_id = operation.id

    await output_save_operations._run_update_backend_inputs_operation(
        settings, operation_id, backend_id, [str(input_b_id)]
    )

    async with maker() as session:
        operation = await session.get(Operation, operation_id)
        stored = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == backend_id)
            )
        ).scalar_one()

    assert [item.id for item in stored.inputs] == [input_a_id]
    assert operation is not None
    assert operation.status == "failed"
    details = json.loads(operation.details_json)
    assert details["input_ids"] == [input_b_id]
    assert "CNC-02001-INPUTS01" in details["flash_error"]
