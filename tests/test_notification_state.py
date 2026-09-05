import threading
import time
import errno
import json

import pytest

from app.config import Settings
from app.services import backend_alerts
from app.services import cloudflare_sync
from app.services import host_state
from app.services import notification_state


def test_shared_host_state_write_prefers_temp_replace(monkeypatch, tmp_path) -> None:
    path = tmp_path / "cnc.env"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o640)
    monkeypatch.setattr(host_state, "uses_shared_host_state_lock", lambda _path: True)

    host_state.write_text_atomic(path, "new", encoding="utf-8")

    assert path.read_text(encoding="utf-8") == "new"
    assert path.stat().st_mode & 0o777 == 0o640
    assert not path.with_suffix(".env.tmp").exists()


def test_shared_host_state_write_does_not_fallback_after_temp_io_error(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "cnc.env"
    path.write_text("old", encoding="utf-8")
    in_place_calls: list[str] = []
    monkeypatch.setattr(host_state, "uses_shared_host_state_lock", lambda _path: True)

    def fail_replace(_path, _content, *, encoding="utf-8") -> None:
        raise OSError(errno.ENOSPC, "no space left")

    def fake_in_place(_path, _content, *, encoding="utf-8") -> None:
        in_place_calls.append(_content)

    monkeypatch.setattr(host_state, "_write_text_via_replace", fail_replace)
    monkeypatch.setattr(host_state, "_write_text_in_place", fake_in_place)

    with pytest.raises(OSError):
        host_state.write_text_atomic(path, "new", encoding="utf-8")

    assert in_place_calls == []
    assert path.read_text(encoding="utf-8") == "old"


def test_update_notification_event_serializes_concurrent_writers(
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

    def worker(event_key: str, status: str) -> None:
        barrier.wait()
        notification_state.update_notification_event(
            settings, event_key, {"status": status}
        )

    first = threading.Thread(target=worker, args=("first", "ok"))
    second = threading.Thread(target=worker, args=("second", "failed"))
    first.start()
    second.start()
    first.join()
    second.join()

    assert max_active_reads == 1
    payload = notification_state.read_notification_state(settings)
    assert payload["events"] == {
        "first": {"status": "ok"},
        "second": {"status": "failed"},
    }


def test_host_state_file_keeps_notification_backend_alert_and_cloudflare_sections(
    tmp_path,
) -> None:
    settings = Settings(host_state_path=tmp_path / "host-state.json")

    notification_state.update_notification_event(
        settings, "startup_self_audit", {"status": "failed"}
    )
    backend_alerts.write_backend_alerts_state(
        settings, {"backends": {"web": {"status": "down"}}}
    )
    cloudflare_sync.write_cloudflare_sync_state(
        settings, {"last_successful_cidrs": ["173.245.48.0/20"]}
    )

    payload = host_state.read_host_state_unlocked(settings.host_state_path)

    assert (
        payload["notification_state"]["events"]["startup_self_audit"]["status"]
        == "failed"
    )
    assert payload["backend_alerts"]["backends"]["web"]["status"] == "down"
    assert payload["cloudflare_sync"]["last_successful_cidrs"] == ["173.245.48.0/20"]


def test_settings_collapse_legacy_host_state_overrides_into_one_path(tmp_path) -> None:
    shared = tmp_path / "host-state.json"

    settings = Settings(notification_state_path=shared)

    assert settings.host_state_path == shared
    assert settings.notification_state_path == shared
    assert settings.backend_alerts_state_path == shared
    assert settings.cloudflare_sync_state_path == shared

    with pytest.raises(
        ValueError,
        match="legacy host-state path overrides must all point to the same file",
    ):
        Settings(
            notification_state_path=tmp_path / "notification-state.json",
            backend_alerts_state_path=tmp_path / "backend-alerts.json",
        )


def test_corrupt_host_state_file_recovers_cleanly_for_all_sections(tmp_path) -> None:
    path = tmp_path / "host-state.json"
    path.write_text("{not-json", encoding="utf-8")
    settings = Settings(host_state_path=path)

    assert notification_state.read_notification_state(settings) == {
        "events": {},
        "pushover_outbox": {},
        "pushover_receipts": {},
        "pushover_deliveries": {},
    }
    assert backend_alerts.read_backend_alerts_state(settings) == {"backends": {}}
    assert cloudflare_sync.read_cloudflare_sync_state(settings) == {}

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["notification_state"] == {
        "events": {},
        "pushover_outbox": {},
        "pushover_receipts": {},
        "pushover_deliveries": {},
    }
    assert repaired["backend_alerts"] == {"backends": {}}
    assert repaired["cloudflare_sync"] == {}
