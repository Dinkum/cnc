from .support import (
    Backend,
    BackendBackup,
    BackgroundTasks,
    BytesIO,
    ControlEvent,
    Input,
    Operation,
    Path,
    Settings,
    SimpleNamespace,
    _PostedRequest,
    _dashboard_client_source,
    _make_session,
    _request,
    app_sandbox_dir,
    backend_commands,
    delete_backend_backup,
    datetime,
    json,
    select,
    selectinload,
    timezone,
    ui_backup,
    ui_pages,
    ui_reads,
    ui_shared,
    view_models,
    write_app_control_assets,
)


def test_backup_summary_includes_covered_paths() -> None:
    backup = SimpleNamespace(
        id=7,
        created_at=None,
        size_bytes=1024,
        scope="mounted_data",
        bundle_path="/tmp/web.tar.gz",
    )

    payload = ui_shared._backup_summary(
        backup,
        {
            "covered_paths_summary": "/srv/web-data",
            "verification_status": "verified",
            "restore_readiness_label": "review before restore",
            "risk_summary": "app bundle does not include a container snapshot",
        },
    )

    assert payload["available"] is True
    assert payload["rows"] == [
        ("covered paths", "/srv/web-data"),
        ("integrity", "verified"),
        ("restore", "review before restore"),
        ("note", "app bundle does not include a container snapshot"),
    ]
    assert not any(
        key in {"latest backup", "size", "file"} for key, _value in payload["rows"]
    )
    assert ("verified", "yes") not in payload["rows"]
    assert ("restore trust", "review before restore") not in payload["rows"]


def test_backup_summary_shows_covered_paths_without_existing_backup() -> None:
    payload = ui_shared._backup_summary(
        None, planned_coverage="/var/lib/cnc/sandboxes/web"
    )

    assert payload["available"] is False
    assert payload["rows"] == [("covered paths", "/var/lib/cnc/sandboxes/web")]


