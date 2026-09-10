from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
import tempfile
from pathlib import Path
from typing import Mapping

from app.config import Settings
from app.models.entities import Backend
from app.logger import get_logger
from app.services.commands import (
    CommandError,
    CommandResult,
    command_result_is_retryable,
    run_command,
    run_command_checked_async,
)
from app.services.backend_ssh_audit import BACKEND_SSH_INTERNAL_PORT
from app.services.file_locks import FileLock
from app.services.retry import retry_async_call


# Host account databases can be locked by apt, cloud-init, login tooling, or
# another admin action. Output creation should wait out that short contention
# instead of failing the whole save on the first useradd/usermod attempt.
SSH_ACCOUNT_MUTATION_MIN_ATTEMPTS = 7
SSH_ACCOUNT_MUTATION_LOCK_NAME = "ssh-account-mutation.lock"
SSH_BACKEND_WRAPPER_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "packaging" / "cnc-ssh-backend-root"
)
logger = get_logger("ssh_access")


@dataclass(frozen=True)
class BackendSshAccess:
    name: str
    public_key: str | None = None


def _account_mutation_lock_path(settings: Settings) -> Path:
    return (
        Path(settings.apply_lock_path)
        .expanduser()
        .with_name(SSH_ACCOUNT_MUTATION_LOCK_NAME)
    )


@asynccontextmanager
async def _host_account_mutation_lock(settings: Settings):
    lock_path = _account_mutation_lock_path(settings)
    lock = FileLock(lock_path, blocking=False, lock_path=lock_path)
    # Keep ownership on the event-loop thread. Separate to_thread calls can
    # reuse an owning worker and falsely appear reentrant, or abandon a waiter
    # on cancellation. Nonblocking attempts preserve the cross-process lock.
    while True:
        try:
            lock.__enter__()
            break
        except BlockingIOError:
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _write_file_atomic(path: Path, content: str, mode: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content and (path.stat().st_mode & 0o777) == mode:
            return False

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return True


def _snapshot_file(
    path: Path,
) -> tuple[bytes | None, int | None, int | None, int | None]:
    if not path.exists():
        return None, None, None, None
    stat = path.stat()
    return path.read_bytes(), stat.st_mode & 0o777, stat.st_uid, stat.st_gid


def _restore_file_snapshot(
    path: Path, snapshot: tuple[bytes | None, int | None, int | None, int | None]
) -> None:
    content, mode, uid, gid = snapshot
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.restore.", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode or 0o644)
        if uid is not None and gid is not None:
            os.chown(tmp_name, uid, gid)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _validate_sudoers_config(settings: Settings, content: str) -> None:
    settings.ssh_backend_sudoers_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{settings.ssh_backend_sudoers_path.name}.",
        dir=str(settings.ssh_backend_sudoers_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o440)
        result = run_command(["visudo", "-cf", tmp_name], timeout_sec=30)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "visudo -cf failed")
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _sync_authorized_keys(settings: Settings) -> bool:
    source = settings.ssh_backend_authorized_keys_source_path
    if not source.exists():
        return False
    content = source.read_text(encoding="utf-8")
    return _write_file_atomic(settings.ssh_backend_authorized_keys_path, content, 0o644)


def _coerce_backend_access(
    backends: list[str]
    | list[Backend]
    | list[BackendSshAccess]
    | list[Mapping[str, object]],
) -> list[BackendSshAccess]:
    access: list[BackendSshAccess] = []
    for item in backends:
        if isinstance(item, BackendSshAccess):
            access.append(item)
            continue
        if isinstance(item, Backend):
            access.append(
                BackendSshAccess(
                    name=str(item.name),
                    public_key=str(item.ssh_public_key or "").strip() or None,
                )
            )
            continue
        if isinstance(item, Mapping):
            access.append(
                BackendSshAccess(
                    name=str(item.get("name") or item.get("backend") or ""),
                    public_key=str(item.get("public_key") or "").strip() or None,
                )
            )
            continue
        access.append(BackendSshAccess(name=str(item)))
    return sorted(
        {entry.name: entry for entry in access if entry.name}.values(),
        key=lambda entry: entry.name,
    )


