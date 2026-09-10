import asyncio
import gzip
import json
import logging
import multiprocessing
import sys
import threading
import time

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import database
from app import logger as logs


@pytest.fixture(autouse=True)
def close_pipeline():
    yield
    logs._shutdown_logging()


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_real_migration_preserves_request_and_exception_sinks(
    monkeypatch, tmp_path
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(database, "engine", engine)
    logs.configure_logging(str(tmp_path / "app.log"), console=False)
    logger = logs.get_logger("main")
    try:
        await database.init_db()
        logger.info(
            "http.request.completed", request_id="req_after_migration", status_code=200
        )
        try:
            raise RuntimeError("synthetic post-migration error")
        except RuntimeError:
            logger.exception("startup.probe.failed")
        assert await logs.flush_logging_pipeline_async()
        assert logs.logging_pipeline_status()["operational"]
        assert not logging.getLogger("main").disabled
        for filename in ("app.log", "app.events.jsonl"):
            text = (tmp_path / filename).read_text()
            assert "req_after_migration" in text
            assert "synthetic post-migration error" in text
            assert "startup.probe.failed" in text
    finally:
        await engine.dispose()


def test_snapshot_redacts_every_renderer_and_preserves_structure(tmp_path):
    listener = logs._RecordQueueListener([])
    handler = logs.FlushableQueueHandler(listener.queue, listener)
    nested = {"items": [{"value": "original", "api_key": "synthetic-key"}]}
    try:
        raise RuntimeError(
            'Authorization: Bearer synthetic-bearer password="synthetic password"'
        )
    except RuntimeError:
        record = logging.makeLogRecord(
            {
                "name": "test",
                "msg": "Authorization: Basic synthetic-basic",
                "levelno": logging.ERROR,
                "levelname": "ERROR",
                "exc_info": sys.exc_info(),
                "event_name": "output.deleted",
                "context": {
                    "name": "other-output",
                    "level": "fake",
                    "context_fields": {"name": "nested"},
                    "nested": nested,
                    "url": "https://user:synthetic-url@example.com/",
                    "request_id": "req_kept",
                    "count": 3,
                },
            }
        )
        handler.filter(record)
        prepared = handler.prepare(record)
    nested["items"][0]["value"] = "mutated"
    assert (
        prepared.exc_info is None
        and prepared.args is None
        and prepared.exc_text is None
    )
    payload = json.loads(logs.JSONLFormatter().format(prepared))
    assert payload["name"] == "output.deleted" and payload["level"] == "ERROR"
    assert payload["context_fields"]["name"] == "other-output"
    assert payload["context_fields"]["context_fields"] == {"name": "nested"}
    assert payload["nested"]["items"][0]["value"] == "original"
    assert payload["count"] == 3 and payload["request_id"] == "req_kept"
    block = logs.BlockBufferedHandler(
        tmp_path / "app.log", max_bytes=100000, backup_count=1, compress_rotated=False
    )
    try:
        texts = [
            logs.JSONLFormatter().format(prepared),
            logs.StreamRecordFormatter().format(prepared),
            block._render_single_event(prepared),
        ]
        for text in texts:
            for secret in (
                "synthetic-key",
                "synthetic-bearer",
                "synthetic password",
                "synthetic-basic",
                "synthetic-url",
            ):
                assert secret not in text
            assert "mutated" not in text
    finally:
        block.close()


def test_snapshot_bounds_cycles_large_collections_and_text():
    cyclic = {}
    cyclic["self"] = cyclic
    value = logs._json_safe_context(
        {"cycle": cyclic, "many": list(range(10000)), "text": "x" * 1000000}
    )
    assert len(json.dumps(value)) < 40000
    assert "truncated" in json.dumps(value)


def _concurrent_writer(path, writer, barrier, compressed):
    cls = (
        logs.GZipRotatingFileHandler if compressed else logs.PrivateRotatingFileHandler
    )
    handler = cls(path, maxBytes=1024, backupCount=80, encoding="utf-8")
    try:
        for sequence in range(40):
            handler.emit(
                logging.makeLogRecord(
                    {
                        "msg": json.dumps(
                            {
                                "writer": writer,
                                "sequence": sequence,
                                "padding": "x" * 160,
                            }
                        ),
                        "args": (),
                    }
                )
            )
            barrier.wait(timeout=10)
            if sequence == 0 and writer == 0:
                handler.doRollover()
            barrier.wait(timeout=10)
        assert handler.write_failures == 0
    finally:
        handler.close()


@pytest.mark.parametrize("compressed", [False, True])
def test_independent_processes_rotate_without_lost_or_malformed_records(
    tmp_path, compressed
):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    path = tmp_path / "events.jsonl"
    processes = [
        context.Process(
            target=_concurrent_writer, args=(str(path), writer, barrier, compressed)
        )
        for writer in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        records = []
        for file in tmp_path.glob("events.jsonl*"):
            text = (
                gzip.decompress(file.read_bytes()).decode()
                if file.suffix == ".gz"
                else file.read_text()
            )
            records.extend(json.loads(line) for line in text.splitlines())
        assert sorted((r["writer"], r["sequence"]) for r in records) == [
            (writer, sequence) for writer in range(2) for sequence in range(40)
        ]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


@pytest.mark.asyncio
async def test_cancellation_has_one_terminal_record_and_releases_blocks(tmp_path):
    logs.configure_logging(str(tmp_path / "app.log"), max_bytes=1000000, console=False)
    logger = logs.get_logger("operations")
    for _ in range(25):
        with pytest.raises(asyncio.CancelledError):
            async with logger.operation("work.run"):
                raise asyncio.CancelledError()
    assert await logs.flush_logging_pipeline_async()
    events = read_events(tmp_path / "app.events.jsonl")
    assert (
        len(
            [
                r
                for r in events
                if r.get("log_kind") == "result" and r["status"] == "cancelled"
            ]
        )
        == 25
    )
    assert len([r for r in events if r.get("log_kind") == "timing"]) == 25
    assert all(
        h.get("buffered_blocks", 0) == 0
        for h in logs.logging_pipeline_status()["managed_handlers"]
    )
    assert "Completed" not in (tmp_path / "app.log").read_text()


@pytest.mark.asyncio
async def test_blocked_sink_bounds_queue_without_stalling_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Blocker(logging.Handler):
        def emit(self, record):
            entered.set()
            release.wait(timeout=5)

    blocker = Blocker()
    listener = logs._RecordQueueListener([blocker], capacity=8)
    handler = logs.FlushableQueueHandler(listener.queue, listener)
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    monkeypatch.setattr(
        logs, "_logging_pipeline", logs._LoggingPipeline(handler, listener, [blocker])
    )
    listener.start()
    try:
        logger = logs.get_logger("blocked")
        logger.info("work.started")
        assert await asyncio.to_thread(entered.wait, 1)
        start = time.monotonic()
        for _ in range(100):
            logger.error("work.failed")
        await asyncio.sleep(0.01)
        assert time.monotonic() - start < 0.25
        status = listener.failure_status()
        assert status["queue_depth"] == 8 and status["dropped_records"] == 92
        assert not await asyncio.to_thread(listener.flush, timeout=0.02)
        assert listener.failure_status()["flush_timeouts"] == 1
    finally:
        release.set()
        await asyncio.to_thread(listener.stop, flush_incomplete=True)


def test_sink_write_errors_and_detachment_are_reported(monkeypatch, tmp_path):
    logs.configure_logging(str(tmp_path / "app.log"), console=False)
    pipeline = logs._logging_pipeline

    def disk_full():
        raise OSError("synthetic disk full")

    for handler in pipeline.managed_handlers:
        monkeypatch.setattr(getattr(handler, "_sink", handler), "_open", disk_full)
    logs.get_logger("sink").info("disk.failed")
    logs.flush_logging_pipeline()
    status = logs.logging_pipeline_status()
    assert status["sink_write_failures"] == 2
    assert status["status"] == "degraded"
    assert all(h["last_write_error"] == "OSError" for h in status["managed_handlers"])
    logging.getLogger().removeHandler(pipeline.queue_handler)
    assert not logs.logging_pipeline_status()["operational"]
    assert not logs.logging_pipeline_status()["queue_handler_attached"]


def test_incomplete_block_retention_has_count_and_record_bounds(tmp_path):
    handler = logs.BlockBufferedHandler(
        tmp_path / "app.log", max_bytes=10000000, backup_count=1, compress_rotated=False
    )
    try:
        for index in range(2000):
            handler.emit(
                logging.makeLogRecord(
                    {
                        "name": "work",
                        "msg": "Started",
                        "message_text": "Started",
                        "block_id": f"b{index % 150}",
                        "seq": index,
                        "log_kind": "root",
                        "depth": 0,
                        "levelno": logging.INFO,
                        "levelname": "INFO",
                    }
                )
            )
            assert len(handler._blocks) <= 128
            assert 0 <= handler._buffered_records <= 512
        assert handler.incomplete_blocks > 0
        handler.flush_incomplete()
        assert handler._buffered_records == 0
        assert "Incomplete" in (tmp_path / "app.log").read_text()
    finally:
        handler.close()


def test_prepared_record_does_not_retain_unbounded_foreign_extras():
    listener = logs._RecordQueueListener([])
    handler = logs.FlushableQueueHandler(listener.queue, listener)
    record = logging.makeLogRecord(
        {"name": "foreign", "msg": "safe", "extra_blob": bytearray(1000000)}
    )
    handler.filter(record)
    prepared = handler.prepare(record)
    assert not hasattr(prepared, "extra_blob")
    assert prepared.getMessage() == "Safe"


def test_secret_redaction_preserves_traceback_source_punctuation():
    source = 'raise RuntimeError("password=synthetic-secret")'
    assert (
        logs.redact_sensitive_text(source)
        == 'raise RuntimeError("password=[redacted]")'
    )


def test_interrupted_compression_retains_and_recovers_plain_segment(
    monkeypatch, tmp_path
):
    path = tmp_path / "app.log"
    handler = logs.GZipRotatingFileHandler(
        str(path), maxBytes=10000, backupCount=3, encoding="utf-8"
    )
    handler.emit(logging.makeLogRecord({"msg": "before-interruption", "args": ()}))
    original_copy = logs.shutil.copyfileobj

    def partial_copy(source, destination):
        destination.write(source.read(4))
        raise OSError("synthetic compression interruption")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(logs.shutil, "copyfileobj", partial_copy)
            with pytest.raises(OSError):
                handler.doRollover()
        assert (tmp_path / "app.log.1").read_text() == "before-interruption\n"
        assert logs.shutil.copyfileobj is original_copy
        handler.emit(logging.makeLogRecord({"msg": "after-recovery", "args": ()}))
        assert (
            gzip.decompress((tmp_path / "app.log.1.gz").read_bytes()).decode()
            == "before-interruption\n"
        )
        assert path.read_text() == "after-recovery\n"
        assert not (tmp_path / "app.log.1").exists()
    finally:
        handler.close()


def test_shutdown_drains_inflight_acceptance_before_exiting(monkeypatch):
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    listener = logs._RecordQueueListener([Capture()])
    entered, release = threading.Event(), threading.Event()
    original_put = listener.queue.put_nowait

    def paused_put(record):
        entered.set()
        assert release.wait(timeout=3)
        original_put(record)

    monkeypatch.setattr(listener.queue, "put_nowait", paused_put)
    listener.start()
    producer = threading.Thread(
        target=listener.enqueue,
        args=(logging.makeLogRecord({"msg": "accepted", "args": ()}),),
    )
    stopper = threading.Thread(target=listener.stop, kwargs={"flush_incomplete": True})
    try:
        producer.start()
        assert entered.wait(timeout=1)
        stopper.start()
        time.sleep(0.15)
        assert stopper.is_alive()
        release.set()
        producer.join(timeout=2)
        stopper.join(timeout=2)
        assert captured == ["accepted"]
        assert listener.queue.empty()
        assert not listener.is_alive()
        listener.enqueue(logging.makeLogRecord({"msg": "too-late"}))
        assert listener.failure_status()["dropped_records"] == 1
    finally:
        release.set()
        listener.stop(flush_incomplete=True)


@pytest.mark.parametrize(
    "source",
    [
        '{"authorization": "Bearer synthetic-json-secret"}',
        '{"authorization": "Basic synthetic-json-secret"}',
        '{"password": "synthetic-json-secret"}',
        "{'password': 'synthetic-json-secret'}",
        "cookie=synthetic-json-secret",
        "csrf=synthetic-json-secret",
        "access_code=synthetic-json-secret",
        "api_token=synthetic-json-secret",
        'password="synthetic-json-secret with spaces"',
        'password="synthetic-json-secret with \\" escaped quote"',
    ],
)
def test_free_text_secret_formats_are_redacted_idempotently(source):
    redacted = logs.redact_sensitive_text(source)
    assert "synthetic-json-secret" not in redacted
    assert logs.redact_sensitive_text(redacted) == redacted
    if source.startswith('{"'):
        assert json.loads(redacted)


@pytest.mark.asyncio
async def test_readiness_logging_is_live_without_changing_db_readiness(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from app.routes import status

    logs.configure_logging(str(tmp_path / "app.log"), console=False)
    startup = {
        "config": {"status": "ok", "logging": logs.logging_pipeline_status()},
        "db": {"status": "ok"},
    }

    async def database_ok():
        return None

    monkeypatch.setattr(status, "verify_db_connection", database_ok)
    monkeypatch.setattr(status, "snapshot_startup_state", lambda app: startup)
    request = SimpleNamespace(app=object())
    before = await status._readiness_response(request)
    assert json.loads(before.body)["logging"]["status"] == "ok"
    logging.getLogger().removeHandler(logs._logging_pipeline.queue_handler)
    after = await status._readiness_response(request)
    payload = json.loads(after.body)
    assert after.status_code == 200
    assert payload["startup"]["config"]["logging"]["status"] == "ok"
    assert payload["logging"]["status"] == "degraded"
    assert payload["logging"]["queue_handler_attached"] is False
