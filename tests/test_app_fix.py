from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, Operation
from app.services import app_fix, app_repair
from app.services.apply_core import AppRuntimeReconcileResult
from app.services.commands import CommandResult


@pytest.mark.asyncio
async def test_fix_app_backend_runs_cleanup_reconcile_and_verify(
    monkeypatch, tmp_path
) -> None:
    backend = Backend(
        name="web",
        kind="app",
        enabled=True,
        port=12000,
        handoff_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    diagnostics = [
        {
            "ok": False,
            "diagnosis": "sandbox_publish_broken",
            "issues": ["loopback_publish_missing"],
        },
        {"ok": True, "diagnosis": "healthy", "issues": []},
    ]
    cleanup_payload = {
        "backend": "web",
        "changed": True,
        "ok": True,
        "before": {"issues": ["loopback_publish_missing"]},
        "after": {"issues": []},
        "actions": ["podman rm -f cnc-app-web"],
        "errors": [],
    }

    monkeypatch.setattr(
        app_fix,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_fix, "repair_app_backend", lambda *_args, **_kwargs: cleanup_payload
    )
    monkeypatch.setattr(
        app_fix,
        "build_resource_profile",
        lambda _settings, backends: {"backend_count": len(backends)},
    )

    class FakeReconciler:
        def __init__(self, backend, settings, *, base_profile, services=None) -> None:
            assert backend.name == "web"
            assert base_profile == {"backend_count": 1}
            self.result = AppRuntimeReconcileResult(
                backend=backend.name,
                container="cnc-app-web",
                phases={},
            )

        async def reconcile_for_publish(self):
            self.result.created = True
            self.result.bootstrapped = True
            self.result.phases = {
                "create": {
                    "phase": "create",
                    "status": "succeeded",
                    "details": {
                        "container_started": True,
                        "guest_rootfs": {"seeded": False},
                    },
                }
            }
            return self.result

        async def verify_steady_state(self):
            self.result.target_ip = "10.88.0.8"
            self.result.phases["verify"] = {
                "phase": "verify",
                "status": "succeeded",
                "details": {},
            }
            return self.result

    monkeypatch.setattr(app_fix, "AppRuntimeReconciler", FakeReconciler)

    payload = await app_fix.fix_app_backend(
        backend,
        settings,
        enabled_app_backends=[backend],
    )

    assert payload["backend"] == "web"
    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["before"]["diagnosis"] == "sandbox_publish_broken"
    assert payload["cleanup"]["changed"] is True
    assert payload["reconcile"]["created"] is True
    assert payload["reconcile"]["bootstrapped"] is True
    assert payload["after"]["diagnosis"] == "healthy"
    assert payload["failure"] is None


@pytest.mark.asyncio
async def test_fix_app_backend_waits_for_recreated_backend_to_become_healthy(
    monkeypatch, tmp_path
) -> None:
    backend = Backend(
        name="web",
        kind="app",
        enabled=True,
        port=12000,
        handoff_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        command_timeout_status_sec=1,
    )
    diagnostics = [
        {
            "ok": True,
            "diagnosis": "healthy",
            "issues": [],
            "maintenance_issues": ["legacy_direct_podman_runtime_owner"],
        },
        {
            "ok": False,
            "diagnosis": "app_service_down",
            "issues": ["loopback_unreachable"],
        },
        {"ok": True, "diagnosis": "healthy", "issues": [], "maintenance_issues": []},
    ]
    sleep_calls: list[int] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        app_fix,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_fix,
        "repair_app_backend",
        lambda *_args, **_kwargs: {"changed": False, "actions": [], "errors": []},
    )
    monkeypatch.setattr(
        app_fix,
        "build_resource_profile",
        lambda _settings, backends: {"backend_count": len(backends)},
    )
    monkeypatch.setattr(app_fix.asyncio, "sleep", fake_sleep)

    class FakeReconciler:
        def __init__(self, backend, settings, *, base_profile, services=None) -> None:
            self.result = AppRuntimeReconcileResult(
                backend=backend.name,
                container="cnc-app-web",
                phases={},
                recreated=True,
            )

        async def reconcile_for_publish(self):
            return self.result

        async def verify_steady_state(self):
            return self.result

    monkeypatch.setattr(app_fix, "AppRuntimeReconciler", FakeReconciler)

    payload = await app_fix.fix_app_backend(
        backend,
        settings,
        enabled_app_backends=[backend],
    )

    assert payload["ok"] is True
    assert payload["after"]["diagnosis"] == "healthy"
    assert sleep_calls == [2]


