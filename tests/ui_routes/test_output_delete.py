from .support import (
    ApplyResponse,
    Backend,
    Operation,
    Path,
    Settings,
    _PostedRequest,
    _cookie_values,
    _make_session,
    json,
    select,
    output_lifecycle_operations,
    ui_output,
)


async def test_delete_backend_form_deletes_backend_after_successful_apply(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    captured: dict[str, object] = {}

    async def fake_run_apply(_session, _settings, services=None):
        captured["services"] = services
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=55
        )

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    removed: list[list[str]] = []

    async def fake_remove_backend_ssh_access(
        backends: list[str], _settings
    ) -> dict[str, object]:
        removed.append(backends)
        return {"ssh_backends_removed": backends, "ssh_aliases_removed": bool(backends)}

    monkeypatch.setattr(
        ui_output, "remove_backend_ssh_access", fake_remove_backend_ssh_access
    )
    finalized: list[set[str]] = []

    async def fake_finalize(backends: set[str], _settings: Settings) -> list[str]:
        async with maker() as verify_session:
            stored = (
                await verify_session.execute(
                    select(Backend).where(Backend.name == "web")
                )
            ).scalar_one_or_none()
        assert stored is None
        finalized.append(backends)
        return []

    monkeypatch.setattr(
        ui_output, "remove_deleted_app_filesystem_artifacts", fake_finalize
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

        response = await ui_output.delete_backend_form(
            1,
            _PostedRequest(path="/ui/backends/1/delete"),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one_or_none()

    assert response.status_code == 303
    assert stored is None
    assert response.headers["location"].endswith("/?tab=outputs")
    assert (
        _cookie_values(response)["cnc_flash_success"] == "Output deleted. Host updated."
    )
    assert removed == [["web"]]
    assert finalized == [{"web"}]
    services = captured["services"]
    assert services is not None
    assert services.cleanup_deleted_app_filesystem is False


async def test_delete_backend_form_returns_json_for_modal_delete(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings, services=None):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=56
        )

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)

    async def fake_remove_backend_ssh_access(_backends, _settings):
        return {"ssh_backends_removed": [], "ssh_aliases_removed": False}

    monkeypatch.setattr(
        ui_output, "remove_backend_ssh_access", fake_remove_backend_ssh_access
    )

    async def fail_finalize(*_args, **_kwargs):
        raise AssertionError("filesystem finalizer must wait for successful commit")

    monkeypatch.setattr(
        ui_output, "remove_deleted_app_filesystem_artifacts", fail_finalize
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

        response = await ui_output.delete_backend_form(
            backend.id,
            _PostedRequest(
                path=f"/ui/backends/{backend.id}/delete",
                headers=[
                    (b"accept", b"application/json"),
                    (b"x-requested-with", b"fetch"),
                ],
            ),
            settings=settings,
            csrf_token="token",
            session=session,
        )

    payload = json.loads(response.body)
    async with maker() as session:
        operation = (
            await session.execute(
                select(Operation).where(Operation.kind == "delete_backend")
            )
        ).scalar_one()
    assert response.status_code == 202
    assert payload == {
        "operation_id": operation.id,
        "operation_status": "queued",
        "message": "Delete started.",
    }
    assert operation.backend_id == 1
    assert json.loads(operation.details_json)["redirect_url"].endswith(
        "/?tab=outputs&defer_status=1"
    )


async def test_delete_backend_form_rolls_back_backend_after_partial_apply_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings, services=None):
        return ApplyResponse(
            status="error",
            message="apply partially failed",
            details={
                "phase": "tailscale_paths",
                "error": "serve config update exploded",
                "failure_mode": "partial",
                "manual_review_required": True,
                "error_code": "CNC-02099",
                "error_name": "APPLY_FAILED",
                "error_inst": "DELETE01",
            },
            run_id=56,
        )

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    removed: list[list[str]] = []

    async def fake_remove_backend_ssh_access(
        backends: list[str], _settings
    ) -> dict[str, object]:
        removed.append(backends)
        return {"ssh_backends_removed": backends, "ssh_aliases_removed": bool(backends)}

    monkeypatch.setattr(
        ui_output, "remove_backend_ssh_access", fake_remove_backend_ssh_access
    )

    async def fail_finalize(*_args, **_kwargs):
        raise AssertionError("filesystem finalizer must wait for successful commit")

    monkeypatch.setattr(
        ui_output, "remove_deleted_app_filesystem_artifacts", fail_finalize
    )

    async with maker() as session:
        backend = Backend(
            name="smokeapp",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id

        response = await ui_output.delete_backend_form(
            backend_id,
            _PostedRequest(path=f"/ui/backends/{backend_id}/delete"),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one_or_none()

    assert response.status_code == 303
    assert stored is not None
    assert stored.name == "smokeapp"
    assert response.headers["location"].endswith("/?tab=outputs")
    assert _cookie_values(response)["cnc_flash_error"] == (
        "Save failed (Error CNC-02099-DELETE01). "
        "Some host changes may already be active. Reason: tailscale paths failed: "
        "serve config update exploded"
    )
    assert removed == []


async def test_delete_backend_form_rolls_back_backend_after_clean_apply_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings, services=None):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={
                "phase": "nginx_validate",
                "error": "bad config",
                "failure_mode": "clean",
                "manual_review_required": False,
                "error_code": "CNC-02001",
                "error_name": "APPLY_NGINX_CONFIG_INVALID",
                "error_inst": "DELETE02",
            },
            run_id=57,
        )

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)
    removed: list[list[str]] = []

    async def fake_remove_backend_ssh_access(
        backends: list[str], _settings
    ) -> dict[str, object]:
        removed.append(backends)
        return {"ssh_backends_removed": backends, "ssh_aliases_removed": bool(backends)}

    monkeypatch.setattr(
        ui_output, "remove_backend_ssh_access", fake_remove_backend_ssh_access
    )

    async with maker() as session:
        backend = Backend(
            name="smokeapp",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id

        response = await ui_output.delete_backend_form(
            backend_id,
            _PostedRequest(path=f"/ui/backends/{backend_id}/delete"),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one_or_none()

    assert response.status_code == 303
    assert stored is not None
    assert stored.name == "smokeapp"
    assert response.headers["location"].endswith("/?tab=outputs")
    assert _cookie_values(response)["cnc_flash_error"] == (
        "Save failed (Error CNC-02001-DELETE02). Existing config is still active. Reason: nginx validate failed: bad config"
    )
    assert removed == []


async def test_delete_backend_form_ignores_backend_ssh_cleanup_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_output, "enforce_csrf", lambda *_args, **_kwargs: None)

    async def fake_run_apply(_session, _settings, services=None):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=58
        )

    monkeypatch.setattr(ui_output, "run_apply", fake_run_apply)

    async def fail_remove_backend_ssh_access(
        _backends: list[str], _settings
    ) -> dict[str, object]:
        raise RuntimeError("ssh cleanup lock exploded")

    monkeypatch.setattr(
        ui_output, "remove_backend_ssh_access", fail_remove_backend_ssh_access
    )

    async with maker() as session:
        backend = Backend(
            name="smokeapp",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        response = await ui_output.delete_backend_form(
            backend.id,
            _PostedRequest(path=f"/ui/backends/{backend.id}/delete"),
            settings=settings,
            csrf_token="token",
            session=session,
        )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one_or_none()

    assert response.status_code == 303
    assert stored is None
    assert (
        _cookie_values(response)["cnc_flash_success"] == "Output deleted. Host updated."
    )


async def test_async_delete_reports_partial_when_post_commit_finalizer_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
    )
    captured: dict[str, object] = {}

    async def fake_run_apply(_session, _settings, services=None):
        captured["services"] = services
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=59
        )

    async def fail_finalize(*_args, **_kwargs):
        raise RuntimeError("filesystem busy")

    async def fake_remove_backend_ssh_access(*_args, **_kwargs):
        return {"ssh_backends_removed": ["web"]}

    monkeypatch.setattr(output_lifecycle_operations, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        output_lifecycle_operations,
        "remove_deleted_app_filesystem_artifacts",
        fail_finalize,
    )
    monkeypatch.setattr(
        output_lifecycle_operations,
        "remove_backend_ssh_access",
        fake_remove_backend_ssh_access,
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
        await session.flush()
        operation = Operation(
            kind="delete_backend",
            status="queued",
            phase="queued",
            actor="ui",
            backend_id=backend.id,
            details_json="{}",
        )
        session.add(operation)
        await session.commit()
        backend_id = backend.id
        operation_id = operation.id

    await output_lifecycle_operations._run_delete_operation(
        settings, operation_id, backend_id
    )

    async with maker() as session:
        operation = await session.get(Operation, operation_id)
        stored = await session.get(Backend, backend_id)

    assert stored is None
    assert operation is not None
    assert operation.status == "partial"
    details = json.loads(operation.details_json)
    assert details["filesystem_cleanup"] == {
        "status": "failed",
        "error": "filesystem busy",
    }
    assert "retained files are recoverable" in details["flash_error"]
    assert captured["services"].cleanup_deleted_app_filesystem is False
