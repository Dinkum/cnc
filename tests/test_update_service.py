import base64
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from urllib import error as urllib_error
import app.services.update_service as update_service

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import UpdateCheck, UpdateRun
from app.services.commands import CommandError, CommandResult
from app.services.status_service import collect_status, invalidate_status_cache
from app.services.update_service import (
    UPDATE_LAUNCH_TIMEOUT_SEC,
    UpdateRejectedError,
    run_update,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_read_update_log_excerpt_prefers_latest_output_when_truncated(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "update.log"
    log_path.write_text("line-1\nline-2\nline-3\nline-4\nline-5\n", encoding="utf-8")

    excerpt, truncated = update_service._read_update_log_excerpt(log_path, 18)

    assert truncated is True
    assert excerpt.startswith("...[truncated]\n")
    assert "line-1" not in excerpt
    assert "line-4" in excerpt
    assert "line-5" in excerpt


def test_fetch_remote_version_retries_transient_http_error(monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(request, timeout: int):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib_error.URLError("temporary network failure")
        payload = {
            "encoding": "base64",
            "content": base64.b64encode(
                json.dumps({"version": "0.1.27"}).encode("utf-8")
            ).decode("ascii"),
        }
        return _FakeResponse(json.dumps(payload))

    monkeypatch.setattr(update_service.urllib_request, "urlopen", fake_urlopen)

    version = update_service._fetch_remote_version(
        "Dinkum/cnc",
        "main",
        "",
        5,
        retries=1,
        backoff_sec=0.0,
    )

    assert version == "0.1.27"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_run_update_queues_transient_systemd_unit(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    script_path = tmp_path / "update.sh"
    script_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(script_path, 0o755)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        updater_script_path=script_path,
        update_log_dir=tmp_path / "update-logs",
    )
    calls: list[tuple[list[str], int]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        calls.append((command, timeout_sec))
        return CommandResult(command=command, returncode=0, stdout="queued", stderr="")

    async def fake_update_version_info(*_args, **_kwargs):
        return {
            "current_version": "0.1.19",
            "available_version": "99.0.0",
            "has_update": True,
            "repo": "Dinkum/cnc",
            "ref": "main",
            "error": None,
        }

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.update_service.get_update_version_info", fake_update_version_info
    )

    async with maker() as session:
        response = await run_update(session, settings)
        stored = (
            await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()))
        ).scalar_one()

    assert response.status == "queued"
    assert response.message == "update queued"
    assert response.details["unit"] == "cnc-update-run-1.service"
    assert stored.status == "queued"
    assert calls == [(response.details["launch_command"], UPDATE_LAUNCH_TIMEOUT_SEC)]
    assert "--no-block" in response.details["launch_command"]
    assert "RuntimeMaxSec=600s" in response.details["launch_command"]
    assert "TimeoutStartSec=600s" in response.details["launch_command"]


@pytest.mark.asyncio
async def test_run_update_rejects_when_no_newer_version_is_available(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(update_service, "current_app_version", lambda: "0.1.28")
    maker = await _make_session(tmp_path / "app.db")
    script_path = tmp_path / "update.sh"
    script_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(script_path, 0o755)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        updater_script_path=script_path,
        update_log_dir=tmp_path / "update-logs",
        github_repo="owner/repo",
    )

    async def fake_update_version_info(*_args, **_kwargs):
        return {
            "current_version": "0.1.28",
            "available_version": "0.1.27",
            "has_update": False,
            "repo": "owner/repo",
            "ref": "main",
            "error": None,
        }

    monkeypatch.setattr(
        "app.services.update_service.get_update_version_info", fake_update_version_info
    )

    async with maker() as session:
        with pytest.raises(UpdateRejectedError, match="not newer than current version"):
            await run_update(session, settings)
        stored = (
            (await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc())))
            .scalars()
            .all()
        )

    assert stored == []


@pytest.mark.asyncio
async def test_run_update_missing_script_notification_includes_run_id(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        updater_script_path=tmp_path / "missing-update.sh",
        update_log_dir=tmp_path / "update-logs",
    )
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        await run_update(session, settings)
        stored = (
            await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()))
        ).scalar_one()

    assert sent
    assert sent[0]["run_id"] == stored.id
    assert f"run id: {stored.id}" in str(sent[0]["message"])