def _backend_authorized_keys_path(settings: Settings, backend: str) -> Path:
    return settings.ssh_backend_home_root / backend / ".ssh" / "authorized_keys"


async def _sync_backend_authorized_key(
    access: BackendSshAccess, settings: Settings
) -> bool:
    key_path = _backend_authorized_keys_path(settings, access.name)
    if not access.public_key:
        if key_path.exists():
            key_path.unlink()
            return True
        return False
    ssh_dir = key_path.parent
    ssh_dir.mkdir(parents=True, exist_ok=True)
    await run_command_checked_async(
        ["chown", f"{access.name}:{settings.ssh_backend_group}", str(ssh_dir)],
        timeout_sec=30,
    )
    await run_command_checked_async(["chmod", "0700", str(ssh_dir)], timeout_sec=30)
    changed = _write_file_atomic(key_path, f"{access.public_key.strip()}\n", 0o600)
    await run_command_checked_async(
        ["chown", f"{access.name}:{settings.ssh_backend_group}", str(key_path)],
        timeout_sec=30,
    )
    return changed


def backend_ssh_root_wrapper_script() -> str:
    template = SSH_BACKEND_WRAPPER_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("__CNC_ADMIN_PORT__", str(BACKEND_SSH_INTERNAL_PORT))


def reconcile_backend_ssh_root_wrapper(settings: Settings) -> bool:
    return _write_file_atomic(
        settings.ssh_backend_root_wrapper_path,
        backend_ssh_root_wrapper_script(),
        0o755,
    )


def _sshd_config(settings: Settings) -> str:
    authorized_keys = settings.ssh_backend_authorized_keys_path
    wrapper = settings.ssh_backend_root_wrapper_path
    group = settings.ssh_backend_group
    return (
        "# Managed by CNC\n"
        f"Match Group {group} Address 100.64.0.0/10,fd7a:115c:a1e0::/48\n"
        "    AuthenticationMethods publickey\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        "    PubkeyAuthentication yes\n"
        "    MaxAuthTries 10\n"
        f"    AuthorizedKeysFile {authorized_keys} .ssh/authorized_keys\n"
        "    PermitTTY yes\n"
        "    X11Forwarding no\n"
        "    AllowAgentForwarding no\n"
        "    AllowTcpForwarding no\n"
        "    PermitTunnel no\n"
        "    GatewayPorts no\n"
        "    PermitUserRC no\n"
        f"    ForceCommand /usr/bin/sudo -n {wrapper}\n"
        f"Match Group {group}\n"
        "    AuthenticationMethods publickey\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        "    PubkeyAuthentication no\n"
        "    AuthorizedKeysFile none\n"
        "    PermitTTY no\n"
        "    X11Forwarding no\n"
        "    AllowAgentForwarding no\n"
        "    AllowTcpForwarding no\n"
        "    PermitTunnel no\n"
        "    GatewayPorts no\n"
        "    PermitUserRC no\n"
        "    ForceCommand /bin/false\n"
    )


def _sudoers_config(settings: Settings) -> str:
    wrapper = settings.ssh_backend_root_wrapper_path
    group = settings.ssh_backend_group
    return (
        "# Managed by CNC\n"
        f"%{group} ALL=(root) NOPASSWD: {wrapper}\n"
        f'Defaults!{wrapper} env_keep += "SSH_ORIGINAL_COMMAND SSH_CONNECTION"\n'
        f"Defaults!{wrapper} !requiretty\n"
    )


def _existing_backend_ssh_users(settings: Settings) -> set[str]:
    home_prefix = str(settings.ssh_backend_home_root)
    result = run_command(["getent", "passwd"], timeout_sec=10)
    if not result.ok:
        return set()
    users: set[str] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 7:
            continue
        username, _password, _uid, gid, gecos, home, _shell = parts[:7]
        if gecos != "CNC backend SSH user":
            continue
        if not home.startswith(home_prefix):
            continue
        if gid and gid.isdigit():
            users.add(username)
    return users


