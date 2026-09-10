import atexit
import asyncio
import fcntl
import math
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from logging.handlers import QueueHandler, RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from typing import Any


SAFE_CTX_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/@-]+$")
EVENT_NAME_RE = re.compile(r"^[a-z0-9.]+$")
EVENT_TOKEN_RE = re.compile(r"^[a-z0-9._]+$")
SENSITIVE_CONTEXT_KEY_RE = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|password|passwd|secret|token|access[_-](?:key|code)|api[_-]key|csrf|shield[_-]code|private[_-]key)(?:$|[_-])",
    re.IGNORECASE,
)
AUTHORIZATION_TEXT_RE = re.compile(
    r"(?i)(\bauthorization[\"']?\s*[:=]\s*)(?:Bearer|Basic)\s+[^\s,;\"'\)\]\}]+"
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9]+[_-])*(?:token|secret|password|passwd|authorization|cookie|csrf|api[_-]?key|access[_-]?(?:key|code)|shield[_-]?code|private[_-]?key)"
    r"[\"']?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\[redacted\]|[^,\s&\"'\)\]\};]+)"
)
URL_CREDENTIALS_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
ROOT_DIR = Path(__file__).resolve().parents[1]
VERSION_PATH = ROOT_DIR / "version.json"
PRIMARY_CONTEXT_KEYS = (
    "request_id",
    "user_id",
    "run_id",
    "trace_id",
    "span_id",
    "instance_id",
    "error_code",
    "error_name",
    "error_inst",
    "status_code",
    "latency_ms",
    "error_type",
    "component",
    "env",
    "version",
)
LEVEL_SYMBOLS = {
    "DEBUG": "(?)",
    "INFO": "(*)",
    "WARNING": "(!)",
    "ERROR": "(x)",
    "CRITICAL": "(X)",
}
NAME_COLUMN_WIDTH = 20
MAX_CONTEXT_NODES = 256
MAX_RECORD_TEXT = 8192
RESERVED_RECORD_FIELDS = frozenset(
    {
        "ts",
        "level",
        "app_id",
        "category",
        "message",
        "name",
        "block_id",
        "seq",
        "depth",
        "log_kind",
        "timing_only",
        "exception",
        "context_fields",
    }
)
NOISY_DEPENDENCY_LOGGERS = (
    "aiosqlite",
    "sqlalchemy.engine",
    "sqlalchemy.orm",
    "sqlalchemy.pool",
    "alembic",
)


@dataclass(frozen=True)
class LoggingRuntime:
    app_id: str
    env: str
    version: str


@dataclass(frozen=True)
class _FlushRequest:
    done: threading.Event


@dataclass
class _LoggingPipeline:
    queue_handler: "FlushableQueueHandler"
    listener: "_RecordQueueListener"
    managed_handlers: list[logging.Handler] = field(default_factory=list)

    def stop(self) -> None:
        self.listener.stop(flush_incomplete=True)
        self.queue_handler.close()


_logging_runtime = LoggingRuntime(app_id="cnc.admin", env="local", version="unknown")
_log_context: ContextVar[dict[str, Any]] = ContextVar("cnc_log_context", default={})
_logging_pipeline: _LoggingPipeline | None = None
_atexit_registered = False


def _is_pytest_capture_handler(handler: logging.Handler) -> bool:
    module_name = str(type(handler).__module__ or "")
    return module_name.startswith("_pytest.")


class UTCIsoFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StreamRecordFormatter(UTCIsoFormatter):
    def format(self, record: logging.LogRecord) -> str:
        category = _record_category(record)
        message = _ascii_text(str(getattr(record, "message_text", record.getMessage())))
        rendered = [
            self.formatTime(record),
            record.levelname,
            str(
                getattr(record, "app_id", _logging_runtime.app_id)
                or _logging_runtime.app_id
            ),
            category,
            message,
        ]
        suffix = _format_log_suffix(
            _record_context(record),
            event_name=_record_event_name(record),
            include_name=True,
        )
        if suffix:
            rendered.append(suffix)
        line = " ".join(part for part in rendered if part)
        if exception_text := _exception_text(record):
            line = f"{line}\n{exception_text}"
        return line


