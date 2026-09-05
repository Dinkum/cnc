from __future__ import annotations

from dataclasses import dataclass
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading

from app.logger import get_logger


AUDIT_PROTOCOL = "CNCSSH1"
AUDIT_LISTEN_HOST = "127.0.0.1"
AUDIT_MAX_DATAGRAM_BYTES = 512
BACKEND_SSH_INTERNAL_PORT = 9090
BACKEND_SSH_INTERNAL_HEADER_PATH = Path("/run/cnc/backend-ssh.header")
_BACKEND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_EVENT_LEVEL = {
    "handoff": "info",
    "completed": "info",
    "failed": "warning",
    "rejected": "warning",
}
_MODES = {"shell", "command", "llm_help"}
logger = get_logger("backend.ssh")


@dataclass(frozen=True)
class BackendSshAuditEvent:
    event: str
    backend: str
    mode: str
    tty: bool
    client_host: str
    reason: str
    exit_code: int | None
    duration_ms: int | None


def create_backend_ssh_internal_header(
    path: Path = BACKEND_SSH_INTERNAL_HEADER_PATH,
) -> str:
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"X-CNC-Internal-Token: {token}")
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return token


def backend_ssh_internal_token_matches(expected: str, presented: str | None) -> bool:
    if not expected or not presented or len(presented) != len(expected):
        return False
    try:
        return hmac.compare_digest(expected, presented)
    except TypeError:
        return False


def _optional_int(value: str, *, maximum: int) -> int | None:
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("invalid integer")
    parsed = int(value)
    if parsed > maximum:
        raise ValueError("integer out of range")
    return parsed


def parse_backend_ssh_audit_datagram(data: bytes) -> BackendSshAuditEvent | None:
    if not data or len(data) > AUDIT_MAX_DATAGRAM_BYTES:
        return None
    try:
        fields = data.decode("utf-8", errors="strict").split("\t")
    except UnicodeDecodeError:
        return None
    if len(fields) != 9 or fields[0] != AUDIT_PROTOCOL:
        return None

    _, event, backend, mode, tty_raw, client_host, reason, exit_raw, duration_raw = (
        fields
    )
    if event not in _EVENT_LEVEL:
        return None
    if not _BACKEND_NAME_RE.fullmatch(backend) or mode not in _MODES:
        return None
    if tty_raw not in {"0", "1"}:
        return None
    if client_host:
        try:
            client_host = str(ipaddress.ip_address(client_host))
        except ValueError:
            return None
    if reason and not _REASON_RE.fullmatch(reason):
        return None
    try:
        exit_code = _optional_int(exit_raw, maximum=255)
        duration_ms = _optional_int(duration_raw, maximum=3_600_000)
    except ValueError:
        return None

    return BackendSshAuditEvent(
        event=event,
        backend=backend,
        mode=mode,
        tty=tty_raw == "1",
        client_host=client_host,
        reason=reason,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def log_backend_ssh_audit_event(event: BackendSshAuditEvent) -> None:
    log_method = getattr(logger, _EVENT_LEVEL[event.event])
    log_method(
        f"backend.ssh.{event.event}",
        backend=event.backend,
        mode=event.mode,
        tty=event.tty,
        client_host=event.client_host,
        reason=event.reason,
        exit_code=event.exit_code,
        duration_ms=event.duration_ms,
    )


def receive_backend_ssh_audit_datagram(data: bytes) -> None:
    event = parse_backend_ssh_audit_datagram(data)
    if event is not None:
        log_backend_ssh_audit_event(event)


def _receive_backend_ssh_audit_events(
    handle: socket.socket, stopping: threading.Event
) -> None:
    while not stopping.is_set():
        try:
            data, _addr = handle.recvfrom(AUDIT_MAX_DATAGRAM_BYTES + 1)
        except TimeoutError:
            continue
        except OSError as exc:
            if not stopping.is_set():
                logger.warning(
                    "backend.ssh.audit_listener.stopped",
                    error=str(exc),
                )
            return
        receive_backend_ssh_audit_datagram(data)


@dataclass
class BackendSshAuditListener:
    handle: socket.socket
    thread: threading.Thread
    stopping: threading.Event

    def close(self) -> None:
        self.stopping.set()
        self.handle.close()
        self.thread.join(timeout=1)


def start_backend_ssh_audit_listener(
    *, port: int = BACKEND_SSH_INTERNAL_PORT
) -> BackendSshAuditListener | None:
    handle = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        handle.settimeout(0.25)
        handle.bind((AUDIT_LISTEN_HOST, port))
    except OSError as exc:
        handle.close()
        # OpenSSH's own journal remains authoritative if this best-effort mirror
        # cannot bind. CNC availability must not depend on telemetry delivery.
        logger.warning(
            "backend.ssh.audit_listener.unavailable",
            host=AUDIT_LISTEN_HOST,
            port=port,
            error=str(exc),
        )
        return None
    stopping = threading.Event()
    thread = threading.Thread(
        target=_receive_backend_ssh_audit_events,
        args=(handle, stopping),
        name="cnc-backend-ssh-audit",
        daemon=True,
    )
    thread.start()
    logger.info(
        "backend.ssh.audit_listener.started",
        host=AUDIT_LISTEN_HOST,
        port=port,
    )
    return BackendSshAuditListener(handle=handle, thread=thread, stopping=stopping)
