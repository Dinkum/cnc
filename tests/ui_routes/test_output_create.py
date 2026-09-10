import app.ui.dashboard.context as ui_dashboard_context
import app.ui.dashboard.data as ui_dashboard_data
import app.ui.http as ui_http

from .support import (
    CREATE_BACKEND_OPERATION_STEPS,
    ApplyResponse,
    Backend,
    BackendIn,
    BackgroundTasks,
    Input,
    Operation,
    Path,
    Settings,
    SimpleNamespace,
    _CreateBackendProgressRecorder,
    _FormRequest,
    _healthy_status,
    _make_session,
    _PostedRequest,
    create_backend_progress_steps,
    create_backend_progress_value,
    create_backend_runtime_progress,
    create_backend_runtime_summary,
    json,
    output_lifecycle_operations,
    select,
    ui_output,
    write_bootstrap_state,
)


async def test_create_backend_form_surfaces_static_root_hint_on_validation_error(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        captured["dashboard_kwargs"] = _kwargs
        return {"flash_error": None, "flash_success": None}

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(
        ui_dashboard_context, "dashboard_context", fake_dashboard_context
    )
    monkeypatch.setattr(ui_http, "dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        request = _FormRequest()
        response = await ui_output.create_backend_form(
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="docs",
            kind="static",
            port="",
            static_root="srv/site",
            sandbox_image="",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )

    assert response.status_code == 400
    assert captured["name"] == "index.html"
    assert captured["dashboard_kwargs"] == {
        "prefer_cached_status": True,
        "active_tab": "outputs",
        "defer_status": True,
    }
    assert captured["context"]["active_tab"] == "outputs"
    flash_error = str(captured["context"]["flash_error"])
    assert flash_error.startswith(
        "static_root must be absolute: srv/site. "
        "Use an absolute host path like /srv/site or /var/www/docs. Static roots cannot contain spaces."
    )
    assert " - Error CNC-01001-" in flash_error


async def test_create_backend_form_surfaces_volume_boundary_hint_on_validation_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr(
        ui_dashboard_context, "dashboard_context", fake_dashboard_context
    )
    monkeypatch.setattr(ui_http, "dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        request = _FormRequest()
        response = await ui_output.create_backend_form(
            request,
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_image="docker.io/library/ubuntu:24.04",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json='["/srv/web-data:/cnc/control:ro"]',
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )

    assert response.status_code == 400
    assert captured["name"] == "index.html"
    assert captured["context"]["active_tab"] == "outputs"
    flash_error = str(captured["context"]["flash_error"])
    assert flash_error.startswith(
        "volume target path for web cannot overlap cnc-reserved container path /cnc: /cnc/control. "
        "Use app-owned data paths only. Volumes cannot point at CNC-managed host paths, host runtime paths, "
        "or CNC control paths inside the container."
    )
    assert " - Error CNC-01001-" in flash_error


async def test_create_backend_form_rejects_invalid_port_without_apply(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"flash_error": None, "flash_success": None}

    async def fail_run_apply(_session, _settings):
        raise AssertionError("run_apply should not be called for invalid form data")

    def fake_template_response(request, name, context, status_code=200):
        captured["name"] = name
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(
        ui_dashboard_context, "dashboard_context", fake_dashboard_context
    )
    monkeypatch.setattr(ui_http, "dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_output, "run_apply", fail_run_apply)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        response = await ui_output.create_backend_form(
            _PostedRequest(path="/ui/backends"),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="not-a-port",
            static_root="",
            sandbox_profile="ubuntu-24.04-systemd",
            sandbox_image="",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )

        stored = (await session.execute(select(Backend))).scalars().all()

    assert response.status_code == 400
    assert stored == []
    assert captured["name"] == "index.html"
    assert captured["context"]["active_tab"] == "outputs"
    flash_error = str(captured["context"]["flash_error"])
    assert flash_error.startswith("port must be a whole number")
    assert " - Error CNC-01001-" in flash_error


async def test_create_backend_form_succeeds_with_attached_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_output_context(
        session, _settings, backend_id: int
    ) -> dict[str, object]:
        backend = await session.get(Backend, backend_id)
        return {
            "selected_backend": backend,
            "selected_output_detail": {},
            "output_signal_cards": [],
            "output_info_rows": [],
            "backup_summary": {"available": False, "rows": []},
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

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=7
        )

    monkeypatch.setattr(ui_output, "output_page_context", fake_output_context)
    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        session.add(Input(kind="domain", hostname="api.example.com", enabled=True))
        await session.commit()
        response = await ui_output.create_backend_form(
            _PostedRequest(path="/ui/backends", fields={"input_ids": ["1"]}),
            background_tasks=BackgroundTasks(),
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_image="docker.io/library/ubuntu:24.04",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )

    assert response.status_code == 201
    assert captured["name"] == "output_detail.html"
    assert captured["context"]["flash_success"] == "Output created. Host updated."
    assert captured["context"]["selected_backend"].resource_mode == "auto"
    assert captured["context"]["selected_backend"].resource_size == "small"


async def test_create_backend_form_returns_operation_for_dashboard_create(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fail_output_context(
        _session, _settings, _backend_id: int
    ) -> dict[str, object]:
        raise AssertionError("async output create should refresh the dashboard")

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=7
        )

    monkeypatch.setattr(ui_output, "output_page_context", fail_output_context)
    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)

    async with maker() as session:
        background_tasks = BackgroundTasks()
        response = await ui_output.create_backend_form(
            _PostedRequest(
                path="/ui/backends", headers=[(b"x-cnc-dashboard-refresh", b"1")]
            ),
            background_tasks=background_tasks,
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_image="docker.io/library/ubuntu:24.04",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id)))
            .scalars()
            .all()
        )

    assert response.status_code == 202
    assert len(background_tasks.tasks) == 1
    payload = json.loads(response.body)
    assert payload["operation_id"] == operations[0].id
    assert payload["operation_status"] == "queued"
    assert operations[0].kind == "create_backend"
    assert operations[0].phase == "queued"


