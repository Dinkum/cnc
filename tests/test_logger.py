import logging
import json
import stat
import sys

import pytest

from app.logger import (
    GZipRotatingFileHandler,
    _RecordQueueListener,
    bind_log_context,
    configure_logging,
    flush_logging_pipeline,
    get_logger,
    logging_pipeline_status,
    resolve_log_version,
)


def test_gzip_rotating_handler_creates_compressed_rollover(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    handler = GZipRotatingFileHandler(
        str(log_path),
        maxBytes=256,
        backupCount=3,
        encoding="utf-8",
    )
    logger = logging.getLogger("test.logger.rotation")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    for _ in range(40):
        logger.info("x" * 80)

    handler.close()
    logger.handlers.clear()

    assert log_path.exists()
    assert (tmp_path / "app.log.1.gz").exists()


def test_configure_logging_writes_canonical_rca_format(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    events_path = tmp_path / "app.events.jsonl"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    logger = get_logger("api.backends")
    logger.info(
        "backend.created",
        backend_id=12,
        request_id="req_123",
        trace_id="trace_456",
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    line = log_path.read_text(encoding="utf-8").strip()
    event_line = events_path.read_text(encoding="utf-8").strip()
    assert "| INFO" in line
    assert "| cnc.admin | api.backends -----" in line
    assert "name: backend.created" in line
    assert "component: api.backends" in line
    assert "env: test" in line
    assert "version: 9.9.9" in line
    assert "request_id: req_123" in line
    assert "trace_id: trace_456" in line
    assert "backend_id: 12" in line
    assert '"category":"api.backends"' in event_line
    assert '"app_id":"cnc.admin"' in event_line
    assert '"name":"backend.created"' in event_line


def test_configure_logging_reenables_logging_after_global_disable(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    logging.disable(logging.CRITICAL)

    try:
        configure_logging(
            str(log_path),
            max_bytes=4096,
            backup_count=1,
            compress_rotated=False,
            app_id="cnc.admin",
            env="test",
            version="9.9.9",
        )

        logger = get_logger("api.backends")
        logger.info("backend.created")
    finally:
        logging.disable(logging.NOTSET)

    for handler in logging.getLogger().handlers:
        handler.flush()

    line = log_path.read_text(encoding="utf-8").strip()
    assert "name: backend.created" in line


def test_configure_logging_keeps_noisy_dependency_loggers_at_warning(tmp_path) -> None:
    configure_logging(
        str(tmp_path / "app.log"),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("sqlalchemy.pool").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("sqlalchemy.orm").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("aiosqlite").getEffectiveLevel() == logging.WARNING


def test_configure_logging_can_disable_console_handler(tmp_path) -> None:
    configure_logging(
        str(tmp_path / "app.log"),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        console=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    status = logging_pipeline_status()
    handler_types = {handler["type"] for handler in status["managed_handlers"]}
    assert "SafeStreamHandler" not in handler_types


def test_configure_logging_failure_preserves_existing_pipeline(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    get_logger("api.backends").info("before.failure")
    flush_logging_pipeline()
    bad_parent = tmp_path / "not-a-directory"
    bad_parent.write_text("occupied", encoding="utf-8")

    with pytest.raises(OSError):
        configure_logging(
            str(bad_parent / "app.log"),
            max_bytes=4096,
            backup_count=1,
            compress_rotated=False,
            app_id="cnc.admin",
            env="test",
            version="9.9.9",
        )

    get_logger("api.backends").info("after.failure")
    flush_logging_pipeline()
    text = log_path.read_text(encoding="utf-8")
    assert "name: before.failure" in text
    assert "name: after.failure" in text


def test_logging_pipeline_status_reports_managed_handlers(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    status = logging_pipeline_status()

    assert status["configured"] is True
    assert status["listener_alive"] is True
    assert status["handler_failures"] == 0
    assert status["root_handler_count"] >= 1
    assert any(
        handler.get("path") == str(log_path) for handler in status["managed_handlers"]
    )


def test_configure_logging_uses_stable_stream_for_queue_listener(monkeypatch) -> None:
    class ClosedStream:
        closed = True

        def write(self, _text: str) -> None:
            raise ValueError("I/O operation on closed file")

        def flush(self) -> None:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", ClosedStream())
    configure_logging(
        None,
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    get_logger("api.apply").info("apply.test")
    flush_logging_pipeline()

    assert logging_pipeline_status()["handler_failures"] == 0


def test_record_queue_listener_continues_after_handler_failure() -> None:
    captured: list[str] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("handler failed")

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    listener = _RecordQueueListener([FailingHandler(), CaptureHandler()])
    listener.start()
    try:
        listener.queue.put(
            logging.makeLogRecord(
                {"name": "test", "levelno": logging.INFO, "msg": "kept"}
            )
        )
        listener.flush()
    finally:
        listener.stop(flush_incomplete=False)

    assert captured == ["kept"]
    assert listener.failure_status()["handler_failures"] == 1


def test_bind_log_context_propagates_request_fields(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    logger = get_logger("api.apply")
    with bind_log_context(request_id="req_bound", trace_id="trace_bound"):
        logger.info("apply.api.requested", run_id=7)

    for handler in logging.getLogger().handlers:
        handler.flush()

    line = log_path.read_text(encoding="utf-8").strip()
    assert "request_id: req_bound" in line
    assert "trace_id: trace_bound" in line
    assert "run_id: 7" in line


def test_flush_logging_pipeline_makes_queued_events_visible(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    logger = get_logger("api.apply")
    logger.warning("apply.failed", run_id=9, reason="self_audit")
    flush_logging_pipeline()

    line = log_path.read_text(encoding="utf-8")
    assert "name: apply.failed" in line
    assert "run_id: 9" in line
    assert "reason: self_audit" in line


def test_high_signal_events_are_mirrored_immediately(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    events_path = tmp_path / "app.events.jsonl"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    logger = get_logger("main")
    with bind_log_context(request_id="req_now"):
        logger.warning(
            "http.request.rejected",
            method="GET",
            path="/ui/backends/20/state",
            status=405,
            status_family="4xx",
            allow="POST",
        )
    get_logger("backend.ssh").info(
        "backend.ssh.handoff",
        backend="worker",
        mode="command",
        client_host="100.64.0.12",
    )

    text = log_path.read_text(encoding="utf-8")
    events = events_path.read_text(encoding="utf-8")
    assert "name: http.request.rejected" in text
    assert "request_id: req_now" in text
    assert "path: /ui/backends/20/state" in text
    assert '"name":"http.request.rejected"' in events
    assert events.count('"name":"http.request.rejected"') == 1
    assert '"env":"test"' in events
    assert '"version":"9.9.9"' in events
    assert '"request_id":"req_now"' in events
    assert '"allow":"POST"' in events
    assert "name: backend.ssh.handoff" in text
    assert '"name":"backend.ssh.handoff"' in events
    assert '"backend":"worker"' in events


def test_logger_canonicalizes_underscore_names_and_redacts_secrets(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    events_path = tmp_path / "app.events.jsonl"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        console=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    get_logger("backend_alerts").error(
        "backend_alerts.check_failed",
        api_token="super-secret-token",
        error="authorization=Bearer-abc",
    )
    flush_logging_pipeline()

    payload = json.loads(events_path.read_text(encoding="utf-8"))
    assert payload["category"] == "backend.alerts"
    assert payload["name"] == "backend.alerts.check.failed"
    assert payload["api_token"] == "[redacted]"
    assert payload["error"] == "authorization=[redacted]"
    assert "super-secret-token" not in log_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600


def test_resolve_log_version_reads_version_json(monkeypatch, tmp_path) -> None:
    version_path = tmp_path / "version.json"
    version_path.write_text('{"version":"1.2.3"}', encoding="utf-8")
    monkeypatch.setattr("app.logger.VERSION_PATH", version_path)

    assert resolve_log_version("") == "1.2.3"


@pytest.mark.asyncio
async def test_logger_operation_writes_grouped_block_and_events_jsonl(tmp_path) -> None:
    log_path = tmp_path / "app.log"
    events_path = tmp_path / "app.events.jsonl"
    configure_logging(
        str(log_path),
        max_bytes=4096,
        backup_count=1,
        compress_rotated=False,
        app_id="cnc.admin",
        env="test",
        version="9.9.9",
    )

    logger = get_logger("apply")
    async with logger.operation("apply.run", run_id="run_42") as op:
        op.step("check", "Resolved desired state", backend_count=2)

    for handler in logging.getLogger().handlers:
        handler.flush()

    rendered = log_path.read_text(encoding="utf-8")
    events = events_path.read_text(encoding="utf-8").splitlines()
    assert "| INFO" in rendered
    assert "| apply -----" in rendered
    assert "Started apply run | run_id: run_42" in rendered
    assert ">> check" in rendered
    assert "Resolved desired state" in rendered
    assert "backend_count: 2" in rendered
    assert ">> result" in rendered and "Completed | run_id: run_42" in rendered
    assert "|=" in rendered and "total" in rendered
    assert any(
        '"category":"apply"' in line and '"log_kind":"root"' in line for line in events
    )
    assert any('"name":"check"' in line and '"depth":1' in line for line in events)
    assert any(
        '"log_kind":"timing"' in line and '"latency_ms"' in line for line in events
    )
