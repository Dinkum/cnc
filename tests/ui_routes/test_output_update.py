import app.services.resource_profile as app_services_resource_profile
import app.ui.outputs.context as ui_outputs_context
import app.ui.resources as ui_resources

from .support import (
    ApplyResponse,
    Backend,
    Input,
    Operation,
    Path,
    Settings,
    SimpleNamespace,
    _make_session,
    _PostedRequest,
    json,
    make_code_hash,
    select,
    selectinload,
    ui_output,
    ui_reads,
)


async def test_output_resource_limits_use_full_enabled_app_mix(
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
    monkeypatch.setattr(ui_reads, "peek_cached_status", lambda: {"services": []})

    async with maker() as session:
        web = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            resource_mode="auto",
            resource_size="small",
            volumes_json="[]",
            enabled=True,
        )
        large_neighbor = Backend(
            name="large-neighbor",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            resource_mode="manual",
            resource_size="large",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([web, large_neighbor])
        await session.commit()

        context = await ui_outputs_context.output_page_context(
            session, settings, web.id, prefer_cached_runtime=True
        )
        memory_payload = await ui_reads.output_metric_history(
            web.id,
            metric="memory",
            timeframe="day",
            settings=settings,
            session=session,
        )

        full_mix_profile = app_services_resource_profile.build_resource_profile(
            settings, [web, large_neighbor]
        )
        selected_only_profile = app_services_resource_profile.build_resource_profile(
            settings, [web]
        )
        expected = app_services_resource_profile.backend_resource_profile(
            web, full_mix_profile
        )
        selected_only = app_services_resource_profile.backend_resource_profile(
            web, selected_only_profile
        )

    output_info_rows = {row[0]: row[1] for row in context["output_info_rows"]}
    detail = context["selected_output_detail"]
    assert detail["resource_memory_high"] == expected.memory_high
    assert detail["resource_memory_max"] == expected.memory_max
    assert detail["resource_memory_max"] != selected_only.memory_max
    assert output_info_rows["memory target"] == ui_resources.format_memory_limit(
        expected.memory_high
    )
    assert output_info_rows["memory cap"] == ui_resources.format_memory_limit(
        expected.memory_max
    )
    assert memory_payload["soft_limit_bytes"] == ui_resources._parse_memory_limit_bytes(
        expected.memory_high
    )
    assert memory_payload[
        "memory_limit_bytes"
    ] == ui_resources._parse_memory_limit_bytes(expected.memory_max)


def test_backend_update_auto_resource_choice_preserves_current_size() -> None:
    payload = ui_output._backend_update_payload_from_form(
        name="web",
        kind="app",
        port="12000",
        static_root="",
        sandbox_profile="",
        sandbox_image="docker.io/library/ubuntu:24.04",
        handoff_port=8000,
        healthcheck_mode="tcp",
        healthcheck_path="/",
        healthcheck_host_header="",
        resource_mode="auto",
        resource_size="auto",
        current_resource_size="large",
        memory_high_override="",
        memory_max_override="",
        cpu_quota_override="",
        volumes_json="[]",
        notes="ordinary settings save",
    )

    assert payload.resource_mode == "auto"
    assert payload.resource_size == "large"
    assert payload.memory_high_override is None
    assert payload.memory_max_override is None
    assert payload.cpu_quota_override is None


async def test_update_backend_form_keeps_inputs_and_enabled_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=52
        )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        assert backend_id == 1
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    def fake_render_output_template(request, settings, context, *, status_code=200):
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output, "render_output_template", fake_render_output_template
    )

    async with maker() as session:
        input_item = Input(kind="domain", hostname="web.example.com", enabled=True)
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
            notes="before",
        )
        backend.inputs = [input_item]
        session.add_all([input_item, backend])
        await session.commit()

        response = await ui_output.update_backend_form(
            1,
            _PostedRequest(path="/ui/backends/1"),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_profile="ubuntu-24.04-systemd",
            sandbox_image="",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            resource_mode="auto",
            resource_size="small",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="after",
            session=session,
        )

        stored = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == 1)
            )
        ).scalar_one()

    assert response.status_code == 200
    assert stored.enabled is True
    assert [item.hostname for item in stored.inputs] == ["web.example.com"]
    assert stored.notes == "after"
    assert captured["context"]["flash_success"] == "Output saved. Host updated."