async def test_async_create_backend_operation_passes_deferred_progress_recorder(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
    )
    seen: dict[str, object] = {}

    async def fake_run_apply(
        _session,
        _settings,
        *,
        operation_handle=None,
        services=None,
        commit_on_success=True,
        emit_success_event=True,
    ):
        assert operation_handle is not None
        assert isinstance(operation_handle.id, int)
        assert operation_handle.kind == "create_backend"
        assert operation_handle.completed is False
        deferred = operation_handle.with_deferred_db_updates()
        seen["deferred_type"] = type(deferred).__name__
        assert deferred.id == operation_handle.id
        assert deferred.kind == operation_handle.kind
        assert deferred.completed is False
        deferred.completed = True
        assert deferred.completed is True
        deferred.completed = False
        await deferred.update(
            status="running",
            phase="Apply",
            details={"progress": 55, "message": "Applying output"},
        )
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=42,
        )

    monkeypatch.setattr(output_lifecycle_operations, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        output_lifecycle_operations,
        "create_backend_runtime_summary",
        lambda *_args: None,
    )

    async with maker() as session:
        operation = Operation(
            kind="create_backend",
            status="queued",
            phase="queued",
            actor="ui",
            details_json="{}",
        )
        session.add(operation)
        await session.commit()
        operation_id = operation.id

    payload = BackendIn(
        name="sample-app",
        kind="app",
        port=None,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8000,
        healthcheck_mode="none",
        healthcheck_path="/",
        resource_mode="auto",
        resource_size="small",
        volumes_json="[]",
        enabled=True,
        input_ids=[],
    )

    await output_lifecycle_operations._run_create_backend_operation(
        settings, operation_id, payload
    )

    async with maker() as session:
        operation = await session.get(Operation, operation_id)
        backend = (
            await session.execute(select(Backend).where(Backend.name == "sample-app"))
        ).scalar_one()

    assert seen["deferred_type"] == "_CreateBackendProgressRecorder"
    assert backend.enabled is True
    assert operation is not None
    assert operation.status == "success"
    assert operation.phase == "Create output"
    operation_details = json.loads(operation.details_json)
    assert operation_details["flash_success"] == "Output created. Host updated."
    assert operation_details["backend_id"] == backend.id
    assert operation_details["backend_name"] == "sample-app"


