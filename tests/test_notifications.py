from __future__ import annotations

import logging
from urllib import error as urllib_error

import pytest

from app.logger import configure_logging
from app.config import Settings
from app.services import notifications
from app.services.notification_state import read_notification_state


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_build_operator_message_humanizes_labels_and_action() -> None:
    message = notifications.build_operator_message(
        "Backend recovered.",
        facts=[
            ("recovered_after", "2h 1m"),
            ("loopback_port", 12001),
        ],
        action="cnc-admin app doctor web",
    )

    assert "status: success" in message
    assert "summary: Backend recovered." in message
    assert "recovered after: 2h 1m" in message
    assert "loopback port: 12001" in message
    assert "next: cnc-admin app doctor web" in message


def test_clip_value_respects_limit_after_truncation() -> None:
    clipped_message = notifications._clip_value(
        "x" * 2000, notifications.PUSHOVER_MESSAGE_LIMIT
    )
    clipped_title = notifications._clip_value(
        "x" * 200, notifications.PUSHOVER_TITLE_LIMIT
    )

    assert len(clipped_message) == notifications.PUSHOVER_MESSAGE_LIMIT
    assert len(clipped_title) == notifications.PUSHOVER_TITLE_LIMIT
    assert clipped_message.endswith(" ...[truncated]")
    assert clipped_title.endswith(" ...[truncated]")


def test_mask_secret_reveals_only_last_four_characters() -> None:
    assert notifications.mask_secret("abcdefghijklmnopqrstuvwxyz1234") == (
        "**************************1234"
    )


def test_record_pushover_delivery_state_retries_lock_contention_synchronously(
    monkeypatch, tmp_path
) -> None:
    calls: list[bool] = []

    def fake_write(_settings, *, blocking: bool, **_payload) -> None:
        calls.append(blocking)
        if not blocking:
            raise BlockingIOError("locked")

    monkeypatch.setattr(notifications, "_write_pushover_delivery_state", fake_write)

    notifications._record_pushover_delivery_state(
        Settings(host_state_path=tmp_path / "host-state.json"),
        status="sent",
        event="apply_failed",
        source="apply",
        backend="web",
        run_id=7,
        title="CNC apply failed",
        priority=0,
    )

    assert calls == [False, True]


@pytest.mark.asyncio
async def test_send_notification_test_suite_emits_expected_titles(monkeypatch) -> None:
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(notifications, "send_pushover_notification_async", fake_notify)

    payload = await notifications.send_notification_test_suite(
        Settings(), backend_name="web"
    )

    assert payload["ok"] is True
    assert [item["kind"] for item in payload["notifications"]] == [
        "host_drift_detected",
        "host_drift_cleared",
        "apply_failed",
        "apply_completed_warning",
        "update_completed",
        "update_failed",
        "auto_size_resized",
        "cloudflare_sync_failed",
        "backend_auto_fix_failed",
        "backup_failed",
        "backup_import_failed",
        "restore_completed",
        "backend_restored",
        "restore_failed",
        "clone_failed",
        "internal_error",
        "settings_changed",
        "shield_settings_changed",
        "pushover_settings_changed",
    ]
    assert [item["title"] for item in sent] == [
        "CNC host drift detected",
        "CNC host drift cleared",
        "CNC apply failed",
        "CNC apply completed with warnings",
        "CNC update completed",
        "CNC update failed",
        "CNC auto-size resized outputs",
        "CNC Cloudflare sync failed",
        "CNC backend auto-fix failed: web",
        "CNC backup failed: web",
        "CNC backup import failed: web",
        "CNC restore completed: web",
        "CNC backend restored: web",
        "CNC restore failed: web",
        "CNC clone failed: web-copy",
        "CNC internal error",
        "CNC security setting changed",
        "CNC security setting changed",
        "CNC notification setting changed",
    ]
    for item in sent:
        message = str(item["message"])
        assert message.startswith("status: ")
        assert "\nsummary: " in message


