"""Own output runtime probes, coalesced tasks, cached results, and shutdown draining."""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.app_diagnostics import collect_app_backend_diagnostics
from app.services.thread_workers import BoundedThreadWorker

logger = get_logger("ui")

UI_RUNTIME_SIGNAL_TIMEOUT_SEC = 0.9

UI_RUNTIME_SIGNAL_CACHE_TTL_SEC = 15.0


class RuntimeDiagnosticsPending(RuntimeError):
    pass


_RUNTIME_DIAGNOSTICS_CACHE: dict[
    tuple[int | None, str], tuple[float, dict[str, object]]
] = {}

_RUNTIME_DIAGNOSTICS_TASKS: dict[
    tuple[int | None, str], asyncio.Task[dict[str, object]]
] = {}

UI_RUNTIME_SIGNAL_CACHE_MAX_ENTRIES = 256

_runtime_diagnostics_workers = BoundedThreadWorker(4)


async def cancel_runtime_diagnostics() -> None:
    loop = asyncio.get_running_loop()
    tasks = [
        task for task in _RUNTIME_DIAGNOSTICS_TASKS.values() if task.get_loop() is loop
    ]
    for task in tasks:
        task.cancel()
    # Queued probes stop immediately; running threads keep their worker slot
    # until completion, so shutdown cannot abandon their subprocess work.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _RUNTIME_DIAGNOSTICS_CACHE.clear()


def _cache_completed_runtime_diagnostics_task(
    cache_key: tuple[int | None, str],
    backend_name: str,
    task: asyncio.Task[dict[str, object]],
) -> None:
    if _RUNTIME_DIAGNOSTICS_TASKS.get(cache_key) is not task:
        return
    _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
    try:
        payload = task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(
            "ui.output_runtime_signals.failed",
            backend=backend_name,
            error=str(exc),
        )
        return
    _RUNTIME_DIAGNOSTICS_CACHE[cache_key] = (time.monotonic(), dict(payload))
    _prune_runtime_diagnostics_cache()


def _prune_runtime_diagnostics_cache(now: float | None = None) -> None:
    cutoff = time.monotonic() if now is None else now
    expired = [
        key
        for key, (cached_at, _payload) in _RUNTIME_DIAGNOSTICS_CACHE.items()
        if cutoff - cached_at > UI_RUNTIME_SIGNAL_CACHE_TTL_SEC
    ]
    for key in expired:
        _RUNTIME_DIAGNOSTICS_CACHE.pop(key, None)
    overflow = len(_RUNTIME_DIAGNOSTICS_CACHE) - UI_RUNTIME_SIGNAL_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(
            _RUNTIME_DIAGNOSTICS_CACHE,
            key=lambda key: _RUNTIME_DIAGNOSTICS_CACHE[key][0],
        )[:overflow]
        for key in oldest_keys:
            _RUNTIME_DIAGNOSTICS_CACHE.pop(key, None)


async def collect_output_runtime_diagnostics_for_ui(
    backend: Backend,
    settings: Settings,
) -> dict[str, object] | None:
    if backend.kind != "app" or not backend.enabled:
        return None
    cache_key = (backend.id, backend.name)
    now = time.monotonic()
    _prune_runtime_diagnostics_cache(now)
    cached = _RUNTIME_DIAGNOSTICS_CACHE.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if now - cached_at <= UI_RUNTIME_SIGNAL_CACHE_TTL_SEC:
            return dict(payload)

    task = _RUNTIME_DIAGNOSTICS_TASKS.get(cache_key)
    if task is not None and task.done():
        _cache_completed_runtime_diagnostics_task(cache_key, backend.name, task)
        cached_after_task = _RUNTIME_DIAGNOSTICS_CACHE.get(cache_key)
        return dict(cached_after_task[1]) if cached_after_task is not None else None
    if task is None:
        task = asyncio.create_task(
            _runtime_diagnostics_workers.run(
                collect_app_backend_diagnostics, backend, settings
            )
        )
        _RUNTIME_DIAGNOSTICS_TASKS[cache_key] = task
        task.add_done_callback(
            lambda completed_task, key=cache_key, name=backend.name: (
                _cache_completed_runtime_diagnostics_task(key, name, completed_task)
            )
        )
    try:
        payload = await asyncio.wait_for(
            asyncio.shield(task), timeout=UI_RUNTIME_SIGNAL_TIMEOUT_SEC
        )
        _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
        _RUNTIME_DIAGNOSTICS_CACHE[cache_key] = (time.monotonic(), dict(payload))
        _prune_runtime_diagnostics_cache()
        return dict(payload)
    except TimeoutError:
        logger.warning(
            "ui.output_runtime_signals.timeout",
            backend=backend.name,
            timeout_sec=UI_RUNTIME_SIGNAL_TIMEOUT_SEC,
        )
        raise RuntimeDiagnosticsPending from None
    except Exception as exc:
        _RUNTIME_DIAGNOSTICS_TASKS.pop(cache_key, None)
        logger.warning(
            "ui.output_runtime_signals.failed",
            backend=backend.name,
            error=str(exc),
        )
        return None
