from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

from app.config import Settings
from app.models.entities import Backend, Input
from app.services import backend_alerts
from app.services import host_state


def _backend(name: str = "web") -> Backend:
    backend = Backend(
        name=name, kind="app", enabled=True, internal_port=8337, port=12001
    )
    backend.inputs = [
        Input(kind="domain", hostname=f"{name}.example.com", enabled=True)
    ]
    return backend


@pytest.mark.asyncio
async def test_backend_alerts_defers_state_when_guest_observation_is_busy(
    monkeypatch, tmp_path, caplog
) -> None:
    caplog.set_level("INFO", logger=backend_alerts.logger_name)
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
    )
    backend = _backend()
    backend.id = 7
    deferred = {
        "ok": False,
        "diagnosis": "backend_observation_deferred",
        "backend_exec_status": "busy",
        "observation_deferred": True,
        "observation_issues": ["podman_exec_busy"],
        "issues": [],
        "cgroup": {
            "memory": {
                "current": 260,
                "max": 261,
                "events": {"oom_kill": 2},
                "pressure": {"some": {"avg10": 99.0}},
            },
            "pids": {"current": 120, "max": 200, "events": {"max": 4}},
        },
    }
    diagnostics = [
        deferred,
        deferred,
        {
            "ok": True,
            "diagnosis": "healthy",
            "backend_exec_status": "available",
            "issues": [],
        },
    ]

    async def fake_load_backends():
        return [backend]

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: diagnostics.pop(0),
    )

    first = await backend_alerts.run_backend_alerts_check(settings)
    second = await backend_alerts.run_backend_alerts_check(settings)
    resumed = await backend_alerts.run_backend_alerts_check(settings)

    for payload in (first, second):
        assert payload["healthy_count"] == 0
        assert payload["unhealthy_count"] == 0
        assert payload["unknown_count"] == 1
        assert payload["notifications"] == []
        assert payload["backends"][0]["status"] == "unknown"
        assert payload["backends"][0]["notification_reason"] == "observation_deferred"
    assert resumed["healthy_count"] == 1
    transitions = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        in {
            "backend.alerts.backend.observation.deferred",
            "backend.alerts.backend.observation.resumed",
        }
    ]
    assert [record.event_name for record in transitions] == [
        "backend.alerts.backend.observation.deferred",
        "backend.alerts.backend.observation.resumed",
    ]
    assert transitions[0].context["backend_id"] == 7
    assert transitions[0].context["cgroup"]["memory_events"] == {"oom_kill": 2}


