from __future__ import annotations

import asyncio
from pathlib import Path
import socket

import pytest

from app.services import backend_ssh_audit


class _LoggerSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, name: str, **context: object) -> None:
        self.events.append(("info", name, context))

    def warning(self, name: str, **context: object) -> None:
        self.events.append(("warning", name, context))


def _datagram(
    *,
    event: str = "handoff",
    backend: str = "worker",
    mode: str = "command",
    tty: str = "0",
    client: str = "100.64.0.12",
    reason: str = "",
    exit_code: str = "",
    duration_ms: str = "",
) -> bytes:
    return "\t".join(
        (
            "CNCSSH1",
            event,
            backend,
            mode,
            tty,
            client,
            reason,
            exit_code,
            duration_ms,
        )
    ).encode()


def test_backend_ssh_audit_logs_safe_canonical_context(monkeypatch) -> None:
    logger = _LoggerSpy()
    monkeypatch.setattr(backend_ssh_audit, "logger", logger)

    backend_ssh_audit.receive_backend_ssh_audit_datagram(_datagram())

    assert logger.events == [
        (
            "info",
            "backend.ssh.handoff",
            {
                "backend": "worker",
                "mode": "command",
                "tty": False,
                "client_host": "100.64.0.12",
                "reason": "",
                "exit_code": None,
                "duration_ms": None,
            },
        )
    ]
    assert "command" not in logger.events[0][2]
    assert "stdout" not in logger.events[0][2]


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-the-protocol",
        _datagram(backend="../../root"),
        _datagram(client="not-an-ip"),
        _datagram(reason="bad reason"),
        _datagram(exit_code="256"),
        b"x" * 513,
    ],
)
def test_backend_ssh_audit_drops_malformed_datagrams(payload: bytes) -> None:
    assert backend_ssh_audit.parse_backend_ssh_audit_datagram(payload) is None


def test_backend_ssh_internal_header_is_private_and_matches(tmp_path: Path) -> None:
    path = tmp_path / "run" / "backend-ssh.header"

    token = backend_ssh_audit.create_backend_ssh_internal_header(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="utf-8") == f"X-CNC-Internal-Token: {token}\n"
    assert backend_ssh_audit.backend_ssh_internal_token_matches(token, token) is True
    assert backend_ssh_audit.backend_ssh_internal_token_matches(token, "wrong") is False
    assert (
        backend_ssh_audit.backend_ssh_internal_token_matches(token, "é" * len(token))
        is False
    )


@pytest.mark.asyncio
async def test_backend_ssh_audit_bind_failure_does_not_fail_cnc(monkeypatch) -> None:
    logger = _LoggerSpy()

    class _Socket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def bind(self, _address: object) -> None:
            raise OSError("address unavailable")

        def close(self) -> None:
            pass

    monkeypatch.setattr(backend_ssh_audit, "logger", logger)
    monkeypatch.setattr(backend_ssh_audit.socket, "socket", lambda *_args: _Socket())

    transport = backend_ssh_audit.start_backend_ssh_audit_listener()

    assert transport is None
    assert logger.events == [
        (
            "warning",
            "backend.ssh.audit_listener.unavailable",
            {
                "host": "127.0.0.1",
                "port": 9090,
                "error": "address unavailable",
            },
        )
    ]


@pytest.mark.asyncio
async def test_backend_ssh_audit_listener_receives_real_datagram(monkeypatch) -> None:
    received: list[backend_ssh_audit.BackendSshAuditEvent] = []
    monkeypatch.setattr(
        backend_ssh_audit,
        "log_backend_ssh_audit_event",
        received.append,
    )
    listener = backend_ssh_audit.start_backend_ssh_audit_listener(port=0)
    assert listener is not None
    assert listener.handle.getsockname()[0] == "127.0.0.1"

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(_datagram(), listener.handle.getsockname())
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.01)
    finally:
        sender.close()
        listener.close()

    assert received == [
        backend_ssh_audit.BackendSshAuditEvent(
            event="handoff",
            backend="worker",
            mode="command",
            tty=False,
            client_host="100.64.0.12",
            reason="",
            exit_code=None,
            duration_ms=None,
        )
    ]