@pytest.mark.asyncio
async def test_send_pushover_notification_logs_event_context(
    monkeypatch, tmp_path
) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=32_768,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    monkeypatch.setattr(
        notifications, "_send_pushover_notification", lambda *_args, **_kwargs: True
    )

    sent = await notifications.send_pushover_notification_async(
        Settings(pushover_app_token="app-token", pushover_user_key="user-key"),
        title="CNC apply failed",
        message="Apply failed.",
        event="apply_failed",
        source="apply",
        backend="web",
        run_id=7,
    )

    assert sent is True
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(
        "notification.pushover.attempt" in line and "event: apply_failed" in line
        for line in lines
    )
    assert any(
        "notification.pushover.sent" in line
        and "source: apply" in line
        and "backend: web" in line
        and "run_id: 7" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_send_notification_test_suite_logs_each_scenario(
    monkeypatch, tmp_path
) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=32_768,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    async def fake_notify(_settings, **_kwargs):
        return True

    monkeypatch.setattr(notifications, "send_pushover_notification_async", fake_notify)

    payload = await notifications.send_notification_test_suite(
        Settings(), backend_name="web"
    )

    assert payload["ok"] is True
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(
        "notification.test.started" in line and "backend: web" in line for line in lines
    )
    assert any(
        "notification.test.scenario" in line
        and "kind: apply_failed" in line
        and "sent: true" in line
        for line in lines
    )
    assert any(
        "notification.test.completed" in line and "notification_count: 19" in line
        for line in lines
    )


def test_send_pushover_notification_queues_transient_http_error(monkeypatch) -> None:
    settings = Settings(
        pushover_app_token="app-token",
        pushover_user_key="user-key",
        external_http_retry_attempts=1,
        external_http_retry_backoff_sec=0.0,
    )
    attempts: list[int] = []

    def fake_urlopen(request, timeout: int):
        attempts.append(timeout)
        raise urllib_error.HTTPError(
            request.full_url, 503, "service unavailable", hdrs=None, fp=None
        )

    monkeypatch.setattr(notifications.urllib_request, "urlopen", fake_urlopen)

    with pytest.raises(notifications.PushoverTransientError):
        notifications._send_pushover_notification(
            settings,
            title="CNC apply failed",
            message="Apply failed.",
            priority=0,
            url=None,
            url_title=None,
        )

    assert len(attempts) == 1


def test_send_pushover_notification_does_not_retry_http_429(monkeypatch) -> None:
    settings = Settings(pushover_app_token="app-token", pushover_user_key="user-key")
    attempts: list[int] = []

    def fake_urlopen(request, timeout: int):
        attempts.append(timeout)
        raise urllib_error.HTTPError(
            request.full_url, 429, "rate limited", hdrs=None, fp=None
        )

    monkeypatch.setattr(notifications.urllib_request, "urlopen", fake_urlopen)

    with pytest.raises(notifications.PushoverRejectedError, match="HTTP 429"):
        notifications._send_pushover_notification(
            settings,
            title="CNC apply failed",
            message="Apply failed.",
            priority=0,
            url=None,
            url_title=None,
        )

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_pushover_outbox_deduplicates_and_records_emergency_receipt(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        host_state_path=tmp_path / "host-state.json",
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )
    attempts: list[int] = []

    def fake_urlopen(_request, timeout: int):
        attempts.append(timeout)
        return _FakeResponse('{"status":1,"request":"request-1","receipt":"receipt-1"}')

    monkeypatch.setattr(notifications.urllib_request, "urlopen", fake_urlopen)
    kwargs = {
        "title": "CNC all outputs down",
        "message": "Every output is unhealthy.",
        "priority": 2,
        "event": "all_backends_down",
        "source": "backend.alerts",
        "dedupe_key": "all-backends-down:incident-1",
        "emergency_key": "all_backends_down",
    }

    assert await notifications.send_pushover_notification_async(settings, **kwargs)
    assert await notifications.send_pushover_notification_async(settings, **kwargs)

    state = read_notification_state(settings)
    assert state["pushover_outbox"] == {}
    assert state["pushover_receipts"]["all_backends_down"]["receipt"] == "receipt-1"
    assert len(attempts) == 1

    assert await notifications.cancel_pushover_emergency_async(
        settings, emergency_key="all_backends_down"
    )
    assert (
        "all_backends_down"
        not in read_notification_state(settings)["pushover_receipts"]
    )
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_pushover_outbox_keeps_transient_failure_for_later_retry(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        host_state_path=tmp_path / "host-state.json",
        pushover_app_token="app-token",
        pushover_user_key="user-key",
    )

    def fake_urlopen(request, timeout: int):
        assert timeout == settings.command_timeout_status_sec
        raise urllib_error.HTTPError(
            request.full_url, 503, "service unavailable", hdrs=None, fp=None
        )

    monkeypatch.setattr(notifications.urllib_request, "urlopen", fake_urlopen)

    sent = await notifications.send_pushover_notification_async(
        settings,
        title="CNC apply failed",
        message="Apply failed.",
        event="apply_failed",
        source="apply",
        emergency_key="test_emergency",
    )

    assert sent is False
    outbox = read_notification_state(settings)["pushover_outbox"]
    assert len(outbox) == 1
    assert next(iter(outbox.values()))["attempts"] == 1
    assert await notifications.cancel_pushover_emergency_async(
        settings, emergency_key="test_emergency"
    )
    assert read_notification_state(settings)["pushover_outbox"] == {}


def test_send_pushover_notification_does_not_retry_rejected_message(
    monkeypatch,
) -> None:
    settings = Settings(
        pushover_app_token="app-token",
        pushover_user_key="user-key",
        external_http_retry_attempts=2,
        external_http_retry_backoff_sec=0.0,
    )
    attempts: list[int] = []

    def fake_urlopen(_request, timeout: int):
        attempts.append(timeout)
        return _FakeResponse('{"status":0,"errors":["invalid user"]}')

    monkeypatch.setattr(notifications.urllib_request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="pushover rejected message"):
        notifications._send_pushover_notification(
            settings,
            title="CNC apply failed",
            message="Apply failed.",
            priority=0,
            url=None,
            url_title=None,
        )

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_send_pushover_notification_logs_error_type_and_retryable(
    monkeypatch, tmp_path
) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    def fail_send(*_args, **_kwargs):
        raise urllib_error.URLError("temporary dns failure")

    monkeypatch.setattr(notifications, "_send_pushover_notification", fail_send)

    sent = await notifications.send_pushover_notification_async(
        Settings(pushover_app_token="app-token", pushover_user_key="user-key"),
        title="CNC apply failed",
        message="Apply failed.",
        event="apply_failed",
        source="apply",
    )

    assert sent is False
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert any(
        "notification.pushover.failed" in line
        and "error_type: URLError" in line
        and "retryable: true" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_dispatch_limit_counts_due_attempts_not_cooldown_entries(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from app.services.notification_state import NotificationStateTransaction

    settings = Settings(
        host_state_path=tmp_path / "state.json",
        pushover_app_token="fixture",
        pushover_user_key="fixture",
    )
    now = datetime.now(UTC)
    with NotificationStateTransaction(settings) as state:
        for index in range(10):
            state.set_pushover_outbox_entry(
                str(index),
                {
                    "id": str(index),
                    "created_at": str(index),
                    "next_attempt_at": (
                        now + timedelta(hours=1)
                        if index < 5
                        else now - timedelta(seconds=1)
                    ).isoformat(),
                },
            )
    attempted = []

    def dispatch(_settings, intent_id):
        attempted.append(intent_id)
        return notifications.PushoverResponse(accepted=True)

    monkeypatch.setattr(notifications, "_dispatch_pushover_intent", dispatch)
    assert (
        await notifications.dispatch_pending_pushover_notifications_async(
            settings, limit=5
        )
        == 5
    )
    assert attempted == ["5", "6", "7", "8", "9"]


@pytest.mark.asyncio
async def test_outbox_lock_does_not_block_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    from app.services.notification_state import NotificationStateTransaction

    settings = Settings(
        host_state_path=tmp_path / "state.json",
        pushover_app_token="fixture",
        pushover_user_key="fixture",
    )
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with NotificationStateTransaction(settings):
            locked.set()
            release.wait(2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert await asyncio.to_thread(locked.wait, 1)
        dispatch = asyncio.create_task(
            notifications.dispatch_pending_pushover_notifications_async(settings)
        )
        await asyncio.sleep(0.02)
        assert not dispatch.done()
        assert not release.is_set()
        release.set()
        assert await dispatch == 0
    finally:
        release.set()
        await asyncio.to_thread(holder.join)
