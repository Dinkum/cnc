import asyncio
from contextlib import suppress
import json
import time

import pytest
from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import main
from app.main import (
    app,
    http_exception_handler,
    request_logging_middleware,
    unhandled_exception_handler,
)
from app.routes import status as status_routes
from app.services import self_audit as self_audit_service
from app.services.self_audit import ControlPlaneSelfAuditError


def _request(
    path: str,
    *,
    accept: str = "text/html",
    asgi_app=None,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    request_headers = [(b"accept", accept.encode())]
    if headers:
        request_headers.extend(headers)
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": request_headers,
            "client": ("100.64.0.12", 12345),
            "server": ("127.0.0.1", 9090),
            "app": asgi_app or app,
        }
    )


class _FakeNotificationStateTransaction:
    def __init__(self, state: dict[str, object]) -> None:
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def get_event(self, _event_key: str) -> dict[str, object]:
        return dict(self._state)

    def set_event(self, _event_key: str, entry: dict[str, object]) -> None:
        self._state.clear()
        self._state.update(entry)


class _FakeLock:
    def __init__(self, releases: list[str]) -> None:
        self._releases = releases

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._releases.append("released")


@pytest.fixture(autouse=True)
def _stub_startup_hardening_cleanup(monkeypatch) -> None:
    async def fake_fail_interrupted_hardening_runs(_settings) -> int:
        return 0

    async def fake_fail_interrupted_backend_backups(_settings) -> int:
        return 0

    monkeypatch.setattr(
        main, "fail_interrupted_hardening_runs", fake_fail_interrupted_hardening_runs
    )
    monkeypatch.setattr(
        main, "fail_interrupted_backend_backups", fake_fail_interrupted_backend_backups
    )


@pytest.mark.asyncio
async def test_http_exception_handler_renders_html_for_browser_requests() -> None:
    response = await http_exception_handler(
        _request("/"),
        StarletteHTTPException(status_code=403, detail="invalid csrf token"),
    )

    assert response.status_code == 403
    body = response.body.decode()
    assert "403 Forbidden" in body
    assert "Copy debug text" in body
    assert "invalid csrf token" in body
    assert "req" in body
    assert "code" in body
    assert "inst" in body
    assert "app id" in body


@pytest.mark.asyncio
async def test_unhandled_exception_handler_renders_html_without_traceback() -> None:
    response = await unhandled_exception_handler(
        _request("/"),
        RuntimeError("boom"),
    )
    await asyncio.sleep(0)

    assert response.status_code == 500
    body = response.body.decode()
    assert "500 Internal Server Error" in body
    assert "Use the codes below to find it in the log." in body
    assert "Copy debug text" in body
    assert "CNC-09001" in body
    assert "traceback:" not in body
    assert "RuntimeError" not in body
    assert "boom" not in body


@pytest.mark.asyncio
async def test_unhandled_exception_handler_returns_json_for_api_requests() -> None:
    response = await unhandled_exception_handler(
        _request("/api/status", accept="application/json"),
        RuntimeError("boom"),
    )
    await asyncio.sleep(0)

    assert response.status_code == 500
    payload = json.loads(response.body)
    assert payload["detail"] == "internal server error"
    assert payload["error_code"] == "CNC-09001"
    assert payload["error_name"] == "INTERNAL_UNHANDLED_EXCEPTION"
    assert payload["error_inst"]
    assert payload["request_id"]


@pytest.mark.asyncio
async def test_unhandled_exception_handler_sends_pushover_alert(monkeypatch) -> None:
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(main, "send_pushover_notification_async", fake_notify)

    response = await unhandled_exception_handler(
        _request("/api/status", accept="application/json"),
        RuntimeError("boom"),
    )
    await asyncio.sleep(0)

    assert response.status_code == 500
    assert sent
    assert sent[0]["title"] == "CNC internal error"
    assert "status: failure" in str(sent[0]["message"])
    assert "app id:" in str(sent[0]["message"])
    assert "error code: CNC-09001" in str(sent[0]["message"])
    assert "error name: INTERNAL_UNHANDLED_EXCEPTION" in str(sent[0]["message"])
    assert "error inst:" in str(sent[0]["message"])
    assert "request id:" in str(sent[0]["message"])
    assert "/api/status" in str(sent[0]["message"])