async def test_async_create_backend_apply_failure_uses_snapshotted_identity(
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
                "error_inst": "CREATE01",
            },
            run_id=43,
        )

    def fail_runtime_summary(*_args, **_kwargs):
        raise AssertionError("failed create must not inspect runtime summary")

    monkeypatch.setattr(output_lifecycle_operations, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        output_lifecycle_operations,
        "create_backend_runtime_summary",
        fail_runtime_summary,
    )

    async with maker() as session:
        operation = Operation(
            kind="create_backend",
            status="queued",
            phase="queued",
            actor="ui",
            details_json="{}",
        )
        session.add(operation)
        await session.commit()
        operation_id = operation.id

    payload = BackendIn(
        name="sample-app",
        kind="app",
        port=None,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8000,
        healthcheck_mode="none",
        healthcheck_path="/",
        resource_mode="auto",
        resource_size="small",
        volumes_json="[]",
        enabled=True,
        input_ids=[],
    )

    await output_lifecycle_operations._run_create_backend_operation(
        settings, operation_id, payload
    )

    async with maker() as session:
        operation = await session.get(Operation, operation_id)
        stored = (
            await session.execute(select(Backend).where(Backend.name == "sample-app"))
        ).scalar_one_or_none()

    assert stored is None
    assert operation is not None
    assert operation.status == "failed"
    details = json.loads(operation.details_json)
    assert details["backend_name"] == "sample-app"
    assert details["backend_id"] is None
    assert details["apply_status"] == "error"
    assert "CNC-02001-CREATE01" in details["flash_error"]


async def test_create_backend_form_blocks_dashboard_create_during_active_host_operation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_collect_status(_session, _settings, **_kwargs):
        return _healthy_status(backend_count=1)

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return SimpleNamespace(status_code=status_code, raw_headers=[])

    monkeypatch.setattr(ui_dashboard_data, "collect_status", fake_collect_status)
    monkeypatch.setattr(ui_http.templates, "TemplateResponse", fake_template_response)

    async with maker() as session:
        session.add(
            Operation(
                kind="ui.input.create", status="running", phase="apply", actor="system"
            )
        )
        await session.commit()
        background_tasks = BackgroundTasks()
        response = await ui_output.create_backend_form(
            _PostedRequest(
                path="/ui/backends", headers=[(b"x-cnc-dashboard-refresh", b"1")]
            ),
            background_tasks=background_tasks,
            settings=settings,
            csrf_token="token",
            name="web",
            kind="app",
            port="12000",
            static_root="",
            sandbox_image="docker.io/library/ubuntu:24.04",
            handoff_port=8000,
            healthcheck_mode="tcp",
            healthcheck_path="/",
            memory_high_override="",
            memory_max_override="",
            cpu_quota_override="",
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            notes="",
            enabled=True,
            session=session,
        )
        operations = (
            (await session.execute(select(Operation).order_by(Operation.id)))
            .scalars()
            .all()
        )

    assert response.status_code == 409
    assert len(background_tasks.tasks) == 0
    assert len(operations) == 1
    assert captured["context"]["active_tab"] == "outputs"
    assert str(captured["context"]["flash_error"]).startswith(
        "Input create is already running (apply). Output create can't start until it finishes. Try again later."
    )
    assert " - Error CNC-08013-" in str(captured["context"]["flash_error"])


