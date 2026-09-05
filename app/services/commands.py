from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Callable

SYSTEMD_NOTIFY_ENV_KEYS = (
    "INVOCATION_ID",
    "LISTEN_FDS",
    "LISTEN_FDNAMES",
    "NOTIFY_SOCKET",
    "WATCHDOG_PID",
    "WATCHDOG_USEC",
)
TRANSIENT_COMMAND_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "resource temporarily unavailable",
    "try again",
    "connection refused",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "failed to connect",
    "i/o timeout",
    "dial tcp",
)


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(Exception):
    def __init__(self, result: CommandResult) -> None:
        message = (
            f"command failed ({result.returncode}): {' '.join(result.command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
        )
        super().__init__(message)
        self.result = result


def command_result_is_retryable(result: CommandResult) -> bool:
    if result.returncode == 124:
        return True
    combined_output = "\n".join(
        part for part in (result.stderr, result.stdout) if part
    ).lower()
    if not combined_output:
        return False
    return any(marker in combined_output for marker in TRANSIENT_COMMAND_ERROR_MARKERS)


def run_command(command: list[str], timeout_sec: float = 30) -> CommandResult:
    proc: subprocess.Popen[str] | None = None
    try:
        env = _subprocess_env_without_systemd_notify()
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=stdout.strip(),
            stderr=stderr.strip(),
        )
    except subprocess.TimeoutExpired:
        assert proc is not None
        _signal_process_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        timeout_message = f"timed out after {timeout_sec}s"
        return CommandResult(
            command=command,
            returncode=124,
            stdout=stdout.strip(),
            stderr="\n".join(
                part for part in (stderr.strip(), timeout_message) if part
            ),
        )
    except OSError as exc:
        return CommandResult(
            command=command,
            returncode=127,
            stdout="",
            stderr=str(exc),
        )


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return


def run_podman_exec(
    container: str,
    guest_command: list[str],
    *,
    timeout_sec: float,
    command_runner: Callable[[list[str], float], CommandResult] = run_command,
) -> CommandResult:
    """Run one bounded command in a guest without letting exec outlive its caller."""
    if timeout_sec <= 0:
        return CommandResult(
            command=["podman", "exec", container, *guest_command],
            returncode=124,
            stdout="",
            stderr="timed out before podman exec started",
        )
    cleanup_margin = min(1.0, timeout_sec / 5)
    guest_timeout = max(0.1, timeout_sec - cleanup_margin)
    guest_timeout_arg = f"{guest_timeout:.3f}".rstrip("0").rstrip(".") + "s"
    command = [
        "podman",
        "exec",
        container,
        "timeout",
        "--signal=TERM",
        "--kill-after=0.1s",
        guest_timeout_arg,
        *guest_command,
    ]
    return command_runner(command, timeout_sec)


def _subprocess_env_without_systemd_notify() -> dict[str, str]:
    env = os.environ.copy()
    for key in SYSTEMD_NOTIFY_ENV_KEYS:
        env.pop(key, None)
    return env


def run_command_checked(command: list[str], timeout_sec: int = 30) -> CommandResult:
    result = run_command(command, timeout_sec=timeout_sec)
    if not result.ok:
        raise CommandError(result)
    return result


async def run_command_checked_async(
    command: list[str], timeout_sec: int = 30
) -> CommandResult:
    return await asyncio.to_thread(run_command_checked, command, timeout_sec)
