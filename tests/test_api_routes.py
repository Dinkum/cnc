import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from starlette.requests import Request

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, Input, Operation
from app.routes import backends as backend_routes
from app.routes import inputs as input_routes
from app.routes import status as status_routes
from app.routes.apply_errors import apply_failure_detail
from app.services.app_containers import write_app_control_assets
from app.services.bootstrap_state import write_bootstrap_state
from app.services.sandbox_profiles import app_sandbox_dir
from app.schemas.backends import BackendIn, BackendUpdate
from app.schemas.inputs import InputIn, InputUpdate
from app.schemas.apply import ApplyResponse
from app.services.mutation_apply import MutationApplyResult
from app.services.operation_progress import write_operation_progress


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )


@pytest.mark.asyncio
async def test_ready_returns_ok_after_db_ping(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")

    async with maker():
        app = FastAPI()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/ready",
                "headers": [],
                "app": app,
            }
        )

        async def fake_verify_db_connection() -> None:
            return None

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                status_routes, "verify_db_connection", fake_verify_db_connection
            )
            monkeypatch.setattr(
                status_routes,
                "snapshot_startup_state",
                lambda _app: {
                    "config": {"status": "ok", "error": ""},
                    "db": {"status": "ok", "error": ""},
                    "runtime_assets": {"status": "ok", "error": ""},
                    "self_audit": {"status": "ok", "error": ""},
                },
            )
            response = await status_routes.ready(request=request)
        finally:
            monkeypatch.undo()

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == payload["config"] == payload["db"] == "ok"
    assert payload["startup"] == {
        "config": {"status": "ok", "error": ""},
        "db": {"status": "ok", "error": ""},
        "runtime_assets": {"status": "ok", "error": ""},
        "self_audit": {"status": "ok", "error": ""},
    }
    assert "queue_handler_attached" in payload["logging"]
    assert "listener_alive" in payload["logging"]


