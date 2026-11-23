from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
from typing import Any, Callable

from app.config import Settings
from app.services.commands import (
    CommandResult,
    command_result_is_retryable,
    run_command,
)
from app.services.ssh_options import SSH_BATCH_OPTIONS

NODE_EXEC_PATH = "/usr/local/bin/cnc-node-exec"
NODE_EXEC_SOURCE = Path(__file__).resolve().parents[2] / "packaging" / "cnc-node-exec"


class RemoteNodeUnavailable(RuntimeError):
    def __init__(self, node_uid: str, node_ip: str, result: CommandResult) -> None:
        message = result.stderr or result.stdout or "follower unavailable"
        super().__init__(message.strip())
        self.node_uid = node_uid
        self.node_ip = node_ip
        self.result = result


def node_exec_bytes() -> bytes:
    return NODE_EXEC_SOURCE.read_bytes()


class RemoteNodeExecutor:
    """Single SSH/SCP and JSON entrypoint boundary for follower node operations."""

    def __init__(
        self,
        node_uid: str,
        node_ip: str,
        settings: Settings,
        *,
        run_command_fn: Callable[[list[str], int], CommandResult] | None = None,
    ) -> None:
        self.node_uid = node_uid
        self.node_ip = node_ip
        self.settings = settings
        self._run_command = run_command_fn or run_command

    def _run_checked(
        self,
        command: list[str],
        *,
        timeout_sec: int | None = None,
        retryable_as_unavailable: bool = False,
    ) -> str:
        timeout = (
            self.settings.command_timeout_apply_sec
            if timeout_sec is None
            else timeout_sec
        )
        result = self._run_command(command, timeout)
        if result.ok:
            return result.stdout
        if retryable_as_unavailable and command_result_is_retryable(result):
            raise RemoteNodeUnavailable(self.node_uid, self.node_ip, result)
        detail = result.stderr or result.stdout or "command failed"
        raise RuntimeError(detail.strip())

    def ssh(
        self,
        script: str,
        *,
        timeout_sec: int | None = None,
        retryable_as_unavailable: bool = False,
    ) -> str:
        return self._run_checked(
            [
                "ssh",
                *SSH_BATCH_OPTIONS,
                f"root@{self.node_ip}",
                f"bash -lc {shlex.quote(script)}",
            ],
            timeout_sec=timeout_sec,
            retryable_as_unavailable=retryable_as_unavailable,
        )

    def scp_upload(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        timeout_sec: int | None = None,
        retryable_as_unavailable: bool = False,
    ) -> None:
        self._run_checked(
            [
                "scp",
                *SSH_BATCH_OPTIONS,
                str(local_path),
                f"root@{self.node_ip}:{remote_path}",
            ],
            timeout_sec=timeout_sec,
            retryable_as_unavailable=retryable_as_unavailable,
        )

    def scp_download(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout_sec: int | None = None,
        retryable_as_unavailable: bool = False,
    ) -> None:
        self._run_checked(
            [
                "scp",
                *SSH_BATCH_OPTIONS,
                f"root@{self.node_ip}:{remote_path}",
                str(local_path),
            ],
            timeout_sec=timeout_sec,
            retryable_as_unavailable=retryable_as_unavailable,
        )

    def ensure_node_exec(self) -> None:
        encoded = base64.b64encode(node_exec_bytes()).decode("ascii")
        script = f"""set -Eeuo pipefail
python3 - <<'CNC_NODE_EXEC_SYNC'
import base64, os
from pathlib import Path
path = Path({NODE_EXEC_PATH!r})
if path.is_symlink():
    raise RuntimeError('cnc-node-exec cannot be a symlink')
raw = base64.b64decode({encoded!r})
if path.is_file() and path.read_bytes() == raw:
    raise SystemExit(0)
tmp = path.with_name('.cnc-node-exec.tmp')
tmp.write_bytes(raw)
tmp.chmod(0o755)
os.replace(tmp, path)
CNC_NODE_EXEC_SYNC"""
        self.ssh(script)

    def invoke(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        sync_entrypoint: bool = True,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        if sync_entrypoint:
            self.ensure_node_exec()
        encoded_payload = base64.b64encode(
            json.dumps({"action": action, **payload}, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        script = (
            f"printf %s {shlex.quote(encoded_payload)} | base64 -d | "
            f"python3 {shlex.quote(NODE_EXEC_PATH)}"
        )
        timeout = (
            self.settings.command_timeout_apply_sec
            if timeout_sec is None
            else timeout_sec
        )
        result = self._run_command(
            [
                "ssh",
                *SSH_BATCH_OPTIONS,
                f"root@{self.node_ip}",
                f"bash -lc {shlex.quote(script)}",
            ],
            timeout,
        )
        parsed: dict[str, Any] | None = None
        try:
            candidate = json.loads((result.stdout or "").strip() or "{}")
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None
        if result.ok and parsed is not None and parsed.get("ok") is not False:
            return parsed
        if parsed and parsed.get("error"):
            raise RuntimeError(str(parsed["error"]))
        detail = result.stderr or result.stdout or f"remote action {action} failed"
        raise RuntimeError(detail.strip())