@pytest.mark.asyncio
async def test_run_update_launch_failure_notification_includes_run_id(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    script_path = tmp_path / "update.sh"
    script_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(script_path, 0o755)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        updater_script_path=script_path,
        update_log_dir=tmp_path / "update-logs",
    )
    sent: list[dict[str, object]] = []

    async def fake_update_version_info(*_args, **_kwargs):
        return {
            "current_version": "0.1.19",
            "available_version": "99.0.0",
            "has_update": True,
            "repo": "Dinkum/cnc",
            "ref": "main",
            "error": None,
        }

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        _ = timeout_sec
        raise CommandError(
            CommandResult(
                command=command, returncode=1, stdout="", stderr="unit denied"
            )
        )

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.update_service.get_update_version_info", fake_update_version_info
    )
    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        await run_update(session, settings)
        stored = (
            await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()))
        ).scalar_one()

    assert sent
    assert sent[0]["run_id"] == stored.id
    assert f"run id: {stored.id}" in str(sent[0]["message"])


@pytest.mark.asyncio
async def test_reconcile_update_run_marks_pending_run_without_unit_failed(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async with maker() as session:
        run = UpdateRun(
            status="queued",
            message="update queued",
            details_json=json.dumps({"script": "update.sh"}),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "error"
    assert reconciled.message == "update failed"
    assert (
        json.loads(reconciled.details_json)["unit_lookup_error"]
        == "pending update run has no systemd unit"
    )


@pytest.mark.asyncio
async def test_reconcile_update_run_marks_missing_transient_unit_failed(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=\nExecMainStatus=\n",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )

    async with maker() as session:
        run = UpdateRun(
            status="queued",
            message="update queued",
            details_json=json.dumps({"unit": "cnc-update-run-7.service"}),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "error"
    assert reconciled.message == "update failed"
    assert json.loads(reconciled.details_json)["unit_lookup_error"] == (
        "systemd unit cnc-update-run-7.service is not-found"
    )


@pytest.mark.asyncio
async def test_reconcile_update_run_marks_gc_transient_unit_success_from_log(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "Using cached uvicorn-0.41.0-py3-none-any.whl\n"
        "[updater] syncing managed CLI wrappers\n"
        "[updater] update complete\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout=(
                "Result=success\n"
                "ExecMainCode=0\n"
                "ExecMainStatus=0\n"
                "LoadState=not-found\n"
                "ActiveState=inactive\n"
                "SubState=dead\n"
            ),
            stderr="",
        )

    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        run = UpdateRun(
            status="running",
            message="update running",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-11.service",
                    "log_path": str(log_path),
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "success"
    assert reconciled.message == "update completed"
    details = json.loads(reconciled.details_json)
    assert details["log_terminal_status"] == "success"
    assert "unit_lookup_error" not in details
    assert details["unit_gc_note"] == (
        "systemd transient unit was removed after the updater log completed"
    )
    assert sent
    assert sent[0]["title"] == "CNC update completed"


@pytest.mark.asyncio
async def test_reconcile_update_run_records_migration_boundary_marker(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "[updater] switched /var/lib/cnc/current -> /var/lib/cnc/releases/new\n"
        "[updater] migration boundary crossed; old-release rollback is disabled after service restart begins\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout=(
                "Result=\n"
                "ExecMainCode=0\n"
                "ExecMainStatus=0\n"
                "LoadState=loaded\n"
                "ActiveState=activating\n"
                "SubState=running\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )

    async with maker() as session:
        run = UpdateRun(
            status="running",
            message="update running",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-12.service",
                    "log_path": str(log_path),
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    details = json.loads(reconciled.details_json)
    assert details["migration_boundary_crossed"] is True
    assert details["rollback_after_migration"] == "disabled"


@pytest.mark.asyncio
async def test_reconcile_update_run_failure_notification_uses_tail_and_real_reason(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(f"Using cached package-{index}.whl" for index in range(80))
        + "\nfinal traceback line explains the actual failure\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=failed\n"
                "SubState=failed\n"
                "Result=success\n"
                "ExecMainStatus=0\n"
            ),
            stderr="",
        )

    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        run = UpdateRun(
            status="running",
            message="update running",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-12.service",
                    "log_path": str(log_path),
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "error"
    assert sent
    message = str(sent[0]["message"])
    assert "error: systemd state: failed/failed" in message
    assert "error: success" not in message
    assert "log tail:" in message
    assert "final traceback line explains the actual failure" in message


@pytest.mark.asyncio
async def test_reconcile_update_run_retries_transient_unit_lookup_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise CommandError(
            CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr="Failed to connect to bus: Connection refused",
            )
        )

    async def fail_notify(*_args, **_kwargs):
        raise AssertionError(
            "transient lookup failures should not notify before retry exhaustion"
        )

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fail_notify
    )

    async with maker() as session:
        run = UpdateRun(
            status="queued",
            message="update queued",
            details_json=json.dumps({"unit": "cnc-update-run-8.service"}),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "running"
    assert reconciled.message == "update status lookup failed; retrying"
    details = json.loads(reconciled.details_json)
    assert details["unit_lookup_retrying"] is True
    assert details["unit_lookup_failure_count"] == 1
    assert (
        details["unit_lookup_error"] == "Failed to connect to bus: Connection refused"
    )


@pytest.mark.asyncio
async def test_reconcile_update_run_marks_repeated_unit_lookup_failure_failed(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        transient_command_retry_attempts=1,
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise CommandError(
            CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr="Failed to connect to bus: Connection refused",
            )
        )

    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        run = UpdateRun(
            status="running",
            message="update status lookup failed; retrying",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-8.service",
                    "unit_lookup_failure_count": 1,
                    "unit_lookup_retrying": True,
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "error"
    assert reconciled.message == "update failed"
    details = json.loads(reconciled.details_json)
    assert details["unit_lookup_retrying"] is False
    assert details["unit_lookup_failure_count"] == 2
    assert sent
    assert sent[0]["title"] == "CNC update failed"


@pytest.mark.asyncio
async def test_reconcile_update_run_does_not_notify_before_terminal_commit(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        transient_command_retry_attempts=1,
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise CommandError(
            CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr="Failed to connect to bus: Connection refused",
            )
        )

    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    async def fail_update_record(*_args, **_kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(update_service, "_update_run_record", fail_update_record)

    async with maker() as session:
        run = UpdateRun(
            status="running",
            message="update status lookup failed; retrying",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-8.service",
                    "unit_lookup_failure_count": 1,
                    "unit_lookup_retrying": True,
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        with pytest.raises(RuntimeError, match="commit failed"):
            await update_service.reconcile_update_run(session, settings, run)

    assert sent == []


@pytest.mark.asyncio
async def test_reconcile_update_run_does_not_exhaust_lookup_retries_inside_backoff(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=60.0,
    )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise CommandError(
            CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr="Failed to connect to bus: Connection refused",
            )
        )

    async def fail_notify(*_args, **_kwargs):
        raise AssertionError(
            "retry backoff should avoid premature failure notification"
        )

    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fail_notify
    )

    async with maker() as session:
        first_failed_at = datetime.now(UTC).isoformat()
        run = UpdateRun(
            status="running",
            message="update status lookup failed; retrying",
            details_json=json.dumps(
                {
                    "unit": "cnc-update-run-8.service",
                    "unit_lookup_failure_count": 1,
                    "unit_lookup_last_failed_at": first_failed_at,
                    "unit_lookup_retrying": True,
                }
            ),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        reconciled = await update_service.reconcile_update_run(session, settings, run)

    assert reconciled is not None
    assert reconciled.status == "running"
    details = json.loads(reconciled.details_json)
    assert details["unit_lookup_retrying"] is True
    assert details["unit_lookup_backoff_active"] is True
    assert details["unit_lookup_failure_count"] == 1
    assert details["unit_lookup_last_failed_at"] == first_failed_at
    assert details["unit_lookup_last_observed_at"] >= first_failed_at


@pytest.mark.asyncio
async def test_collect_status_reconciles_queued_update_run_to_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invalidate_status_cache()
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[updater] update complete\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
        status_cache_ttl_sec=1,
        update_output_max_chars=256,
        github_repo="owner/repo",
    )

    async def fake_status_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n",
            stderr="",
        )

    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_status_run
    )
    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", fake_notify
    )

    async with maker() as session:
        session.add(
            UpdateCheck(
                status="success",
                current_version="0.1.19",
                available_version="99.0.0",
                has_update=True,
                repo=update_service._normalize_github_repo(settings.github_repo)
                or settings.github_repo,
                ref="main",
                details_json="{}",
                checked_at=datetime.now(UTC),
            )
        )
        session.add(
            UpdateRun(
                status="queued",
                message="update queued",
                details_json=json.dumps(
                    {
                        "script": str(tmp_path / "update.sh"),
                        "log_path": str(log_path),
                        "unit": "cnc-update-run-9.service",
                    }
                ),
            )
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)
        await asyncio.sleep(0)
        stored = (
            await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()))
        ).scalar_one()

    assert payload["last_update"]["status"] == "success"
    assert payload["last_update"]["message"] == "update completed"
    assert "log_excerpt" in payload["last_update"]["details"]
    assert payload["update_version"]["available_version"] == "99.0.0"
    assert payload["update_version"]["has_update"] is True
    assert stored.status == "success"
    assert sent
    assert sent[0]["title"] == "CNC update completed"


@pytest.mark.asyncio
async def test_collect_status_does_not_wait_for_update_notification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invalidate_status_cache()
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[updater] update complete\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
        status_cache_ttl_sec=1,
        update_output_max_chars=256,
    )

    async def fake_status_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n",
            stderr="",
        )

    sent: list[dict[str, object]] = []

    async def slow_notify(_settings, **kwargs):
        await asyncio.sleep(0.2)
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_status_run
    )
    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )
    monkeypatch.setattr(
        "app.services.update_service.send_pushover_notification_async", slow_notify
    )

    async with maker() as session:
        session.add(
            UpdateRun(
                status="queued",
                message="update queued",
                details_json=json.dumps(
                    {
                        "script": str(tmp_path / "update.sh"),
                        "log_path": str(log_path),
                        "unit": "cnc-update-run-9.service",
                    }
                ),
            )
        )
        await session.commit()

        started_at = time.monotonic()
        payload = await collect_status(session, settings, force_refresh=True)
        elapsed = time.monotonic() - started_at

    assert payload["last_update"]["status"] == "success"
    assert elapsed < 0.15
    assert sent == []
    await asyncio.sleep(0.25)
    assert sent
    assert sent[0]["title"] == "CNC update completed"