def test_create_backend_runtime_progress_reads_bootstrap_substep(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
    )
    write_bootstrap_state(
        settings,
        "sample-app-k",
        {
            "status": "running",
            "reconcile_phase": "create",
            "reconcile_phase_status": "running",
            "reconcile_details": {"substep": "provision_guest_tools"},
            "events": [
                {
                    "timestamp": "2026-05-01T04:25:00Z",
                    "kind": "seed",
                    "name": "provision_guest_tools",
                    "status": "running",
                    "details": {"substep": "provision_guest_tools"},
                }
            ]
            + [
                {
                    "timestamp": f"2026-05-01T04:25:{index:02d}Z",
                    "kind": "phase",
                    "name": f"step_{index}",
                    "status": "succeeded",
                }
                for index in range(1, 18)
            ],
        },
    )

    progress = create_backend_runtime_progress(settings, "sample-app-k")

    assert progress == {
        "phase": "Provision guest",
        "status": "running",
        "substep": "provision_guest_tools",
        "substate": "Installing guest tools",
        "message": "Installing guest tools",
        "progress": create_backend_progress_value(
            "Provision guest", substep="provision_guest_tools"
        ),
        "updated_at": progress["updated_at"],
    }
    summary = create_backend_runtime_summary(settings, "sample-app-k")
    assert summary is not None
    assert summary["message"] == "Installing guest tools"
    assert summary["events"][0]["name"] == "provision_guest_tools"
    assert summary["events"][-1]["name"] == "step_17"


def test_create_backend_runtime_progress_ignores_completed_bootstrap_step(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
    )
    write_bootstrap_state(
        settings,
        "sample-app-k",
        {
            "status": "running",
            "reconcile_phase": "create",
            "reconcile_phase_status": "succeeded",
            "reconcile_details": {"substep": "rootfs_reuse"},
        },
    )

    assert create_backend_runtime_progress(settings, "sample-app-k") is None
    summary = create_backend_runtime_summary(settings, "sample-app-k")
    assert summary is not None
    assert summary["phase"] == "Prepare guest filesystem"
    assert summary["message"] == "Reusing existing rootfs"


def test_create_backend_progress_uses_substep_position() -> None:
    assert (
        create_backend_progress_value(
            "Prepare guest filesystem", substep="seed_archive_extract"
        )
        == 35
    )
    assert (
        create_backend_progress_value("Provision guest", substep="profile_self_test")
        == 51
    )
    assert (
        create_backend_progress_value("Bootstrap guest", substep="guest_readiness_wait")
        == 66
    )
    assert (
        create_backend_progress_value(
            "Bootstrap guest", current=84, substep="guest_readiness_wait"
        )
        == 84
    )


