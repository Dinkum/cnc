from __future__ import annotations

import argparse
import asyncio
import time

from sqlalchemy import select

from app.cli.common import CommandResult, bind_command
from app.config import Settings


POLL_SECONDS = 0.5
MAX_LOG_BYTES = 1024 * 1024


def _wait_seconds(value: str) -> float:
    seconds = float(value)
    if not 0 <= seconds <= 60:
        raise argparse.ArgumentTypeError("wait must be between 0 and 60 seconds")
    return seconds


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _offset(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("offset must be nonnegative")
    return number


def _limit(value: str) -> int:
    number = _positive_int(value)
    if number > 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return number


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "operation", help="Inspect and control submitted work"
    )
    commands = parser.add_subparsers(dest="operation_command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--output")
    listing.add_argument("--limit", type=_limit, default=20)
    show = commands.add_parser("show")
    show.add_argument("operation_id", type=_positive_int)
    show.add_argument(
        "--wait",
        nargs="?",
        const=30.0,
        default=0.0,
        type=_wait_seconds,
        help="Wait for completion for up to SECONDS (default 30, maximum 60)",
        metavar="SECONDS",
    )
    logs = commands.add_parser("logs")
    logs.add_argument("operation_id", type=_positive_int)
    logs.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    logs.add_argument("--offset", type=_offset, default=0)
    logs.add_argument(
        "--follow",
        action="store_true",
        help="Collect new output for a bounded interval",
    )
    logs.add_argument("--wait-seconds", type=_wait_seconds, default=30.0)
    cancel = commands.add_parser(
        "cancel", help="Request cancellation of a background command"
    )
    cancel.add_argument("operation_id", type=_positive_int)
    for command, handler in (
        (listing, _run_list),
        (show, _run_show),
        (logs, _run_logs),
        (cancel, _run_cancel),
    ):
        command.add_argument("--json", action="store_true", dest="json_output")
        bind_command(
            command,
            handler=handler,
            formatter=format_operation_text,
            needs_settings=True,
        )


def format_operation_text(payload: dict[str, object]) -> str:
    if "data" in payload:
        return str(payload["data"])
    if "operations" in payload:
        return (
            "\n".join(
                f"{item['id']}  {item['kind']}  {item['status']}"
                for item in payload["operations"]
            )
            or "No operations."
        )
    return "\n".join(
        f"{key}: {value}" for key, value in payload.items() if value is not None
    )


def _operation_ids(payload: dict[str, object]) -> dict[str, object]:
    operation_id = payload.get("operation_id", payload.get("id"))
    return (
        {**payload, "id": operation_id, "operation_id": operation_id}
        if operation_id is not None
        else payload
    )


async def _load_operation(settings: Settings, operation_id: int) -> dict[str, object]:
    from app.database import SessionLocal
    from app.models.entities import Operation
    from app.routes.status import operation_payload
    from app.services.command_jobs import CommandJobError, get_job

    async with SessionLocal() as session:
        operation = await session.get(Operation, operation_id)
        if operation is None:
            raise CommandJobError(
                "operation_not_found", "operation not found", operation_id=operation_id
            )
        if operation.kind != "output_exec":
            return _operation_ids(operation_payload(operation, settings))
    return _operation_ids(await get_job(settings, operation_id))


async def _run_list(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    from app.database import SessionLocal
    from app.models.entities import Backend, Operation
    from app.routes.status import operation_payload
    from app.services.command_jobs import CommandJobError

    assert settings is not None
    async with SessionLocal() as session:
        query = select(Operation).order_by(Operation.id.desc()).limit(args.limit)
        if args.output:
            backend_id = (
                await session.execute(
                    select(Backend.id).where(Backend.name == args.output)
                )
            ).scalar_one_or_none()
            if backend_id is None:
                raise CommandJobError(
                    "output_not_found", f"output not found: {args.output}"
                )
            query = query.where(Operation.backend_id == backend_id)
        records = (await session.execute(query)).scalars().all()
        payloads = [operation_payload(row, settings) for row in records]
    from app.services.command_jobs import get_job

    for index, payload in enumerate(payloads):
        if payload["kind"] == "output_exec":
            # Read retained completion evidence without probing every guest runtime.
            payload = {
                **payload,
                **await get_job(settings, payload["id"], observe=False),
            }
        payloads[index] = _operation_ids(payload)
    return 0, {"operations": payloads}, None


async def _run_show(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    deadline = time.monotonic() + args.wait
    while True:
        payload = await _load_operation(settings, args.operation_id)
        pending = payload.get("status") in {"queued", "running"}
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            if args.wait:
                payload = {**payload, "wait_timed_out": pending}
            return 0, payload, None
        # Waiting observes work; reaching this deadline never cancels it.
        await asyncio.sleep(min(POLL_SECONDS, remaining))


async def _run_logs(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    from app.services.command_jobs import job_logs

    assert settings is not None
    deadline = time.monotonic() + (args.wait_seconds if args.follow else 0)
    offset = args.offset
    chunks: list[str] = []
    size = 0
    while True:
        payload = await job_logs(
            settings, args.operation_id, stream=args.stream, offset=offset
        )
        data = str(payload.get("data") or "")
        chunks.append(data)
        size += len(data.encode("utf-8"))
        next_offset = int(payload["next_offset"])
        advanced = next_offset > offset
        offset = next_offset
        remaining = deadline - time.monotonic()
        if (
            not args.follow
            or payload.get("eof")
            or remaining <= 0
            or size >= MAX_LOG_BYTES
        ):
            return (
                0,
                _operation_ids(
                    {**payload, "offset": args.offset, "data": "".join(chunks)}
                ),
                None,
            )
        if not advanced:
            await asyncio.sleep(min(POLL_SECONDS, remaining))


async def _run_cancel(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    from app.services.command_jobs import cancel_job

    assert settings is not None
    return 0, _operation_ids(await cancel_job(settings, args.operation_id)), None
