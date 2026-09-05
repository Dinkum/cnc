from app.config import Settings
from app.models.entities import Backend, Input
from app.services import shield_status
from app.services.commands import CommandResult


def _shield_graph() -> tuple[Backend, Input]:
    backend = Backend(id=1, name="shield", kind="shield", enabled=True)
    route = Input(id=1, kind="shield", hostname="shield.example.com", enabled=True)
    route.backends = [backend]
    return backend, route


def test_shield_status_requires_live_health_probe(monkeypatch) -> None:
    backend, route = _shield_graph()

    def fake_run(command: list[str], timeout_sec: int) -> CommandResult:
        if command[:2] == ["systemctl", "is-active"]:
            return CommandResult(command, 0, "active", "")
        return CommandResult(command, 7, "", "connection refused")

    monkeypatch.setattr(shield_status, "run_command", fake_run)

    payload = shield_status.collect_shield_status(
        backends=[backend],
        inputs=[route],
        settings=Settings(shield_enabled=True),
    )

    assert payload["config_ready"] is True
    assert payload["service_active"] is True
    assert payload["output_state"] == "unhealthy"
    assert payload["output_healthy"] is False
    assert payload["ready"] is False


def test_shield_status_is_ready_after_live_health_passes(monkeypatch) -> None:
    backend, route = _shield_graph()

    monkeypatch.setattr(
        shield_status,
        "run_command",
        lambda command, timeout_sec: CommandResult(
            command,
            0,
            "active" if command[:2] == ["systemctl", "is-active"] else "ok",
            "",
        ),
    )

    payload = shield_status.collect_shield_status(
        backends=[backend],
        inputs=[route],
        settings=Settings(shield_enabled=True),
    )

    assert payload["output_state"] == "healthy"
    assert payload["output_healthy"] is True
    assert payload["ready"] is True