@pytest.mark.asyncio
async def test_operation_status_returns_operation_progress(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add(
            Operation(
                kind="restore_backend",
                status="running",
                phase="restore",
                actor="ui",
                backend_id=7,
                started_at=datetime(2026, 5, 1, 4, 25, 45),
                finished_at=datetime(2026, 5, 1, 4, 26, 3),
                details_json='{"progress": 52, "message": "Restoring"}',
            )
        )
        await session.commit()

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["id"] == 1
    assert payload["kind"] == "restore_backend"
    assert payload["status"] == "running"
    assert payload["details"] == {"progress": 52, "message": "Restoring"}
    assert payload["started_at"] == "2026-05-01T04:25:45Z"
    assert payload["finished_at"] == "2026-05-01T04:26:03Z"


@pytest.mark.asyncio
async def test_active_operations_filters_by_output_detail_surface(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add_all(
            [
                Operation(
                    kind="backup_backend",
                    status="running",
                    phase="backup",
                    actor="ui",
                    backend_id=7,
                    details_json='{"progress": 22, "message": "Backing up"}',
                ),
                Operation(
                    kind="restore_backend",
                    status="queued",
                    phase="restore",
                    actor="ui",
                    backend_id=8,
                    details_json="{}",
                ),
                Operation(
                    kind="backup_backend",
                    status="success",
                    phase="backup",
                    actor="ui",
                    backend_id=7,
                    details_json="{}",
                ),
                Operation(
                    kind="ui.input.update",
                    status="running",
                    phase="save",
                    actor="ui",
                    backend_id=None,
                    details_json="{}",
                ),
            ]
        )
        await session.commit()

        payload = await status_routes.active_operations(
            surface="output_detail",
            backend_id=7,
            settings=settings,
            session=session,
        )

    operations = payload["operations"]
    assert len(operations) == 1
    assert operations[0]["kind"] == "backup_backend"
    assert operations[0]["backend_id"] == 7
    assert operations[0]["status"] == "running"


@pytest.mark.asyncio
async def test_active_operations_requires_backend_for_output_detail(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        with pytest.raises(status_routes.HTTPException):
            await status_routes.active_operations(
                surface="output_detail",
                backend_id=None,
                settings=settings,
                session=session,
            )


@pytest.mark.asyncio
async def test_active_operations_includes_output_repair(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add(
            Operation(
                kind="repair_backend",
                status="running",
                phase="recover",
                actor="system",
                backend_id=7,
                details_json='{"diagnosis":"backend_exec_unavailable"}',
            )
        )
        await session.commit()

        payload = await status_routes.active_operations(
            surface="output_detail",
            backend_id=7,
            settings=settings,
            session=session,
        )

    assert len(payload["operations"]) == 1
    assert payload["operations"][0]["kind"] == "repair_backend"
    assert payload["operations"][0]["phase"] == "recover"


@pytest.mark.asyncio
async def test_operation_events_streams_shared_operation_payload(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add(
            Operation(
                kind="restore_backend",
                status="success",
                phase="restore",
                actor="ui",
                backend_id=7,
                started_at=datetime(2026, 5, 1, 4, 25, 45),
                finished_at=datetime(2026, 5, 1, 4, 26, 3),
                details_json='{"progress": 100, "message": "Restored"}',
            )
        )
        await session.commit()
        expected_payload = await status_routes.operation_status_payload(
            1, settings=settings, session=session
        )

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/operations/1/events",
                "headers": [],
            },
            receive,
        )
        response = await status_routes.operation_events(
            1, request=request, settings=settings, session=session
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        body = "".join(chunks)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert "event: operation\n" in body
    assert f"data: {json.dumps(expected_payload, separators=(',', ':'))}\n\n" in body


@pytest.mark.asyncio
async def test_operation_status_enriches_running_create_with_bootstrap_progress(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    write_bootstrap_state(
        settings,
        "sample-app-n",
        {
            "status": "running",
            "reconcile_phase": "create",
            "reconcile_phase_status": "running",
            "reconcile_details": {"substep": "provision_guest_tools"},
        },
    )

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="reconciling host",
                actor="ui",
                details_json='{"progress": 42, "message": "Creating runtime assets.", "backend_name": "sample-app-n"}',
            )
        )
        await session.commit()

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["phase"] == "Provision guest"
    assert payload["details"]["progress"] == 46
    assert payload["details"]["message"] == "Installing guest tools"
    assert payload["details"]["substate"] == "Installing guest tools"
    assert payload["details"]["runtime"]["substep"] == "provision_guest_tools"


@pytest.mark.asyncio
async def test_operation_status_does_not_overlay_runtime_before_runtime_phase(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    write_bootstrap_state(
        settings,
        "demo-app-p",
        {
            "status": "running",
            "reconcile_phase": "bootstrap",
            "reconcile_phase_status": "running",
            "reconcile_details": {"substep": "guest_readiness_wait"},
        },
    )

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="Save desired state",
                actor="ui",
                details_json='{"progress": 12, "message": "Saving route graph", "substate": "Saving route graph", "backend_name": "demo-app-p"}',
            )
        )
        await session.commit()

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["phase"] == "Save desired state"
    assert payload["details"]["progress"] == 12
    assert payload["details"]["message"] == "Saving route graph"
    assert "runtime" not in payload["details"]


@pytest.mark.asyncio
async def test_operation_status_reads_out_of_band_progress_snapshot(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="Save desired state",
                actor="ui",
                details_json='{"progress": 30, "message": "Waiting for host mutation lock", "backend_name": "demo-app-p"}',
            )
        )
        await session.commit()
        write_operation_progress(
            settings,
            1,
            status="running",
            phase="Apply host access",
            details={
                "progress": 84,
                "message": "Reconciling backend SSH",
                "substate": "Reconciling backend SSH",
                "backend_name": "demo-app-p",
            },
        )

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["status"] == "running"
    assert payload["phase"] == "Apply host access"
    assert payload["details"]["progress"] == 84
    assert payload["details"]["substate"] == "Reconciling backend SSH"


@pytest.mark.asyncio
async def test_operation_status_merges_snapshot_details_without_regressing_progress(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="Apply host access",
                actor="ui",
                details_json=json.dumps(
                    {
                        "progress": 88,
                        "message": "Reloading host proxy",
                        "backend_name": "demo-app-p",
                        "progress_plan": {"pipeline": "createOutput", "steps": []},
                    }
                ),
            )
        )
        await session.commit()
        write_operation_progress(
            settings,
            1,
            status="running",
            phase="Verify output",
            details={
                "progress": 80,
                "message": "Checking output readiness",
                "substate": "Checking output readiness",
            },
        )

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["phase"] == "Verify output"
    assert payload["details"]["progress"] == 88
    assert payload["details"]["message"] == "Checking output readiness"
    assert payload["details"]["substate"] == "Checking output readiness"
    assert payload["details"]["backend_name"] == "demo-app-p"
    assert payload["details"]["progress_plan"] == {
        "pipeline": "createOutput",
        "steps": [],
    }


