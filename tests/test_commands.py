import json
import os
import sys
import time

from app.services.commands import (
    SYSTEMD_NOTIFY_ENV_KEYS,
    CommandResult,
    run_command,
    run_podman_exec,
)


def test_run_command_does_not_inherit_systemd_notify_environment(monkeypatch) -> None:
    for key in SYSTEMD_NOTIFY_ENV_KEYS:
        monkeypatch.setenv(key, f"cnc-test-{key.lower()}")

    result = run_command(
        [
            sys.executable,
            "-c",
            "import os; print(','.join(sorted(key for key in os.environ if key.startswith(('NOTIFY_', 'WATCHDOG_', 'LISTEN_FD', 'INVOCATION_ID')))))",
        ]
    )

    assert result.ok
    assert result.stdout == ""
    for key in SYSTEMD_NOTIFY_ENV_KEYS:
        assert os.environ[key] == f"cnc-test-{key.lower()}"


def test_run_command_terminates_the_child_process_group_on_timeout(tmp_path) -> None:
    marker = tmp_path / "child-terminated"
    child_script = (
        "import pathlib, signal, time; "
        f"marker = pathlib.Path({json.dumps(str(marker))}); "
        "signal.signal(signal.SIGTERM, lambda *_: (marker.write_text('yes'), __import__('os')._exit(0))); "
        "print('ready', flush=True); "
        "time.sleep(30)"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {json.dumps(child_script)}], "
        "stdout=subprocess.PIPE, text=True); "
        "child.stdout.readline(); "
        "time.sleep(30)"
    )

    result = run_command([sys.executable, "-c", parent_script], timeout_sec=0.3)

    deadline = time.monotonic() + 1
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert result.returncode == 124
    assert "timed out after 0.3s" in result.stderr
    assert marker.read_text(encoding="utf-8") == "yes"


def test_run_podman_exec_bounds_the_guest_inside_the_host_timeout() -> None:
    calls: list[tuple[list[str], float]] = []

    def fake_runner(command: list[str], timeout_sec: float) -> CommandResult:
        calls.append((command, timeout_sec))
        return CommandResult(command, 0, "running", "")

    result = run_podman_exec(
        "cnc-app-web",
        ["systemctl", "is-system-running", "--wait"],
        timeout_sec=5,
        command_runner=fake_runner,
    )

    assert result.ok
    assert calls == [
        (
            [
                "podman",
                "exec",
                "cnc-app-web",
                "timeout",
                "--signal=TERM",
                "--kill-after=0.1s",
                "4s",
                "systemctl",
                "is-system-running",
                "--wait",
            ],
            5,
        )
    ]
