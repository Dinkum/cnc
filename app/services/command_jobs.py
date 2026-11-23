from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import AsyncIterator, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, CommandJob, Operation
from app.services import command_job_runtime as runtime
from app.services.command_job_files import (
    read_job_result,
    read_job_stream,
    release_job,
    reserve_job,
)
from app.services.guest_exec import (
    BackendGuestExecGuardState,
    backend_guest_exec_guard,
    open_guest_exec_circuit,
    read_guest_exec_circuit,
)
from app.services.operations import (
    _operation_session,
    operation_session_scope,
    try_acquire_host_mutation_lock,
)


logger = get_logger("command.jobs")
ACTIVE = {"queued", "running"}
RETAINED_OUTPUT_JOBS = 20


class CommandJobError(ValueError):
    def __init__(
        self, code: str, message: str, operation_id: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


@contextmanager
def submission_guard(settings: Settings) -> Iterator[None]:
    lock = try_acquire_host_mutation_lock(settings)
    if lock is None:
        raise CommandJobError(
            "host_busy",
            "CNC maintenance is running; wait before submitting or cancelling",
        )
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


@asynccontextmanager
async def job_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    async with _operation_session(settings.database_url) as session:
        yield session


async def _bounded_thread(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def command_argv(frame: dict) -> list[str]:
    if ("argv" in frame) == ("shell" in frame):
        raise CommandJobError("invalid_command", "provide exactly one of argv or shell")
    if "shell" in frame:
        if (
            not isinstance(frame["shell"], str)
            or not frame["shell"].strip()
            or "\0" in frame["shell"]
        ):
            raise CommandJobError(
                "invalid_command", "shell must be nonempty text without NUL"
            )
        argv = ["/bin/bash", "-lc", frame["shell"]]
    else:
        argv = frame["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
            or not argv[0]
        ):
            raise CommandJobError(
                "invalid_command",
                "argv must contain a command and string arguments without NUL",
            )
    if len(json.dumps(argv).encode()) > 48 * 1024:
        raise CommandJobError("invalid_command", "command exceeds 48 KiB")
    return argv


async def submit_job(
    settings: Settings,
    output: str,
    frame: dict,
    *,
    request_key: str | None = None,
) -> dict:
    argv = command_argv(frame)
    timeout = frame.get("timeout_sec")
    if timeout is not None and (
        type(timeout) is not int or not 1 <= timeout <= 30 * 86400
    ):
        raise CommandJobError(
            "invalid_timeout", "execution timeout must be 1..2592000 seconds or omitted"
        )
    key = request_key if request_key is not None else str(uuid4())
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise CommandJobError(
            "invalid_request_key",
            "request key must be 1..128 letters, digits, dots, colons, underscores or hyphens",
        )
    digest = hashlib.sha256(
        json.dumps([output, argv, timeout], ensure_ascii=True).encode()
    ).hexdigest()
    async with job_session(settings) as session:
        previous = (
            await session.execute(
                select(CommandJob).where(CommandJob.request_key == key)
            )
        ).scalar_one_or_none()
        if previous is not None:
            if previous.request_hash != digest:
                raise CommandJobError(
                    "request_conflict",
                    "request key already belongs to a different command",
                    previous.operation_id,
                )
            previous_id = previous.operation_id
        else:
            previous_id = None
        backend = (
            await session.execute(select(Backend).where(Backend.name == output))
        ).scalar_one_or_none()
        if backend is None or backend.kind != "app" or not backend.enabled:
            raise CommandJobError(
                "output_unavailable",
                "background execution requires an enabled app output",
            )
        backend_id = backend.id
    if previous_id is not None:
        return await get_job(settings, previous_id)

    async with job_session(settings) as session:
        active_ids = list(
            (
                await session.execute(
                    select(Operation.id).where(
                        Operation.kind == "output_exec",
                        Operation.backend_id == backend_id,
                        Operation.status.in_(ACTIVE),
                    )
                )
            ).scalars()
        )
    for previous_id in active_ids:
        await get_job(settings, previous_id)
    if read_guest_exec_circuit(settings, output):
        raise CommandJobError(
            "backend_exec_unavailable",
            "guest execution circuit is open; use output doctor/fix",
        )
    with (
        submission_guard(settings),
        backend_guest_exec_guard(settings, output) as guard,
    ):
        if guard != BackendGuestExecGuardState.ACQUIRED:
            raise CommandJobError(
                "backend_busy"
                if guard == BackendGuestExecGuardState.BUSY
                else "backend_exec_unavailable",
                "output execution slot is unavailable; inspect active operations",
            )
        if read_guest_exec_circuit(settings, output):
            raise CommandJobError(
                "backend_exec_unavailable", "guest execution circuit is open"
            )
        observed = await _bounded_thread(runtime.observe_runtime, output)
        if observed["state"] != "running":
            raise CommandJobError(
                "runtime_unavailable", "a running output could not be confirmed"
            )
        async with job_session(settings) as session:
            backend = await session.get(Backend, backend_id)
            if (
                backend is None
                or backend.name != output
                or backend.kind != "app"
                or not backend.enabled
            ):
                raise CommandJobError(
                    "output_unavailable", "output changed before submission"
                )
            purge_jobs = list(
                (
                    await session.execute(
                        select(CommandJob.operation_id, CommandJob.execution_token)
                        .join(Operation, Operation.id == CommandJob.operation_id)
                        .where(
                            CommandJob.output_name == output,
                            ~Operation.status.in_(ACTIVE),
                        )
                        .order_by(CommandJob.operation_id.desc())
                        .offset(RETAINED_OUTPUT_JOBS - 1)
                        .limit(32)
                    )
                ).all()
            )
            operation = Operation(
                kind="output_exec",
                status="queued",
                phase="submitting",
                backend_id=backend_id,
                actor="operator",
                details_json=json.dumps(
                    {"output": output, "request_key": key, "timeout_sec": timeout}
                ),
            )
            session.add(operation)
            await session.flush()
            execution_token = uuid4().hex
            job = CommandJob(
                operation_id=operation.id,
                execution_token=execution_token,
                request_key=key,
                request_hash=digest,
                output_name=output,
                container_id=observed["container_id"],
                container_started_at=observed["started_at"],
                cancel_requested=False,
            )
            session.add(job)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                previous = (
                    await session.execute(
                        select(CommandJob).where(CommandJob.request_key == key)
                    )
                ).scalar_one_or_none()
                if previous is None or previous.request_hash != digest:
                    raise CommandJobError(
                        "request_conflict", "request key already exists"
                    ) from None
                return await get_job(settings, previous.operation_id)
            operation_id = operation.id
        try:
            reserve_job(settings, output, operation_id, execution_token=execution_token)
        except OSError as exc:
            async with job_session(settings) as session:
                operation = await session.get(Operation, operation_id)
                assert operation is not None
                operation.status, operation.phase = "failed", "not_started"
                operation.error = "could not reserve output execution"
                operation.finished_at = datetime.now(UTC)
                await session.commit()
            raise CommandJobError(
                "storage_unavailable", "command was not started", operation_id
            ) from exc
        result = await _bounded_thread(
            runtime.launch_job,
            observed["container_id"],
            operation_id,
            argv,
            timeout,
            purge_jobs,
            execution_token,
        )
        if result.timed_out:
            open_guest_exec_circuit(
                settings,
                output,
                timeout_sec=runtime.CONTROL_TIMEOUT_SEC,
                error="background command submission timed out",
                source="command_submit",
            )
        async with job_session(settings) as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None
            operation.status = "running"
            operation.phase = "executing" if result.ok else "submission_unknown"
            if not result.ok:
                operation.error = "submission outcome is unknown; inspect this operation, do not resubmit with a new key"
            await session.commit()
    logger.info(
        "command.submitted",
        operation_id=operation_id,
        backend=output,
        accepted=result.ok,
    )
    return await get_job(settings, operation_id, observe=False)


def _job_payload(job: CommandJob, operation: Operation) -> dict:
    return {
        **json.loads(operation.details_json),
        "operation_id": operation.id,
        "kind": operation.kind,
        "output": job.output_name,
        "status": operation.status,
        "phase": operation.phase,
        "error": operation.error,
        "request_key": job.request_key,
        "cancel_requested": job.cancel_requested,
    }


async def get_job(
    settings: Settings, operation_id: int, *, observe: bool = True
) -> dict:
    async with operation_session_scope(settings.database_url):
        async with job_session(settings) as session:
            record = (
                await session.execute(
                    select(CommandJob, Operation)
                    .join(Operation, Operation.id == CommandJob.operation_id)
                    .where(CommandJob.operation_id == operation_id)
                )
            ).one_or_none()
        if record is None:
            raise CommandJobError(
                "operation_not_found", "command operation not found", operation_id
            )
        job, operation = record
        return await _observe_job(settings, job, operation, observe=observe)


async def _observe_job(
    settings: Settings, job: CommandJob, operation: Operation, *, observe: bool
) -> dict:
    from sqlalchemy import case, update

    operation_id = operation.id
    output = job.output_name
    execution_token = job.execution_token
    if operation.status not in ACTIVE:
        release_job(settings, output, operation_id, execution_token=execution_token)
        return _job_payload(job, operation)
    container_id = job.container_id
    container_started_at = job.container_started_at

    result = None
    unreadable = False
    observed = None
    try:
        result = read_job_result(
            settings, output, operation_id, execution_token=execution_token
        )
    except (OSError, ValueError):
        unreadable = True
    if result is None and observe and not unreadable:
        observed = await _bounded_thread(runtime.observe_runtime, output)
        try:
            result = read_job_result(
                settings, output, operation_id, execution_token=execution_token
            )
        except (OSError, ValueError):
            unreadable = True

    async with job_session(settings) as session:
        job = await session.get(CommandJob, operation_id)
        operation = await session.get(Operation, operation_id)
        if job is None or operation is None:
            raise CommandJobError(
                "operation_not_found", "command operation not found", operation_id
            )
        if job.execution_token != execution_token:
            return _job_payload(job, operation)
        values = {}
        if operation.status in ACTIVE:
            if result is not None:
                if result["service_result"] == "success" and result["exit_code"] == 0:
                    status = "success"
                elif result["service_result"] in {"success", "signal"} and result[
                    "exit_kind"
                ] in {"killed", "dumped"}:
                    cancellation = (
                        select(CommandJob.cancel_requested)
                        .where(CommandJob.operation_id == operation_id)
                        .scalar_subquery()
                    )
                    status = case((cancellation.is_(True), "cancelled"), else_="failed")
                else:
                    status = "failed"
                values = {
                    "status": status,
                    "phase": "completed",
                    "error": None
                    if result["service_result"] == "success"
                    and result["exit_code"] == 0
                    else result["service_result"],
                    "finished_at": datetime.now(UTC),
                    "details_json": json.dumps(
                        {**json.loads(operation.details_json), **result}
                    ),
                }
            elif unreadable or (
                observed is not None and observed["state"] == "unknown"
            ):
                values = {"phase": "observation_unavailable"}
            elif observed is not None:
                if (
                    observed["state"] != "running"
                    or observed.get("container_id") != container_id
                    or observed.get("started_at") != container_started_at
                ):
                    values = {
                        "status": "failed",
                        "phase": "interrupted",
                        "error": "output runtime stopped or changed before a command result was recorded",
                        "finished_at": datetime.now(UTC),
                    }
                elif operation.phase == "observation_unavailable":
                    values = {"phase": "executing"}
            if values:
                await session.execute(
                    update(Operation)
                    .where(
                        Operation.id == operation_id,
                        Operation.status.in_(ACTIVE),
                        select(CommandJob.execution_token)
                        .where(CommandJob.operation_id == operation_id)
                        .scalar_subquery()
                        == execution_token,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            else:
                await session.rollback()
            await session.refresh(operation)
            await session.refresh(job)
        response = _job_payload(job, operation)
    if response["status"] not in ACTIVE:
        release_job(settings, output, operation_id, execution_token=execution_token)
    return response


async def cancel_job(settings: Settings, operation_id: int) -> dict:
    payload = await get_job(settings, operation_id)
    if payload["status"] not in ACTIVE:
        return payload
    async with job_session(settings) as session:
        job = await session.get(CommandJob, operation_id)
        assert job is not None
        job.cancel_requested = True
        await session.commit()
        output = job.output_name
        container_id = job.container_id
        execution_token = job.execution_token
    with submission_guard(settings):
        payload = await get_job(settings, operation_id)
        if payload["status"] not in ACTIVE:
            return payload
        result = await _bounded_thread(
            runtime.stop_job,
            settings,
            output,
            operation_id,
            container_id,
            execution_token,
        )
        if result.ok and result.stdout == "CNC_COMMAND_ABSENT":
            async with job_session(settings) as session:
                operation = await session.get(Operation, operation_id)
                assert operation is not None
                operation.status, operation.phase = "failed", "outcome_unknown"
                operation.error = "command service is absent and no result was retained; it is no longer running"
                operation.finished_at = datetime.now(UTC)
                await session.commit()
            release_job(settings, output, operation_id, execution_token=execution_token)
    payload = await get_job(settings, operation_id, observe=False)
    if not result.ok:
        payload["cancel_error"] = (
            "cancellation could not be confirmed; inspect the operation and runtime"
        )
    return payload


async def job_logs(
    settings: Settings, operation_id: int, stream: str = "stdout", offset: int = 0
) -> dict:
    payload = await get_job(settings, operation_id, observe=False)
    async with job_session(settings) as session:
        job = await session.get(CommandJob, operation_id)
        if job is None:
            raise CommandJobError(
                "operation_not_found", "command operation not found", operation_id
            )
        execution_token = job.execution_token
    try:
        chunk = read_job_stream(
            settings,
            payload["output"],
            operation_id,
            stream,
            execution_token=execution_token,
            offset=offset,
            terminal=payload["status"] not in ACTIVE,
        )
    except (OSError, ValueError) as exc:
        raise CommandJobError(
            "output_unavailable",
            "command output could not be read safely",
            operation_id,
        ) from exc
    return {"operation_id": operation_id, "status": payload["status"], **chunk}


async def list_jobs(settings: Settings, output: str, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CommandJobError("invalid_limit", "limit must be between 1 and 100")
    async with operation_session_scope(settings.database_url):
        async with job_session(settings) as session:
            records = (
                await session.execute(
                    select(CommandJob, Operation)
                    .join(Operation, Operation.id == CommandJob.operation_id)
                    .where(CommandJob.output_name == output)
                    .order_by(CommandJob.operation_id.desc())
                    .limit(limit)
                )
            ).all()
        return {
            "operations": [
                await _observe_job(settings, job, operation, observe=False)
                for job, operation in records
            ]
        }