@pytest.mark.asyncio
async def test_operation_status_does_not_regress_snapshot_with_runtime_progress(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    write_bootstrap_state(
        settings,
        "demo-app-p",
        {
            "status": "running",
            "reconcile_phase": "bootstrap",
            "reconcile_phase_status": "running",
            "reconcile_details": {"substep": "guest_readiness_wait"},
        },
    )

    async with maker() as session:
        session.add(
            Operation(
                kind="create_backend",
                status="running",
                phase="Save desired state",
                actor="ui",
                details_json='{"progress": 30, "message": "Waiting for host mutation lock", "backend_name": "demo-app-p"}',
            )
        )
        await session.commit()
        write_operation_progress(
            settings,
            1,
            status="running",
            phase="Apply host access",
            details={
                "progress": 84,
                "message": "Reconciling backend SSH",
                "substate": "Reconciling backend SSH",
                "backend_name": "demo-app-p",
            },
        )

        payload = await status_routes.operation_status(
            1, settings=settings, session=session
        )

    assert payload["phase"] == "Apply host access"
    assert payload["details"]["progress"] == 84
    assert payload["details"]["substate"] == "Reconciling backend SSH"


@pytest.mark.asyncio
async def test_clone_backend_api_uses_clone_defaults_when_payload_is_empty(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                resource_mode="manual",
                resource_size="large",
                volumes_json="[]",
                memory_high_override="512M",
                memory_max_override="768M",
                cpu_quota_override="100%",
                enabled=True,
            )
        )
        await session.commit()
        backend = (
            await session.execute(select(Backend).where(Backend.id == 1))
        ).scalar_one()
        write_app_control_assets(backend, _settings(tmp_path))
        app_sandbox_dir(_settings(tmp_path), "web").mkdir(parents=True, exist_ok=True)

        cloned = await backend_routes.clone_backend(
            1,
            payload=backend_routes.BackendCloneIn(),
            session=session,
            settings=_settings(tmp_path),
            _csrf=None,
        )
        cloned = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == cloned.id)
            )
        ).scalar_one()

    assert cloned.name == "clone-web"
    assert cloned.enabled is False
    assert cloned.port == 12001
    assert cloned.inputs == []
    assert cloned.sandbox_profile == "ubuntu-24.04-systemd"
    assert cloned.handoff_port == 8337
    assert cloned.resource_mode == "manual"
    assert cloned.resource_size == "large"
    assert cloned.memory_high_override == "512M"
    assert cloned.memory_max_override == "768M"
    assert cloned.cpu_quota_override == "100%"