@pytest.mark.asyncio
async def test_unhandled_exception_handler_uses_live_settings(monkeypatch) -> None:
    captured_settings: list[object] = []

    async def fake_notify(settings, **_kwargs):
        captured_settings.append(settings)
        return True

    live_settings = main.boot_settings.model_copy(
        update={
            "pushover_app_token": "live-app-token",
            "pushover_user_key": "live-user-key",
        }
    )

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "send_pushover_notification_async", fake_notify)

    response = await unhandled_exception_handler(
        _request("/api/status", accept="application/json"),
        RuntimeError("boom"),
    )
    await asyncio.sleep(0)

    assert response.status_code == 500
    assert captured_settings == [live_settings]


@pytest.mark.asyncio
async def test_http_exception_handler_returns_request_id_for_api_requests() -> None:
    response = await http_exception_handler(
        _request("/api/forbidden", accept="application/json"),
        StarletteHTTPException(status_code=403, detail="invalid csrf token"),
    )

    payload = json.loads(response.body)
    assert response.status_code == 403
    assert payload["detail"] == "invalid csrf token"
    assert payload["request_id"]


@pytest.mark.asyncio
async def test_request_logging_middleware_sets_request_id_header(monkeypatch) -> None:
    request = _request("/status", accept="text/html")

    async def fake_call_next(inner_request: Request):
        assert getattr(inner_request.state, "request_id")
        return PlainTextResponse("ok", status_code=204)

    monkeypatch.setattr(
        main, "build_access_required_response", lambda *_args, **_kwargs: None
    )
    response = await request_logging_middleware(request, fake_call_next)

    assert response.status_code == 204
    assert response.headers["X-Request-ID"] == request.state.request_id


