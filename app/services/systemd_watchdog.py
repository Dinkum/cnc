from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import socket
import threading
from typing import Mapping

from app.logger import get_logger


logger = get_logger("systemd_watchdog")


@dataclass(frozen=True)
class SystemdWatchdogController:
    notify_socket: str
    interval_sec: float

    def notify(self, *lines: str) -> bool:
        payload = "\n".join(line.strip() for line in lines if str(line).strip())
        if not payload:
            return True
        return send_systemd_notify(payload, notify_socket=self.notify_socket)

    def notify_status(self, *, status_message: str) -> bool:
        return self.notify(f"STATUS={status_message}")

    def notify_ready(self, *, status_message: str | None = None) -> bool:
        lines = ["READY=1"]
        if status_message:
            lines.append(f"STATUS={status_message}")
        return self.notify(*lines)

    def notify_watchdog(self, *, status_message: str | None = None) -> bool:
        lines = ["WATCHDOG=1"]
        if status_message:
            lines.append(f"STATUS={status_message}")
        return self.notify(*lines)

    def notify_stopping(self, *, status_message: str | None = None) -> bool:
        lines = ["STOPPING=1"]
        if status_message:
            lines.append(f"STATUS={status_message}")
        return self.notify(*lines)


def _notify_socket_address(notify_socket: str) -> str | bytes:
    if notify_socket.startswith("@"):
        return b"\0" + notify_socket[1:].encode("utf-8")
    return notify_socket


def send_systemd_notify(message: str, *, notify_socket: str) -> bool:
    payload = str(message).strip()
    if not payload:
        return True
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as handle:
            handle.connect(_notify_socket_address(notify_socket))
            handle.sendall(payload.encode("utf-8"))
        return True
    except OSError as exc:
        logger.warning(
            "systemd.notify.failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False


def systemd_watchdog_controller_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    pid: int | None = None,
) -> SystemdWatchdogController | None:
    env = environ or os.environ
    notify_socket = str(env.get("NOTIFY_SOCKET", "")).strip()
    if not notify_socket:
        return None

    watchdog_usec_raw = str(env.get("WATCHDOG_USEC", "")).strip()
    if not watchdog_usec_raw:
        return None
    try:
        watchdog_usec = int(watchdog_usec_raw)
    except ValueError:
        return None
    if watchdog_usec <= 0:
        return None

    watchdog_pid_raw = str(env.get("WATCHDOG_PID", "")).strip()
    current_pid = os.getpid() if pid is None else pid
    if watchdog_pid_raw:
        try:
            if int(watchdog_pid_raw) != current_pid:
                return None
        except ValueError:
            return None

    return SystemdWatchdogController(
        notify_socket=notify_socket,
        interval_sec=max(1.0, watchdog_usec / 2_000_000.0),
    )


async def run_systemd_watchdog(
    controller: SystemdWatchdogController,
    *,
    status_message: str | None = None,
) -> None:
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        last_send_ok = True
        while not stop_event.wait(controller.interval_sec):
            send_ok = controller.notify_watchdog(status_message=status_message)
            if not send_ok and last_send_ok:
                logger.warning(
                    "systemd.watchdog.heartbeat_failed",
                    interval_sec=controller.interval_sec,
                )
            elif send_ok and not last_send_ok:
                logger.info(
                    "systemd.watchdog.heartbeat_recovered",
                    interval_sec=controller.interval_sec,
                )
            last_send_ok = send_ok

    thread = threading.Thread(
        target=heartbeat_loop,
        name="cnc-systemd-watchdog",
        daemon=True,
    )
    thread.start()
    try:
        await asyncio.Future()
    finally:
        stop_event.set()
        await asyncio.to_thread(thread.join, min(2.0, controller.interval_sec))