@pytest.mark.asyncio
async def test_run_backend_alerts_check_sends_down_then_recovered(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
        backend_alert_repeat_sec=3600,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    backend = _backend()
    diagnostics = [
        {
            "ok": False,
            "issues": ["private_ip_missing", "loopback_unreachable"],
            "bootstrap": {"status": "failed"},
            "private_ip": "",
            "loopback_port": 12001,
        },
        {
            "ok": True,
            "issues": [],
            "bootstrap": {"status": "succeeded"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        },
    ]
    sent_titles: list[str] = []

    async def fake_load_backends():
        return [backend]

    async def fake_notify(
        _settings,
        *,
        title: str,
        message: str,
        priority: int = 0,
        url=None,
        url_title=None,
        **_kwargs,
    ) -> bool:
        assert message
        sent_titles.append(title)
        return True

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: diagnostics.pop(0),
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)

    first = await backend_alerts.run_backend_alerts_check(settings)
    second = await backend_alerts.run_backend_alerts_check(settings)

    assert first["unhealthy_count"] == 1
    assert first["notifications"][0]["kind"] == "down"
    assert first["backends"][0]["notification_reason"] == "first_down_alert_due"
    assert second["healthy_count"] == 1
    assert second["notifications"][0]["kind"] == "recovered"
    assert second["backends"][0]["notification_reason"] == "recovered_after_alert"
    assert sent_titles == [
        "CNC backend down: web",
        "CNC backend recovered: web",
    ]


@pytest.mark.asyncio
async def test_all_backends_down_sends_one_aggregate_emergency(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        host_state_path=tmp_path / "host-state.json",
        backend_alert_grace_period_sec=0,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    first = _backend("web")
    first.id = 1
    second = _backend("docs")
    second.id = 2
    sent: list[dict[str, object]] = []

    async def fake_load_backends():
        return [first, second]

    async def fake_notify(_settings, **kwargs) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: {
            "ok": False,
            "diagnosis": "runtime_down",
            "issues": ["loopback_unreachable"],
        },
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["unhealthy_count"] == 2
    assert [item["priority"] for item in sent] == [1, 1, 2]
    assert sent[-1]["event"] == "all_backends_down"
    assert sent[-1]["emergency_key"] == "all_backends_down"
    assert settings.backend_alerts_state_path.exists()


@pytest.mark.asyncio
async def test_run_backend_alerts_check_respects_grace_and_sends_reminder(
    monkeypatch, tmp_path
) -> None:
    base_time = datetime(2026, 4, 18, 20, 30, tzinfo=UTC)
    timestamps = [
        base_time,
        base_time + timedelta(seconds=301),
        base_time + timedelta(seconds=1_002),
    ]
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=300,
        backend_alert_repeat_sec=600,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    backend = _backend()
    sent_titles: list[str] = []

    async def fake_load_backends():
        return [backend]

    async def fake_notify(
        _settings,
        *,
        title: str,
        message: str,
        priority: int = 0,
        url=None,
        url_title=None,
        **_kwargs,
    ) -> bool:
        assert message
        sent_titles.append(title)
        return True

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: {
            "ok": False,
            "issues": ["loopback_unreachable"],
            "bootstrap": {"status": "failed"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        },
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)
    monkeypatch.setattr(backend_alerts, "_utc_now", lambda: timestamps.pop(0))

    first = await backend_alerts.run_backend_alerts_check(settings)
    second = await backend_alerts.run_backend_alerts_check(settings)
    third = await backend_alerts.run_backend_alerts_check(settings)

    assert first["notifications"] == []
    assert first["backends"][0]["notification_reason"] == "within_grace_period"
    assert second["notifications"][0]["kind"] == "down"
    assert second["backends"][0]["notification_reason"] == "first_down_alert_due"
    assert third["notifications"][0]["kind"] == "reminder"
    assert third["backends"][0]["notification_reason"] == "reminder_due"
    assert sent_titles == [
        "CNC backend down: web",
        "CNC backend still down: web",
    ]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_retries_failed_first_down_alert_before_reminder_window(
    monkeypatch, tmp_path
) -> None:
    base_time = datetime(2026, 4, 18, 20, 30, tzinfo=UTC)
    timestamps = [
        base_time,
        base_time + timedelta(seconds=30),
        base_time + timedelta(seconds=61),
    ]
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
        backend_alert_down_retry_sec=60,
        backend_alert_repeat_sec=3600,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    backend = _backend()
    attempts = 0

    async def fake_load_backends():
        return [backend]

    async def fake_notify(*_args, **_kwargs) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: {
            "ok": False,
            "issues": ["loopback_unreachable"],
            "bootstrap": {"status": "failed"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        },
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)
    monkeypatch.setattr(backend_alerts, "_utc_now", lambda: timestamps.pop(0))

    first = await backend_alerts.run_backend_alerts_check(settings)
    second = await backend_alerts.run_backend_alerts_check(settings)
    third = await backend_alerts.run_backend_alerts_check(settings)

    assert first["notifications"][0]["sent"] is False
    assert first["backends"][0]["notification_reason"] == "first_down_alert_due"
    assert second["notifications"] == []
    assert second["backends"][0]["notification_reason"] == "down_alert_retry_not_due"
    assert third["notifications"][0]["sent"] is True
    assert third["backends"][0]["notification_reason"] == "first_down_alert_due"
    assert attempts == 2


@pytest.mark.asyncio
async def test_run_backend_alerts_check_auto_fixes_safe_backend_drift(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
        backend_alert_repeat_sec=3600,
    )
    backend = _backend()
    fix_calls: list[str] = []
    events: list[dict[str, object]] = []

    async def fake_load_backends():
        return [backend]

    async def fake_fix_app_backend(
        backend_obj,
        _settings,
        *,
        enabled_app_backends,
        services=None,
        record_event=True,
    ):
        assert enabled_app_backends == [backend]
        assert services is None
        assert record_event is False
        fix_calls.append(backend_obj.name)
        return {
            "ok": True,
            "changed": True,
            "failure": None,
            "operation_id": 1996,
            "repair_plan": [{"action": "restart_quadlet"}],
            "repair_actions": ["systemctl restart cnc-app-web.service"],
            "pre_recovery_evidence": {"diagnosis": "backend_exec_unavailable"},
            "after": {
                "ok": True,
                "issues": [],
                "diagnosis": "healthy",
                "bootstrap": {"status": "succeeded"},
                "private_ip": "10.89.1.19",
                "loopback_port": 12001,
            },
        }

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: {
            "ok": False,
            "issues": ["container_missing"],
            "diagnosis": "sandbox_missing",
            "bootstrap": {"status": "missing"},
            "private_ip": "",
            "loopback_port": 12001,
        },
    )
    monkeypatch.setattr(backend_alerts, "fix_app_backend", fake_fix_app_backend)

    async def fake_emit_control_event(_settings, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(backend_alerts, "emit_control_event", fake_emit_control_event)

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["healthy_count"] == 1
    assert payload["unhealthy_count"] == 0
    assert payload["auto_fix_attempt_count"] == 1
    assert payload["auto_fix_success_count"] == 1
    assert payload["auto_fix_failure_count"] == 0
    assert payload["notifications"] == []
    assert payload["backends"][0]["auto_fix"]["attempted"] is True
    assert payload["backends"][0]["auto_fix"]["outcome"] == "recovered"
    assert payload["backends"][0]["auto_fix"]["reason"] == "auto_fix_recovered"
    assert payload["backends"][0]["auto_fix"]["operation_id"] == 1996
    assert payload["backends"][0]["auto_fix"]["repair_plan"] == [
        {"action": "restart_quadlet"}
    ]
    auto_fix_event = next(
        event for event in events if event["kind"] == "backend_auto_fix_recovered"
    )
    assert auto_fix_event["details"]["operation_id"] == 1996
    assert auto_fix_event["details"]["repair_actions"] == [
        "systemctl restart cnc-app-web.service"
    ]
    assert auto_fix_event["details"]["pre_recovery_evidence"] == {
        "diagnosis": "backend_exec_unavailable"
    }
    assert fix_calls == ["web"]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_auto_migrates_legacy_runtime_drift(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
        backend_alert_repeat_sec=3600,
    )
    backend = _backend()
    fix_calls: list[str] = []

    async def fake_load_backends():
        return [backend]

    async def fake_fix_app_backend(
        backend_obj,
        _settings,
        *,
        enabled_app_backends,
        services=None,
        record_event=True,
    ):
        assert enabled_app_backends == [backend]
        assert services is None
        assert record_event is False
        fix_calls.append(backend_obj.name)
        return {
            "ok": True,
            "changed": True,
            "failure": None,
            "after": {
                "ok": True,
                "issues": [],
                "maintenance_issues": [],
                "diagnosis": "healthy",
                "bootstrap": {"status": "succeeded"},
                "private_ip": "10.89.1.19",
                "loopback_port": 12001,
            },
        }

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: {
            "ok": True,
            "issues": [],
            "maintenance_issues": ["legacy_direct_podman_runtime_owner"],
            "diagnosis": "healthy",
            "bootstrap": {"status": "succeeded"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        },
    )
    monkeypatch.setattr(backend_alerts, "fix_app_backend", fake_fix_app_backend)

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["healthy_count"] == 1
    assert payload["unhealthy_count"] == 0
    assert payload["auto_fix_attempt_count"] == 1
    assert payload["auto_fix_success_count"] == 1
    assert payload["notifications"] == []
    assert payload["backends"][0]["ok"] is True
    assert payload["backends"][0]["maintenance_issues"] == []
    assert payload["backends"][0]["auto_fix"]["attempted"] is True
    assert payload["backends"][0]["auto_fix"]["considered_issues"] == [
        "legacy_direct_podman_runtime_owner"
    ]
    assert fix_calls == ["web"]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_auto_fix_respects_cooldown(
    monkeypatch, tmp_path
) -> None:
    base_time = datetime(2026, 4, 18, 20, 30, tzinfo=UTC)
    timestamps = [
        base_time,
        base_time + timedelta(seconds=60),
    ]
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=300,
        backend_auto_fix_cooldown_sec=900,
    )
    backend = _backend()
    fix_calls: list[str] = []

    async def fake_load_backends():
        return [backend]

    async def fake_fix_app_backend(
        backend_obj,
        _settings,
        *,
        enabled_app_backends,
        services=None,
        record_event=True,
    ):
        assert enabled_app_backends == [backend]
        assert services is None
        assert record_event is False
        fix_calls.append(backend_obj.name)
        return {
            "ok": False,
            "changed": False,
            "failure": {"phase": "app_bootstrap", "error": "still broken"},
            "after": {
                "ok": False,
                "issues": ["container_missing"],
                "diagnosis": "sandbox_missing",
                "bootstrap": {"status": "failed"},
                "private_ip": "",
                "loopback_port": 12001,
            },
        }

    diagnostics = {
        "ok": False,
        "issues": ["container_missing"],
        "diagnosis": "sandbox_missing",
        "bootstrap": {"status": "failed"},
        "private_ip": "",
        "loopback_port": 12001,
    }

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts, "collect_app_backend_diagnostics", lambda *_args: diagnostics
    )
    monkeypatch.setattr(backend_alerts, "fix_app_backend", fake_fix_app_backend)
    monkeypatch.setattr(backend_alerts, "_utc_now", lambda: timestamps.pop(0))

    first = await backend_alerts.run_backend_alerts_check(settings)
    second = await backend_alerts.run_backend_alerts_check(settings)

    assert first["auto_fix_attempt_count"] == 1
    assert first["auto_fix_failure_count"] == 1
    assert first["backends"][0]["auto_fix"]["attempted"] is True
    assert second["auto_fix_attempt_count"] == 0
    assert second["backends"][0]["auto_fix"]["attempted"] is False
    assert second["backends"][0]["auto_fix"]["reason"] == "cooldown_active"
    assert second["backends"][0]["auto_fix"]["cooldown_remaining_sec"] > 0
    assert fix_calls == ["web"]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_does_not_auto_fix_app_service_down(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=300,
    )
    backend = _backend()
    fix_calls: list[str] = []

    async def fake_load_backends():
        return [backend]

    async def fake_fix_app_backend(*_args, **_kwargs):
        fix_calls.append("called")
        return {}

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args: {
            "ok": False,
            "issues": ["loopback_unreachable"],
            "diagnosis": "app_service_down",
            "bootstrap": {"status": "succeeded"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        },
    )
    monkeypatch.setattr(backend_alerts, "fix_app_backend", fake_fix_app_backend)

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["auto_fix_attempt_count"] == 0
    assert payload["backends"][0]["auto_fix"]["attempted"] is False
    assert payload["backends"][0]["auto_fix"]["eligible"] is False
    assert (
        payload["backends"][0]["auto_fix"]["reason"]
        == "diagnosis_app_service_down_not_auto_fix_safe"
    )
    assert fix_calls == []


