from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Operation
from app.services.host_journal import commit_verified
from app.services.operations import (
    OperationHandle,
    QUEUED_JOB_KEY,
    _json_dumps,
    _json_loads_details,
    _operation_session,
    job_awaiting_dispatch,
    operation_session_scope,
    queued_job,
    reserve_host_mutation_lock,
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
_SECRETS: dict[str, dict[str, Any]] = {}
_UNCONFIRMED_SECRETS: dict[str, dict[str, int]] = {}


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
    runner: str,
    *,
    kind: str | None = None,
    phase: str = "queued",
    details: dict[str, Any] | None = None,
    secret_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> OperationHandle:
    load_runners()
    if runner not in _RUNNERS:
        raise RuntimeError(f"mutation runner {runner} is not registered")
    secrets = {key: v for key, v in (secret_kwargs or {}).items() if v}
    submission_id = uuid4().hex
    job = {
        "submission_id": submission_id,
        "runner": runner,
        "kwargs": {key: _encode(v) for key, v in kwargs.items()},
        "secret_keys": sorted(secrets),
        "enqueued_at": datetime.now(UTC).isoformat(),
    }
    operation = Operation(
        kind=kind or runner,
        status="queued",
        actor="ui",
        phase=phase,
        backend_id=kwargs.get("backend_id"),
        details_json=json.dumps({**(details or {}), QUEUED_JOB_KEY: job}),
    )
    async with _operation_session(settings.database_url) as session:
        session.add(operation)
        await session.flush()
        operation_id = operation.id
        if secrets:
            _SECRETS[submission_id] = secrets

        def same_submission(row: Operation) -> bool:
            saved = queued_job(_json_loads_details(row.details_json))
            return bool(saved and saved.get("submission_id") == submission_id)

        try:
            commit = await commit_verified(
                session, settings, Operation, operation_id, matches=same_submission
            )
        except asyncio.CancelledError:
            if secrets:
                _UNCONFIRMED_SECRETS.setdefault(settings.database_url, {})[
                    submission_id
                ] = operation_id
            _wake.set()
            raise
        if commit.outcome == "rolled_back":
            _SECRETS.pop(submission_id, None)
            raise RuntimeError(f"Could not queue {runner}: {commit.error}")
        if commit.outcome == "unknown":
            if secrets:
                _UNCONFIRMED_SECRETS.setdefault(settings.database_url, {})[
                    submission_id
                ] = operation_id
            _wake.set()
            raise RuntimeError(f"Could not confirm operation {operation_id}. Check its status before submitting again.")
    _wake.set()
    logger.info("mutation_queue.enqueued", operation_id=operation_id, runner=runner)
    return OperationHandle(id=operation_id, kind=kind or runner, settings=settings)


async def _next_waiting_job(settings: Settings) -> int | None:
    async with _operation_session(settings.database_url) as session:
        unconfirmed = _UNCONFIRMED_SECRETS.get(settings.database_url)
        if unconfirmed:
            for submission_id, operation_id in tuple(unconfirmed.items()):
                operation = await session.get(Operation, operation_id)
                details = _json_loads_details(operation.details_json) if operation else {}
                job = queued_job(details)
                if not (
                    operation and operation.status == "queued"
                    and job and job.get("submission_id") == submission_id
                    and job_awaiting_dispatch(details)
                ):
                    _SECRETS.pop(submission_id, None)
                unconfirmed.pop(submission_id, None)
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
            if job_awaiting_dispatch(details):
                return int(operation.id)
    return None


async def _claim_job(settings: Settings, operation_id: int) -> dict[str, Any] | None:
    async with _operation_session(settings.database_url) as session:
        operation = await session.get(Operation, operation_id)
        if operation is None or operation.status != "queued":
            return None
        details = _json_loads_details(operation.details_json)
        if not job_awaiting_dispatch(details):
            return None
        job = queued_job(details)
        assert job is not None
        job["dispatched_at"] = datetime.now(UTC).isoformat()
        operation.details_json = _json_dumps(details)
        await session.commit()
        return job


async def _fail_undispatchable(
    settings: Settings, operation_id: int, error: str
) -> None:
    async with _operation_session(settings.database_url) as session:
        operation = await session.get(Operation, operation_id)
        if operation is None or operation.status not in {"queued", "running"}:
            return
        operation.status = "failed"
        operation.error = error
        operation.finished_at = datetime.now(UTC)
        await session.commit()


async def run_one(settings: Settings) -> bool:
    operation_id = await _next_waiting_job(settings)
    if operation_id is None:
        return False
    async with reserve_host_mutation_lock(settings) as admitted:
        if not admitted:
            return False
        return await _dispatch_job(settings, operation_id)


async def _dispatch_job(settings: Settings, operation_id: int) -> bool:
    job = await _claim_job(settings, operation_id)
    if job is None:
        return False
    secrets = _SECRETS.pop(job.get("submission_id", ""), {})
    registered = _RUNNERS.get(str(job["runner"]))
    if registered is None:
        await _fail_undispatchable(
            settings, operation_id, f"mutation runner {job['runner']} is not registered"
        )
        return True
    kwargs = dict(job.get("kwargs") or {})
    secret_keys = list(job.get("secret_keys") or [])
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
    async with operation_session_scope(settings.database_url):
        while not stop.is_set():
            _wake.clear()
            try:
                while not stop.is_set() and await run_one(settings):
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("mutation_queue.worker_iteration_failed", error=str(exc))
            if not stop.is_set():
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