async def test_update_backend_form_runs_apply_for_shield_code_change(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        app_control_dir=tmp_path / "app-control",
        app_quadlet_dir=tmp_path / "quadlets",
        shield_state_dir=tmp_path / "shield",
        shield_env_file_path=tmp_path / "shield" / "shield.env",
        shield_config_path=tmp_path / "shield" / "shield-config.json",
        shield_secret_key="secret",
    )
    captured: dict[str, object] = {}
    old_hash = make_code_hash(settings.shield_secret_key, "old access code")
    new_hash = make_code_hash(settings.shield_secret_key, "new access code")
    settings.shield_config_path.parent.mkdir(parents=True)
    settings.shield_config_path.write_text(
        json.dumps({"outputs": {"web": {"code_hash": old_hash}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    apply_calls = {"count": 0}

    async def fake_run_apply(_session, _settings):
        apply_calls["count"] += 1
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=81,
        )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    def fake_render_output_template(request, settings, context, *, status_code=200):
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output, "render_output_template", fake_render_output_template
    )

    async with maker() as session:
        app_input = Input(kind="domain", hostname="web.example.com", enabled=True)
        shield_input = Input(kind="shield", hostname="shield.example.com", enabled=True)
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            resource_mode="manual",
            resource_size="small",
            shield_enabled=True,
            shield_code_hash=old_hash,
            volumes_json="[]",
            enabled=True,
            notes="same",
        )
        shield_backend = Backend(
            name="shield",
            kind="shield",
            volumes_json="[]",
            enabled=True,
        )
        backend.inputs = [app_input]
        shield_backend.inputs = [shield_input]
        session.add_all([app_input, shield_input, backend, shield_backend])
        await session.commit()
        backend_id = backend.id

        response = await ui_output.update_backend_form(
            backend_id,
            _PostedRequest(path=f"/ui/backends/{backend_id}"),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_profile="ubuntu-24.04-systemd",
            sandbox_image="",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            resource_mode="auto",
            resource_size="small",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="same",
            shield_enabled=True,
            shield_access_code="new access code",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    assert response.status_code == 200
    assert stored.shield_code_hash == new_hash
    assert stored.shield_access_code == "new access code"
    assert apply_calls == {"count": 1}
    assert operations == []
    assert captured["context"]["flash_success"] == "Output saved. Host updated."


async def test_update_backend_form_returns_json_without_rendering_page(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=52
        )

    def fail_render(*_args, **_kwargs):
        raise AssertionError("JSON output save should not render a full page")

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "render_output_template", fail_render)

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

        response = await ui_output.update_backend_form(
            1,
            _PostedRequest(
                path="/ui/backends/1",
                headers=[
                    (b"accept", b"application/json"),
                    (b"x-requested-with", b"fetch"),
                ],
            ),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_profile="ubuntu-24.04-systemd",
            sandbox_image="",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="web.example.com",
            resource_mode="auto",
            resource_size="small",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="after",
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    body = json.loads(response.body)
    assert response.status_code == 202
    assert body["operation_status"] == "queued"
    assert body["message"] == "Output save started."
    assert body["operation_id"] == operations[0].id
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.backend.update", "queued", "Validate output")]


async def test_update_backend_form_rejects_invalid_port_before_mutating(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fail_run_apply(_session, _settings):
        raise AssertionError("run_apply should not be called for invalid form data")

    async def fake_output_page_context(_session, _settings, backend_id: int):
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    def fake_render_output_template(request, settings, context, *, status_code=200):
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_output, "run_apply", fail_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output, "render_output_template", fake_render_output_template
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
            notes="before",
        )
        session.add(backend)
        await session.commit()

        response = await ui_output.update_backend_form(
            1,
            _PostedRequest(path="/ui/backends/1"),
            settings=settings,
            csrf_token="token",
            name="web-renamed",
            kind="app",
            port="bad",
            static_root="",
            sandbox_profile="ubuntu-24.04-systemd",
            sandbox_image="",
            handoff_port=8337,
            healthcheck_mode="http",
            healthcheck_path="/health",
            healthcheck_host_header="",
            resource_mode="auto",
            resource_size="small",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="after",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one()

    assert response.status_code == 400
    assert stored.name == "web"
    assert stored.port == 12000
    assert stored.notes == "before"
    flash_error = str(captured["context"]["flash_error"])
    assert flash_error.startswith("port must be a whole number")
    assert " - Error CNC-01001-" in flash_error


async def test_update_backend_inputs_form_updates_attached_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=53
        )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    def fake_render_output_template(request, settings, context, *, status_code=200):
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output, "render_output_template", fake_render_output_template
    )

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
        session.add_all([input_a, input_b, backend])
        await session.commit()

        response = await ui_output.update_backend_inputs_form(
            1,
            _PostedRequest(path="/ui/backends/1/inputs", fields={"input_ids": ["2"]}),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == 1)
            )
        ).scalar_one()

    assert response.status_code == 200
    assert [item.hostname for item in stored.inputs] == ["b.example.com"]
    assert (
        captured["context"]["flash_success"] == "Attached inputs saved. Host updated."
    )