def _passwd_entry(username: str) -> list[str] | None:
    result = run_command(["getent", "passwd", username], timeout_sec=10)
    if not result.ok or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(":")
    return parts if len(parts) >= 7 else None


def _group_entry(group: str) -> list[str] | None:
    result = run_command(["getent", "group", group], timeout_sec=10)
    if not result.ok or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(":")
    return parts if len(parts) >= 4 else None


def _password_locked(username: str) -> bool:
    result = run_command(["passwd", "-S", username], timeout_sec=10)
    if not result.ok or not result.stdout.strip():
        return False
    parts = result.stdout.strip().split()
    return len(parts) >= 2 and parts[1].upper() == "L"


def _backend_user_exists(username: str) -> bool:
    return _passwd_entry(username) is not None


async def _ensure_group(settings: Settings) -> None:
    group = settings.ssh_backend_group
    result = run_command(["getent", "group", group], timeout_sec=10)
    if result.ok:
        return None
    await _run_retryable_account_command(
        ["groupadd", "--system", group], settings=settings
    )
    return None


async def _ensure_group_created(settings: Settings) -> bool:
    group = settings.ssh_backend_group
    result = run_command(["getent", "group", group], timeout_sec=10)
    if result.ok:
        return False
    await _run_retryable_account_command(
        ["groupadd", "--system", group], settings=settings
    )
    return True


async def _run_retryable_account_command(
    command: list[str],
    *,
    settings: Settings,
    timeout_sec: int = 30,
) -> CommandResult:
    attempts = max(
        SSH_ACCOUNT_MUTATION_MIN_ATTEMPTS, settings.transient_command_retry_attempts + 1
    )

    def should_retry(exc: Exception) -> bool:
        return isinstance(exc, CommandError) and command_result_is_retryable(exc.result)

    return await retry_async_call(
        lambda: run_command_checked_async(command, timeout_sec=timeout_sec),
        attempts=attempts,
        backoff_sec=settings.transient_command_retry_backoff_sec,
        should_retry=should_retry,
    )


async def _ensure_backend_user(backend: str, settings: Settings) -> None:
    group = settings.ssh_backend_group
    home_dir = settings.ssh_backend_home_root / backend
    group_entry = _group_entry(group)
    desired_gid = group_entry[2] if group_entry and len(group_entry) >= 3 else None
    entry = _passwd_entry(backend)
    should_lock_password = True
    if entry is not None:
        gecos = entry[4]
        gid = entry[3]
        home = entry[5]
        shell = entry[6]
        if gecos != "CNC backend SSH user" or home != str(home_dir):
            raise RuntimeError(
                f"backend ssh user collides with existing host account: {backend}"
            )
        # Existing managed backend users should be a no-op during apply unless
        # host account drift actually needs correction.
        if gid != desired_gid or shell != "/bin/bash":
            await _run_retryable_account_command(
                [
                    "usermod",
                    "--comment",
                    "CNC backend SSH user",
                    "--gid",
                    group,
                    "--home",
                    str(home_dir),
                    "--shell",
                    "/bin/bash",
                    backend,
                ],
                settings=settings,
            )
        should_lock_password = not _password_locked(backend)
    else:
        await _run_retryable_account_command(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--comment",
                "CNC backend SSH user",
                "--gid",
                group,
                "--home-dir",
                str(home_dir),
                "--shell",
                "/bin/bash",
                backend,
            ],
            settings=settings,
        )

    if should_lock_password:
        await _run_retryable_account_command(
            ["passwd", "-l", backend], settings=settings
        )
    home_dir.mkdir(parents=True, exist_ok=True)
    await run_command_checked_async(
        ["chown", f"{backend}:{group}", str(home_dir)], timeout_sec=30
    )
    await run_command_checked_async(["chmod", "0750", str(home_dir)], timeout_sec=30)


async def _remove_backend_user(backend: str) -> None:
    await run_command_checked_async(["userdel", backend], timeout_sec=30)