@pytest.mark.asyncio
async def test_collect_status_reconciles_running_update_run_with_active_exited_unit_to_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invalidate_status_cache()
    maker = await _make_session(tmp_path / "app.db")
    log_path = tmp_path / "update-logs" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[updater] update complete\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        update_log_dir=tmp_path / "update-logs",
        status_cache_ttl_sec=1,
        update_output_max_chars=256,
    )

    async def fake_status_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running\n",
            stderr="",
        )

    async def fake_update_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_status_run
    )
    monkeypatch.setattr(
        "app.services.update_service.run_command_checked_async", fake_update_run
    )

    async with maker() as session:
        session.add(
            UpdateRun(
                status="running",
                message="update running",
                details_json=json.dumps(
                    {
                        "script": str(tmp_path / "update.sh"),
                        "log_path": str(log_path),
                        "unit": "cnc-update-run-10.service",
                    }
                ),
            )
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)
        stored = (
            await session.execute(select(UpdateRun).order_by(UpdateRun.id.desc()))
        ).scalar_one()

    assert payload["last_update"]["status"] == "success"
    assert payload["last_update"]["message"] == "update completed"
    assert stored.status == "success"


@pytest.mark.asyncio
async def test_collect_status_reads_cached_update_info_without_remote_check(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        status_cache_ttl_sec=1,
        github_repo="owner/repo",
    )

    async def fake_status_run(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fail_remote_check(*_args, **_kwargs):
        raise AssertionError("status should not fetch remote update version")

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_status_run
    )
    monkeypatch.setattr(
        "app.services.update_service.get_update_version_info", fail_remote_check
    )

    async with maker() as session:
        payload = await collect_status(session, settings, force_refresh=True)

    assert payload["update_version"]["status"] == "unchecked"
    assert payload["update_version"]["available_version"] is None
