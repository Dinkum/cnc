import asyncio
import threading

import pytest

import app.ui.diagnostics as ui_diagnostics
import app.ui.outputs.data as ui_outputs_data
from app.config import Settings
from app.models.entities import Backend


@pytest.mark.parametrize("shutdown", [False, True])
async def test_runtime_probes_bound_distinct_outputs_and_retain_thread_ownership(
    monkeypatch, shutdown
):
    ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE.clear()
    release, full = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = calls = 0

    def probe(backend, _settings):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
        try:
            assert release.wait(3)
            return {"diagnosis": "healthy", "backend_id": backend.id}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ui_diagnostics, "collect_app_backend_diagnostics", probe)
    monkeypatch.setattr(ui_outputs_data, "collect_app_backend_diagnostics", probe)
    monkeypatch.setattr(ui_diagnostics, "UI_RUNTIME_SIGNAL_TIMEOUT_SEC", 0.01)
    backends = [
        Backend(id=i + 1, name=f"web-{i}", kind="app", enabled=True) for i in range(8)
    ]
    settings = Settings(_env_file=None)
    try:
        responses = await asyncio.gather(
            *[
                ui_diagnostics.collect_output_runtime_diagnostics_for_ui(
                    backend, settings
                )
                for backend in backends
            ],
            return_exceptions=True,
        )
        assert all(
            isinstance(result, ui_diagnostics.RuntimeDiagnosticsPending)
            for result in responses
        )
        assert await asyncio.to_thread(full.wait, 1)
        assert calls == peak == 4
        # Overlapping subscribers must attach to queued as well as running work.
        duplicate = await asyncio.gather(
            *[
                ui_diagnostics.collect_output_runtime_diagnostics_for_ui(
                    backend, settings
                )
                for backend in backends
            ],
            return_exceptions=True,
        )
        assert all(
            isinstance(result, ui_diagnostics.RuntimeDiagnosticsPending)
            for result in duplicate
        )
        assert calls == 4
        if shutdown:
            stopping = asyncio.create_task(ui_diagnostics.cancel_runtime_diagnostics())
            await asyncio.sleep(0.01)
            assert not stopping.done()
            release.set()
            await stopping
            assert calls == 4  # Queued probes never started during shutdown.
            assert ui_diagnostics._RUNTIME_DIAGNOSTICS_CACHE == {}
        else:
            tasks = list(ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS.values())
            release.set()
            await asyncio.gather(*tasks)
            payloads = [
                await ui_diagnostics.collect_output_runtime_diagnostics_for_ui(
                    backend, settings
                )
                for backend in backends
            ]
            assert [payload["backend_id"] for payload in payloads] == list(range(1, 9))
            assert calls == 8
        assert peak == 4
        assert ui_diagnostics._RUNTIME_DIAGNOSTICS_TASKS == {}
        assert ui_diagnostics._runtime_diagnostics_workers._slots == {}
    finally:
        release.set()
        await ui_diagnostics.cancel_runtime_diagnostics()