async def _reload_sshd() -> None:
    syntax_ok = run_command(["sshd", "-t"], timeout_sec=30)
    if not syntax_ok.ok:
        raise RuntimeError(syntax_ok.stderr or "sshd -t failed")

    for service_name in ("ssh", "sshd"):
        result = run_command(["systemctl", "reload", service_name], timeout_sec=30)
        if result.ok:
            return
    raise RuntimeError("unable to reload ssh service")


async def reconcile_backend_ssh_access(
    backends: list[str]
    | list[Backend]
    | list[BackendSshAccess]
    | list[Mapping[str, object]],
    settings: Settings,
) -> dict[str, object]:
    async with _host_account_mutation_lock(settings):
        desired_access = _coerce_backend_access(backends)
        desired = [entry.name for entry in desired_access]
        changed = False
        created_group = False
        created_users: list[str] = []
        stale_user_remove_errors: list[dict[str, str]] = []
        managed_paths = (
            settings.ssh_backend_authorized_keys_path,
            settings.ssh_backend_root_wrapper_path,
            settings.ssh_backend_sshd_config_path,
            settings.ssh_backend_sudoers_path,
            *(_backend_authorized_keys_path(settings, name) for name in desired),
        )
        previous_files = {path: _snapshot_file(path) for path in managed_paths}
        sudoers_content = _sudoers_config(settings)
        _validate_sudoers_config(settings, sudoers_content)

        try:
            changed = _sync_authorized_keys(settings) or changed
            changed = reconcile_backend_ssh_root_wrapper(settings) or changed
            changed = (
                _write_file_atomic(
                    settings.ssh_backend_sshd_config_path, _sshd_config(settings), 0o644
                )
                or changed
            )
            changed = (
                _write_file_atomic(
                    settings.ssh_backend_sudoers_path, sudoers_content, 0o440
                )
                or changed
            )

            created_group = await _ensure_group_created(settings)
            settings.ssh_backend_home_root.mkdir(parents=True, exist_ok=True)

            existing = _existing_backend_ssh_users(settings)
            for access in desired_access:
                existed_before = _backend_user_exists(access.name)
                await _ensure_backend_user(access.name, settings)
                if not existed_before and _backend_user_exists(access.name):
                    created_users.append(access.name)
                changed = (
                    await _sync_backend_authorized_key(access, settings) or changed
                )
                if access.name not in existing:
                    changed = True

            if changed:
                await _reload_sshd()

            for backend in sorted(existing - set(desired)):
                try:
                    await _remove_backend_user(backend)
                    changed = True
                except Exception as exc:
                    logger.warning(
                        "ssh_access.stale_user_remove_failed",
                        backend=backend,
                        error=str(exc),
                    )
                    stale_user_remove_errors.append(
                        {"backend": backend, "error": str(exc)}
                    )
        except Exception:
            for path, snapshot in previous_files.items():
                _restore_file_snapshot(path, snapshot)
            for backend in reversed(created_users):
                try:
                    await _remove_backend_user(backend)
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_access.created_user_rollback_failed",
                        backend=backend,
                        error=str(cleanup_exc),
                    )
            if created_group:
                try:
                    await _run_retryable_account_command(
                        ["groupdel", settings.ssh_backend_group],
                        settings=settings,
                    )
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_access.created_group_rollback_failed",
                        group=settings.ssh_backend_group,
                        error=str(cleanup_exc),
                    )
            raise

        result: dict[str, object] = {
            "ssh_backends": desired,
            "ssh_aliases_enabled": bool(desired),
        }
        if stale_user_remove_errors:
            result["stale_user_remove_errors"] = stale_user_remove_errors
        return result


async def remove_backend_ssh_access(
    backends: list[str], settings: Settings
) -> dict[str, object]:
    async with _host_account_mutation_lock(settings):
        removed: list[str] = []
        existing = _existing_backend_ssh_users(settings)
        for backend in sorted(set(backends)):
            if backend not in existing:
                continue
            await _remove_backend_user(backend)
            removed.append(backend)
        return {
            "ssh_backends_removed": removed,
            "ssh_aliases_removed": bool(removed),
        }
