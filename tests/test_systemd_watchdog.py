import asyncio
import os
import socket
import threading
from pathlib import Path

import pytest

from app.services.systemd_watchdog import (
    SystemdWatchdogController,
    run_systemd_watchdog,
    systemd_watchdog_controller_from_env,
)


def test_systemd_watchdog_controller_from_env_ignores_missing_or_mismatched_values() -> (
    None
):
    assert systemd_watchdog_controller_from_env({}) is None
    assert (
        systemd_watchdog_controller_from_env({"NOTIFY_SOCKET": "/tmp/notify.sock"})
        is None
    )
    assert (
        systemd_watchdog_controller_from_env(
            {
                "NOTIFY_SOCKET": "/tmp/notify.sock",
                "WATCHDOG_USEC": "invalid",
            }
        )
        is None
    )
    assert (
        systemd_watchdog_controller_from_env(
            {
                "NOTIFY_SOCKET": "/tmp/notify.sock",
                "WATCHDOG_USEC": "1000000",
                "WATCHDOG_PID": "9999",
            },
            pid=1234,
        )
        is None
    )


def test_systemd_watchdog_controller_from_env_builds_half_interval() -> None:
    controller = systemd_watchdog_controller_from_env(
        {
            "NOTIFY_SOCKET": "/tmp/notify.sock",
            "WATCHDOG_USEC": "6000000",
            "WATCHDOG_PID": "1234",
        },
        pid=1234,
    )

    assert controller == SystemdWatchdogController(
        notify_socket="/tmp/notify.sock",
        interval_sec=3.0,
    )


def test_systemd_watchdog_controller_sends_expected_payload(tmp_path: Path) -> None:
    socket_path = Path(f"/tmp/cnc-watchdog-{os.getpid()}.sock")
    controller = SystemdWatchdogController(
        notify_socket=str(socket_path),
        interval_sec=2.0,
    )

    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(socket_path))
        server.settimeout(1.0)

        assert controller.notify_ready(status_message="cnc-admin ready") is True

        payload = server.recv(1024).decode("utf-8")
    socket_path.unlink()

    assert payload == "READY=1\nSTATUS=cnc-admin ready"


@pytest.mark.asyncio
async def test_run_systemd_watchdog_sends_heartbeat_until_cancelled() -> None:
    calls: list[str | None] = []
    heartbeat_seen = threading.Event()

    class FakeController:
        interval_sec = 0.01

        def notify_watchdog(self, *, status_message: str | None = None) -> bool:
            calls.append(status_message)
            heartbeat_seen.set()
            return True

    task = asyncio.create_task(
        run_systemd_watchdog(FakeController(), status_message="cnc-admin healthy")
    )
    assert await asyncio.to_thread(heartbeat_seen.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == ["cnc-admin healthy"]