async def test_update_backend_inputs_form_returns_json_without_rendering_page(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=53
        )

    def fail_render(*_args, **_kwargs):
        raise AssertionError("JSON attached-input save should not render a full page")

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "render_output_template", fail_render)

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
        session.add_all([input_a, input_b, backend])
        await session.commit()

        response = await ui_output.update_backend_inputs_form(
            1,
            _PostedRequest(
                path="/ui/backends/1/inputs",
                fields={"input_ids": ["2"]},
                headers=[
                    (b"accept", b"application/json"),
                    (b"x-requested-with", b"fetch"),
                ],
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

    body = json.loads(response.body)
    assert response.status_code == 202
    assert body["operation_status"] == "queued"
    assert body["message"] == "Attached inputs save started."
    assert body["operation_id"] == operations[0].id
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.backend.inputs", "queued", "Validate output")]


async def test_update_backend_state_form_disables_output(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=54
        )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    def fake_render_output_template(request, settings, context, *, status_code=200):
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output, "render_output_template", fake_render_output_template
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

        response = await ui_output.update_backend_state_form(
            1,
            _PostedRequest(path="/ui/backends/1/state"),
            settings=settings,
            csrf_token="token",
            action="disable",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one()

    assert response.status_code == 200
    assert stored.enabled is False
    assert captured["context"]["flash_success"] == "Output disabled. Host updated."


async def test_update_backend_state_form_returns_json_without_rendering_page(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=54
        )

    def fail_render(*_args, **_kwargs):
        raise AssertionError("JSON output state save should not render a full page")

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "render_output_template", fail_render)

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

        response = await ui_output.update_backend_state_form(
            1,
            _PostedRequest(
                path="/ui/backends/1/state",
                headers=[
                    (b"accept", b"application/json"),
                    (b"x-requested-with", b"fetch"),
                ],
            ),
            settings=settings,
            csrf_token="token",
            action="disable",
            session=session,
        )
        stored = (
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one()
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id.asc())))
            .scalars()
            .all()
        )

    body = json.loads(response.body)
    assert response.status_code == 202
    assert stored.enabled is True
    assert body["operation_status"] == "queued"
    assert body["message"] == "Output state save started."
    assert body["operation_id"] == operations[0].id
    assert [
        (operation.kind, operation.status, operation.phase) for operation in operations
    ] == [("ui.backend.state", "queued", "Validate output")]


async def test_output_state_preflight_returns_409_without_creating_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async def blocked(*_args, **_kwargs):
        return "Output state save is unavailable while host apply is running."

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_output, "host_mutation_preflight_message", blocked)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()
        response = await ui_output.update_backend_state_form(
            1,
            _PostedRequest(
                path="/ui/backends/1/state",
                headers=[(b"accept", b"application/json")],
            ),
            settings=settings,
            csrf_token="token",
            action="disable",
            session=session,
        )
        operations = list((await session.execute(select(Operation))).scalars())

    assert response.status_code == 409
    assert operations == []