def test_guest_init_failure_is_not_auto_fix_safe() -> None:
    payload = backend_alerts._auto_fix_eligibility(
        {
            "ok": False,
            "diagnosis": "guest_init_broken",
            "issues": ["guest_system_unready"],
            "maintenance_issues": [],
        }
    )

    assert payload["eligible"] is False
    assert payload["reason"] == "diagnosis_guest_init_broken_not_auto_fix_safe"


def test_exec_circuit_recovery_allows_health_failures_but_rejects_runtime_drift() -> (
    None
):
    safe = backend_alerts._auto_fix_eligibility(
        {
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "issues": [
                "podman_exec_unavailable",
                "private_unreachable",
                "loopback_unreachable",
            ],
            "container_exists": True,
            "backend_exec_status": "circuit_open",
        }
    )
    unsafe = backend_alerts._auto_fix_eligibility(
        {
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "issues": ["podman_exec_unavailable", "missing_saved_spec"],
            "container_exists": True,
            "backend_exec_status": "circuit_open",
        }
    )

    assert safe["eligible"] is True
    assert unsafe["eligible"] is False
    assert unsafe["reason"] == "exec_recovery_issues_not_safe"


@pytest.mark.asyncio
async def test_exec_circuit_recovery_waits_for_grace_then_restarts_once(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=300,
    )
    backend = _backend()
    base_time = datetime(2026, 4, 18, 20, 30, tzinfo=UTC)
    diagnostics = {
        "ok": False,
        "diagnosis": "backend_exec_unavailable",
        "issues": ["podman_exec_unavailable"],
        "container_exists": True,
        "backend_exec_status": "circuit_open",
    }
    previous = {
        "last_issue_signature": "podman_exec_unavailable",
        "first_unhealthy_at": base_time.isoformat(),
    }
    fix_calls: list[str] = []

    async def fake_fix_app_backend(*args, **kwargs):
        fix_calls.append(args[0].name)
        return {
            "ok": True,
            "changed": True,
            "repair_plan": [{"action": "restart_quadlet"}],
            "after": {"ok": True, "issues": [], "diagnosis": "healthy"},
        }

    async def fake_emit_control_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(backend_alerts, "fix_app_backend", fake_fix_app_backend)
    monkeypatch.setattr(backend_alerts, "emit_control_event", fake_emit_control_event)

    deferred, unchanged = await backend_alerts._maybe_auto_fix_backend(
        backend=backend,
        diagnostics=diagnostics,
        previous=previous,
        enabled_app_backends=[backend],
        settings=settings,
        now=base_time + timedelta(seconds=299),
    )
    recovered, after = await backend_alerts._maybe_auto_fix_backend(
        backend=backend,
        diagnostics=diagnostics,
        previous=previous,
        enabled_app_backends=[backend],
        settings=settings,
        now=base_time + timedelta(seconds=300),
    )

    assert deferred["attempted"] is False
    assert deferred["reason"] == "exec_recovery_grace_active"
    assert deferred["recovery_grace_remaining_sec"] == 1
    assert unchanged is diagnostics
    assert recovered["attempted"] is True
    assert recovered["outcome"] == "recovered"
    assert recovered["repair_plan"] == [{"action": "restart_quadlet"}]
    assert after == {"ok": True, "issues": [], "diagnosis": "healthy"}
    assert fix_calls == ["web"]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_releases_state_lock_during_diagnostics(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(backend_alerts_state_path=tmp_path / "backend-alerts.json")
    backend = _backend()
    lock_active = False
    diagnostics_lock_states: list[bool] = []
    stored_state: dict[str, object] = {"backends": {}}

    class _TrackedTransaction:
        def __init__(self, _settings, *, blocking: bool = True) -> None:
            self.dirty = False

        def __enter__(self):
            nonlocal lock_active
            assert lock_active is False
            lock_active = True
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            nonlocal lock_active
            lock_active = False
            return False

        @property
        def value(self):
            return stored_state

        def replace(self, value):
            stored_state.clear()
            stored_state.update(value)
            self.dirty = True

    async def fake_load_backends():
        return [backend]

    def fake_collect_diagnostics(_backend, _settings, **_kwargs):
        diagnostics_lock_states.append(lock_active)
        return {
            "ok": True,
            "issues": [],
            "diagnosis": "healthy",
            "bootstrap": {"status": "succeeded"},
            "private_ip": "10.89.1.19",
            "loopback_port": 12001,
        }

    monkeypatch.setattr(
        backend_alerts, "_backend_alerts_state_transaction", _TrackedTransaction
    )
    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts, "collect_app_diagnostics_snapshot", lambda _settings: object()
    )
    monkeypatch.setattr(
        backend_alerts, "collect_app_backend_diagnostics", fake_collect_diagnostics
    )

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["healthy_count"] == 1
    assert diagnostics_lock_states == [False]
    assert "running" not in stored_state


