from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ApplyRun, Backend, Input, Operation, UpdateRun
from app.services.debug_bundle import (
    build_error_debug_bundle,
    build_support_debug_bundle,
    normalize_error_inst,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_build_error_debug_bundle_uses_standard_fields_and_snippets(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "\n".join(
            [
                "distant unrelated line",
                "filler 1",
                "filler 2",
                "filler 3",
                "filler 4",
                "filler 5",
                "filler 6",
                "filler 7",
                "before context",
                "restore failed | error_code: CNC-05002, "
                "error_name: RESTORE_TAR_SYMLINK_ESCAPES, "
                "error_inst: 7K2Q9M4D, request_id: req_X7kqpQ, token: secret",
                "after context",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "app.events.jsonl").write_text(
        json.dumps(
            {
                "event": "restore.failed",
                "error_inst": "7K2Q9M4D",
                "request_id": "req_X7kqpQ",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        log_path=log_path,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        details = {
            "error_code": "CNC-05002",
            "error_name": "RESTORE_TAR_SYMLINK_ESCAPES",
            "error_inst": "7K2Q9M4D",
            "request_id": "req_X7kqpQ",
            "flash_error": "Restore failed while validating backup archive.",
            "csrf_token": "do-not-ship",
        }
        operation = Operation(
            kind="restore_backend",
            status="failed",
            phase="restore_validate",
            backend_id=backend.id,
            error="Restore failed - Error CNC-05002-7K2Q9M4D",
            details_json=json.dumps(details),
        )
        session.add(operation)
        await session.flush()
        session.add(
            ApplyRun(
                operation_id=operation.id,
                status="error",
                message="Apply failed - Error CNC-05002-7K2Q9M4D",
                details_json=json.dumps(details),
            )
        )
        await session.commit()

        bundle = await build_error_debug_bundle(
            session,
            settings,
            error_inst="7K2Q9M4D",
        )

    assert bundle.filename == "cnc-debug-7K2Q9M4D.zip"
    with ZipFile(BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert "report.txt" in names
        assert "report.json" in names
        assert "logs/app.log" in names
        report = json.loads(archive.read("report.json"))
        assert report["error_code"] == "CNC-05002"
        assert report["error_name"] == "RESTORE_TAR_SYMLINK_ESCAPES"
        assert report["error_inst"] == "7K2Q9M4D"
        assert report["request_id"] == "req_X7kqpQ"
        assert report["location"] == "restore_backend/restore_validate"
        assert report["output"] == "web (#1)"
        report_text = archive.read("report.txt").decode("utf-8")
        assert "error_code: CNC-05002" in report_text
        assert "error_name: RESTORE_TAR_SYMLINK_ESCAPES" in report_text
        app_log = archive.read("logs/app.log").decode("utf-8")
        assert "restore failed" in app_log
        assert "distant unrelated line" not in app_log
        assert "token: [redacted]" in app_log
        operations = json.loads(archive.read("state/operations.json"))
        assert operations[0]["details"]["csrf_token"] == "[redacted]"


def test_normalize_error_inst_rejects_non_crockford_values() -> None:
    with pytest.raises(ValueError, match="error_inst"):
        normalize_error_inst("OOOOOOOO")


@pytest.mark.asyncio
async def test_build_support_debug_bundle_includes_redacted_state_and_log_tail(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "\n".join(
            [
                "older line",
                "recent support line token=secret",
                "latest support line access_key=abc123",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "app.events.jsonl").write_text(
        json.dumps({"event": "ui.failed", "password": "hidden"}) + "\n",
        encoding="utf-8",
    )
    maker = await _make_session(tmp_path / "support.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'support.db'}",
        log_path=log_path,
        pushover_app_token="do-not-ship",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            volumes_json=json.dumps([{"path": "/data", "access_key": "secret"}]),
            enabled=True,
            shield_access_code="do-not-ship",
        )
        input_row = Input(
            kind="domain",
            hostname="web.example.test",
            shield_access_code="do-not-ship",
            enabled=True,
        )
        operation = Operation(
            kind="create_backend",
            status="failed",
            phase="apply",
            backend_id=1,
            error="Create failed - Error CNC-02099-7K2Q9M4D",
            details_json=json.dumps(
                {
                    "error_code": "CNC-02099",
                    "error_inst": "7K2Q9M4D",
                    "csrf_token": "do-not-ship",
                }
            ),
        )
        update_run = UpdateRun(
            status="failed",
            message="Update failed",
            details_json=json.dumps({"token": "do-not-ship"}),
        )
        session.add_all([backend, input_row, operation, update_run])
        await session.commit()

        bundle = await build_support_debug_bundle(session, settings)

    assert bundle.filename.startswith("cnc-support-")
    assert bundle.filename.endswith(".zip")
    with ZipFile(BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert "report.json" in names
        assert "state/backends.json" in names
        assert "state/inputs.json" in names
        assert "state/operations.json" in names
        assert "state/update-runs.json" in names
        assert "logs/app.log" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["artifact"] == "cnc-support-bundle"
        report = json.loads(archive.read("report.json"))
        assert report["counts"]["backends"] == 1
        assert report["counts"]["recent_failed_operations"] == 1
        operations = json.loads(archive.read("state/operations.json"))
        assert operations[0]["details"]["csrf_token"] == "[redacted]"
        backends = json.loads(archive.read("state/backends.json"))
        assert backends[0]["volumes"][0]["access_key"] == "[redacted]"
        update_runs = json.loads(archive.read("state/update-runs.json"))
        assert update_runs[0]["details"]["token"] == "[redacted]"
        app_log = archive.read("logs/app.log").decode("utf-8")
        assert "token=[redacted]" in app_log
        assert "access_key=[redacted]" in app_log
        event_log = archive.read("logs/app.events.jsonl").decode("utf-8")
        assert '"password": "[redacted]"' in event_log