@pytest.mark.asyncio
async def test_request_logging_middleware_records_high_signal_failure_context(
    monkeypatch,
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, message: str, **kwargs: object) -> None:
            events.append(("info", message, kwargs))

        def warning(self, message: str, **kwargs: object) -> None:
            events.append(("warning", message, kwargs))

    request = _request(
        "/ui/backends/20/state",
        accept="application/json",
        method="GET",
        headers=[(b"x-requested-with", b"fetch")],
    )

    async def fake_call_next(_inner_request: Request):
        return PlainTextResponse(
            "method not allowed", status_code=405, headers={"allow": "POST"}
        )

    monkeypatch.setattr(main, "logger", FakeLogger())
    monkeypatch.setattr(
        main, "build_access_required_response", lambda *_args, **_kwargs: None
    )
    response = await request_logging_middleware(request, fake_call_next)

    assert response.status_code == 405
    completed = [item for item in events if item[1] == "http.request.completed"][-1]
    assert completed[0] == "warning"
    context = completed[2]
    assert context["method"] == "GET"
    assert context["path"] == "/ui/backends/20/state"
    assert context["status_code"] == 405
    assert context["status_family"] == "4xx"
    assert context["request_kind"] == "async:fetch"
    assert context["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_http_exception_handler_logs_router_rejections(monkeypatch) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, message: str, **kwargs: object) -> None:
            events.append(("warning", message, kwargs))

    request = _request("/ui/backends/20/state", accept="application/json", method="GET")
    monkeypatch.setattr(main, "logger", FakeLogger())
    response = await http_exception_handler(
        request,
        StarletteHTTPException(
            status_code=405, detail="Method Not Allowed", headers={"Allow": "POST"}
        ),
    )

    assert response.status_code == 405
    assert events[-1][1] == "http.request.rejected"
    assert events[-1][2]["method"] == "GET"
    assert events[-1][2]["path"] == "/ui/backends/20/state"
    assert events[-1][2]["status_code"] == 405
    assert events[-1][2]["detail"] == "Method Not Allowed"
    assert events[-1][2]["allow"] == "POST"


def test_app_uses_lifespan_instead_of_startup_event() -> None:
    assert app.router.on_startup == []
    assert app.router.lifespan_context is not None


def test_app_uses_trusted_host_middleware() -> None:
    assert any(
        middleware.cls.__name__ == "CNCTrustedHostMiddleware"
        for middleware in app.user_middleware
    )


@pytest.mark.asyncio
async def test_lifespan_checks_database_before_init(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_verify_db_connection() -> None:
        calls.append("verify")

    async def fake_init_db() -> None:
        calls.append("init")

    async def fake_cleanup(_settings) -> str:
        return "completed"

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "_run_interrupted_cleanup_if_unlocked", fake_cleanup)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        pass

    assert calls == ["verify", "init"]


@pytest.mark.asyncio
async def test_lifespan_inspects_runtime_assets_without_mutating(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_verify_db_connection() -> None:
        calls.append("verify")

    async def fake_init_db() -> None:
        calls.append("init")

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": ["cnc-auto-size.timer"],
            "timer_reconcile_needed": True,
            "timer_states": {
                "cnc-auto-size.timer": {"enabled": False, "active": False}
            },
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        pass

    assert calls == ["verify", "init"]


@pytest.mark.asyncio
async def test_lifespan_uses_live_settings_snapshot(monkeypatch) -> None:
    observed_settings: list[object] = []
    live_settings = main.boot_settings.model_copy(
        update={"pushover_app_token": "live-app-token"}
    )

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda settings: observed_settings.append(settings)
        or {"changed_units": [], "timer_reconcile_needed": False, "timer_states": {}},
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda settings: observed_settings.append(settings) or {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        pass

    assert observed_settings == [live_settings, live_settings]


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_systemd_watchdog(monkeypatch) -> None:
    calls: list[str] = []
    heartbeat_started = asyncio.Event()

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    class FakeWatchdog:
        def notify_status(self, *, status_message: str) -> bool:
            calls.append(f"status:{status_message}")
            return True

        def notify_ready(self, *, status_message: str | None = None) -> bool:
            calls.append(f"ready:{status_message}")
            return True

        def notify_stopping(self, *, status_message: str | None = None) -> bool:
            calls.append(f"stopping:{status_message}")
            return True

    async def fake_run_systemd_watchdog(
        _controller, *, status_message: str | None = None
    ) -> None:
        calls.append(f"heartbeat:{status_message}")
        heartbeat_started.set()
        await asyncio.Future()

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        main, "systemd_watchdog_controller_from_env", lambda: FakeWatchdog()
    )
    monkeypatch.setattr(main, "run_systemd_watchdog", fake_run_systemd_watchdog)
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(heartbeat_started.wait(), timeout=1.0)

    assert calls == [
        "status:cnc-admin starting",
        "ready:cnc-admin ready",
        "heartbeat:cnc-admin healthy",
        "stopping:cnc-admin stopping",
    ]


@pytest.mark.asyncio
async def test_lifespan_fails_when_systemd_watchdog_ready_notify_fails(
    monkeypatch,
) -> None:
    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    class FakeWatchdog:
        def notify_status(self, *, status_message: str) -> bool:
            return True

        def notify_ready(self, *, status_message: str | None = None) -> bool:
            return False

        def notify_stopping(self, *, status_message: str | None = None) -> bool:
            return True

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        main, "systemd_watchdog_controller_from_env", lambda: FakeWatchdog()
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    with pytest.raises(
        RuntimeError, match="systemd watchdog ready notification failed"
    ):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_lifespan_logs_and_continues_when_control_plane_self_audit_fails(
    monkeypatch,
) -> None:
    calls: list[str] = []
    sent: list[dict[str, object]] = []
    state: dict[str, object] = {}

    async def fake_verify_db_connection() -> None:
        calls.append("verify")

    async def fake_init_db() -> None:
        calls.append("init")

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction(state),
    )

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        self_audit_service, "send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: (_ for _ in ()).throw(
            ControlPlaneSelfAuditError(
                [
                    {
                        "check": "tailscale_admin_exposure",
                        "message": "tailscale funnel exposes HTTPS port 443",
                        "severity": "blocking",
                    }
                ]
            )
        ),
    )

    async with app.router.lifespan_context(app):
        pass

    assert calls == ["verify", "init"]
    assert sent
    assert sent[0]["title"] == "CNC host safeguard violation"
    assert "Critical host guardrail drift detected during CNC startup" in str(
        sent[0]["message"]
    )


