import importlib

from app import logger as logger_module
import app.main as main_module


def test_importing_main_does_not_configure_logging(monkeypatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_configure_logging(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(logger_module, "configure_logging", fake_configure_logging)

    try:
        importlib.reload(main_module)
        assert calls == []
    finally:
        importlib.reload(main_module)