@pytest.mark.asyncio
async def test_clone_backend_api_copies_mount_data_and_drops_inputs(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("hello", encoding="utf-8")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
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

        cloned = await backend_routes.clone_backend(
            backend.id,
            payload=backend_routes.BackendCloneIn(name="clone-web", port=12001),
            session=session,
            settings=settings,
            _csrf=None,
        )
        cloned = (
            await session.execute(
                select(Backend)
                .options(selectinload(Backend.inputs))
                .where(Backend.id == cloned.id)
            )
        ).scalar_one()

    cloned_source = Path(json.loads(cloned.volumes_json)[0].split(":", 1)[0])
    assert cloned.name == "clone-web"
    assert cloned.enabled is False
    assert cloned.port == 12001
    assert cloned.inputs == []
    assert app_sandbox_dir(settings, "clone-web").exists()
    assert cloned_source != data_dir
    assert cloned_source.exists()
    assert (cloned_source / "state.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_backend_write_routes_invalidate_status_cache(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    invalidations: list[dict[str, object]] = []
    settings = _settings(tmp_path)
    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    (data_dir / "state.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(
        backend_routes,
        "invalidate_status_cache",
        lambda *_args, **kwargs: invalidations.append(dict(kwargs)),
    )

    async def fake_commit_and_apply(_session, _settings, *, operation: str):
        await _session.flush()
        await _session.commit()
        return MutationApplyResult(
            operation=operation,
            state="applied",
            apply_response=ApplyResponse(
                status="success", message="apply completed", details={}, run_id=1
            ),
            state_history=("pending", "applying", "applied"),
        )

    monkeypatch.setattr(backend_routes, "commit_and_apply", fake_commit_and_apply)

    async with maker() as session:
        created = await backend_routes.create_backend(
            BackendIn(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                resource_mode="auto",
                resource_size="small",
                volumes_json=f'["{data_dir}:/srv/web"]',
                enabled=True,
                input_ids=[],
            ),
            session=session,
            settings=settings,
            _csrf=None,
        )
        await backend_routes.update_backend(
            created.id,
            BackendUpdate(notes="updated"),
            session=session,
            settings=settings,
            _csrf=None,
        )
        await backend_routes.toggle_backend(
            created.id,
            backend_routes.BackendStateIn(enabled=False),
            session=session,
            settings=settings,
            _csrf=None,
        )
        write_app_control_assets(created, settings)
        await backend_routes.clone_backend(
            created.id,
            payload=backend_routes.BackendCloneIn(name="clone-web", port=12001),
            session=session,
            settings=settings,
            _csrf=None,
        )

    assert invalidations == [
        {"prefill": True},
        {"prefill": True},
        {"prefill": True},
        {"prefill": True},
    ]


@pytest.mark.asyncio
async def test_backend_update_rejects_kind_change(monkeypatch, tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)

    async def fail_commit_and_apply(*_args, **_kwargs):
        raise AssertionError("kind-only changes should fail before apply")

    monkeypatch.setattr(backend_routes, "commit_and_apply", fail_commit_and_apply)

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
        backend_id = backend.id

        with pytest.raises(backend_routes.HTTPException) as exc_info:
            await backend_routes.update_backend(
                backend_id,
                BackendUpdate(kind="static"),
                session=session,
                settings=settings,
                _csrf=None,
            )

        stored = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["message"] == "output type is locked after creation"
    assert exc_info.value.detail["error_code"] == "CNC-01001"
    assert exc_info.value.detail["error_name"] == "VALIDATION_FAILED"
    assert exc_info.value.detail["error_inst"]
    assert stored.kind == "app"


def test_backend_apply_failure_detail_surfaces_self_audit_finding() -> None:
    detail = apply_failure_detail(
        {
            "phase": "control_plane_self_audit",
            "error": "control-plane self-audit failed",
            "findings": [
                {
                    "check": "admin_loopback_bind",
                    "severity": "blocking",
                    "message": "admin listener is not loopback-only",
                }
            ],
        }
    )

    assert (
        detail["message"]
        == "save failed; existing config is still active: admin listener is not loopback-only"
    )


def test_input_apply_failure_detail_surfaces_self_audit_finding() -> None:
    detail = apply_failure_detail(
        {
            "phase": "control_plane_self_audit",
            "error": "control-plane self-audit failed",
            "findings": [
                {
                    "check": "tailscale_admin_exposure",
                    "severity": "blocking",
                    "message": "tailscale serve exposes an unapproved admin mapping",
                }
            ],
        }
    )

    assert detail["message"] == (
        "save failed; existing config is still active: tailscale serve exposes an unapproved admin mapping"
    )


@pytest.mark.asyncio
async def test_input_write_routes_invalidate_status_cache(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    invalidations: list[dict[str, object]] = []
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        input_routes,
        "invalidate_status_cache",
        lambda *_args, **kwargs: invalidations.append(dict(kwargs)),
    )

    async def fake_commit_and_apply(_session, _settings, *, operation: str):
        await _session.flush()
        await _session.commit()
        return MutationApplyResult(
            operation=operation,
            state="applied",
            apply_response=ApplyResponse(
                status="success", message="apply completed", details={}, run_id=1
            ),
            state_history=("pending", "applying", "applied"),
        )

    monkeypatch.setattr(input_routes, "commit_and_apply", fake_commit_and_apply)

    async with maker() as session:
        backend = Backend(
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
        session.add(backend)
        await session.commit()

        created = await input_routes.create_input(
            InputIn(
                kind="domain",
                value="example.com",
                backend_ids=[backend.id],
                enabled=True,
            ),
            session=session,
            settings=settings,
            _csrf=None,
        )
        await input_routes.update_input(
            created.id,
            InputUpdate(value="www.example.com"),
            session=session,
            settings=settings,
            _csrf=None,
        )
        await input_routes.toggle_input(
            created.id,
            input_routes.InputStateIn(enabled=False),
            session=session,
            settings=settings,
            _csrf=None,
        )

        stored = (
            await session.execute(select(Input).where(Input.id == created.id))
        ).scalar_one()

    assert stored.hostname == "www.example.com"
    assert invalidations == [
        {"prefill": True},
        {"prefill": True},
        {"prefill": True},
    ]


@pytest.fixture(autouse=True)
def _unmounted_guest_archive_view(monkeypatch):
    # Route fixtures use local filesystem trees with no running Podman guest.
    from app.services.guest_metadata import GuestArchiveView

    monkeypatch.setattr(
        GuestArchiveView, "capture",
        classmethod(lambda cls, container, runner=None: cls(container, "fixture")),
    )