@pytest.mark.asyncio
async def test_self_audit_releases_notification_state_before_durable_send(
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    transaction_active = False

    class GuardedNotificationStateTransaction(_FakeNotificationStateTransaction):
        def __enter__(self):
            nonlocal transaction_active
            assert transaction_active is False
            transaction_active = True
            return super().__enter__()

        def __exit__(self, exc_type, exc, tb) -> bool:
            nonlocal transaction_active
            transaction_active = False
            return super().__exit__(exc_type, exc, tb)

    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: GuardedNotificationStateTransaction(state),
    )

    async def fake_notify(_settings, **_kwargs):
        assert transaction_active is False
        return True

    monkeypatch.setattr(
        self_audit_service, "send_pushover_notification_async", fake_notify
    )

    await self_audit_service.handle_startup_self_audit_notification(
        main.boot_settings,
        ControlPlaneSelfAuditError(
            [
                {
                    "check": "tailscale_admin_exposure",
                    "message": "tailscale funnel exposes HTTPS port 443",
                    "severity": "blocking",
                }
            ]
        ),
    )

    assert transaction_active is False
    assert state["status"] == "failed"
    assert state["last_failure_notified_signature"] == state["signature"]


@pytest.mark.asyncio
async def test_lifespan_dedupes_repeated_self_audit_failures_and_notifies_recovery(
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    sent: list[dict[str, object]] = []

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction(state),
    )

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        self_audit_service, "send_pushover_notification_async", fake_notify
    )

    finding = ControlPlaneSelfAuditError(
        [
            {
                "check": "tailscale_admin_exposure",
                "message": "tailscale funnel exposes HTTPS port 443",
                "severity": "blocking",
            }
        ]
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: (_ for _ in ()).throw(finding),
    )

    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        pass

    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )

    async with app.router.lifespan_context(app):
        pass

    assert [item["title"] for item in sent] == [
        "CNC host safeguard violation",
        "CNC host drift cleared",
    ]


@pytest.mark.asyncio
async def test_lifespan_retries_self_audit_notification_for_new_signature_after_failed_send(
    monkeypatch,
) -> None:
    state: dict[str, object] = {
        "status": "failed",
        "signature": "blocking:old_check:old problem",
        "last_failure_notified_signature": "blocking:old_check:old problem",
        "last_failure_notified_at": "2026-04-18T21:00:00Z",
        "last_recovery_notified_signature": "",
        "last_recovery_notified_at": "",
    }
    sent: list[dict[str, object]] = []
    attempt_count = 0

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction(state),
    )

    async def fake_notify(_settings, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        sent.append(kwargs)
        return attempt_count > 1

    monkeypatch.setattr(
        self_audit_service, "send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: (_ for _ in ()).throw(
            ControlPlaneSelfAuditError(
                [
                    {
                        "check": "tailscale_admin_exposure",
                        "message": "tailscale funnel exposes HTTPS port 443",
                        "severity": "blocking",
                    }
                ]
            )
        ),
    )

    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        pass

    assert [item["title"] for item in sent] == [
        "CNC host safeguard violation",
        "CNC host safeguard violation",
    ]
    assert state["status"] == "failed"
    assert (
        state["signature"]
        == "blocking:tailscale_admin_exposure:tailscale funnel exposes HTTPS port 443"
    )
    assert state["last_failure_notified_signature"] == state["signature"]