@pytest.mark.asyncio
async def test_exec_unavailable_restart_waits_for_guest_to_become_healthy(
    monkeypatch, tmp_path
) -> None:
    backend = Backend(
        name="web",
        kind="app",
        enabled=True,
        port=12000,
        handoff_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        command_timeout_status_sec=1,
    )
    diagnostics = [
        {
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "backend_exec_status": "timeout",
            "issues": ["backend_exec_timeout"],
            "container_exists": True,
        },
        {
            "ok": False,
            "diagnosis": "guest_init_broken",
            "issues": ["guest_system_unready"],
        },
        {"ok": True, "diagnosis": "healthy", "issues": []},
    ]
    sleep_calls: list[int] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        app_fix,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_fix,
        "repair_app_backend",
        lambda *_args, **_kwargs: {
            "changed": True,
            "actions": ["systemctl restart cnc-app-web.service"],
            "errors": [],
        },
    )
    monkeypatch.setattr(app_fix.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        app_fix,
        "AppRuntimeReconciler",
        lambda *_args, **_kwargs: pytest.fail(
            "exec-unavailable repair must not reconcile"
        ),
    )

    payload = await app_fix.fix_app_backend(
        backend,
        settings,
        enabled_app_backends=[backend],
    )

    assert payload["ok"] is True
    assert payload["repair_plan"] == [{"action": "restart_quadlet"}]
    assert payload["after"]["diagnosis"] == "healthy"
    assert diagnostics == []
    assert sleep_calls == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnosis",
    [
        "backend_observation_deferred",
        "backend_observation_unavailable",
        "sandbox_observation_unavailable",
    ],
)
async def test_unavailable_observation_finishes_as_deferred_without_false_failure(
    monkeypatch, tmp_path, diagnosis
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    engine = create_async_engine(database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    backend = Backend(
        id=8,
        name="web",
        kind="app",
        enabled=True,
        port=12000,
        handoff_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    async with session_factory() as session:
        session.add(backend)
        await session.commit()
    settings = Settings(
        database_url=database_url,
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    deferred_diagnostics = {
        "ok": False,
        "diagnosis": diagnosis,
        "backend_exec_status": "busy",
        "issues": ["backend_exec_busy"],
        "container_exists": True,
    }
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        app_fix,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: dict(deferred_diagnostics),
    )
    monkeypatch.setattr(
        app_fix,
        "repair_app_backend",
        lambda *_args, **_kwargs: {"changed": False, "actions": [], "errors": []},
    )
    monkeypatch.setattr(
        app_fix,
        "emit_control_event",
        lambda _settings, **kwargs: _capture_event(events, kwargs),
    )

    payload = await app_fix.fix_app_backend(
        backend,
        settings,
        enabled_app_backends=[backend],
    )

    async with session_factory() as session:
        operation = (await session.execute(select(Operation))).scalar_one()
    await engine.dispose()

    assert payload["ok"] is False
    assert payload["deferred"] is True
    assert payload["changed"] is False
    assert payload["failure"] is None
    assert operation.status == "partial"
    assert operation.phase == "deferred"
    assert events[0]["kind"] == "backend_fix_deferred"
    assert events[0]["severity"] == "info"
    assert events[0]["details"]["deferred"] is True


@pytest.mark.asyncio
async def test_exec_unavailable_timeout_is_terminal_without_full_reconcile(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level("INFO", logger="app.repair")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    engine = create_async_engine(database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    backend = Backend(
        id=7,
        name="web",
        kind="app",
        enabled=True,
        port=12000,
        handoff_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    async with session_factory() as session:
        session.add(backend)
        await session.commit()
    settings = Settings(
        database_url=database_url,
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
    )
    diagnostics = [
        {
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "issues": ["backend_exec_timeout"],
            "container_exists": True,
            "backend_exec_available": False,
            "backend_exec_status": "timeout",
            "backend_exec_error": "guest exec timed out",
            "backend_exec_circuit": {
                "open": True,
                "opened_at": 10.0,
                "expires_at": 40.0,
                "timeout_sec": 5,
                "error": "not duplicated into durable evidence",
            },
            "cgroup": {
                "memory": {
                    "current": 272539648,
                    "max": 272629760,
                    "events": {"oom": 3, "oom_kill": 2},
                    "pressure": {"some": {"avg10": 99.0, "total": 12}},
                },
                "pids": {
                    "current": 147,
                    "max": 2308,
                    "events": {"max": 4},
                },
            },
        },
        {
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "issues": ["backend_exec_timeout"],
            "container_exists": True,
        },
    ]
    commands: list[list[str]] = []
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        app_fix,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def timeout(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 124, "", "command timed out")

    monkeypatch.setattr(app_repair, "run_command", timeout)
    monkeypatch.setattr(
        app_fix,
        "emit_control_event",
        lambda _settings, **kwargs: _capture_event(events, kwargs),
    )
    monkeypatch.setattr(
        app_fix,
        "AppRuntimeReconciler",
        lambda *_args, **_kwargs: pytest.fail(
            "exec-unavailable repair must not reconcile"
        ),
    )

    payload = await app_fix.fix_app_backend(
        backend,
        settings,
        enabled_app_backends=[backend],
    )

    async with session_factory() as session:
        operation = (await session.execute(select(Operation))).scalar_one()
    await engine.dispose()

    assert payload["ok"] is False
    assert payload["failure"] == {"error": "command timed out", "phase": "recover"}
    assert commands == [["systemctl", "restart", "cnc-app-web.service"]]
    assert diagnostics == []
    assert operation.status == "failed"
    assert operation.phase == "recover"
    assert operation.finished_at is not None
    details = json.loads(operation.details_json or "{}")
    assert details["diagnosis"] == "backend_exec_unavailable"
    assert details["repair_plan"] == [{"action": "restart_quadlet"}]
    expected_evidence = {
        "diagnosis": "backend_exec_unavailable",
        "exec": {
            "available": False,
            "status": "timeout",
            "error": "guest exec timed out",
            "circuit": {
                "open": True,
                "opened_at": 10.0,
                "expires_at": 40.0,
                "timeout_sec": 5,
            },
        },
        "memory": {
            "current": 272539648,
            "max": 272629760,
            "events": {"oom": 3, "oom_kill": 2},
            "pressure": {"some": {"avg10": 99.0, "total": 12}},
        },
        "pids": {"current": 147, "max": 2308, "events": {"max": 4}},
    }
    assert details["pre_recovery_evidence"] == expected_evidence
    assert events[0]["details"]["pre_recovery_evidence"] == expected_evidence
    assert events[0]["details"]["backend_id"] == 7
    assert events[0]["details"]["operation_id"] == operation.id
    assert events[0]["details"]["repair_plan"] == [{"action": "restart_quadlet"}]
    planned = next(
        record
        for record in caplog.records
        if record.getMessage() == "App repair planned"
    )
    assert planned.context["backend_id"] == 7
    assert planned.context["diagnosis"] == "backend_exec_unavailable"
    assert planned.context["repair_plan"] == [{"action": "restart_quadlet"}]


async def _capture_event(
    events: list[dict[str, object]], payload: dict[str, object]
) -> None:
    events.append(payload)
