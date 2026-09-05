from datetime import UTC, datetime

from app.config import Settings
from app.services import bootstrap_state


def test_write_bootstrap_state_uses_state_lock(monkeypatch, tmp_path) -> None:
    settings = Settings(app_control_dir=tmp_path / "app-control")
    events: list[str] = []

    class FakeLock:
        def __init__(self, _settings, _backend_name) -> None:
            events.append("init")

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> None:
            events.append("exit")

    monkeypatch.setattr(bootstrap_state, "BootstrapStateLock", FakeLock)

    bootstrap_state.write_bootstrap_state(
        settings,
        "web",
        {"status": "running"},
    )

    payload = bootstrap_state.read_bootstrap_state(settings, "web")

    assert events == ["init", "enter", "exit"]
    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["status"] == "running"


def test_read_bootstrap_state_normalizes_legacy_payload(tmp_path) -> None:
    settings = Settings(app_control_dir=tmp_path / "app-control")
    path = bootstrap_state.bootstrap_state_path(settings, "web")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"status":"failed","failure_phase":"app_bootstrap","last_log_excerpt":"boom"}',
        encoding="utf-8",
    )

    payload = bootstrap_state.read_bootstrap_state(settings, "web")

    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["backend"] == "web"
    assert payload["status"] == "failed"
    assert payload["failure_phase"] == "app_bootstrap"


def test_write_bootstrap_state_rejects_invalid_transition(tmp_path) -> None:
    settings = Settings(app_control_dir=tmp_path / "app-control")

    bootstrap_state.write_bootstrap_state(settings, "web", {"status": "running"})

    try:
        bootstrap_state.write_bootstrap_state(settings, "web", {"status": "pending"})
    except ValueError as exc:
        assert "running -> pending" in str(exc)
        return

    raise AssertionError("expected invalid bootstrap transition to be rejected")


def test_bootstrap_state_age_tracks_last_heartbeat() -> None:
    age = bootstrap_state.bootstrap_state_age_seconds(
        {
            "started_at": "2000-01-01T00:00:00Z",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )

    assert age is not None
    assert age <= 1