@pytest.mark.asyncio
async def test_lifespan_retries_recovery_notification_after_failed_send(
    monkeypatch,
) -> None:
    signature = (
        "blocking:tailscale_admin_exposure:tailscale funnel exposes HTTPS port 443"
    )
    state: dict[str, object] = {
        "status": "failed",
        "signature": signature,
        "last_failure_notified_signature": signature,
        "last_failure_notified_at": "2026-04-18T21:00:00Z",
        "last_recovery_notified_signature": "",
        "last_recovery_notified_at": "",
    }
    sent: list[dict[str, object]] = []
    attempt_count = 0

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction(state),
    )

    async def fake_notify(_settings, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        sent.append(kwargs)
        return attempt_count > 1

    monkeypatch.setattr(
        self_audit_service, "send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )

    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        pass

    assert [item["title"] for item in sent] == [
        "CNC host drift cleared",
        "CNC host drift cleared",
    ]
    assert state["status"] == "ok"
    assert state["signature"] == ""
    assert state["pending_recovery_signature"] == ""
    assert state["last_recovery_notified_signature"] == signature


@pytest.mark.asyncio
async def test_lifespan_marks_db_failure_and_aborts_startup(monkeypatch) -> None:
    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    with pytest.raises(
        RuntimeError,
        match="startup database phase failed during init_db: database is locked",
    ):
        async with app.router.lifespan_context(app):
            pass

    startup = dict(app.state.cnc_startup_state)

    assert startup["db"]["status"] == "failed"
    assert startup["db"]["stage"] == "init_db"
    assert "database is locked" in startup["db"]["error"]


@pytest.mark.asyncio
async def test_startup_fails_closed_when_host_mutation_lock_is_unusable(
    monkeypatch, tmp_path
) -> None:
    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    unusable_parent = tmp_path / "not-a-directory"
    unusable_parent.write_text("occupied", encoding="utf-8")
    settings = main.boot_settings.model_copy(
        update={"apply_lock_path": unusable_parent / "host.lock"}
    )
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)

    main.initialize_startup_state(app)
    with pytest.raises(
        RuntimeError,
        match="startup database phase failed during validate_host_mutation_lock",
    ):
        await main._run_startup_database_phase(app, settings)

    startup = dict(app.state.cnc_startup_state)
    assert startup["db"]["status"] == "failed"
    assert startup["db"]["stage"] == "validate_host_mutation_lock"


@pytest.mark.asyncio
async def test_startup_defers_interrupted_cleanup_when_host_mutation_lock_busy(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_verify_db_connection() -> None:
        calls.append("verify")

    async def fake_init_db() -> None:
        calls.append("init")

    async def fail_interrupted_operations(_settings) -> int:
        raise AssertionError(
            "operation cleanup should wait while another host mutation holds the lock"
        )

    async def fail_interrupted_hardening_runs(_settings) -> int:
        raise AssertionError(
            "hardening cleanup should wait while another host mutation holds the lock"
        )

    async def fail_interrupted_backend_backups(_settings) -> int:
        raise AssertionError(
            "backup cleanup should wait while another host mutation holds the lock"
        )

    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "try_acquire_host_mutation_lock", lambda _settings: None)
    monkeypatch.setattr(
        main, "fail_interrupted_operations", fail_interrupted_operations
    )
    monkeypatch.setattr(
        main, "fail_interrupted_hardening_runs", fail_interrupted_hardening_runs
    )
    monkeypatch.setattr(
        main, "fail_interrupted_backend_backups", fail_interrupted_backend_backups
    )

    main.initialize_startup_state(app)
    await main._run_startup_database_phase(app, main.boot_settings)

    startup = dict(app.state.cnc_startup_state)
    assert calls == ["verify", "init"]
    assert startup["db"]["status"] == "warning"
    assert startup["db"]["stage"] == "fail_interrupted_host_mutations_deferred"
    assert startup["db"]["cleanup_deferred_reason"] == "host_mutation_lock_busy"
    task = app.state.cnc_startup_interrupted_cleanup_task
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_deferred_interrupted_cleanup_runs_after_lock_clears(monkeypatch) -> None:
    calls: list[str] = []
    lock_results: list[object | None] = [None, _FakeLock(calls)]

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def fake_fail_interrupted_operations(_settings) -> int:
        calls.append("operations")
        return 1

    async def fake_fail_interrupted_hardening_runs(_settings) -> int:
        calls.append("hardening")
        return 2

    async def fake_fail_interrupted_backend_backups(_settings) -> int:
        calls.append("backups")
        return 3

    def fake_acquire(_settings):
        return lock_results.pop(0) if lock_results else _FakeLock(calls)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main, "try_acquire_host_mutation_lock", fake_acquire)
    monkeypatch.setattr(main, "STARTUP_INTERRUPTED_CLEANUP_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(
        main, "fail_interrupted_operations", fake_fail_interrupted_operations
    )
    monkeypatch.setattr(
        main, "fail_interrupted_hardening_runs", fake_fail_interrupted_hardening_runs
    )
    monkeypatch.setattr(
        main, "fail_interrupted_backend_backups", fake_fail_interrupted_backend_backups
    )

    await main._retry_interrupted_cleanup_when_unlocked(main.boot_settings)

    assert calls == ["operations", "hardening", "backups", "released"]


