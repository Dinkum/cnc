import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from app.config import Settings
from app.services.commands import CommandError, CommandResult
from app.services import ssh_access


def _run_wrapper(
    tmp_path: Path,
    wrapper: str,
    *,
    original_command: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    wrapper_path = tmp_path / "cnc-ssh-backend-root"
    wrapper_path.write_text(wrapper, encoding="utf-8")
    wrapper_path.chmod(0o755)
    env = {
        **os.environ,
        "SUDO_USER": "worker",
        "SSH_CONNECTION": "100.64.0.12 54321 100.64.0.2 22",
        "SSH_ORIGINAL_COMMAND": original_command,
        **(extra_env or {}),
    }
    return subprocess.run(
        ["/bin/bash", str(wrapper_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _executable(path: Path, body: str) -> None:
    path.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_backend_ssh_command_has_one_direct_podman_operation(tmp_path: Path) -> None:
    podman = tmp_path / "podman"
    args_file = tmp_path / "podman.args"
    _executable(podman, 'printf "%s\\n" "$@" >"${PODMAN_ARGS_FILE}"')
    wrapper = ssh_access.backend_ssh_root_wrapper_script().replace(
        "/usr/bin/podman",
        str(podman),
    )

    result = _run_wrapper(
        tmp_path,
        wrapper,
        original_command="python3 --version",
        extra_env={"PODMAN_ARGS_FILE": str(args_file)},
    )

    assert result.returncode == 0
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args == [
        "exec",
        "-i",
        "cnc-app-worker",
        "/bin/bash",
        "-lc",
        "python3 --version",
    ]
    assert "container" not in args


def test_backend_ssh_llm_help_never_calls_podman(tmp_path: Path) -> None:
    curl = tmp_path / "curl"
    podman = tmp_path / "podman"
    header = tmp_path / "backend-ssh.header"
    curl_args = tmp_path / "curl.args"
    podman_called = tmp_path / "podman.called"
    header.write_text("X-CNC-Internal-Token: runtime-secret\n", encoding="utf-8")
    header.chmod(0o600)
    _executable(
        curl,
        'printf "%s\\n" "$@" >"${CURL_ARGS_FILE}"\nprintf "served by cnc\\n"',
    )
    _executable(podman, 'touch "${PODMAN_CALLED_FILE}"')
    wrapper = (
        ssh_access.backend_ssh_root_wrapper_script()
        .replace("/usr/bin/curl", str(curl))
        .replace("/usr/bin/podman", str(podman))
        .replace("/run/cnc/backend-ssh.header", str(header))
    )

    result = _run_wrapper(
        tmp_path,
        wrapper,
        original_command="llm-help",
        extra_env={
            "CURL_ARGS_FILE": str(curl_args),
            "PODMAN_CALLED_FILE": str(podman_called),
        },
    )

    assert result.returncode == 0
    assert result.stdout == "served by cnc\n"
    assert podman_called.exists() is False
    args = curl_args.read_text(encoding="utf-8")
    assert "--max-time\n2\n" in args
    assert "--header\n@" in args
    assert "/internal/backend-ssh/llm-help/worker" in args


def test_backend_ssh_llm_help_fails_once_with_actionable_fallback(
    tmp_path: Path,
) -> None:
    curl = tmp_path / "curl"
    podman = tmp_path / "podman"
    header = tmp_path / "backend-ssh.header"
    podman_called = tmp_path / "podman.called"
    header.write_text("X-CNC-Internal-Token: runtime-secret\n", encoding="utf-8")
    header.chmod(0o600)
    _executable(curl, "exit 28")
    _executable(podman, 'touch "${PODMAN_CALLED_FILE}"')
    wrapper = (
        ssh_access.backend_ssh_root_wrapper_script()
        .replace("/usr/bin/curl", str(curl))
        .replace("/usr/bin/podman", str(podman))
        .replace("/run/cnc/backend-ssh.header", str(header))
    )

    result = _run_wrapper(
        tmp_path,
        wrapper,
        original_command="llm-help",
        extra_env={"PODMAN_CALLED_FILE": str(podman_called)},
    )

    assert result.returncode == 75
    assert "bounded 2-second attempt" in result.stderr
    assert "does not prove the app sandbox is down" in result.stderr
    assert "cnc-admin app doctor worker" in result.stderr
    assert podman_called.exists() is False


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_writes_assets_and_user_commands(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        ssh_backend_authorized_keys_path=tmp_path
        / "ssh"
        / "cnc-backend-authorized_keys",
        ssh_backend_authorized_keys_source_path=tmp_path
        / "root"
        / ".ssh"
        / "authorized_keys",
        apply_lock_path=tmp_path / "apply.lock",
    )
    settings.ssh_backend_authorized_keys_source_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    settings.ssh_backend_authorized_keys_source_path.write_text(
        "ssh-ed25519 AAAATEST root@test\n", encoding="utf-8"
    )
    checked: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 2, "", "missing")
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(command, 2, "", "missing")
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(
        [ssh_access.BackendSshAccess("web", "ssh-ed25519 AAAAOUTPUT cnc-web")],
        settings,
    )

    assert details == {"ssh_backends": ["web"], "ssh_aliases_enabled": True}
    assert settings.ssh_backend_root_wrapper_path.exists()
    assert settings.ssh_backend_sshd_config_path.exists()
    assert settings.ssh_backend_sudoers_path.exists()
    assert (
        settings.ssh_backend_authorized_keys_path.read_text(encoding="utf-8")
        == "ssh-ed25519 AAAATEST root@test\n"
    )
    user_authorized_keys = (
        settings.ssh_backend_home_root / "web" / ".ssh" / "authorized_keys"
    )
    assert (
        user_authorized_keys.read_text(encoding="utf-8")
        == "ssh-ed25519 AAAAOUTPUT cnc-web\n"
    )
    wrapper = settings.ssh_backend_root_wrapper_path.read_text(encoding="utf-8")
    assert 'podman container exists "${container}"' not in wrapper
    assert 'if [[ -n "${SSH_ORIGINAL_COMMAND:-}" || -t 0 ]]; then' in wrapper
    assert (
        'read -r ssh_client _ssh_client_port banner_host _ssh_server_port <<< "${SSH_CONNECTION}"'
        in wrapper
    )
    assert 'if [[ "${original_args[0]:-}" == "llm-help" ]]; then' in wrapper
    assert "CNCSSH1" in wrapper
    assert '>"/dev/udp/127.0.0.1/9090"' in wrapper
    assert "backend-ssh-audit" not in wrapper
    assert "audit_event handoff" in wrapper
    assert 'audit_event completed "" 0 "${duration_ms}"' in wrapper
    assert "audit_event failed help_unavailable" in wrapper
    assert "--max-time 2" in wrapper
    assert "--header @/run/cnc/backend-ssh.header" in wrapper
    assert (
        'exec /usr/bin/podman exec "${exec_args[@]}" "${container}" /bin/bash -lc "${SSH_ORIGINAL_COMMAND}"'
        in wrapper
    )
    assert '--env "CNC_SSH_BACKEND=${backend}"' in wrapper
    assert '--env "CNC_SSH_BANNER_HOST=${banner_host}"' in wrapper
    assert (
        "CNC app shell for %s. Refresh backend guidance with: ssh %s@%s llm-help"
        in wrapper
    )
    assert "if [[ -t 0 && -t 1 ]]; then" in wrapper
    assert (
        'exec /usr/bin/podman exec "${exec_args[@]}" "${container}" /bin/bash'
        in wrapper
    )
    sudoers = settings.ssh_backend_sudoers_path.read_text(encoding="utf-8")
    assert 'env_keep += "SSH_ORIGINAL_COMMAND SSH_CONNECTION"' in sudoers
    sshd_config = settings.ssh_backend_sshd_config_path.read_text(encoding="utf-8")
    assert (
        "Match Group cnc-backends Address 100.64.0.0/10,fd7a:115c:a1e0::/48"
        in sshd_config
    )
    assert (
        f"AuthorizedKeysFile {settings.ssh_backend_authorized_keys_path} .ssh/authorized_keys"
        in sshd_config
    )
    assert "Match Group cnc-backends\n" in sshd_config
    assert "PubkeyAuthentication no" in sshd_config
    assert "AuthorizedKeysFile none" in sshd_config
    assert "MaxAuthTries 10" in sshd_config
    assert checked[:4] == [
        ["groupadd", "--system", "cnc-backends"],
        [
            "useradd",
            "--system",
            "--no-create-home",
            "--comment",
            "CNC backend SSH user",
            "--gid",
            "cnc-backends",
            "--home-dir",
            str(settings.ssh_backend_home_root / "web"),
            "--shell",
            "/bin/bash",
            "web",
        ],
        ["passwd", "-l", "web"],
        ["chown", "web:cnc-backends", str(settings.ssh_backend_home_root / "web")],
    ]


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_retries_transient_account_lock(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
        apply_lock_path=tmp_path / "apply.lock",
    )
    checked: list[list[str]] = []
    useradd_attempts = 0

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(command, 2, "", "missing")
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal useradd_attempts
        checked.append(command)
        if command[:1] == ["useradd"]:
            useradd_attempts += 1
            if useradd_attempts == 1:
                raise CommandError(
                    CommandResult(
                        command,
                        1,
                        "",
                        "useradd: cannot lock /etc/passwd; try again later.",
                    )
                )
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(["web-batch"], settings)

    assert details == {"ssh_backends": ["web-batch"], "ssh_aliases_enabled": True}
    assert useradd_attempts == 2
    assert [command[0] for command in checked[:3]] == ["useradd", "useradd", "passwd"]


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_waits_out_longer_passwd_lock(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
        apply_lock_path=tmp_path / "apply.lock",
    )
    useradd_attempts = 0

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(command, 2, "", "missing")
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal useradd_attempts
        if command[:1] == ["useradd"]:
            useradd_attempts += 1
            if useradd_attempts < ssh_access.SSH_ACCOUNT_MUTATION_MIN_ATTEMPTS:
                raise CommandError(
                    CommandResult(
                        command,
                        1,
                        "",
                        "useradd: cannot lock /etc/passwd; try again later.",
                    )
                )
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(["web-batch"], settings)

    assert details == {"ssh_backends": ["web-batch"], "ssh_aliases_enabled": True}
    assert useradd_attempts == ssh_access.SSH_ACCOUNT_MUTATION_MIN_ATTEMPTS


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_serializes_account_reconcile(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        apply_lock_path=tmp_path / "apply.lock",
    )
    group_exists = False
    created_users: set[str] = set()
    groupadd_count = 0

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            if group_exists:
                return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
            return CommandResult(command, 2, "", "missing")
        if (
            command[:3] == ["getent", "passwd", "web-batch"]
            and "web-batch" in created_users
        ):
            return CommandResult(
                command,
                0,
                f"web-batch:x:1001:999:CNC backend SSH user:{settings.ssh_backend_home_root / 'web-batch'}:/bin/bash\n",
                "",
            )
        if command[:2] == ["getent", "passwd"]:
            rows = [
                f"{name}:x:1001:999:CNC backend SSH user:{settings.ssh_backend_home_root / name}:/bin/bash"
                for name in sorted(created_users)
            ]
            return CommandResult(
                command, 0 if rows else 2, "\n".join(rows), "" if rows else "missing"
            )
        if command[:2] == ["passwd", "-S"]:
            return CommandResult(
                command, 0, f"{command[-1]} L 2026-03-30 -1 -1 -1 -1\n", ""
            )
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal group_exists, groupadd_count
        if command[:1] == ["groupadd"]:
            groupadd_count += 1
            await asyncio.sleep(0.01)
            if group_exists:
                raise CommandError(
                    CommandResult(command, 9, "", "group already exists")
                )
            group_exists = True
        if command[:1] == ["useradd"]:
            created_users.add(command[-1])
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    first, second = await asyncio.gather(
        ssh_access.reconcile_backend_ssh_access(["web-batch"], settings),
        ssh_access.reconcile_backend_ssh_access(["web-batch"], settings),
    )

    assert first == {"ssh_backends": ["web-batch"], "ssh_aliases_enabled": True}
    assert second == {"ssh_backends": ["web-batch"], "ssh_aliases_enabled": True}
    assert groupadd_count == 1
    assert created_users == {"web-batch"}


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_rejects_existing_host_user_collision(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        apply_lock_path=tmp_path / "apply.lock",
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:3] == ["getent", "passwd", "web"]:
            return CommandResult(
                command,
                0,
                "web:x:1001:1001:real host user:/home/web:/bin/bash\n",
                "",
            )
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        raise AssertionError(f"unexpected checked command: {command}")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    with pytest.raises(RuntimeError, match="collides with existing host account"):
        await ssh_access.reconcile_backend_ssh_access(["web"], settings)


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_skips_account_mutation_for_existing_locked_backend_user(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        ssh_backend_authorized_keys_path=tmp_path
        / "ssh"
        / "cnc-backend-authorized_keys",
        ssh_backend_authorized_keys_source_path=tmp_path
        / "root"
        / ".ssh"
        / "authorized_keys",
        apply_lock_path=tmp_path / "apply.lock",
    )
    settings.ssh_backend_authorized_keys_source_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    settings.ssh_backend_authorized_keys_source_path.write_text(
        "ssh-ed25519 AAAATEST root@test\n", encoding="utf-8"
    )
    (settings.ssh_backend_home_root / "web").mkdir(parents=True, exist_ok=True)
    checked: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:3] == ["getent", "passwd", "web"]:
            return CommandResult(
                command,
                0,
                f"web:x:1001:999:CNC backend SSH user:{settings.ssh_backend_home_root / 'web'}:/bin/bash\n",
                "",
            )
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(
                command,
                0,
                "web:x:1001:999:CNC backend SSH user:/unused:/bin/bash\n",
                "",
            )
        if command[:2] == ["passwd", "-S"]:
            return CommandResult(command, 0, "web L 2026-03-30 -1 -1 -1 -1\n", "")
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(["web"], settings)

    assert details == {"ssh_backends": ["web"], "ssh_aliases_enabled": True}
    assert checked == [
        ["chown", "web:cnc-backends", str(settings.ssh_backend_home_root / "web")],
        ["chmod", "0750", str(settings.ssh_backend_home_root / "web")],
    ]


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_reclaims_stale_home_dir_ownership(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        ssh_backend_authorized_keys_path=tmp_path
        / "ssh"
        / "cnc-backend-authorized_keys",
        ssh_backend_authorized_keys_source_path=tmp_path
        / "root"
        / ".ssh"
        / "authorized_keys",
        apply_lock_path=tmp_path / "apply.lock",
    )
    settings.ssh_backend_authorized_keys_source_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    settings.ssh_backend_authorized_keys_source_path.write_text(
        "ssh-ed25519 AAAATEST root@test\n", encoding="utf-8"
    )
    home_dir = settings.ssh_backend_home_root / "web"
    home_dir.mkdir(parents=True, exist_ok=True)
    checked: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:3] == ["getent", "passwd", "web"]:
            return CommandResult(
                command,
                0,
                f"web:x:1001:999:CNC backend SSH user:{home_dir}:/bin/bash\n",
                "",
            )
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(
                command,
                0,
                "web:x:1001:999:CNC backend SSH user:/unused:/bin/bash\n",
                "",
            )
        if command[:2] == ["passwd", "-S"]:
            return CommandResult(command, 0, "web L 2026-03-30 -1 -1 -1 -1\n", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(["web"], settings)

    assert details == {"ssh_backends": ["web"], "ssh_aliases_enabled": True}
    assert ["chown", "web:cnc-backends", str(home_dir)] in checked
    assert ["chmod", "0750", str(home_dir)] in checked


@pytest.mark.asyncio
async def test_reconcile_backend_ssh_access_relocks_existing_backend_user_when_needed(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_group="cnc-backends",
        ssh_backend_home_root=tmp_path / "ssh-users",
        ssh_backend_root_wrapper_path=tmp_path / "bin" / "cnc-ssh-backend-root",
        ssh_backend_sshd_config_path=tmp_path / "ssh" / "cnc-backend-users.conf",
        ssh_backend_sudoers_path=tmp_path / "sudoers" / "cnc-backend-users",
        ssh_backend_authorized_keys_path=tmp_path
        / "ssh"
        / "cnc-backend-authorized_keys",
        ssh_backend_authorized_keys_source_path=tmp_path
        / "root"
        / ".ssh"
        / "authorized_keys",
        apply_lock_path=tmp_path / "apply.lock",
    )
    settings.ssh_backend_authorized_keys_source_path.parent.mkdir(
        parents=True, exist_ok=True
    )
    settings.ssh_backend_authorized_keys_source_path.write_text(
        "ssh-ed25519 AAAATEST root@test\n", encoding="utf-8"
    )
    (settings.ssh_backend_home_root / "web").mkdir(parents=True, exist_ok=True)
    checked: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:3] == ["getent", "group", "cnc-backends"]:
            return CommandResult(command, 0, "cnc-backends:x:999:\n", "")
        if command[:3] == ["getent", "passwd", "web"]:
            return CommandResult(
                command,
                0,
                f"web:x:1001:999:CNC backend SSH user:{settings.ssh_backend_home_root / 'web'}:/bin/bash\n",
                "",
            )
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(
                command,
                0,
                "web:x:1001:999:CNC backend SSH user:/unused:/bin/bash\n",
                "",
            )
        if command[:2] == ["passwd", "-S"]:
            return CommandResult(command, 0, "web P 2026-03-30 -1 -1 -1 -1\n", "")
        if command[:2] == ["systemctl", "reload"] and command[-1] == "ssh":
            return CommandResult(command, 0, "", "")
        if command[:2] == ["sshd", "-t"]:
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.reconcile_backend_ssh_access(["web"], settings)

    assert details == {"ssh_backends": ["web"], "ssh_aliases_enabled": True}
    assert checked == [
        ["passwd", "-l", "web"],
        ["chown", "web:cnc-backends", str(settings.ssh_backend_home_root / "web")],
        ["chmod", "0750", str(settings.ssh_backend_home_root / "web")],
    ]


@pytest.mark.asyncio
async def test_remove_backend_ssh_access_only_removes_requested_managed_users(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        ssh_backend_home_root=tmp_path / "ssh-users",
        apply_lock_path=tmp_path / "apply.lock",
    )
    checked: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if command[:2] == ["getent", "passwd"]:
            return CommandResult(
                command,
                0,
                (
                    f"web:x:1001:999:CNC backend SSH user:{settings.ssh_backend_home_root / 'web'}:/bin/bash\n"
                    f"ghost:x:1002:999:CNC backend SSH user:{settings.ssh_backend_home_root / 'ghost'}:/bin/bash\n"
                ),
                "",
            )
        return CommandResult(command, 0, "", "")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(ssh_access, "run_command", fake_run)
    monkeypatch.setattr(ssh_access, "run_command_checked_async", fake_run_checked)

    details = await ssh_access.remove_backend_ssh_access(["web", "missing"], settings)

    assert details == {"ssh_backends_removed": ["web"], "ssh_aliases_removed": True}
    assert checked == [["userdel", "web"]]
