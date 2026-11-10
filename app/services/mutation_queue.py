from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Operation
from app.services.operations import (
    QUEUED_JOB_KEY,
    _active_update_run_blocker,
    _json_dumps,
    _json_loads_details,
    _operation_session,
    job_awaiting_dispatch,
    queued_job,
    try_acquire_host_mutation_lock,
)


logger = get_logger("mutation_queue")

Runner = Callable[..., Awaitable[Any]]

POLL_INTERVAL_SEC = 2.0
SHUTDOWN_GRACE_SEC = 20.0


class _RegisteredRunner:
    def __init__(self, fn: Runner, models: dict[str, type[BaseModel]]) -> None:
        self.fn = fn
        self.models = models


_RUNNERS: dict[str, _RegisteredRunner] = {}

# The worker runs jobs by name, so it imports the modules that define them rather
# than depending on which routes happened to be imported first.
RUNNER_MODULES = (
    "app.ui.operations.backups",
    "app.ui.operations.cloudflare",
    "app.ui.operations.cluster",
    "app.ui.operations.hardening",
    "app.ui.operations.inputs",
    "app.ui.operations.output_lifecycle",
    "app.ui.operations.output_save",
)


def load_runners() -> None:
    for module in RUNNER_MODULES:
        importlib.import_module(module)
_wake = asyncio.Event()
# Secrets never reach the operation row, which is retained and shown in history.
# A job whose secrets were lost to a restart fails rather than running without them.
_SECRETS: dict[int, dict[str, Any]] = {}


def mutation_runner(
    name: str, **models: type[BaseModel]
) -> Callable[[Runner], Runner]:
    """Register `fn(settings, operation_id, **kwargs)`; `models` names kwargs to rebuild as pydantic models."""

    def register(fn: Runner) -> Runner:
        existing = _RUNNERS.get(name)
        if existing is not None and existing.fn is not fn:
            raise RuntimeError(f"mutation runner {name} registered twice")
        _RUNNERS[name] = _RegisteredRunner(fn, models)
        return fn

    return register


def _encode(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


async def enqueue_mutation(
    settings: Settings,
    operation_id: int,
    runner: str,
    *,
    secret_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    load_runners()
    if runner not in _RUNNERS:
        raise RuntimeError(f"mutation runner {runner} is not registered")
    secrets = {key: v for key, v in (secret_kwargs or {}).items() if v}
    job = {
        "runner": runner,
        "kwargs": {key: _encode(v) for key, v in kwargs.items()},
        "secret_keys": sorted(secrets),
    }
    json.dumps(job)
    if secrets:
        _SECRETS[operation_id] = secrets
    async with _operation_session(settings.database_url) as session:
        await session.execute(text("PRAGMA busy_timeout=5000"))
        operation = await session.get(Operation, operation_id)
        if operation is None:
            raise LookupError(f"operation {operation_id} not found")
        details = _json_loads_details(operation.details_json)
        details[QUEUED_JOB_KEY] = {**job, "enqueued_at": datetime.now(UTC).isoformat()}
        operation.details_json = _json_dumps(details)
        await session.commit()
    _wake.set()
    logger.info("mutation_queue.enqueued", operation_id=operation_id, runner=runner)


async def _next_job(settings: Settings) -> tuple[int, dict[str, Any]] | None:
    async with _operation_session(settings.database_url) as session:
        await session.execute(text("PRAGMA busy_timeout=5000"))
        rows = (
            (
                await session.execute(
                    select(Operation)
                    .where(Operation.status == "queued")
                    .order_by(Operation.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for operation in rows:
            details = _json_loads_details(operation.details_json)
            if not job_awaiting_dispatch(details):
                continue
            job = queued_job(details)
            assert job is not None
            job["dispatched_at"] = datetime.now(UTC).isoformat()
            details[QUEUED_JOB_KEY] = job
            operation.details_json = _json_dumps(details)
            await session.commit()
            return int(operation.id), job
    return None


async def _fail_undispatchable(
    settings: Settings, operation_id: int, error: str
) -> None:
    async with _operation_session(settings.database_url) as session:
        await session.execute(text("PRAGMA busy_timeout=5000"))
        operation = await session.get(Operation, operation_id)
        if operation is None or operation.status not in {"queued", "running"}:
            return
        operation.status = "failed"
        operation.error = error
        operation.finished_at = datetime.now(UTC)
        await session.commit()


async def _host_is_free(settings: Settings) -> bool:
    if await _active_update_run_blocker(settings) is not None:
        return False
    lock = try_acquire_host_mutation_lock(settings)
    if lock is None:
        return False
    lock.__exit__(None, None, None)
    return True


async def run_one(settings: Settings) -> bool:
    if not await _host_is_free(settings):
        return False
    claimed = await _next_job(settings)
    if claimed is None:
        return False
    operation_id, job = claimed
    registered = _RUNNERS.get(str(job["runner"]))
    if registered is None:
        await _fail_undispatchable(
            settings, operation_id, f"mutation runner {job['runner']} is not registered"
        )
        return True
    kwargs = dict(job.get("kwargs") or {})
    secret_keys = list(job.get("secret_keys") or [])
    secrets = _SECRETS.pop(operation_id, {})
    if any(key not in secrets for key in secret_keys):
        await _fail_undispatchable(
            settings,
            operation_id,
            "CNC restarted before this save ran and does not keep submitted "
            "secrets; submit the change again",
        )
        return True
    kwargs.update(secrets)
    try:
        for key, model in registered.models.items():
            if key in kwargs:
                kwargs[key] = model.model_validate(kwargs[key])
    except Exception as exc:
        await _fail_undispatchable(settings, operation_id, f"invalid job arguments: {exc}")
        return True
    logger.info(
        "mutation_queue.dispatch", operation_id=operation_id, runner=job["runner"]
    )
    try:
        await registered.fn(settings, operation_id, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "mutation_queue.runner_failed",
            operation_id=operation_id,
            runner=job["runner"],
            error=str(exc),
        )
        await _fail_undispatchable(settings, operation_id, str(exc))
    return True


async def run_mutation_worker(settings: Settings, stop: asyncio.Event) -> None:
    load_runners()
    while not stop.is_set():
        try:
            while not stop.is_set() and await run_one(settings):
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("mutation_queue.worker_iteration_failed", error=str(exc))
        _wake.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=POLL_INTERVAL_SEC)


async def stop_mutation_worker(
    task: asyncio.Task[None], stop: asyncio.Event, *, grace_sec: float
) -> None:
    """Let the running job finish within the grace period; a cancelled job is recovered on the next start."""
    stop.set()
    _wake.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=grace_sec)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    except asyncio.CancelledError:
        pass