def test_planned_backup_coverage_includes_app_sandbox_and_volumes(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    data_dir = tmp_path / "data"
    backend = Backend(
        name="web",
        kind="app",
        port=12000,
        volumes_json=f'["{data_dir}:/srv/data"]',
    )

    assert ui_shared._planned_backup_coverage_summary(backend, settings) == (
        f"{app_sandbox_dir(settings, 'web')}, {data_dir}"
    )


def test_backup_history_rows_include_covered_paths() -> None:
    backups = [
        SimpleNamespace(
            id=7,
            status="success",
            created_at=None,
            scope="mounted_data",
            size_bytes=1024,
            bundle_path="/tmp/web.tar.gz",
            notes="verified",
            error=None,
        )
    ]

    rows = view_models._backup_history_rows(
        backups,
        {
            7: {
                "covered_paths_summary": "/srv/web-data",
                "verification_status": "verified",
                "restore_readiness_label": "review before restore",
                "risk_summary": "app bundle does not include a container snapshot",
            }
        },
    )

    assert rows[0]["covered_paths_summary"] == "/srv/web-data"
    assert rows[0]["size"] == "1KB"
    assert "verification_status" not in rows[0]
    assert "restore_readiness" not in rows[0]


def test_pending_backup_history_rows_hide_failed_backups() -> None:
    backups = [
        SimpleNamespace(
            id=8,
            status="error",
            created_at=None,
            scope="mounted_data",
            size_bytes=None,
            bundle_path=None,
            notes=None,
            error="archive failed",
        ),
        SimpleNamespace(
            id=7,
            status="success",
            created_at=None,
            scope="mounted_data",
            size_bytes=1024,
            bundle_path="/tmp/web.tar.gz",
            notes="verified",
            error=None,
        ),
    ]

    rows = view_models._pending_backup_history_rows(backups)

    assert [row["id"] for row in rows] == [7]
    assert rows[0]["covered_paths_summary"] == "not recorded"


async def test_backup_backend_form_renders_success(monkeypatch, tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_create_backup(_session, _backend, _settings):
        return SimpleNamespace(
            status="success", scope="mounted_data", size_bytes=1024, error=None
        )

    async def fake_output_context(
        _session, _settings, backend_id: int
    ) -> dict[str, object]:
        return {
            "selected_backend": SimpleNamespace(id=backend_id, name="web"),
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "backup_summary": {"available": False, "rows": []},
            "flash_error": None,
            "flash_success": None,
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_backup, "create_backend_backup", fake_create_backup)
    monkeypatch.setattr(ui_backup, "_output_page_context", fake_output_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

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
                memory_high_override="512M",
                memory_max_override="768M",
                cpu_quota_override="100%",
                enabled=True,
            )
        )
        await session.commit()
        request = _request()
        response = await ui_backup.backup_backend_form(
            1,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            session=session,
        )

    assert response.status_code == 200
    assert captured["name"] == "output_detail.html"
    assert "Backup created:" in captured["context"]["flash_success"]


async def test_backup_backend_form_returns_operation_for_async_request(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()
        request = _request(
            headers=[(b"accept", b"application/json"), (b"x-requested-with", b"fetch")]
        )
        response = await ui_backup.backup_backend_form(
            1,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            session=session,
        )
        operation = (
            await session.execute(
                select(Operation).where(Operation.kind == "backup_backend")
            )
        ).scalar_one()

    assert response.status_code == 202
    payload = json.loads(response.body.decode())
    assert payload == {
        "operation_id": operation.id,
        "operation_status": "queued",
        "message": "Backup started.",
    }
    assert operation.status == "queued"
    assert operation.phase == "queued"
    assert operation.backend_id == 1


async def test_backup_preflight_returns_409_without_creating_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async def blocked(*_args, **_kwargs):
        return "Backup is unavailable while host apply is running."

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_backup, "_host_mutation_preflight_message", blocked)

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
        response = await ui_backup.backup_backend_form(
            1,
            _request(headers=[(b"accept", b"application/json")]),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            session=session,
        )
        operations = list((await session.execute(select(Operation))).scalars())

    assert response.status_code == 409
    assert operations == []


async def test_delete_backup_form_returns_operation_for_async_request(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

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
        await session.flush()
        session.add(
            BackendBackup(
                backend_id=backend.id,
                status="success",
                scope="mounted_data",
                bundle_path=str(tmp_path / "web.tar.gz"),
                size_bytes=1024,
            )
        )
        await session.commit()

        response = await ui_backup.delete_backup_form(
            backend.id,
            1,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/backups/1/delete",
                headers=[
                    (b"accept", b"application/json"),
                    (b"x-requested-with", b"fetch"),
                ],
            ),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            session=session,
        )
        operation = (
            await session.execute(
                select(Operation).where(Operation.kind == "delete_backend_backup")
            )
        ).scalar_one()

    assert response.status_code == 202
    payload = json.loads(response.body.decode())
    assert payload == {
        "operation_id": operation.id,
        "operation_status": "queued",
        "message": "Backup delete started.",
    }
    assert operation.status == "queued"
    assert operation.phase == "queued"
    assert operation.backend_id == 1
    assert json.loads(operation.details_json)["backup_id"] == 1


async def test_delete_backend_backup_removes_bundle_and_history(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
    )
    bundle_dir = tmp_path / "backend-backups" / "web"
    bundle_dir.mkdir(parents=True)
    bundle_path = bundle_dir / "backup.tar.gz"
    bundle_path.write_text("backup", encoding="utf-8")

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
        await session.flush()
        backup = BackendBackup(
            backend_id=backend.id,
            status="success",
            scope="mounted_data",
            bundle_path=str(bundle_path),
            size_bytes=6,
        )
        session.add(backup)
        await session.commit()

        result = await delete_backend_backup(session, backend.id, backup.id, settings)
        stored_backup = (
            await session.execute(select(BackendBackup))
        ).scalar_one_or_none()
        event = (
            await session.execute(
                select(ControlEvent).where(ControlEvent.kind == "backup_deleted")
            )
        ).scalar_one()

    assert result["backup_id"] == 1
    assert result["bundle_deleted"] is True
    assert stored_backup is None
    assert not bundle_path.exists()
    assert not bundle_dir.exists()
    assert event.backend_name == "web"


async def test_restore_backend_form_returns_operation_for_async_request(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()
        request = _request(headers=[(b"accept", b"application/json")])
        response = await ui_backup.restore_backend_form(
            1,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            backup_id=9,
            session=session,
        )
        operation = (
            await session.execute(
                select(Operation).where(Operation.kind == "restore_backend")
            )
        ).scalar_one()

    assert response.status_code == 202
    payload = json.loads(response.body.decode())
    assert payload == {
        "operation_id": operation.id,
        "operation_status": "queued",
        "message": "Restore started.",
    }
    assert operation.status == "queued"
    assert operation.phase == "queued"
    assert operation.backend_id == 1
    assert json.loads(operation.details_json)["backup_id"] == 9


async def test_restore_backend_form_renders_missing_backup_error(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_restore_backup(_session, _backend, _settings):
        raise LookupError("no successful backup available")

    async def fake_output_context(
        _session, _settings, backend_id: int
    ) -> dict[str, object]:
        return {
            "selected_backend": SimpleNamespace(id=backend_id, name="web"),
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "backup_summary": {"available": False, "rows": []},
            "flash_error": None,
            "flash_success": None,
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_backup, "restore_latest_backend_backup", fake_restore_backup)
    monkeypatch.setattr(ui_backup, "_output_page_context", fake_output_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

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
                memory_high_override="512M",
                memory_max_override="768M",
                cpu_quota_override="100%",
                enabled=True,
            )
        )
        await session.commit()
        request = _request()
        response = await ui_backup.restore_backend_form(
            1,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            session=session,
        )

    assert response.status_code == 404
    assert captured["name"] == "output_detail.html"
    assert str(captured["context"]["flash_error"]).startswith(
        "no successful backup available"
    )
    assert " - Error CNC-08012-" in str(captured["context"]["flash_error"])


async def test_restore_backend_form_uses_selected_backup_id(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_get_backup(_session, backend_id: int, backup_id: int):
        assert backend_id == 1
        assert backup_id == 7
        return SimpleNamespace(id=7, backend_id=1, status="success")

    async def fake_restore_backup(_session, _backend, backup, _settings):
        assert backup.id == 7
        return {"backup": backup, "restored_paths": ["/srv/apps/web/data"]}

    async def fake_output_context(
        _session, _settings, backend_id: int
    ) -> dict[str, object]:
        return {
            "selected_backend": SimpleNamespace(id=backend_id, name="web"),
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "backup_summary": {"available": True, "rows": []},
            "backup_history": [],
            "flash_error": None,
            "flash_success": None,
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_backup, "get_backend_backup", fake_get_backup)
    monkeypatch.setattr(ui_backup, "restore_backend_backup", fake_restore_backup)
    monkeypatch.setattr(ui_backup, "_output_page_context", fake_output_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

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
                memory_high_override="512M",
                memory_max_override="768M",
                cpu_quota_override="100%",
                enabled=True,
            )
        )
        await session.commit()
        request = _request()
        response = await ui_backup.restore_backend_form(
            1,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            backup_id=7,
            session=session,
        )

    assert response.status_code == 200
    assert captured["name"] == "output_detail.html"
    assert (
        captured["context"]["flash_success"]
        == "Restore completed from backup #7. Restored 1 mounted path(s)."
    )


async def test_import_backend_form_imports_without_restore(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_import_backup(
        _session, _backend, source_path: Path, *, original_name: str, settings: Settings
    ):
        assert source_path.name == "bundle.tar.gz"
        assert original_name == "bundle.tar.gz"
        return SimpleNamespace(
            id=9, status="success", scope="mounted_data", size_bytes=1024, error=None
        )

    async def fail_restore(*_args, **_kwargs):
        raise AssertionError("import should not restore")

    async def fake_output_context(
        _session, _settings, backend_id: int
    ) -> dict[str, object]:
        return {
            "selected_backend": SimpleNamespace(id=backend_id, name="web"),
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "backup_summary": {"available": True, "rows": []},
            "backup_history": [],
            "flash_error": None,
            "flash_success": None,
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(ui_backup, "import_backend_backup_bundle", fake_import_backup)
    monkeypatch.setattr(ui_backup, "restore_backend_backup", fail_restore)
    monkeypatch.setattr(ui_backup, "_output_page_context", fake_output_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()
        response = await ui_backup.import_restore_backend_form(
            1,
            _request(),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            bundle=SimpleNamespace(filename="bundle.tar.gz", file=BytesIO(b"bundle")),
            session=session,
        )

    assert response.status_code == 200
    assert captured["name"] == "output_detail.html"
    assert captured["context"]["flash_success"] == "Backup #9 imported."


async def test_clone_backend_form_copies_mount_data_and_drops_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_image="docker.io/library/ubuntu:24.04",
            handoff_port=8337,
            volumes_json=f'["{data_dir}:/srv/web"]',
            enabled=True,
        )
        backend.inputs = [
            Input(kind="domain", hostname="web.example.com", enabled=True)
        ]
        session.add(backend)
        await session.commit()
        await session.refresh(backend)
        write_app_control_assets(backend, settings)

        request = _request()
        response = await ui_backup.clone_backend_form(
            backend.id,
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="clone-web",
            port="12001",
            session=session,
        )
        clone = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.name == "clone-web")
            )
        ).scalar_one()

    clone_source = Path(json.loads(clone.volumes_json)[0].split(":", 1)[0])
    assert response.status_code == 201
    payload = json.loads(response.body)
    assert payload["backend_name"] == "clone-web"
    assert payload["backend_port"] == 12001
    assert payload["copied_paths"] == 2
    assert payload["redirect_url"] == f"/outputs/{clone.id}"
    assert clone.name == "clone-web"
    assert clone.enabled is False
    assert clone.inputs == []
    assert clone.port == 12001
    assert app_sandbox_dir(settings, "clone-web").exists()
    assert clone_source != data_dir
    assert (clone_source / "state.txt").read_text(encoding="utf-8") == "hello"


async def test_clone_backend_form_rejects_invalid_port_without_cloning(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_backup, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fail_clone(*_args, **_kwargs):
        raise AssertionError("clone should not run for invalid form data")

    monkeypatch.setattr(backend_commands, "clone_backend", fail_clone)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        response = await ui_backup.clone_backend_form(
            1,
            _PostedRequest(path="/ui/backends/1/clone"),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="clone-web",
            port="abc",
            session=session,
        )

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"].startswith("port must be a whole number")
    assert " - Error CNC-01001-" in payload["error"]


async def test_output_page_context_uses_backup_shell_until_hydration(
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
        await session.flush()
        session.add(
            BackendBackup(
                backend_id=backend.id,
                status="success",
                scope="mounted_data",
                bundle_path=str(tmp_path / "bundle.tar.gz"),
                size_bytes=1024,
            )
        )
        await session.commit()

        context = await ui_shared._output_page_context(session, settings, backend.id)

    assert context["backup_signals_pending"] is True
    assert context["backup_history"] == []
    assert context["latest_backup"] is None
    assert dict(context["backup_summary"]["rows"])["covered paths"] != "loading"


async def test_output_backup_signals_inspects_only_latest_backup(
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
    inspected_ids: list[int] = []

    async def fake_describe_backend_backup(backup, **_kwargs):
        inspected_ids.append(backup.id)
        return {
            "covered_paths_summary": f"/srv/backup-{backup.id}",
            "verification_status": "verified",
        }

    monkeypatch.setattr(
        ui_reads, "describe_backend_backup", fake_describe_backend_backup
    )

    async with maker() as session:
        backend = Backend(name="web", kind="app", port=12000, enabled=True)
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendBackup(
                    backend_id=backend.id,
                    status="success",
                    scope="mounted_data",
                    bundle_path=str(tmp_path / "older.tar.gz"),
                    size_bytes=1024,
                    created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                ),
                BackendBackup(
                    backend_id=backend.id,
                    status="success",
                    scope="mounted_data",
                    bundle_path=str(tmp_path / "latest.tar.gz"),
                    size_bytes=2048,
                    created_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                ),
                BackendBackup(
                    backend_id=backend.id,
                    status="error",
                    scope="mounted_data",
                    bundle_path=None,
                    size_bytes=None,
                    error="backup interrupted",
                    created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
                ),
            ]
        )
        await session.commit()

        payload = await ui_reads.output_backup_signals(
            backend.id, settings=settings, session=session
        )

    assert inspected_ids == [2]
    assert dict(payload["backup_summary_rows"])["covered paths"] == "/srv/backup-2"
    assert payload["backup_history"][0]["covered_paths_summary"] == "/srv/backup-2"
    assert payload["backup_history"][1]["covered_paths_summary"] == "not recorded"
    assert [row["id"] for row in payload["backup_history"]] == [2, 1]


async def test_output_detail_renders_fast_page_shell_with_lazy_backup_hydration(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        beta_hardening=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        multi_node_enabled=True,
    )
    monkeypatch.setattr(
        ui_shared,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "metrics": {
                        "cpu_percent": 12.5,
                        "memory_percent": 31.25,
                        "network_total_bps": 2048.0,
                    },
                }
            ]
        },
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
        await session.flush()
        session.add(
            BackendBackup(
                backend_id=backend.id,
                status="success",
                scope="mounted_data",
                bundle_path=str(tmp_path / "bundle.tar.gz"),
                size_bytes=1024,
            )
        )
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

    body = "\n".join(
        (
            response.body.decode(),
            Path("app/static/js/output-detail.js").read_text(encoding="utf-8"),
        )
    )
    template_text = "\n".join(
        (
            Path("app/templates/output_detail.html").read_text(encoding="utf-8"),
            Path("app/static/js/output-detail.js").read_text(encoding="utf-8"),
        )
    )
    css = Path("app/static/css/app.css").read_text(encoding="utf-8")
    assert response.status_code == 200
    assert "Loading backup details." in body
    assert "const afterInitialLoad = (callback) =>" in template_text
    assert "fetch(`/api/backends/${backendId}/runtime-signals`" in body
    assert "const runtimeSignalsAreRenderable = (payload) =>" in body
    assert 'label !== "unknown"' in body
    assert "renderedRuntimeStatusRowsKey" in body
    assert "currentRuntimeStatusRowsKey" in body
    assert "fetch(`/api/backends/${backendId}/backup-signals`" in body
    assert "data-output-live-metrics=" in body
    assert "cpu_percent" in body
    assert "const outputLiveMetrics = (() =>" in body
    assert "const outputMetricsCacheKey = () =>" in body
    assert "window.sessionStorage?.getItem(outputMetricsCacheKey())" in body
    assert "window.sessionStorage?.setItem(outputMetricsCacheKey()" in body
    assert "writeCachedMetricPayload(payload)" in body
    assert "renderMetricsFromCachedStatus(outputLiveMetrics)" in body
    assert "hydrateOutputMetrics();" in body
    assert "fetch(`/api/backends/${backendId}/metrics-history?" in body
    assert '<option value="disk" ' in body
    assert '<option value="network" ' in body
    assert "width: String(chartWidth)" in body
    assert "const waitForMetricsLoadingPaint = ()" in body
    assert "requestId !== metricsRequestId" in body
    assert "new Chart(ctx" in body
    assert "tooltip:" in body
    assert "payload?.range_start_at" in body
    assert "payload?.range_end_at" in body
    assert "const payloadYAxisMin = Number(payload?.y_axis_min);" in body
    assert "const payloadYAxisMax = Number(payload?.y_axis_max);" in body
    assert "min: yAxisMin" in body
    assert "max: effectiveYMax" in body
    assert "const tooltipTitleForTime = (ts) =>" in body
    assert "return `${weekday}, ${monthDay} ${time}`;" in body
    assert "item.visual_value ?? item.avg" in body
    assert "item.visual_min ?? item.min" in body
    assert "item.visual_max ?? item.max" in body
    assert "return `${Math.round(percent)}% (${formatBytes(bytes)})`;" in body
    assert 'label: "down"' in body
    assert 'label: "up"' in body
    assert 'borderColor: "#68d8ff"' in body
    assert 'borderColor: "#9bf0c7"' in body
    assert (
        '{ label: "peak", value: formatMetricValue(summary.peak_value, unitKind) }'
        in body
    )
    assert (
        '{ label: "avg", value: formatMetricValue(summary.average_value, unitKind) }'
        in body
    )
    assert (
        '{ label: "last", value: formatMetricValue(summary.latest_value, unitKind) }'
        in body
    )
    assert 'display: metricKey === "network"' in body
    assert "return `${label} ${formatMetricValue(item.parsed.y, unitKind)}`;" in body
    assert 'if (metricKey === "disk") return "bytes";' in body
    assert "disk: metrics.disk_usage_bytes" in body
    assert 'interaction: { mode: "index", intersect: false, axis: "x" }' in body
    assert 'mode: "index"' in body
    assert "stroke-dashoffset" not in body
    assert "<animate attributeName" not in body
    assert '"backendId": 1' in body
    assert (
        '<div class="output-page-badges">\n          <span class="stat-chip">app</span>'
        not in body
    )
    assert "<h2>Status</h2>" in body
    assert "<h2>Health</h2>" not in body
    assert "guest profile" in body
    assert '<span class="settings-key">runtime state</span>' not in body
    assert '<span class="settings-key">runtime owner</span>' not in body
    assert 'id="sf-resource-mode"' not in body
    assert "resource mode" not in body
    assert (
        'id="sf-resource-size" name="resource_size" data-resource-size-control' in body
    )
    assert '<option value="auto" selected>auto</option>' in body
    assert '<option value="custom"' in body
    assert "data-custom-resource-limit hidden" in body
    assert "handoff port" in body
    assert "published port" not in body
    assert "internal app port" in body
    assert body.index("<h2>Access</h2>") < body.index("<h2>Recent event history</h2>")
    assert f'<form method="post" action="/api/backends/{backend.id}/ssh-key">' in body
    assert f'href="/api/backends/{backend.id}/ssh-key"' not in body
    assert "Recent event history" in body
    assert "event-details" in template_text
    assert "event-source" not in template_text
    assert "<h2>Security features</h2>" not in body
    assert (
        body.index("<h2>Settings</h2>")
        < body.index("<h2>Shield</h2>")
        < body.index("<h2>Beta features</h2>")
    )
    assert (
        'form="output-settings-form"\n                  name="shield_enabled"' in body
    )
    assert "data-shield-code-configured=" in body
    assert 'href="/?tab=outputs"' in body
    assert 'href="/?tab=settings"' in body
    assert 'href="/#outputs"' not in body
    assert 'href="/#settings"' not in body
    assert "selected_output_detail.get('shield_access_code', '')" in template_text
    assert "please enter an access code to enable shield" in body
    assert 'id="shield-code-error-modal"' in body
    assert "showShieldCodeError(message)" in body
    assert "window.alert(message)" not in body
    assert "<h2>Beta features</h2>" in body
    assert "Experimental Automatic Security Hardening" in body
    assert "Find the tightest runtime flags this app can tolerate." not in body
    assert "Phase 1 - Monitor Only" in body
    assert "Phase 2 - Clone and Break" in body
    assert "Run Phase 1 first before running Phase 2" in body
    assert "Security baseline ready" not in body
    assert "data-hardening-result-shell" not in body
    assert "data-hardening-result-dismiss" not in body
    assert "data-hardening-result-view" not in body
    assert "hardeningResultDismissKey" not in body
    assert "Review the latest baseline before running the clone test." not in body
    assert "data-hardening-result-recommended" not in body
    assert "data-hardening-result-skipped" not in body
    assert "data-hardening-phase-one-pill" not in body
    assert "data-hardening-phase-two-pill" not in body
    assert "data-hardening-phase-one-start" in body
    assert "data-hardening-phase-one-stop hidden" in body
    assert "data-hardening-phase-one-start-time" in body
    assert "data-hardening-phase-one-end-time" in body
    assert "data-hardening-phase-one-fill" in body
    assert "data-hardening-phase-two-start disabled" in body
    assert "Resume Phase 2" in body
    assert "hardeningPhaseTwoCanResume" in body
    assert "data-hardening-phase-two-meta" in body
    assert "hardeningProgressDetails" in body
    assert "data-hardening-features hidden" in body
    assert "bindHardening" in body
    assert "fetch(`/api/backends/${backendId}/hardening`" in body
    assert (
        "new EventSource(`/api/backends/${backendId}/hardening/phase2/events`)" in body
    )
    assert "const HARDENING_STREAM_BACKUP_POLL_MS = 15000;" in body
    assert "phaseTwoActive && phaseTwoStreaming" in body
    assert "hardening/phase1" in body
    assert "hardening/phase2" in body
    assert ' ? "/resume" : ""' in body
    assert "hardening-group-row" in body
    assert "hardening-flag-badge" in body
    assert "row.description" in body
    assert "hardening-evidence-badge" in body
    assert 'hardening-flag-badge" tabindex="0" title=' not in body
    assert 'hardening-evidence-badge" tabindex="0" title=' not in body
    assert "<th>Rating</th>" not in body
    assert "<th>Phase results (P1 / P2)</th>" in body
    assert "<th>Evidence</th>" not in body
    assert "<th>Apply</th>" not in body
    assert 'row.recommendation || "uncertain"' in body
    assert 'colspan="3"' in body
    assert "previousGroup" in body
    assert '<button type="button" class="ghost" disabled>Save</button>' not in body
    assert (
        "5-minute resource history with adaptive bucketing and a smoothed trend line."
        not in body
    )
    assert "Output metric history chart" in body
    assert "metrics-loading-core" in body
    assert "Save attached inputs" in body
    assert "data-output-attach-save hidden" in body
    assert "outputAttachFormIds" in body
    assert "markOutputAttachFormClean(form)" in body
    assert "data-output-save-inline-progress" in body
    assert 'aria-label="Output settings save progress"' in body
    assert "renderInlineOutputSaveProgress(form" in body
    assert "renderOutputSaveProgressForForm(form" in body
    assert "hasLocalOutputSaveProgress" in body
    assert "startOutputSaveProgress(runningLabel, form)" in body
    assert (
        '<h2>Attached inputs</h2>\n          <div class="section-actions">' not in body
    )
    assert "Delete output" in body
    assert "data-transfer-output-trigger" in body
    assert 'id="transfer-output-modal"' in body
    assert "Transfer output" in body
    assert '<select name="target_node"' not in body
    assert "data-transfer-target-picker" in body
    assert "data-transfer-target-value" in body
    assert "data-transfer-mini" in body
    assert "Deleting output" in body
    assert "Removing the output." in body
    assert "data-delete-progress-label" in body
    assert "data-delete-progress-value" not in body
    assert "data-delete-progress-note" in body
    assert "data-delete-progress-status" not in body
    assert ">Hide</button>" not in body
    assert 'data-delete-close aria-label="Close delete dialog"' in body
    assert (
        'deleteProgressFill.classList.toggle("is-complete", normalized >= 100)' in body
    )
    assert "finishDeleteWithoutReload" in body
    assert "startDeleteNarrative" not in body
    assert "completeDeleteProgress" not in body
    assert "intervalMs: 1000" in body
    finish_delete_block = body[
        body.index("const finishDeleteWithoutReload") : body.index(
            "if (deleteModal", body.index("const finishDeleteWithoutReload")
        )
    ]
    assert "window.location.href" not in finish_delete_block
    assert (
        '<button type="button" class="ghost" disabled>Deleting...</button>' not in body
    )
    assert "fetch(formActionPath(deleteForm)" in body
    assert (
        'responseErrorMessage(response, payload, text, "Delete request failed"' in body
    )
    assert '"X-Requested-With": "fetch"' in body
    assert "Creating backup" in body
    assert "operationProgressPipelines" in body
    assert "operationProgressTrackers" in body
    assert "const markFirstOperationStep = " in body
    assert "operationProgressTrackers.set(pipelineName, step.progress)" in body
    assert 'operationProgressTrackers.set("outputSave", firstProgress)' in body
    assert "operationProgress(operation, firstBackupStep.progress, pipeline)" in body
    assert 'operationProgress(operation, fallbackProgress, "outputSave")' in body
    assert (
        'operationProgress(operation, firstDeleteStep.progress, "deleteOutput")' in body
    )
    assert 'operationProgress(operation, firstCloneStep.progress, "clone")' in body
    assert (
        'operationProgress(operation, firstTransferStep.progress, "transferOutput")'
        in body
    )
    assert 'pipeline: "importBackup"' in body
    assert 'pipeline: "restore"' in body
    assert 'pipeline: "deleteBackup"' in body
    assert (
        "const cloneNarratives = ((operationProgressPipelines.clone || [])" not in body
    )
    assert "outputSaveProgressSteps" in body
    assert "startOutputSaveProgress(runningLabel, form)" in body
    assert "window.setInterval(renderStep, 1200)" not in body
    assert "renderOutputSaveOperationProgress" in body
    assert "renderOutputSaveOperationResult" in body
    assert "renderOutputSaveProgressForForm(form, {" in body
    assert 'role="progressbar"' in body
    assert "Reading output form" in body
    assert "Checking changed slices" in body
    assert "data-backup-progress-label" not in body
    assert "data-backup-progress-note" not in body
    assert "shouldAppendBackupNarrative" in body
    assert '<span class="metric-label">Choose file</span>' not in body
    assert "status-checks-value" in body
    assert "status-check-components" in body
    assert "Refreshing backup history..." in body
    assert "data-backup-progress-value" not in body
    assert "showSuccessBanner: false" in body
    assert "data-backup-form" in body
    assert "<th>Notes</th>" not in body
    assert "item.notes" not in body
    assert "backup-import-card" in body
    assert "data-import-backup-form" in body
    assert "Verify and import" in body
    assert "Import and restore" not in body
    assert "data-import-restore-form" not in body
    assert "data-delete-backup-form" in body
    assert "data-delete-backup-trigger" in body
    dashboard_body = "\n".join(
        (
            Path("app/templates/index.html").read_text(encoding="utf-8"),
            _dashboard_client_source(),
        )
    )
    assert "detail.get('shield_access_code', '')" in dashboard_body
    assert "const stageProgress = Number(stage.progress);" in dashboard_body
    assert (
        "const stageRegressed = Number.isFinite(stageProgress) && stageProgress < lastProgress;"
        in dashboard_body
    )
    assert "progressRegressed || stageRegressed ? lastStage : stage" in dashboard_body
    assert "if (!progressRegressed && !stageRegressed)" in dashboard_body
    assert "delete-backup-modal" in body
    assert "Deleting backup" in body
    assert "ssh web@cnc-admin.example.ts.net" in body
    assert "ssh web@SERVER_IP" not in body
    assert ".output-add-dropdown[hidden]" in css
    assert ".output-input-card .output-add-dropdown" in css
    assert "width: min(420px, calc(100vw - 64px));" in css
    assert ".output-inline-save-progress" in css
    assert ".output-settings-save-progress" in css
    assert ".output-inline-save-track" in css
    assert ".metrics-chart-frame.is-loading" in css
    assert "metrics-loading-fade-in" in css
    assert ".metrics-loading-core::before" not in css
    assert ".metrics-loading-core::after" not in css
    assert "[hidden]" in css
    assert ".debug-card .detail-columns" in css
    assert ".delete-progress-state" in css
    assert ".delete-progress-status" not in css
    assert ".modal-close-button" in css
    assert ".backup-action-buttons" in css
    assert "#backup-history-block th:last-child" in css
    assert '#backup-history-block td:nth-child(5)::before { content: "Paths"; }' in css
    assert ".backup-import-card" in css
    assert ".hardening-phase-grid" in css
    assert ".hardening-lock-overlay" in css
    assert ".hardening-result-card" not in css
    assert ".hardening-result-actions" not in css
    assert "padding-right: 70px;" not in css
    assert ".hardening-monitor-track" in css
    assert ".hardening-monitor-fill" in css
    assert ".hardening-spinner" in css
    assert "@keyframes hardening-dots" in css
    assert "overflow-x: hidden;" in css
    assert ".code-block pre,\n  .compact-block pre" in css
    assert "button:disabled" in css