def test_create_backend_progress_catalog_covers_known_state_substate_pairs() -> None:
    steps = create_backend_progress_steps()
    pairs = {(str(step["headline"]), str(step["substep"])) for step in steps}

    assert len(steps) == 42
    progress_values = [int(step["progress"]) for step in steps]
    assert progress_values == sorted(progress_values)
    assert len(progress_values) == len(set(progress_values))
    assert all(int(step.get("state_weight", 0)) >= 1 for step in steps)
    assert all(int(step.get("substep_weight", 0)) >= 1 for step in steps)
    assert all(int(step["state_weight"]) <= 3 for step in steps)
    assert all(int(step["substep_weight"]) <= 3 for step in steps)
    non_default_state_weights = {
        str(step["headline"]): int(step["state_weight"])
        for step in steps
        if int(step["state_weight"]) != 1
    }
    non_default_substep_weights = {
        (str(step["headline"]), str(step["substep"])): int(step["substep_weight"])
        for step in steps
        if int(step["substep_weight"]) != 1
    }
    assert non_default_state_weights == {
        "Plan runtime": 2,
        "Prepare guest filesystem": 2,
        "Provision guest": 3,
        "Apply host access": 2,
        "Verify output": 2,
    }
    assert non_default_substep_weights == {
        ("Prepare host", "Checking admin exposure"): 2,
        ("Prepare guest filesystem", "Exporting seed filesystem"): 2,
        ("Prepare guest filesystem", "Extracting guest filesystem"): 2,
        ("Provision guest", "Installing guest runtime"): 2,
        ("Provision guest", "Installing guest tools"): 2,
        ("Apply host access", "Validating host proxy config"): 2,
        ("Apply host access", "Reloading host proxy"): 2,
        ("Verify output", "Auditing control plane"): 2,
    }
    for phase, substep, _note in CREATE_BACKEND_OPERATION_STEPS:
        assert (phase, substep) in pairs
    assert ("Save desired state", "Building host plan") in pairs
    assert ("Prepare host", "Checking tailnet service host") in pairs
    assert ("Prepare host", "Checking admin exposure") in pairs
    assert ("Prepare Shield", "Preparing Shield gate") in pairs
    assert ("Apply cluster", "Syncing follower ingress") in pairs
    assert ("Verify output", "Verifying admin exposure") in pairs
    assert ("Apply host access", "Verifying admin exposure") not in pairs
    assert ("Prepare guest filesystem", "Reusing existing rootfs") not in pairs
    assert ("Install service", "Reloading service manager") not in pairs


async def test_create_backend_progress_recorder_keeps_substate_timings(
    tmp_path: Path,
) -> None:
    class FakeOperation:
        id = 91
        kind = "create_backend"
        completed = False
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
            app_control_dir=tmp_path / "app-control",
        )

        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        async def update(self, **kwargs: object) -> None:
            self.updates.append(kwargs)

        async def complete(self, status: str, **kwargs: object) -> None:
            self.updates.append({"status": status, **kwargs})

        def with_deferred_db_updates(self) -> "FakeOperation":
            deferred = FakeOperation()
            deferred.updates = self.updates
            return deferred

    fake_operation = FakeOperation()
    recorder = _CreateBackendProgressRecorder(fake_operation)
    deferred = recorder.with_deferred_db_updates()

    assert isinstance(deferred, _CreateBackendProgressRecorder)
    assert deferred is not recorder
    assert deferred.id == 91
    assert deferred.kind == "create_backend"
    assert deferred.completed is False
    deferred.completed = True
    assert deferred.completed is True
    deferred.completed = False

    for progress, substate in (
        (6, "Opening save transaction"),
        (6, "Opening save transaction"),
        (7, "Staging output config"),
    ):
        await recorder.update(
            status="running",
            phase="Save desired state",
            details={"progress": progress, "message": substate, "substate": substate},
        )
    await recorder.complete(
        "success",
        phase="Create output",
        details={"progress": 100, "message": "Output created."},
    )

    final_details = fake_operation.updates[-1]["details"]
    assert isinstance(final_details, dict)
    timings = final_details["progress_timings"]
    assert isinstance(timings, list)
    assert [
        (item["phase"], item["substate"], item["progress"], item["updates"])
        for item in timings
    ] == [
        ("Save desired state", "Opening save transaction", 6, 2),
        ("Save desired state", "Staging output config", 7, 1),
    ]
    assert all(str(item["first_seen_at"]).endswith("Z") for item in timings)
    assert all(str(item["last_seen_at"]).endswith("Z") for item in timings)


async def test_update_backend_form_preserves_created_output_kind(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=52
        )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        return {
            "selected_backend": SimpleNamespace(id=backend_id),
            "flash_error": None,
            "flash_success": None,
        }

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    monkeypatch.setattr(ui_output, "output_page_context", fake_output_page_context)
    monkeypatch.setattr(
        ui_output,
        "render_output_template",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200),
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
            name="web",
            kind="static",
            port="12000",
            static_root="/srv/static",
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
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one()

    assert response.status_code == 200
    assert stored.kind == "app"
    assert stored.notes == "after"