class JSONLFormatter(UTCIsoFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "app_id": str(
                getattr(record, "app_id", _logging_runtime.app_id)
                or _logging_runtime.app_id
            ),
            "category": _record_category(record),
            "message": _ascii_text(
                str(getattr(record, "message_text", record.getMessage()))
            ),
        }
        event_name = _record_event_name(record)
        if event_name:
            payload["name"] = event_name
        block_id = getattr(record, "block_id", None)
        if block_id:
            payload["block_id"] = block_id
            payload["seq"] = int(getattr(record, "seq", 0))
            payload["depth"] = int(getattr(record, "depth", 0))
        if getattr(record, "log_kind", None):
            payload["log_kind"] = str(getattr(record, "log_kind"))
        if getattr(record, "timing_only", False):
            payload["timing_only"] = True
        if exception_text := _exception_text(record):
            payload["exception"] = exception_text
        context = _json_safe_context(_record_context(record))
        collisions = {
            key: value
            for key, value in context.items()
            if key in RESERVED_RECORD_FIELDS and key != "context_fields"
        }
        payload.update(
            {
                key: value
                for key, value in context.items()
                if key not in RESERVED_RECORD_FIELDS or key == "context_fields"
            }
        )
        if collisions:
            payload["context_fields"] = collisions
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class PrivateRotatingFileHandler(RotatingFileHandler):
    """One serialized check/rotate/write transaction across local CNC processes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["delay"] = True
        self.write_failures = 0
        self.last_write_error = ""
        self.last_successful_write: float | None = None
        super().__init__(*args, **kwargs)
        path = Path(self.baseFilename)
        self.lock_path = path.with_name(f".{path.name}.lock")

    def _open(self):
        stream = super()._open()
        os.fchmod(stream.fileno(), 0o600)
        return stream

    @contextmanager
    def _writer_lock(self):
        descriptor = os.open(
            self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("log writer lock timed out") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    def _reopen_current_file(self) -> None:
        if self.stream is not None:
            try:
                active = os.stat(self.baseFilename)
                opened = os.fstat(self.stream.fileno())
                current = (active.st_dev, active.st_ino) == (
                    opened.st_dev,
                    opened.st_ino,
                )
            except FileNotFoundError:
                current = False
            if not current:
                self.stream.close()
                self.stream = None
        if self.stream is None:
            self.stream = self._open()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._writer_lock():
                self._reopen_current_file()
                if self.shouldRollover(record):
                    self._rollover_locked()
                previous_failures = self.write_failures
                logging.FileHandler.emit(self, record)
                if self.write_failures == previous_failures:
                    self.last_successful_write = time.time()
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # StreamHandler swallows I/O failures here; never echo raw records to stderr.
        self.write_failures += 1
        error_type = sys.exc_info()[0]
        self.last_write_error = error_type.__name__ if error_type else "WriteError"

    def doRollover(self) -> None:  # noqa: N802
        with self._writer_lock():
            self._reopen_current_file()
            self._rollover_locked()

    def _rollover_locked(self) -> None:
        super().doRollover()


class GZipRotatingFileHandler(PrivateRotatingFileHandler):
    """Size-based rotating handler that compresses rolled files as .gz."""

    def _reopen_current_file(self) -> None:
        self._finish_pending_compression()
        super()._reopen_current_file()

    def _finish_pending_compression(self) -> None:
        import gzip

        plain = f"{self.baseFilename}.1"
        if not os.path.exists(plain):
            return
        destination = f"{plain}.gz"
        temporary = f"{destination}.tmp"
        # Keep the source until the complete gzip is published; a killed writer
        # leaves a recoverable segment for the next lock owner.
        with open(plain, "rb") as source:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                    shutil.copyfileobj(source, compressed)
        os.replace(temporary, destination)
        os.remove(plain)

    def _rollover_locked(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        self._finish_pending_compression()
        if self.backupCount > 0:
            oldest = f"{self.baseFilename}.{self.backupCount}.gz"
            if os.path.exists(oldest):
                os.remove(oldest)
            for index in range(self.backupCount - 1, 0, -1):
                source = f"{self.baseFilename}.{index}.gz"
                dest = f"{self.baseFilename}.{index + 1}.gz"
                if os.path.exists(source):
                    os.replace(source, dest)
            self.rotate(self.baseFilename, f"{self.baseFilename}.1")
            self._finish_pending_compression()
        if not self.delay:
            self.stream = self._open()


class SafeStreamHandler(logging.StreamHandler):
    """Stream handler that stays quiet when a captured stderr stream closes."""

    def emit(self, record: logging.LogRecord) -> None:
        stream = self.stream
        if stream is not None and getattr(stream, "closed", False):
            return
        try:
            super().emit(record)
        except ValueError:
            return


class ContextSnapshotFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        existing = getattr(record, "context", None)
        context = current_log_context()
        if isinstance(existing, dict):
            context.update(existing)
        context.setdefault("env", _logging_runtime.env)
        context.setdefault("version", _logging_runtime.version)
        record.context = context
        record.app_id = str(
            getattr(record, "app_id", _logging_runtime.app_id)
            or _logging_runtime.app_id
        )
        record.category = _record_category(record)
        event_name, message_text = _normalize_event_payload(
            str(getattr(record, "message_text", record.getMessage())),
            event_name=getattr(record, "event_name", None),
        )
        record.event_name = event_name
        record.message_text = message_text
        return True


class FlushableQueueHandler(QueueHandler):
    def __init__(
        self, log_queue: queue.Queue[object], listener: "_RecordQueueListener"
    ) -> None:
        super().__init__(log_queue)
        self._listener = listener
        self.addFilter(ContextSnapshotFilter())

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Ignore arbitrary third-party extras: only bounded canonical fields cross
        # the queue boundary, so unrelated objects cannot retain application state.
        fields = {}
        for key in (
            "name",
            "levelname",
            "app_id",
            "category",
            "event_name",
            "block_id",
            "log_kind",
        ):
            value = getattr(record, key, None)
            if value is not None:
                fields[key] = _ascii_text(redact_sensitive_text(value))[:256]
        for key in (
            "created",
            "msecs",
            "relativeCreated",
            "levelno",
            "seq",
            "depth",
            "timing_only",
        ):
            value = getattr(record, key, None)
            if isinstance(value, bool | int | float):
                fields[key] = value
        prepared = logging.makeLogRecord(fields)
        prepared.msg = _ascii_text(
            str(getattr(record, "message_text", record.getMessage()))
        )
        prepared.context = _json_safe_context(_record_context(record))
        collisions = {
            key: prepared.context.pop(key)
            for key in list(prepared.context)
            if key in RESERVED_RECORD_FIELDS
        }
        if collisions:
            prepared.context["context_fields"] = collisions
        prepared.msg = redact_sensitive_text(prepared.msg)[:MAX_RECORD_TEXT]
        prepared.message_text = prepared.msg
        prepared.exception_text = _exception_text(record)
        prepared.exc_text = None
        # Tracebacks retain frames and mutable application state; queue only safe text.
        prepared.exc_info = None
        prepared.args = None
        prepared.stack_info = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        self._listener.enqueue(record)

    def flush(self) -> None:
        if self._listener is not None and not _in_async_context():
            self._listener.flush()


class _RecordQueueListener:
    def __init__(self, handlers: list[logging.Handler], *, capacity: int = 512) -> None:
        self._handlers = handlers
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max(1, capacity))
        self._thread = threading.Thread(
            target=self._run, name="cnc-log-listener", daemon=True
        )
        self._started = False
        self._stopping = threading.Event()
        self._state_lock = threading.Lock()
        self._handler_failures = 0
        self._last_handler_error = ""
        self._dropped_records = 0
        self._dropped_by_level: dict[str, int] = {}
        self._last_drop_at: float | None = None
        self._flush_timeouts = 0
        self._flush_failures = 0

    @property
    def queue(self) -> queue.Queue[object]:
        return self._queue

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def is_alive(self) -> bool:
        return self._started and self._thread.is_alive()

    def enqueue(self, record: logging.LogRecord) -> None:
        # Linearize acceptance with shutdown so no accepted record arrives after drain.
        with self._state_lock:
            if self._stopping.is_set():
                self._record_drop(record)
                return
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._record_drop(record)

    def _record_drop(self, record: logging.LogRecord) -> None:
        self._dropped_records += 1
        level = record.levelname
        self._dropped_by_level[level] = self._dropped_by_level.get(level, 0) + 1
        self._last_drop_at = time.time()

    def failure_status(self) -> dict[str, Any]:
        return {
            "handler_failures": self._handler_failures,
            "last_handler_error": self._last_handler_error,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "dropped_records": self._dropped_records,
            "dropped_by_level": dict(self._dropped_by_level),
            "last_drop_at": self._last_drop_at,
            "flush_timeouts": self._flush_timeouts,
            "flush_failures": self._flush_failures,
        }

    def flush(self, *, timeout: float = 2.0) -> bool:
        done = threading.Event()
        with self._state_lock:
            if not self.is_alive() or self._stopping.is_set():
                return False
            try:
                self._queue.put_nowait(_FlushRequest(done=done))
            except queue.Full:
                self._flush_timeouts += 1
                return False
        if not done.wait(timeout=max(0, timeout)):
            with self._state_lock:
                self._flush_timeouts += 1
            return False
        return self._handler_failures == 0 and all(
            getattr(getattr(handler, "_sink", handler), "write_failures", 0) == 0
            for handler in self._handlers
        )

    def stop(self, *, flush_incomplete: bool) -> bool:
        with self._state_lock:
            self._flush_incomplete_on_stop = flush_incomplete
            self._stopping.set()
        if self._started:
            self._thread.join(timeout=2.0)
        return not self.is_alive()

    def _failure(self, handler: logging.Handler, *, flushing: bool = False) -> None:
        with self._state_lock:
            self._handler_failures += 1
            self._last_handler_error = f"{type(handler).__name__} failed"
            if flushing:
                self._flush_failures += 1

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stopping.is_set():
                    break
                continue
            if isinstance(item, _FlushRequest):
                self._flush_handlers()
                item.done.set()
                continue
            for handler in self._handlers:
                try:
                    handler.handle(item)
                except Exception:
                    self._failure(handler)
        if self._flush_incomplete_on_stop:
            for handler in self._handlers:
                flush_incomplete = getattr(handler, "flush_incomplete", None)
                if callable(flush_incomplete):
                    try:
                        flush_incomplete()
                    except Exception:
                        self._failure(handler, flushing=True)
        self._flush_handlers()
        # The writer owns closure, even when a caller's bounded stop times out.
        for handler in self._handlers:
            try:
                handler.close()
            except Exception:
                self._failure(handler, flushing=True)

    def _flush_handlers(self) -> None:
        for handler in self._handlers:
            try:
                handler.flush()
            except Exception:
                self._failure(handler, flushing=True)


class BlockBufferedHandler(logging.Handler):
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        backup_count: int,
        compress_rotated: bool,
    ) -> None:
        super().__init__(level=logging.INFO)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        if compress_rotated:
            self._sink: RotatingFileHandler = GZipRotatingFileHandler(
                str(path),
                maxBytes=max(1024, max_bytes),
                backupCount=max(1, backup_count),
                encoding="utf-8",
            )
        else:
            self._sink = PrivateRotatingFileHandler(
                str(path),
                maxBytes=max(1024, max_bytes),
                backupCount=max(1, backup_count),
                encoding="utf-8",
            )
        self._sink.setFormatter(logging.Formatter("%(message)s"))
        self._blocks: dict[str, list[logging.LogRecord]] = {}
        self._lock = threading.Lock()
        self.incomplete_blocks = 0
        self._buffered_records = 0

    def emit(self, record: logging.LogRecord) -> None:
        block_id = str(getattr(record, "block_id", "") or "").strip()
        if not block_id:
            self._write_rendered(self._render_single_event(record), record.levelno)
            return
        evicted = []
        records = None
        with self._lock:
            if block_id not in self._blocks and len(self._blocks) >= 128:
                evicted.append(self._blocks.pop(next(iter(self._blocks))))
            bucket = self._blocks.setdefault(block_id, [])
            bucket.append(record)
            self._buffered_records += 1
            if (
                bool(getattr(record, "timing_only", False))
                or str(getattr(record, "log_kind", "")) == "timing"
            ):
                records = self._blocks.pop(block_id)
            elif len(bucket) >= 128:
                evicted.append(self._blocks.pop(block_id))
            self._buffered_records -= sum(len(partial) for partial in evicted)
            if records:
                self._buffered_records -= len(records)
            while self._buffered_records > 512:
                partial = self._blocks.pop(next(iter(self._blocks)))
                self._buffered_records -= len(partial)
                evicted.append(partial)
        for partial in evicted:
            self.incomplete_blocks += 1
            self._write_rendered(
                self._render_block(partial, incomplete=True),
                _max_levelno(partial, default=logging.WARNING),
            )
        if records:
            self._write_rendered(self._render_block(records), _max_levelno(records))

    def flush_incomplete(self) -> None:
        with self._lock:
            pending = list(self._blocks.values())
            self._blocks.clear()
            self._buffered_records = 0
        for records in pending:
            self._write_rendered(
                self._render_block(records, incomplete=True),
                _max_levelno(records, default=logging.WARNING),
            )

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        self.flush_incomplete()
        try:
            self._sink.close()
        finally:
            super().close()

    def _write_rendered(self, text: str, levelno: int) -> None:
        line_record = logging.makeLogRecord(
            {
                "name": "app.log",
                "levelno": levelno,
                "levelname": logging.getLevelName(levelno),
                "msg": text,
                "args": (),
            }
        )
        self._sink.emit(line_record)

    def _render_single_event(self, record: logging.LogRecord) -> str:
        lines = [
            _render_header(
                record.created,
                record.levelname,
                str(
                    getattr(record, "app_id", _logging_runtime.app_id)
                    or _logging_runtime.app_id
                ),
                _record_category(record),
            )
        ]
        fields = _format_log_suffix(
            _record_context(record),
            event_name=_record_event_name(record),
            include_name=True,
        )
        lines.append(
            f"{_level_symbol(record.levelname)} {_ascii_text(str(record.message_text))}{_suffix(fields)}"
        )
        lines.extend(_render_exception_dump(record))
        return "\n".join(lines)

    def _render_block(
        self, records: list[logging.LogRecord], *, incomplete: bool = False
    ) -> str:
        ordered = sorted(records, key=lambda item: int(getattr(item, "seq", 0)))
        header_level = logging.getLevelName(_max_levelno(ordered, default=logging.INFO))
        category = _record_category(ordered[0])
        app_id = str(
            getattr(ordered[0], "app_id", _logging_runtime.app_id)
            or _logging_runtime.app_id
        )
        lines = [_render_header(ordered[0].created, header_level, app_id, category)]
        has_result = False
        has_timing = False
        for index, record in enumerate(ordered):
            kind = str(getattr(record, "log_kind", "") or "")
            if kind == "timing" or bool(getattr(record, "timing_only", False)):
                latency_ms = _record_context(record).get("latency_ms")
                if latency_ms is not None:
                    lines.append(f"|= {_format_duration(latency_ms)} total")
                has_timing = True
                continue
            depth = max(0, int(getattr(record, "depth", 0)))
            fields = _format_log_suffix(_record_context(record), include_name=False)
            if index == 0 and depth == 0:
                lines.append(
                    f"{_level_symbol(record.levelname)} {_ascii_text(str(record.message_text))}{_suffix(fields)}"
                )
            else:
                label = _record_event_name(record) or "event"
                lines.append(
                    f"{_step_prefix(depth)}{label.ljust(NAME_COLUMN_WIDTH)} | "
                    f"{_level_symbol(record.levelname)} {_ascii_text(str(record.message_text))}{_suffix(fields)}"
                )
            lines.extend(_render_exception_dump(record))
            if kind == "result" or (
                _record_event_name(record) == "result" and depth > 0
            ):
                has_result = True
        if incomplete and not has_result:
            lines.append(
                f"{_step_prefix(1)}{'result'.ljust(NAME_COLUMN_WIDTH)} | "
                f"{_level_symbol('WARNING')} Incomplete | status: incomplete"
            )
        if incomplete and not has_timing:
            lines.append("|= incomplete")
        return "\n".join(lines)


def configure_logging(
    log_file: str | None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    compress_rotated: bool = True,
    console: bool = True,
    app_id: str = "cnc.admin",
    env: str = "local",
    version: str = "unknown",
) -> None:
    global _logging_runtime
    global _logging_pipeline
    global _atexit_registered
    next_runtime = LoggingRuntime(
        app_id=_normalize_identity(app_id, fallback="cnc.admin"),
        env=_normalize_identity(env, fallback="local"),
        version=_normalize_identity(version, fallback="unknown"),
    )

    root = logging.getLogger()
    preserved_root_handlers = [
        handler for handler in root.handlers if _is_pytest_capture_handler(handler)
    ]
    managed_handlers: list[logging.Handler] = []
    try:
        if console and not preserved_root_handlers:
            stream_handler = SafeStreamHandler(sys.stderr)
            stream_handler.setFormatter(StreamRecordFormatter())
            managed_handlers.append(stream_handler)

        if log_file:
            log_path = Path(log_file)
            events_path = _events_log_path(log_path)
            events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if compress_rotated:
                jsonl_handler: RotatingFileHandler = GZipRotatingFileHandler(
                    str(events_path),
                    maxBytes=max(1024, max_bytes),
                    backupCount=max(1, backup_count),
                    encoding="utf-8",
                )
            else:
                jsonl_handler = PrivateRotatingFileHandler(
                    str(events_path),
                    maxBytes=max(1024, max_bytes),
                    backupCount=max(1, backup_count),
                    encoding="utf-8",
                )
            jsonl_handler.setFormatter(JSONLFormatter())
            managed_handlers.append(jsonl_handler)
            managed_handlers.append(
                BlockBufferedHandler(
                    log_path,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                    compress_rotated=compress_rotated,
                )
            )
    except Exception:
        for handler in managed_handlers:
            with suppress(Exception):
                handler.close()
        raise

    _logging_runtime = next_runtime

    if _logging_pipeline is not None:
        _logging_pipeline.stop()
        _logging_pipeline = None

    for handler in list(root.handlers):
        if handler in preserved_root_handlers:
            continue
        try:
            handler.close()
        finally:
            root.removeHandler(handler)
    logging.disable(logging.NOTSET)
    root.handlers.clear()
    root.setLevel(logging.INFO)
    for existing in list(logging.root.manager.loggerDict.values()):
        if not isinstance(existing, logging.Logger):
            continue
        for handler in list(existing.handlers):
            try:
                handler.close()
            finally:
                existing.removeHandler(handler)
        existing.setLevel(logging.NOTSET)
        existing.propagate = True
        existing.disabled = False
    for logger_name in NOISY_DEPENDENCY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    for handler in preserved_root_handlers:
        root.addHandler(handler)

    listener = _RecordQueueListener(managed_handlers)
    queue_handler = FlushableQueueHandler(listener.queue, listener)
    root.addHandler(queue_handler)
    listener.start()
    _logging_pipeline = _LoggingPipeline(
        queue_handler=queue_handler,
        listener=listener,
        managed_handlers=managed_handlers,
    )

    if not _atexit_registered:
        atexit.register(_shutdown_logging)
        _atexit_registered = True


def resolve_log_version(explicit_version: str | None = None) -> str:
    candidate = str(explicit_version or "").strip()
    if candidate:
        return candidate
    try:
        payload = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "unknown"
    version = payload.get("version")
    return _normalize_identity(version, fallback="unknown")


def current_app_id() -> str:
    return _logging_runtime.app_id


def current_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def logging_pipeline_status() -> dict[str, Any]:
    root = logging.getLogger()
    pipeline = _logging_pipeline
    handlers = (
        [_handler_status(handler) for handler in pipeline.managed_handlers]
        if pipeline
        else []
    )
    failures = pipeline.listener.failure_status() if pipeline else {}
    attached = bool(pipeline and pipeline.queue_handler in root.handlers)
    alive = bool(pipeline and pipeline.listener.is_alive())
    disabled = sorted(
        name
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger) and logger.disabled
    )
    operational = (
        attached
        and alive
        and bool(handlers)
        and not root.disabled
        and root.getEffectiveLevel() <= logging.INFO
        and root.manager.disable < logging.INFO
        and not disabled
    )
    degraded = (
        not operational
        or any(
            failures.get(key, 0)
            for key in ("handler_failures", "dropped_records", "flush_timeouts")
        )
        or any(handler.get("write_failures", 0) for handler in handlers)
    )
    return {
        "configured": pipeline is not None,
        "status": "degraded" if degraded else "ok",
        "operational": operational,
        "queue_handler_attached": attached,
        "root_handler_count": len(root.handlers),
        "root_level": logging.getLevelName(root.getEffectiveLevel()),
        "global_disable_level": root.manager.disable,
        "disabled_loggers": disabled,
        "listener_alive": alive,
        **failures,
        "managed_handlers": handlers,
        "sink_write_failures": sum(handler["write_failures"] for handler in handlers),
    }


def _in_async_context() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def flush_logging_pipeline() -> bool:
    """Synchronous CLI durability boundary; async callers use the awaited variant."""
    if _logging_pipeline is not None:
        return _logging_pipeline.listener.flush()
    return False


async def flush_logging_pipeline_async() -> bool:
    pipeline = _logging_pipeline
    if pipeline is None:
        return False
    return await asyncio.to_thread(pipeline.listener.flush)


def _should_flush_immediate_event(
    event_name: str | None, context: dict[str, Any], level: int
) -> bool:
    if not event_name:
        return False
    if event_name == "http.request.rejected":
        return True
    if event_name == "http.request.completed":
        status = context.get("status_code")
        return isinstance(status, int) and status >= 400
    if event_name.startswith("ui.backend."):
        return True
    if event_name.startswith("backend.ssh."):
        return True
    return level >= logging.ERROR


@contextmanager
def bind_log_context(**kwargs: Any):
    existing = current_log_context()
    merged = dict(existing)
    for key, value in kwargs.items():
        if value is None:
            continue
        merged[str(key)] = value
    token = _log_context.set(merged)
    try:
        yield merged
    finally:
        _log_context.reset(token)


def _shutdown_logging() -> None:
    global _logging_pipeline
    if _logging_pipeline is None:
        return
    _logging_pipeline.stop()
    _logging_pipeline = None


def _normalize_identity(value: Any, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _record_context(record: logging.LogRecord) -> dict[str, Any]:
    context = getattr(record, "context", None)
    if not isinstance(context, dict):
        context = {}
    merged = dict(context)
    merged.setdefault("env", _logging_runtime.env)
    merged.setdefault("version", _logging_runtime.version)
    return merged


def _record_category(record: logging.LogRecord) -> str:
    category = str(getattr(record, "category", "") or record.name or "app").strip()
    return _canonical_log_name(category) or "app"


def _record_event_name(record: logging.LogRecord) -> str | None:
    event_name = str(getattr(record, "event_name", "") or "").strip()
    return event_name or None


def _normalize_event_payload(
    message: str, *, event_name: str | None
) -> tuple[str | None, str]:
    normalized_message = _ascii_text(redact_sensitive_text(message))
    normalized_event_name = _canonical_log_name(event_name)
    if normalized_event_name:
        return normalized_event_name, normalized_message
    inferred_event_name = _canonical_log_name(normalized_message)
    if inferred_event_name:
        return inferred_event_name, _humanize_event_name(inferred_event_name)
    return None, normalized_message


def _canonical_log_name(value: object) -> str | None:
    candidate = str(value or "").strip().lower()
    if not candidate or not EVENT_TOKEN_RE.fullmatch(candidate):
        return None
    canonical = re.sub(r"[._]+", ".", candidate).strip(".")
    return canonical if EVENT_NAME_RE.fullmatch(canonical) else None


def _humanize_event_name(value: str) -> str:
    words = value.replace(".", " ").split()
    if not words:
        return "Event"
    humanized = " ".join(words)
    return humanized[:1].upper() + humanized[1:]


def _format_log_suffix(
    context: dict[str, Any],
    *,
    event_name: str | None = None,
    include_name: bool,
) -> str:
    safe_context = _redact_context(context)
    rendered: list[str] = []
    if include_name and event_name:
        rendered.append(f"name: {_format_ctx_value(event_name)}")
    if not safe_context:
        return ", ".join(rendered)
    ordered_keys = [key for key in PRIMARY_CONTEXT_KEYS if key in safe_context]
    ordered_keys.extend(
        sorted(key for key in safe_context if key not in PRIMARY_CONTEXT_KEYS)
    )
    rendered.extend(
        f"{key}: {_format_ctx_value(safe_context[key])}" for key in ordered_keys
    )
    return ", ".join(rendered)


def _format_ctx_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}" if value.is_integer() is False else f"{value:.1f}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        ascii_value = _ascii_text(redact_sensitive_text(value))
        if ascii_value and SAFE_CTX_VALUE_RE.fullmatch(ascii_value):
            return ascii_value
        return json.dumps(ascii_value, ensure_ascii=True)
    return json.dumps(
        _json_safe_context(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ascii_text(value: str) -> str:
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def _json_safe_context(value: Any) -> Any:
    # A shared budget bounds cycles, depth, width and text across the entire snapshot.
    nodes = MAX_CONTEXT_NODES
    characters = MAX_RECORD_TEXT

    def snapshot(item: Any, depth: int = 0) -> Any:
        nonlocal nodes, characters
        nodes -= 1
        if nodes < 0 or depth > 8 or characters <= 0:
            return "[truncated]"
        if isinstance(item, dict):
            result = {}
            for raw_key, child in item.items():
                if nodes <= 0 or characters <= 0:
                    result["_truncated"] = True
                    break
                key = redact_sensitive_text(raw_key)[:256]
                characters -= len(key)
                result[key] = (
                    "[redacted]"
                    if SENSITIVE_CONTEXT_KEY_RE.search(key)
                    else snapshot(child, depth + 1)
                )
            return result
        if isinstance(item, list | tuple):
            result = []
            for child in item:
                if nodes <= 0 or characters <= 0:
                    result.append("[truncated]")
                    break
                result.append(snapshot(child, depth + 1))
            return result
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        text = _ascii_text(redact_sensitive_text(item))[: max(0, characters)]
        characters -= len(text)
        return text

    return snapshot(value)


def _redact_context(context: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for raw_key, value in context.items():
        key = str(raw_key)
        if SENSITIVE_CONTEXT_KEY_RE.search(key):
            redacted[key] = "[redacted]"
        elif isinstance(value, dict):
            redacted[key] = _redact_context(value)
        elif isinstance(value, list | tuple):
            redacted[key] = [
                _redact_context(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def redact_sensitive_text(value: object) -> str:
    text = str(value or "")
    text = URL_CREDENTIALS_RE.sub(r"\1[redacted]@", text)
    text = AUTHORIZATION_TEXT_RE.sub(r"\1[redacted]", text)

    def replace(match: re.Match[str]) -> str:
        value = match.group(2)
        quote = value[0] if value.startswith(('"', "'")) else ""
        return f"{match.group(1)}{quote}[redacted]{quote}"

    return SENSITIVE_TEXT_RE.sub(replace, text)


def _events_log_path(log_path: Path) -> Path:
    if log_path.suffix == ".log":
        return log_path.with_name(f"{log_path.stem}.events.jsonl")
    return log_path.with_name(f"{log_path.name}.events.jsonl")


def _level_symbol(level_name: str) -> str:
    return LEVEL_SYMBOLS.get(level_name, "(*)")


def _render_header(created: float, level_name: str, app_id: str, category: str) -> str:
    ts = datetime.fromtimestamp(created, tz=timezone.utc)
    return (
        f"[{ts:%Y-%m-%d}] ----- {ts:%H:%M:%S}.{int(ts.microsecond / 1000):03d} | "
        f"{level_name.ljust(8)} | {app_id} | {category} -----"
    )


def _suffix(fields: str) -> str:
    return f" | {fields}" if fields else ""


def _step_prefix(depth: int) -> str:
    if depth <= 0:
        return ""
    if depth == 1:
        return ">> "
    return f"{'>> ' * (depth - 1)}>> "


def _exception_text(record: logging.LogRecord) -> str:
    rendered = getattr(record, "exception_text", "") or record.exc_text or ""
    if not rendered and record.exc_info:
        rendered = "".join(
            traceback.format_exception(*record.exc_info, limit=32)
        ).rstrip("\n")
    return _ascii_text(redact_sensitive_text(rendered))[:MAX_RECORD_TEXT]


def _render_exception_dump(record: logging.LogRecord) -> list[str]:
    return [f"~~ {line}" for line in _exception_text(record).splitlines()]


def _max_levelno(records: list[logging.LogRecord], default: int = logging.INFO) -> int:
    if not records:
        return default
    return max(int(getattr(record, "levelno", default)) for record in records)


def _format_duration(value: Any) -> str:
    try:
        return f"{float(value):.1f}ms"
    except (TypeError, ValueError):
        return "0.0ms"


def _handler_status(handler: logging.Handler) -> dict[str, Any]:
    status: dict[str, Any] = {
        "type": type(handler).__name__,
        "level": logging.getLevelName(handler.level),
    }
    base_filename = getattr(handler, "baseFilename", None)
    if base_filename:
        status["path"] = str(base_filename)
    buffered_path = getattr(handler, "path", None)
    if buffered_path is not None:
        status["path"] = str(buffered_path)
        sink = getattr(handler, "_sink", None)
        sink_filename = getattr(sink, "baseFilename", None)
        if sink_filename:
            status["sink_path"] = str(sink_filename)
    sink = getattr(handler, "_sink", handler)
    status["write_failures"] = getattr(sink, "write_failures", 0)
    status["last_write_error"] = getattr(sink, "last_write_error", "")
    status["last_successful_write"] = getattr(sink, "last_successful_write", None)
    if isinstance(handler, BlockBufferedHandler):
        status["buffered_blocks"] = len(handler._blocks)
        status["buffered_records"] = handler._buffered_records
        status["incomplete_blocks"] = handler.incomplete_blocks
    return status


@dataclass
class Operation:
    logger: "AppLogger"
    category: str
    event_name: str
    root_message: str
    block_id: str
    start: float
    root_context: dict[str, Any]
    next_seq: int = 1
    result_emitted: bool = False

    def step(
        self,
        event_name: str,
        message: str | None = None,
        *,
        level: int = logging.INFO,
        depth: int = 1,
        **kwargs: Any,
    ) -> None:
        resolved_name, resolved_message = _normalize_event_payload(
            message or event_name,
            event_name=event_name,
        )
        self.logger._log_event(
            level,
            resolved_message,
            event_name=resolved_name,
            block_id=self.block_id,
            seq=self._next_seq(),
            depth=max(1, depth),
            log_kind="step",
            **kwargs,
        )

    def result(
        self, message: str = "Completed", *, level: int = logging.INFO, **kwargs: Any
    ) -> None:
        self.result_emitted = True
        self.logger._log_event(
            level,
            message,
            event_name="result",
            block_id=self.block_id,
            seq=self._next_seq(),
            depth=1,
            log_kind="result",
            **kwargs,
        )

    def failed(
        self, message: str = "Failed", *, exc_info: bool = False, **kwargs: Any
    ) -> None:
        self.result_emitted = True
        self.logger._log_event(
            logging.ERROR,
            message,
            event_name="result",
            block_id=self.block_id,
            seq=self._next_seq(),
            depth=1,
            log_kind="result",
            exc_info=exc_info,
            **kwargs,
        )

    def finish_timing(self) -> None:
        latency_ms = round((time.monotonic() - self.start) * 1000, 1)
        self.logger._log_event(
            logging.INFO,
            "Total",
            event_name="total",
            block_id=self.block_id,
            seq=self._next_seq(),
            depth=0,
            log_kind="timing",
            timing_only=True,
            latency_ms=latency_ms,
        )

    def _next_seq(self) -> int:
        current = self.next_seq
        self.next_seq += 1
        return current


class AppLogger:
    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(_canonical_log_name(name) or "app")

    def _log_event(
        self,
        level: int,
        message: str,
        *,
        exc_info: bool = False,
        event_name: str | None = None,
        block_id: str | None = None,
        seq: int | None = None,
        depth: int = 0,
        log_kind: str | None = None,
        timing_only: bool = False,
        **kwargs: Any,
    ) -> None:
        context = {"component": self._logger.name, **kwargs}
        resolved_event_name, resolved_message = _normalize_event_payload(
            message, event_name=event_name
        )
        self._logger.log(
            level,
            resolved_message,
            extra={
                "app_id": _logging_runtime.app_id,
                "category": self._logger.name,
                "context": context,
                "event_name": resolved_event_name,
                "message_text": resolved_message,
                "block_id": block_id,
                "seq": int(seq or 0),
                "depth": int(depth),
                "log_kind": log_kind,
                "timing_only": bool(timing_only),
            },
            exc_info=exc_info,
        )
        if not _in_async_context() and _should_flush_immediate_event(
            resolved_event_name, context, level
        ):
            flush_logging_pipeline()

    def debug(self, message: str, **kwargs: Any) -> None:
        self._log_event(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._log_event(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._log_event(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._log_event(logging.ERROR, message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        self._log_event(logging.CRITICAL, message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        exception_type = sys.exc_info()[0]
        if exception_type is not None:
            kwargs.setdefault("error_type", exception_type.__name__)
        self._log_event(logging.ERROR, message, exc_info=True, **kwargs)

    @asynccontextmanager
    async def operation(self, event: str, **kwargs: Any):
        block_id = f"blk_{secrets.token_hex(6)}"
        start = time.monotonic()
        event_name, root_message = _normalize_event_payload(
            f"Started {_humanize_event_name(event).lower()}",
            event_name=event,
        )
        operation = Operation(
            logger=self,
            category=self._logger.name,
            event_name=event_name or event,
            root_message=root_message,
            block_id=block_id,
            start=start,
            root_context=dict(kwargs),
        )
        self._log_event(
            logging.INFO,
            root_message,
            event_name=event_name or event,
            block_id=block_id,
            seq=0,
            depth=0,
            log_kind="root",
            **kwargs,
        )
        try:
            yield operation
        except asyncio.CancelledError:
            if not operation.result_emitted:
                operation.result(
                    "Cancelled",
                    level=logging.WARNING,
                    **{**kwargs, "status": "cancelled"},
                )
            raise
        except BaseException as exc:
            if not operation.result_emitted:
                operation.failed(
                    "Failed",
                    exc_info=True,
                    error_type=type(exc).__name__,
                    **kwargs,
                )
            raise
        else:
            if not operation.result_emitted:
                operation.result("Completed", **kwargs)
        finally:
            operation.finish_timing()


def get_logger(name: str) -> AppLogger:
    return AppLogger(name)