@pytest.mark.asyncio
async def test_interrupted_cleanup_reports_failure_when_operation_query_fails(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fail_interrupted_operations(_settings) -> int:
        raise RuntimeError("operation cleanup query failed")

    monkeypatch.setattr(
        main, "try_acquire_host_mutation_lock", lambda _settings: _FakeLock(calls)
    )
    monkeypatch.setattr(
        main, "fail_interrupted_operations", fail_interrupted_operations
    )

    result = await main._run_interrupted_cleanup_if_unlocked(main.boot_settings)

    assert result == "cleanup_failed"
    assert calls == ["released"]


@pytest.mark.asyncio
async def test_interrupted_cleanup_holds_lock_until_cancelled_cleanup_finishes(
    monkeypatch,
) -> None:
    calls: list[str] = []
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_cleanup(_settings) -> None:
        started.set()
        await finish.wait()
        calls.append("cleanup_done")

    monkeypatch.setattr(
        main,
        "try_acquire_host_mutation_lock",
        lambda _settings: _FakeLock(calls),
    )
    monkeypatch.setattr(main, "_run_interrupted_cleanup_steps", slow_cleanup)

    task = asyncio.create_task(
        main._run_interrupted_cleanup_if_unlocked(main.boot_settings)
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert calls == []

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["released"]


@pytest.mark.asyncio
async def test_lifespan_marks_db_timeout_and_aborts_startup(monkeypatch) -> None:
    live_settings = main.boot_settings.model_copy(update={"startup_db_timeout_sec": 1})

    async def fake_verify_db_connection() -> None:
        await asyncio.sleep(1.1)

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    with pytest.raises(
        RuntimeError,
        match="startup database phase timed out during verify_db_connection",
    ):
        async with app.router.lifespan_context(app):
            pass

    startup = dict(app.state.cnc_startup_state)
    assert startup["db"]["status"] == "timeout"
    assert startup["db"]["stage"] == "verify_db_connection"
    assert startup["db"]["timeout_sec"] == 1


@pytest.mark.asyncio
async def test_lifespan_does_not_timeout_in_process_database_migration(
    monkeypatch,
) -> None:
    live_settings = main.boot_settings.model_copy(
        update={"startup_db_timeout_sec": 0.01}
    )
    calls: list[str] = []

    async def fake_verify_db_connection() -> None:
        calls.append("verify")

    async def fake_init_db() -> None:
        await asyncio.sleep(0.05)
        calls.append("init")

    async def fake_cleanup(_settings) -> str:
        return "completed"

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "_run_interrupted_cleanup_if_unlocked", fake_cleanup)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        startup = dict(app.state.cnc_startup_state)

    assert calls == ["verify", "init"]
    assert startup["db"]["status"] == "ok"
    assert startup["db"]["stage"] == "fail_interrupted_host_mutations"


@pytest.mark.asyncio
async def test_lifespan_logs_and_exits_cleanly_on_invalid_settings(
    monkeypatch, caplog
) -> None:
    def raise_invalid_settings():
        raise ValueError("admin_host='0.0.0.0' is not loopback")

    monkeypatch.setattr(main, "get_settings", raise_invalid_settings)
    caplog.set_level("ERROR")

    with pytest.raises(
        RuntimeError,
        match="invalid CNC configuration: admin_host='0.0.0.0' is not loopback",
    ):
        async with app.router.lifespan_context(app):
            pass

    startup = dict(app.state.cnc_startup_state)
    assert startup["config"]["status"] == "failed"
    assert startup["config"]["error_type"] == "ValueError"
    assert "admin_host='0.0.0.0' is not loopback" in startup["config"]["error"]
    assert any(
        getattr(record, "event_name", "") == "config.invalid"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_marks_self_audit_timeout_and_continues(monkeypatch) -> None:
    live_settings = main.boot_settings.model_copy(
        update={"startup_observe_timeout_sec": 1}
    )

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    def fake_self_audit(_settings):
        time.sleep(1.1)
        return {"ok": True}

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(
        main,
        "inspect_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        },
    )
    monkeypatch.setattr(
        main, "_run_control_plane_self_audit_for_startup", fake_self_audit
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        startup = dict(app.state.cnc_startup_state)

    assert startup["self_audit"]["status"] == "timeout"
    assert startup["self_audit"]["timeout_sec"] == 1


@pytest.mark.asyncio
async def test_lifespan_marks_runtime_asset_observation_timeout_and_continues(
    monkeypatch,
) -> None:
    live_settings = main.boot_settings.model_copy(
        update={"startup_observe_timeout_sec": 1}
    )

    async def fake_verify_db_connection() -> None:
        return None

    async def fake_init_db() -> None:
        return None

    def fake_runtime_assets(_settings):
        time.sleep(1.1)
        return {
            "changed_units": [],
            "timer_reconcile_needed": False,
            "timer_states": {},
        }

    monkeypatch.setattr(main, "get_settings", lambda: live_settings)
    monkeypatch.setattr(main, "verify_db_connection", fake_verify_db_connection)
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "inspect_managed_systemd_assets", fake_runtime_assets)
    monkeypatch.setattr(
        main,
        "_run_control_plane_self_audit_for_startup",
        lambda _settings: {"ok": True},
    )
    monkeypatch.setattr(
        self_audit_service,
        "_notification_state_transaction",
        lambda _settings: _FakeNotificationStateTransaction({}),
    )

    async with app.router.lifespan_context(app):
        startup = dict(app.state.cnc_startup_state)

    assert startup["runtime_assets"]["status"] == "timeout"
    assert startup["runtime_assets"]["timeout_sec"] == 1


@pytest.mark.asyncio
async def test_ready_reports_startup_db_failure_as_not_ready(monkeypatch) -> None:
    async def fake_verify_db_connection() -> None:
        return None

    monkeypatch.setattr(
        status_routes, "verify_db_connection", fake_verify_db_connection
    )
    app.state.cnc_startup_state = {
        "db": {"status": "failed", "error": "database is locked", "stage": "init_db"},
        "runtime_assets": {"status": "ok", "error": ""},
        "self_audit": {"status": "ok", "error": ""},
    }

    response = await status_routes.ready(
        _request("/ready", accept="application/json", asgi_app=app)
    )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "not_ready"
    assert payload["db"] == "error"
    assert payload["startup"]["db"]["status"] == "failed"


@pytest.mark.asyncio
async def test_ready_reports_startup_config_failure_as_not_ready(monkeypatch) -> None:
    async def fake_verify_db_connection() -> None:
        return None

    monkeypatch.setattr(
        status_routes, "verify_db_connection", fake_verify_db_connection
    )
    app.state.cnc_startup_state = {
        "config": {"status": "failed", "error": "invalid CNC configuration"},
        "db": {"status": "ok", "error": ""},
        "runtime_assets": {"status": "ok", "error": ""},
        "self_audit": {"status": "ok", "error": ""},
    }

    response = await status_routes.ready(
        _request("/ready", accept="application/json", asgi_app=app)
    )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "not_ready"
    assert payload["config"] == "error"
    assert payload["startup"]["config"]["status"] == "failed"


@pytest.mark.asyncio
async def test_live_does_not_probe_database() -> None:
    assert await status_routes.live() == {"status": "ok"}