@pytest.mark.asyncio
async def test_send_backend_alert_test_notifications_emits_all_shapes(
    monkeypatch,
) -> None:
    settings = Settings(
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    sent_titles: list[str] = []

    async def fake_notify(
        _settings,
        *,
        title: str,
        message: str,
        priority: int = 0,
        url=None,
        url_title=None,
        **_kwargs,
    ) -> bool:
        assert message
        sent_titles.append(title)
        return True

    async def fake_load_backend_by_name(_name: str):
        return _backend("web")

    monkeypatch.setattr(
        backend_alerts, "_load_backend_by_name", fake_load_backend_by_name
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)

    payload = await backend_alerts.send_backend_alert_test_notifications(
        settings, backend_name="web"
    )

    assert payload["ok"] is True
    assert [item["kind"] for item in payload["notifications"]] == [
        "down",
        "reminder",
        "recovered",
    ]
    assert payload["notifications"][0]["backend"] == "web"
    assert sent_titles == [
        "CNC backend down: web",
        "CNC backend still down: web",
        "CNC backend recovered: web",
    ]


@pytest.mark.asyncio
async def test_run_backend_alerts_check_skips_when_another_run_holds_lock(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )

    class _BusyTransaction:
        @property
        def value(self):
            return {}

        def __enter__(self):
            raise BlockingIOError

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(
        backend_alerts,
        "_backend_alerts_state_transaction",
        lambda _settings, *, blocking=True: _BusyTransaction(),
    )

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["ok"] is True
    assert payload["skipped"] is True
    assert payload["reason"] == "already_running"
    assert payload["notifications"] == []


@pytest.mark.asyncio
async def test_run_backend_alerts_check_does_not_notify_before_state_commit(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        backend_alerts_state_path=tmp_path / "backend-alerts.json",
        backend_alert_grace_period_sec=0,
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    backend = _backend()
    stored_state: dict[str, object] = {}
    sent_titles: list[str] = []

    class _FailFinalStateTransaction:
        def __init__(self, _settings, *, blocking: bool = True) -> None:
            pass

        @property
        def value(self):
            return stored_state

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def replace(self, value):
            if "checked_at" in value:
                raise RuntimeError("state commit failed")
            stored_state.clear()
            stored_state.update(value)

    async def fake_load_backends():
        return [backend]

    async def fake_notify(_settings, *, title: str, **_kwargs) -> bool:
        sent_titles.append(title)
        return True

    monkeypatch.setattr(
        backend_alerts,
        "_backend_alerts_state_transaction",
        _FailFinalStateTransaction,
    )
    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: {"ok": False, "issues": ["loopback_unreachable"]},
    )
    monkeypatch.setattr(backend_alerts, "send_pushover_notification_async", fake_notify)

    with pytest.raises(RuntimeError, match="state commit failed"):
        await backend_alerts.run_backend_alerts_check(settings)

    assert sent_titles == []


@pytest.mark.asyncio
async def test_run_backend_alerts_check_contains_backend_diagnostics_exception(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(backend_alerts_state_path=tmp_path / "backend-alerts.json")
    backend = _backend()

    async def fake_load_backends():
        return [backend]

    def fail_diagnostics(*_args, **_kwargs):
        raise RuntimeError("podman inspect exploded")

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts, "collect_app_backend_diagnostics", fail_diagnostics
    )

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["ok"] is True
    assert payload["unhealthy_count"] == 1
    assert payload["backends"][0]["issues"] == ["diagnostics_failed"]
    assert payload["backends"][0]["auto_fix"]["eligible"] is False


@pytest.mark.asyncio
async def test_run_backend_alerts_check_continues_when_snapshot_fails(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(backend_alerts_state_path=tmp_path / "backend-alerts.json")
    backend = _backend()

    async def fake_load_backends():
        return [backend]

    monkeypatch.setattr(
        backend_alerts, "_load_enabled_app_backends", fake_load_backends
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_diagnostics_snapshot",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("snapshot exploded")),
    )
    monkeypatch.setattr(
        backend_alerts,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: {"ok": True, "issues": []},
    )

    payload = await backend_alerts.run_backend_alerts_check(settings)

    assert payload["ok"] is True
    assert payload["healthy_count"] == 1
    assert payload["diagnostics_snapshot_error"] == "snapshot exploded"


def test_backend_alerts_state_transaction_serializes_concurrent_writers(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(host_state_path=tmp_path / "host-state.json")
    active_reads = 0
    max_active_reads = 0
    counter_lock = threading.Lock()
    original = host_state.read_host_state_unlocked

    def wrapped(path):
        nonlocal active_reads, max_active_reads
        with counter_lock:
            active_reads += 1
            max_active_reads = max(max_active_reads, active_reads)
        time.sleep(0.05)
        try:
            return original(path)
        finally:
            with counter_lock:
                active_reads -= 1

    monkeypatch.setattr(host_state, "read_host_state_unlocked", wrapped)
    barrier = threading.Barrier(2)

    def worker(backend_name: str, status: str) -> None:
        barrier.wait()
        with backend_alerts._backend_alerts_state_transaction(settings) as transaction:
            payload = dict(transaction.value)
            backends_payload = dict(payload.get("backends") or {})
            backends_payload[backend_name] = {"status": status}
            payload["backends"] = backends_payload
            transaction.replace(payload)

    first = threading.Thread(target=worker, args=("web", "down"))
    second = threading.Thread(target=worker, args=("smokeapp", "up"))
    first.start()
    second.start()
    first.join()
    second.join()

    assert max_active_reads == 1
    payload = backend_alerts.read_backend_alerts_state(settings)
    assert payload["backends"] == {
        "smokeapp": {"status": "up"},
        "web": {"status": "down"},
    }
