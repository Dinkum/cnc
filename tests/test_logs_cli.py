import pytest

from app.cli import logs as logs_cli
from app.config import Settings
from app.models.entities import Backend
from app.services import app_logs


def test_app_log_text_surfaces_partial_collection_errors() -> None:
    text = logs_cli.format_logs_text(
        {
            "backend": "web",
            "container": "cnc-app-web",
            "issues": ["container_logs_unavailable"],
            "bootstrap_status": "failed",
            "requested_lines": 80,
            "sources_available": True,
            "sources": [{"source": "bootstrap-state", "output": "last known failure"}],
            "source_errors": [
                {
                    "source": "container",
                    "returncode": 124,
                    "stderr": "timed out after 3s",
                    "stdout": "",
                }
            ],
            "partial": True,
            "ok": False,
        }
    )

    assert "collection_status: partial" in text
    assert "== bootstrap-state ==" in text
    assert "source_errors:" in text
    assert "container (exit 124): timed out after 3s" in text


@pytest.mark.asyncio
async def test_app_logs_returns_nonzero_when_collection_is_partial(
    monkeypatch, tmp_path
) -> None:
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )
    settings = Settings(app_control_dir=tmp_path / "app-control")

    async def fake_load_backend(_backend_name: str) -> Backend:
        return backend

    async def fake_shield_status(_settings: Settings) -> dict[str, object]:
        return {}

    monkeypatch.setattr(logs_cli, "load_backend_async", fake_load_backend)
    monkeypatch.setattr(logs_cli, "load_shield_status_payload", fake_shield_status)
    monkeypatch.setattr(
        app_logs,
        "collect_app_backend_logs",
        lambda *_args, **_kwargs: {
            "backend": "web",
            "sources": [{"source": "bootstrap-state", "output": "failure"}],
            "source_errors": [{"source": "container", "returncode": 124}],
            "partial": True,
            "ok": False,
        },
    )

    status_code, payload, error = await logs_cli.logs_app_async("web", 80, settings)

    assert status_code == 2
    assert payload is not None and payload["partial"] is True
    assert error is None
