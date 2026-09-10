import argparse
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cli import operation


def parse(*argv):
    parser = argparse.ArgumentParser()
    operation.register(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["operation", *argv])


@pytest.mark.parametrize(
    "argv",
    [
        ("show", "1", "--wait", "61"),
        ("show", "1", "--wait", "nan"),
        ("show", "0"),
        ("logs", "1", "--offset", "-1"),
        ("logs", "1", "--wait-seconds", "-1"),
        ("list", "--limit", "101"),
    ],
)
def test_rejects_invalid_bounds(argv):
    with pytest.raises(SystemExit):
        parse(*argv)


def test_wait_default_is_bounded():
    assert parse("show", "1", "--wait").wait == 30
    assert parse("show", "1").wait == 0


@pytest.mark.asyncio
async def test_show_wait_observes_completion(monkeypatch):
    load = AsyncMock(side_effect=[{"status": "running"}, {"status": "success"}])
    monkeypatch.setattr(operation, "_load_operation", load)
    monkeypatch.setattr(operation.asyncio, "sleep", AsyncMock())
    code, payload, error = await operation._run_show(
        parse("show", "12", "--wait"), SimpleNamespace()
    )
    assert (code, error) == (0, None)
    assert payload == {"status": "success", "wait_timed_out": False}
    assert load.await_count == 2


@pytest.mark.asyncio
async def test_wait_expiry_preserves_running_job(monkeypatch):
    load = AsyncMock(return_value={"status": "running", "id": 12})
    monkeypatch.setattr(operation, "_load_operation", load)
    ticks = iter([0, 31])
    monkeypatch.setattr(
        operation, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    code, payload, error = await operation._run_show(
        parse("show", "12", "--wait"), SimpleNamespace()
    )
    assert (code, error) == (0, None)
    assert payload == {"status": "running", "id": 12, "wait_timed_out": True}
    assert load.await_count == 1


@pytest.mark.asyncio
async def test_logs_follow_resumes_byte_cursor(monkeypatch):
    logs = AsyncMock(
        side_effect=[
            {"data": "hi", "next_offset": 5, "eof": False},
            {"data": " there", "next_offset": 11, "eof": True},
        ]
    )
    monkeypatch.setitem(
        sys.modules, "app.services.command_jobs", SimpleNamespace(job_logs=logs)
    )
    settings = SimpleNamespace()
    code, payload, error = await operation._run_logs(
        parse("logs", "12", "--offset", "3", "--follow"), settings
    )
    assert (code, error) == (0, None)
    assert payload == {"data": "hi there", "offset": 3, "next_offset": 11, "eof": True}
    assert logs.await_args_list[1].kwargs == {"stream": "stdout", "offset": 5}


@pytest.mark.asyncio
async def test_logs_follow_stops_at_wait_boundary(monkeypatch):
    logs = AsyncMock(return_value={"data": "", "next_offset": 0, "eof": False})
    monkeypatch.setitem(
        sys.modules, "app.services.command_jobs", SimpleNamespace(job_logs=logs)
    )
    code, payload, error = await operation._run_logs(
        parse("logs", "12", "--follow", "--wait-seconds", "0"), SimpleNamespace()
    )
    assert (code, error) == (0, None)
    assert payload["eof"] is False
    assert logs.await_count == 1


@pytest.mark.asyncio
async def test_cancel_keeps_unconfirmed_status(monkeypatch):
    cancel = AsyncMock(
        return_value={"id": 12, "status": "running", "cancel_requested": True}
    )
    monkeypatch.setitem(
        sys.modules, "app.services.command_jobs", SimpleNamespace(cancel_job=cancel)
    )
    _, payload, _ = await operation._run_cancel(
        parse("cancel", "12"), SimpleNamespace()
    )
    assert payload["status"] == "running"
    assert payload["cancel_requested"] is True


@pytest.mark.asyncio
async def test_list_refreshes_command_evidence_without_guest_probes(monkeypatch):
    from unittest.mock import Mock
    from app import database
    from app.routes import status
    from app.services import command_jobs

    records = [
        SimpleNamespace(id=1, kind="output_exec", status="running"),
        SimpleNamespace(id=2, kind="create_backend", status="success"),
    ]
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: records)
    )
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(database, "SessionLocal", Mock(return_value=context))
    monkeypatch.setattr(
        status, "operation_payload", lambda row, settings: vars(row).copy()
    )
    refresh = AsyncMock(
        return_value={"operation_id": 1, "kind": "output_exec", "status": "success"}
    )
    monkeypatch.setattr(command_jobs, "get_job", refresh)
    settings = SimpleNamespace()
    _, payload, _ = await operation._run_list(parse("list"), settings)
    assert payload["operations"] == [
        {"id": 1, "operation_id": 1, "kind": "output_exec", "status": "success"},
        {"id": 2, "operation_id": 2, "kind": "create_backend", "status": "success"},
    ]
    refresh.assert_awaited_once_with(settings, 1, observe=False)


@pytest.mark.asyncio
async def test_show_command_has_consistent_identifiers(monkeypatch):
    from unittest.mock import Mock
    from app import database
    from app.services import command_jobs

    session = AsyncMock()
    session.get.return_value = SimpleNamespace(kind="output_exec")
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(database, "SessionLocal", Mock(return_value=context))
    monkeypatch.setattr(
        command_jobs,
        "get_job",
        AsyncMock(return_value={"operation_id": 7, "status": "success"}),
    )
    assert await operation._load_operation(SimpleNamespace(), 7) == {
        "id": 7,
        "operation_id": 7,
        "status": "success",
    }
