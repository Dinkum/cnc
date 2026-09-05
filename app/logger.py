import atexit
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
    r"(?:^|_)(?:authorization|cookie|password|secret|token|access_key|api_key|csrf)(?:$|_)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b(token|secret|password|authorization|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)([^,\s&]+)"
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


@dataclass(frozen=True)
class _StopRequest:
    flush_incomplete: bool
    done: threading.Event


@dataclass
class _LoggingPipeline:
    queue_handler: "FlushableQueueHandler"
    listener: "_RecordQueueListener"
    managed_handlers: list[logging.Handler] = field(default_factory=list)

    def stop(self) -> None:
        self.queue_handler.flush()
        self.listener.stop(flush_incomplete=True)
        self.queue_handler.close()
        for handler in self.managed_handlers:
            try:
                handler.close()
            except Exception:
                continue


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
        if record.exc_info:
            exception_text = self.formatException(record.exc_info)
            if exception_text:
                line = f"{line}\n{_ascii_text(exception_text)}"
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
        if record.exc_info:
            payload["exception"] = _ascii_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            )
        payload.update(_json_safe_context(_record_context(record)))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler whose active log is readable only by CNC."""

    def _open(self):
        stream = super()._open()
        with suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream


class GZipRotatingFileHandler(PrivateRotatingFileHandler):
    """Size-based rotating handler that compresses rolled files as .gz."""

    def doRollover(self) -> None:  # noqa: N802
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            oldest = f"{self.baseFilename}.{self.backupCount}.gz"
            if os.path.exists(oldest):
                os.remove(oldest)

            for index in range(self.backupCount - 1, 0, -1):
                source = f"{self.baseFilename}.{index}.gz"
                dest = f"{self.baseFilename}.{index + 1}.gz"
                if os.path.exists(source):
                    if os.path.exists(dest):
                        os.remove(dest)
                    os.rename(source, dest)

            rolled_plain = f"{self.baseFilename}.1"
            rolled_gz = f"{rolled_plain}.gz"
            if os.path.exists(rolled_plain):
                os.remove(rolled_plain)
            if os.path.exists(rolled_gz):
                os.remove(rolled_gz)
            self.rotate(self.baseFilename, rolled_plain)
            with open(rolled_plain, "rb") as source:
                import gzip

                with gzip.open(rolled_gz, "wb") as dest:
                    shutil.copyfileobj(source, dest)
            with suppress(OSError):
                os.chmod(rolled_gz, 0o600)
            os.remove(rolled_plain)

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
        prepared = logging.makeLogRecord(record.__dict__.copy())
        prepared.msg = _ascii_text(
            str(getattr(record, "message_text", record.getMessage()))
        )
        prepared.args = None
        return prepared

    def flush(self) -> None:
        if self._listener is not None:
            self._listener.flush()


class _RecordQueueListener:
    def __init__(self, handlers: list[logging.Handler]) -> None:
        self._handlers = handlers
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="cnc-log-listener", daemon=True
        )
        self._started = False
        self._handler_failures = 0
        self._last_handler_error = ""

    @property
    def queue(self) -> queue.Queue[object]:
        return self._queue

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def is_alive(self) -> bool:
        return self._started and self._thread.is_alive()

    def failure_status(self) -> dict[str, Any]:
        return {
            "handler_failures": self._handler_failures,
            "last_handler_error": self._last_handler_error,
        }

    def flush(self) -> None:
        if not self._started:
            return
        done = threading.Event()
        self._queue.put(_FlushRequest(done=done))
        done.wait(timeout=2.0)

    def stop(self, *, flush_incomplete: bool) -> None:
        if not self._started:
            return
        done = threading.Event()
        self._queue.put(_StopRequest(flush_incomplete=flush_incomplete, done=done))
        done.wait(timeout=2.0)
        self._thread.join(timeout=2.0)
        self._started = False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, _FlushRequest):
                self._flush_handlers()
                item.done.set()
                continue
            if isinstance(item, _StopRequest):
                if item.flush_incomplete:
                    for handler in self._handlers:
                        flush_incomplete = getattr(handler, "flush_incomplete", None)
                        if callable(flush_incomplete):
                            try:
                                flush_incomplete()
                            except Exception:
                                continue
                self._flush_handlers()
                item.done.set()
                return
            for handler in self._handlers:
                try:
                    handler.handle(item)
                except Exception:
                    self._handler_failures += 1
                    self._last_handler_error = f"{type(handler).__name__} failed"
                    continue

    def _flush_handlers(self) -> None:
        for handler in self._handlers:
            try:
                handler.flush()
            except Exception:
                continue


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

    def emit(self, record: logging.LogRecord) -> None:
        block_id = str(getattr(record, "block_id", "") or "").strip()
        if not block_id:
            self._write_rendered(self._render_single_event(record), record.levelno)
            return
        with self._lock:
            bucket = self._blocks.setdefault(block_id, [])
            bucket.append(record)
            if (
                bool(getattr(record, "timing_only", False))
                or str(getattr(record, "log_kind", "")) == "timing"
            ):
                records = self._blocks.pop(block_id, [])
        if "records" in locals():
            self._write_rendered(self._render_block(records), _max_levelno(records))

    def flush_incomplete(self) -> None:
        with self._lock:
            pending = list(self._blocks.values())
            self._blocks.clear()
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
    managed_handlers = (
        _logging_pipeline.managed_handlers if _logging_pipeline is not None else []
    )
    return {
        "configured": _logging_pipeline is not None,
        "root_handler_count": len(root.handlers),
        "listener_alive": bool(
            _logging_pipeline and _logging_pipeline.listener.is_alive()
        ),
        **(_logging_pipeline.listener.failure_status() if _logging_pipeline else {}),
        "managed_handlers": [_handler_status(handler) for handler in managed_handlers],
    }


def flush_logging_pipeline() -> None:
    """Flush queued log records so operator-facing diagnostics are immediately visible."""
    if _logging_pipeline is not None:
        _logging_pipeline.listener.flush()


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
    if isinstance(value, dict):
        return {
            str(key): _json_safe_context(item)
            for key, item in _redact_context(value).items()
        }
    if isinstance(value, list | tuple):
        return [_json_safe_context(item) for item in value]
    if isinstance(value, str):
        return _ascii_text(redact_sensitive_text(value))
    if value is None or isinstance(value, bool | int | float):
        return value
    return _ascii_text(redact_sensitive_text(value))


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
    return SENSITIVE_TEXT_RE.sub(r"\1\2[redacted]", text)


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


def _render_exception_dump(record: logging.LogRecord) -> list[str]:
    if not record.exc_info:
        return []
    rendered = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
    if not rendered:
        return []
    return [f"~~ {_ascii_text(line)}" for line in rendered.splitlines()]


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
        if _should_flush_immediate_event(resolved_event_name, context, level):
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
        except Exception as exc:
            if not operation.result_emitted:
                operation.failed(
                    "Failed",
                    exc_info=True,
                    error_type=type(exc).__name__,
                    **kwargs,
                )
            operation.finish_timing()
            raise
        else:
            if not operation.result_emitted:
                operation.result("Completed", **kwargs)
            operation.finish_timing()


def get_logger(name: str) -> AppLogger:
    return AppLogger(name)
