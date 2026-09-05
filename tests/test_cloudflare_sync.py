from __future__ import annotations

import errno
import json
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, ClusterNode, Input, Operation, UpdateRun
from app.services import cloudflare_sync
from app.services import host_state
from app.services import managed_env as managed_env_service
from app.services import self_audit
from app.services.commands import CommandResult


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _DummyApplyResponse:
    def __init__(self, *, status: str, run_id: int = 7) -> None:
        self.status = status
        self._run_id = run_id

    def model_dump_json(self) -> str:
        return (
            '{"status":"%s","message":"apply %s","details":{},"run_id":%d,"created_at":null}'
            % (
                self.status,
                "completed" if self.status == "success" else "failed",
                self._run_id,
            )
        )


def _preview_payload(
    *, cidrs: tuple[str, ...], checked: bool = True
) -> dict[str, object]:
    return {
        "checked": checked,
        "cidrs": list(cidrs),
        "nginx": {"checked": checked, "cidr_count": len(cidrs)},
        "firewall": {"checked": checked, "cidr_count": len(cidrs)},
        "nginx_files": ["cnc-host-example-com.conf"],
    }


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_write_cloudflare_sync_state_serializes_concurrent_writers(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(host_state_path=tmp_path / "host-state.json")
    active_reads = 0
    max_active_reads = 0
    counter_lock = threading.Lock()
    original = host_state.read_host_state_unlocked

    def wrapped(path: Path) -> dict[str, object]:
        nonlocal active_reads, max_active_reads
        with counter_lock:
            active_reads += 1
            max_active_reads = max(max_active_reads, active_reads)
        time.sleep(0.05)
        try:
            return original(path)
        finally:
            with counter_lock:
                active_reads -= 1

    monkeypatch.setattr(host_state, "read_host_state_unlocked", wrapped)
    barrier = threading.Barrier(2)

    def worker(kind: str) -> None:
        barrier.wait()
        cloudflare_sync.write_cloudflare_sync_state(
            settings, {"ok": True, "kind": kind}
        )

    first = threading.Thread(target=worker, args=("first",))
    second = threading.Thread(target=worker, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert max_active_reads == 1
    stored = cloudflare_sync.read_cloudflare_sync_state(settings)
    assert stored["ok"] is True
    assert stored["kind"] in {"first", "second"}


def test_fetch_cloudflare_cidrs_parses_v4_and_v6_feeds() -> None:
    settings = Settings()

    def fake_urlopen(request, timeout: int):
        if request.full_url.endswith("/ips-v4"):
            return _FakeResponse("173.245.48.0/20\n103.21.244.0/22\n")
        if request.full_url.endswith("/ips-v6"):
            return _FakeResponse("2400:cb00::/32\n2606:4700::/32\n")
        raise AssertionError(f"unexpected URL: {request.full_url}")

    cidrs = cloudflare_sync.fetch_cloudflare_cidrs(settings, urlopen_func=fake_urlopen)

    assert cidrs == (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
    )


def test_fetch_cloudflare_cidrs_retries_with_backoff(monkeypatch) -> None:
    settings = Settings(
        cloudflare_sync_fetch_retries=2,
        cloudflare_sync_fetch_retry_backoff_sec=0.5,
    )
    attempts: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout: int):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise OSError("temporary network failure")
        if request.full_url.endswith("/ips-v4"):
            return _FakeResponse("173.245.48.0/20\n")
        if request.full_url.endswith("/ips-v6"):
            return _FakeResponse("2400:cb00::/32\n")
        raise AssertionError(f"unexpected URL: {request.full_url}")

    cidrs = cloudflare_sync.fetch_cloudflare_cidrs(
        settings,
        urlopen_func=fake_urlopen,
        sleep_func=sleeps.append,
    )

    assert cidrs == (
        "173.245.48.0/20",
        "2400:cb00::/32",
    )
    assert sleeps == [0.5, 1.0]


def test_last_known_good_cidrs_falls_back_to_sync_state_cache(tmp_path: Path) -> None:
    state_path = tmp_path / "host-state.json"
    state_path.write_text(
        json.dumps(
            {
                "cloudflare_sync": {
                    "last_successful_cidrs": ["173.245.48.0/20", "103.21.244.0/22"],
                    "last_successful_fetch_at": "2026-04-19T12:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        nginx_cloudflare_only=False,
        host_state_path=state_path,
    ).model_copy(update={"nginx_cloudflare_ips": ""})

    cidrs = cloudflare_sync._last_known_good_cidrs(settings)

    assert cidrs == ("173.245.48.0/20", "103.21.244.0/22")


def test_reconcile_cloudflare_http_firewall_rewrites_rule_files(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### tuple ### allow tcp 443 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 443 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    status_payloads = [
        "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n[ 2] 80/tcp ALLOW IN Anywhere\n[ 3] 443/tcp ALLOW IN Anywhere\n",
    ]
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(command, 0, status_payloads.pop(0), "")
        if command == ["ufw", "reload"]:
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_live_cloudflare_http_firewall",
        lambda *_args, **_kwargs: {
            "checked": True,
            "cidr_count": 2,
            "expected_rule_count": 4,
            "live_rule_count": 4,
            "ipv6_enabled": True,
            "ipv6_chain_present": True,
        },
    )

    payload = cloudflare_sync.reconcile_cloudflare_http_firewall(
        settings,
        cidrs=("173.245.48.0/20", "2400:cb00::/32"),
    )

    assert payload["skipped"] is False
    assert payload["mode"] == "file_rewrite"
    assert payload["live_rule_count"] == 4
    updated_v4 = user_rules.read_text(encoding="utf-8")
    updated_v6 = user6_rules.read_text(encoding="utf-8")
    assert "-A ufw-user-input -p tcp --dport 22 -j ACCEPT" in updated_v4
    assert (
        "-A ufw-user-input -p tcp --dport 80 -s 173.245.48.0/20 -j ACCEPT" in updated_v4
    )
    assert "-A ufw-user-input -p tcp --dport 80 -j ACCEPT" not in updated_v4
    assert (
        "-A ufw6-user-input -p tcp --dport 443 -s 2400:cb00::/32 -j ACCEPT"
        in updated_v6
    )
    assert commands.count(["ufw", "reload"]) == 1


def test_reconcile_cloudflare_http_firewall_retries_reload_after_transient_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    reload_attempts = 0
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        nonlocal reload_attempts
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command,
                0,
                "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n[ 2] 80/tcp ALLOW IN Anywhere\n",
                "",
            )
        if command == ["ufw", "reload"]:
            reload_attempts += 1
            if reload_attempts == 1:
                return CommandResult(command, 124, "", "timed out after 300s")
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_live_cloudflare_http_firewall",
        lambda *_args, **_kwargs: {
            "checked": True,
            "cidr_count": 1,
            "expected_rule_count": 2,
            "live_rule_count": 2,
            "ipv6_enabled": False,
            "ipv6_chain_present": False,
        },
    )

    payload = cloudflare_sync.reconcile_cloudflare_http_firewall(
        settings,
        cidrs=("173.245.48.0/20",),
    )

    assert payload["skipped"] is False
    assert payload["live_rule_count"] == 2
    assert reload_attempts == 2
    assert commands.count(["ufw", "reload"]) == 2


def test_reconcile_cloudflare_http_firewall_skips_noop_file_writes_and_reload(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    policy = cloudflare_sync.cloudflare_ingress.build_cloudflare_ingress_policy(
        ("173.245.48.0/20", "2400:cb00::/32")
    )
    cloudflare_sync.cloudflare_ingress.rewrite_cloudflare_http_firewall_files(
        policy,
        user_rules_path=user_rules,
        user6_rules_path=user6_rules,
    )

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "write_text_atomic",
        lambda *_args, **_kwargs: pytest.fail(
            "noop reconcile should not write UFW files"
        ),
    )
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command,
                0,
                "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n[ 2] 80/tcp ALLOW IN Anywhere\n",
                "",
            )
        if command == ["ufw", "status"]:
            return CommandResult(command, 0, "Status: active\n", "")
        if command == ["iptables", "-S", "ufw-user-input"]:
            return CommandResult(command, 0, user_rules.read_text(encoding="utf-8"), "")
        if command == ["ip6tables", "-S", "ufw-user-input"]:
            return CommandResult(
                command, 0, user6_rules.read_text(encoding="utf-8"), ""
            )
        if command == ["ufw", "reload"]:
            pytest.fail("noop reconcile should not reload UFW")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)

    payload = cloudflare_sync.reconcile_cloudflare_http_firewall(
        settings,
        cidrs=("173.245.48.0/20", "2400:cb00::/32"),
    )

    assert payload["changed"] is False
    assert payload["reload_attempted"] is False
    assert ["ufw", "reload"] not in commands


def test_reconcile_cloudflare_http_firewall_readonly_write_failure_is_not_rollback_noise(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    def readonly_write(path, *_args, **_kwargs):
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "write_text_atomic", readonly_write
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "restore_cloudflare_http_firewall_files",
        lambda **_kwargs: pytest.fail("read-only first write did not mutate files"),
    )

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command, 0, "Status: active\n[ 1] 80/tcp ALLOW IN Anywhere\n", ""
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)

    with pytest.raises(OSError) as exc_info:
        cloudflare_sync.reconcile_cloudflare_http_firewall(
            settings,
            cidrs=("173.245.48.0/20",),
        )

    assert "Read-only file system" in str(exc_info.value)
    assert "rollback failed" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_cloudflare_sync_failure_notification_includes_diagnosis_and_rollback_state(
    monkeypatch,
) -> None:
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        cloudflare_sync, "send_pushover_notification_async", fake_notify
    )

    await cloudflare_sync._notify_cloudflare_sync_failure(
        Settings(),
        phase="firewall",
        error="[Errno 30] Read-only file system: '/etc/ufw/user.rules'",
        payload={
            "changed": False,
            "fetched_cidr_count": 22,
            "previous_cidr_count": 22,
            "firewall": {"changed": False},
        },
    )

    assert sent
    message = str(sent[0]["message"])
    assert "diagnosis: ufw_rules_read_only" in message
    assert "changed: no" in message
    assert "firewall: audit only; files already matched" in message
    assert "rollback: not needed" in message


def test_reconcile_cloudflare_http_firewall_restores_original_files_when_live_audit_fails(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    original_v4 = (
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    original_v6 = (
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    user_rules.write_text(original_v4, encoding="utf-8")
    user6_rules.write_text(original_v6, encoding="utf-8")

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command, 0, "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n", ""
            )
        if command == ["ufw", "reload"]:
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_live_cloudflare_http_firewall",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("live audit failed")
        ),
    )

    with pytest.raises(RuntimeError, match="live audit failed"):
        cloudflare_sync.reconcile_cloudflare_http_firewall(
            settings,
            cidrs=("173.245.48.0/20", "2400:cb00::/32"),
        )

    assert user_rules.read_text(encoding="utf-8") == original_v4
    assert user6_rules.read_text(encoding="utf-8") == original_v6
    assert commands.count(["ufw", "reload"]) == 2


def test_reconcile_cloudflare_http_firewall_restores_original_files_when_reload_fails(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    original_v4 = (
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    original_v6 = (
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    user_rules.write_text(original_v4, encoding="utf-8")
    user6_rules.write_text(original_v6, encoding="utf-8")

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command, 0, "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n", ""
            )
        if command == ["ufw", "reload"]:
            return CommandResult(command, 1, "", "reload failed")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_live_cloudflare_http_firewall",
        lambda *_args, **_kwargs: pytest.fail(
            "live firewall audit should not run after reload failure"
        ),
    )

    with pytest.raises(RuntimeError, match="reload failed"):
        cloudflare_sync.reconcile_cloudflare_http_firewall(
            settings,
            cidrs=("173.245.48.0/20", "2400:cb00::/32"),
        )

    assert user_rules.read_text(encoding="utf-8") == original_v4
    assert user6_rules.read_text(encoding="utf-8") == original_v6
    assert commands.count(["ufw", "reload"]) == 2


def test_reconcile_cloudflare_http_firewall_validates_rendered_files_before_live_reload(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    original_v4 = (
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    original_v6 = (
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n"
    )
    user_rules.write_text(original_v4, encoding="utf-8")
    user6_rules.write_text(original_v6, encoding="utf-8")

    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )

    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int = 30):
        commands.append(command)
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command, 0, "Status: active\n[ 1] 22/tcp ALLOW IN Anywhere\n", ""
            )
        if command == ["ufw", "reload"]:
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_run_command)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_rendered_cloudflare_http_firewall",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rendered firewall invalid")
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress,
        "audit_live_cloudflare_http_firewall",
        lambda *_args, **_kwargs: pytest.fail(
            "live firewall audit should not run after rendered validation failure"
        ),
    )

    with pytest.raises(RuntimeError, match="rendered firewall invalid"):
        cloudflare_sync.reconcile_cloudflare_http_firewall(
            settings,
            cidrs=("173.245.48.0/20", "2400:cb00::/32"),
        )

    assert user_rules.read_text(encoding="utf-8") == original_v4
    assert user6_rules.read_text(encoding="utf-8") == original_v6
    assert commands.count(["ufw", "reload"]) == 0


@pytest.mark.asyncio
async def test_preview_cloudflare_sync_skips_validation_when_cloudflare_only_disabled(
    monkeypatch,
) -> None:
    settings = Settings(
        nginx_cloudflare_only=False,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "_load_desired_state_for_sync",
        lambda *_args, **_kwargs: pytest.fail(
            "preview should not load desired state when Cloudflare-only mode is disabled"
        ),
    )

    payload = await cloudflare_sync._preview_cloudflare_sync(
        settings,
        cidrs=("173.245.48.0/20", "103.21.244.0/22"),
    )

    assert payload["checked"] is False
    assert payload["reason"] == "nginx_cloudflare_only_disabled"


@pytest.mark.asyncio
async def test_preview_cloudflare_sync_accepts_cluster_node_ingress_allowlist(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,103.21.244.0/22",
        multi_node_enabled=True,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(kind="domain", hostname="web.example.com", enabled=True)
        item.backends = [backend]
        node = ClusterNode(
            node_uid="node-a",
            name="follower-a",
            role="follower",
            state="healthy",
            wireguard_ip="100.64.0.19",
            wireguard_public_key="pub",
            tailnet_ip="100.64.0.19",
        )
        session.add_all([backend, item, node])
        await session.commit()

    import app.database as database

    monkeypatch.setattr(database, "SessionLocal", maker)
    monkeypatch.setattr(
        "app.services.apply_state.leader_tailnet_ip", lambda _settings: "100.64.0.10"
    )
    monkeypatch.setattr(
        "app.services.apply_state.current_tailscale_admin_hostnames",
        lambda _settings: (),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "_validate_firewall_preview",
        lambda *, cidrs: {"checked": True, "cidr_count": len(cidrs)},
    )

    payload = await cloudflare_sync._preview_cloudflare_sync(
        settings,
        cidrs=("173.245.48.0/20", "103.21.244.0/22"),
    )

    assert payload["checked"] is True
    assert payload["nginx"]["checked"] is True
    assert payload["nginx"]["extra_ingress_cidr_count"] == 1
    assert payload["nginx_files"] == ["cnc-host-web-example-com.conf"]


@pytest.mark.asyncio
async def test_run_cloudflare_sync_refreshes_cached_cidrs_without_apply_when_cloudflare_only_disabled(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=False,
        nginx_cloudflare_ips="173.245.48.0/20",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []
    observed_steps: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    def fake_apply_managed_env_updates(_settings, updates):
        observed_steps.append(("env", str(updates["NGINX_CLOUDFLARE_IPS"])))
        return Settings(
            nginx_cloudflare_only=False,
            nginx_cloudflare_ips=str(updates["NGINX_CLOUDFLARE_IPS"]),
            cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
        )

    monkeypatch.setattr(
        cloudflare_sync, "apply_managed_env_updates", fake_apply_managed_env_updates
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "reconcile_cloudflare_http_firewall",
        lambda *_args, **_kwargs: pytest.fail(
            "firewall reconcile should not run when Cloudflare-only mode is disabled"
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    class _DummySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", lambda: _DummySessionContext())

    async def fake_run_apply(_session, _settings):
        raise AssertionError(
            "apply should not run when Cloudflare-only mode is disabled"
        )

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["managed_env_updated"] is True
    assert payload["preview"]["reason"] == "nginx_cloudflare_only_disabled"
    assert payload["phases"]["firewall"]["status"] == "skipped"
    assert payload["phases"]["apply"]["status"] == "skipped"
    assert observed_steps == [("env", "173.245.48.0/20,103.21.244.0/22")]
    assert state_writes[-1]["ok"] is True


@pytest.mark.asyncio
async def test_run_cloudflare_sync_updates_env_firewall_and_apply(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []
    observed_steps: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    async def fake_preview(_settings, *, cidrs):
        observed_steps.append(("preview", ",".join(cidrs)))
        return _preview_payload(cidrs=cidrs)

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)

    def fake_apply_managed_env_updates(_settings, updates):
        observed_steps.append(("env", str(updates["NGINX_CLOUDFLARE_IPS"])))
        return Settings(
            nginx_cloudflare_only=True,
            nginx_cloudflare_ips=str(updates["NGINX_CLOUDFLARE_IPS"]),
            cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
        )

    monkeypatch.setattr(
        cloudflare_sync, "apply_managed_env_updates", fake_apply_managed_env_updates
    )

    def fake_reconcile(_settings, *, cidrs):
        observed_steps.append(("firewall", _settings.nginx_cloudflare_ips))
        return {"skipped": False, "live_rule_count": len(cidrs) * 2}

    monkeypatch.setattr(
        cloudflare_sync, "reconcile_cloudflare_http_firewall", fake_reconcile
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    class _DummySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", lambda: _DummySessionContext())

    async def fake_run_apply(session, resolved_settings):
        observed_steps.append(("apply", resolved_settings.nginx_cloudflare_ips))
        assert (
            resolved_settings.nginx_cloudflare_ips == "173.245.48.0/20,103.21.244.0/22"
        )
        return _DummyApplyResponse(status="success")

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["managed_env_updated"] is True
    assert payload["firewall"]["live_rule_count"] == 4
    assert payload["apply"]["status"] == "success"
    assert state_writes[-1]["ok"] is True
    assert observed_steps == [
        ("preview", "173.245.48.0/20,103.21.244.0/22"),
        ("firewall", "173.245.48.0/20,103.21.244.0/22"),
        ("apply", "173.245.48.0/20,103.21.244.0/22"),
        ("env", "173.245.48.0/20,103.21.244.0/22"),
    ]


@pytest.mark.asyncio
async def test_run_cloudflare_sync_skips_apply_when_cidrs_unchanged(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,103.21.244.0/22",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    async def fake_preview(_settings, *, cidrs):
        return _preview_payload(cidrs=cidrs)

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)
    monkeypatch.setattr(
        cloudflare_sync,
        "reconcile_cloudflare_http_firewall",
        lambda _settings, *, cidrs: {
            "skipped": False,
            "live_rule_count": len(cidrs) * 2,
        },
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    async def fake_run_apply(session, resolved_settings):
        raise AssertionError("apply should not run when Cloudflare CIDRs are unchanged")

    import app.services.apply_service as apply_service

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is True
    assert payload["changed"] is False
    assert payload["managed_env_updated"] is False
    assert "apply" not in payload
    assert state_writes[-1]["ok"] is True


@pytest.mark.parametrize(
    ("configured_cidrs", "expected_changed", "expected_later_phase_status"),
    [
        ("173.245.48.0/20,103.21.244.0/22", False, "skipped"),
        ("173.245.48.0/20", True, "deferred"),
    ],
    ids=("cidrs-unchanged", "cidrs-changed"),
)
@pytest.mark.asyncio
async def test_run_cloudflare_sync_defers_during_control_plane_update(
    monkeypatch,
    tmp_path,
    configured_cidrs: str,
    expected_changed: bool,
    expected_later_phase_status: str,
) -> None:
    database_path = tmp_path / "app.db"
    maker = await _make_session(database_path)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        apply_lock_path=tmp_path / "host.lock",
        app_control_dir=tmp_path / "app-control",
        host_state_path=tmp_path / "host-state.json",
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips=configured_cidrs,
    )
    state_writes: list[dict[str, object]] = []

    async def keep_update_pending(_session, _settings, run):
        return run

    monkeypatch.setattr(
        "app.services.operations._reconcile_update_run_for_blocker",
        keep_update_pending,
    )
    async with maker() as session:
        update_run = UpdateRun(
            status="running",
            message="update running",
            details_json='{"unit": "cnc-update-run-27.service"}',
        )
        session.add(update_run)
        await session.commit()
        await session.refresh(update_run)

    fetched_cidrs = ("173.245.48.0/20", "103.21.244.0/22")
    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: fetched_cidrs,
    )

    async def fake_preview(_settings, *, cidrs):
        return _preview_payload(cidrs=cidrs)

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)
    monkeypatch.setattr(
        cloudflare_sync,
        "reconcile_cloudflare_http_firewall",
        lambda *_args, **_kwargs: pytest.fail(
            "firewall reconcile should be deferred during an update"
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "apply_managed_env_updates",
        lambda *_args, **_kwargs: pytest.fail(
            "managed env persistence should be deferred during an update"
        ),
    )

    async def unexpected_apply(*_args, **_kwargs):
        pytest.fail("apply should be deferred during an update")

    async def unexpected_rollback(*_args, **_kwargs):
        pytest.fail("rollback should not run when no mutation began")

    async def unexpected_notification(*_args, **_kwargs):
        pytest.fail("an expected update deferral should not notify")

    monkeypatch.setattr(cloudflare_sync, "_run_apply_for_sync", unexpected_apply)
    monkeypatch.setattr(
        cloudflare_sync, "_rollback_cloudflare_sync", unexpected_rollback
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "_notify_cloudflare_sync_failure",
        unexpected_notification,
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is False
    assert payload["status"] == "deferred"
    assert payload["deferred"] is True
    assert payload["deferred_reason"] == "control_plane_update_running"
    assert payload["deferred_phase"] == "mutation_gate"
    assert payload["changed"] is expected_changed
    assert payload["managed_env_updated"] is False
    assert payload["notification_sent"] is False
    assert payload["preview"]["checked"] is True
    assert payload["phases"]["preview"]["status"] == "ok"
    assert payload["phases"]["mutation_gate"]["status"] == "deferred"
    assert payload["phases"]["firewall"]["status"] == "deferred"
    assert payload["phases"]["apply"]["status"] == expected_later_phase_status
    assert payload["phases"]["persist_env"]["status"] == expected_later_phase_status
    assert payload["blocker"] == {
        "id": update_run.id,
        "kind": "update_control_plane",
        "status": "running",
        "phase": "background_update",
    }
    assert payload["firewall"]["skipped"] is True
    assert payload["firewall"]["reason"] == "control_plane_update_running"
    assert "error" not in payload
    assert "failure" not in payload
    assert "failed_phase" not in payload
    assert "rollback" not in payload
    assert state_writes[-1] == payload

    async with maker() as session:
        operation = (
            await session.execute(
                select(Operation).where(Operation.kind == "cloudflare_sync")
            )
        ).scalar_one()
    assert operation.status == "partial"
    assert operation.phase == "deferred"
    assert operation.error is None
    assert operation.finished_at is not None
    operation_details = json.loads(operation.details_json)
    assert operation_details["deferred"] is True
    assert operation_details["deferred_reason"] == "control_plane_update_running"
    assert operation_details["blocker_id"] == update_run.id


@pytest.mark.asyncio
async def test_run_cloudflare_sync_rolls_back_firewall_when_apply_fails(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []
    firewall_settings: list[str] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    async def fake_preview(_settings, *, cidrs):
        return _preview_payload(cidrs=cidrs)

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)

    def fake_reconcile(_settings, *, cidrs):
        firewall_settings.append(_settings.nginx_cloudflare_ips)
        return {"skipped": False, "live_rule_count": len(cidrs) * 2}

    monkeypatch.setattr(
        cloudflare_sync, "reconcile_cloudflare_http_firewall", fake_reconcile
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "apply_managed_env_updates",
        lambda *_args, **_kwargs: pytest.fail(
            "managed env should not update when apply fails"
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    class _DummySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", lambda: _DummySessionContext())

    async def fake_run_apply(_session, _settings):
        return _DummyApplyResponse(status="error")

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is False
    assert payload["apply"]["status"] == "error"
    assert payload["managed_env_updated"] is False
    assert payload["rollback"]["ok"] is True
    assert payload["rollback"]["firewall_restored"] is True
    assert firewall_settings == [
        "173.245.48.0/20,103.21.244.0/22",
        "173.245.48.0/20",
    ]
    assert state_writes[-1]["ok"] is False


@pytest.mark.asyncio
async def test_run_cloudflare_sync_exercises_managed_env_rendered_nginx_self_audit_and_apply(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    managed_env_path = tmp_path / "cnc.env"
    managed_env_path.write_text(
        "NGINX_CLOUDFLARE_IPS=173.245.48.0/20\n", encoding="utf-8"
    )
    nginx_dir = tmp_path / "nginx"
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    ufw_defaults = tmp_path / "ufw-defaults"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n\n"
        "### tuple ### allow tcp 443 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 443 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    ufw_defaults.write_text("IPV6=no\n", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        managed_env_file_path=managed_env_path,
        nginx_generated_dir=nginx_dir,
        app_quadlet_dir=tmp_path / "quadlets",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        host_state_path=tmp_path / "host-state.json",
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )
    monkeypatch.delenv("NGINX_CLOUDFLARE_IPS", raising=False)

    async with maker() as session:
        backend = Backend(
            name="web-static",
            kind="static",
            enabled=True,
            static_root="/srv/web-static",
        )
        session.add(backend)
        await session.flush()
        item = Input(kind="domain", hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", maker)
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER_RULES_PATH", user_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_USER6_RULES_PATH", user6_rules
    )
    monkeypatch.setattr(
        cloudflare_sync.cloudflare_ingress, "UFW_DEFAULTS_PATH", ufw_defaults
    )

    def fake_firewall_command(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["ufw", "status", "numbered"]:
            return CommandResult(
                command,
                0,
                "Status: active\n"
                "[ 1] 22/tcp ALLOW IN Anywhere\n"
                "[ 2] 80/tcp ALLOW IN Anywhere\n"
                "[ 3] 443/tcp ALLOW IN Anywhere\n",
                "",
            )
        if command == ["ufw", "status"]:
            return CommandResult(command, 0, "Status: active\n", "")
        if command == ["ufw", "reload"]:
            return CommandResult(command, 0, "", "")
        if command == ["iptables", "-S", "ufw-user-input"]:
            return CommandResult(command, 0, user_rules.read_text(encoding="utf-8"), "")
        if command == ["ip6tables", "-S", "ufw-user-input"]:
            return CommandResult(
                command, 1, "", "ip6tables: No chain/target/match by that name.\n"
            )
        raise AssertionError(f"unexpected firewall command: {command}")

    def fake_self_audit_command(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["ss", "-ltnH"]:
            return CommandResult(
                command, 0, "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n", ""
            )
        if command == ["iptables", "-S", "CNC_APP_ISOLATION"]:
            return CommandResult(command, 0, "-N CNC_APP_ISOLATION\n", "")
        if command == ["iptables", "-S", "FORWARD"]:
            return CommandResult(command, 0, "-A FORWARD -j CNC_APP_ISOLATION\n", "")
        if command == ["sshd", "-T"]:
            return CommandResult(
                command,
                0,
                "\n".join(
                    [
                        "passwordauthentication no",
                        "kbdinteractiveauthentication no",
                        "permitemptypasswords no",
                        "pubkeyauthentication yes",
                        "permitrootlogin prohibit-password",
                        "maxauthtries 3",
                        "logingracetime 20",
                    ]
                ),
                "",
            )
        return fake_firewall_command(command, timeout_sec)

    def apply_managed_env_updates_preserving_temp_paths(
        current_settings: Settings, updates
    ):
        refreshed = managed_env_service.apply_managed_env_updates(
            current_settings, updates
        )
        return refreshed.model_copy(
            update={
                "database_url": current_settings.database_url,
                "managed_env_file_path": current_settings.managed_env_file_path,
                "nginx_generated_dir": current_settings.nginx_generated_dir,
                "apply_backup_dir": current_settings.apply_backup_dir,
                "apply_lock_path": current_settings.apply_lock_path,
                "host_state_path": current_settings.host_state_path,
                "cloudflare_sync_state_path": current_settings.cloudflare_sync_state_path,
                "notification_state_path": current_settings.notification_state_path,
                "tailscale_serve_state_path": current_settings.tailscale_serve_state_path,
            }
        )

    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    async def fake_reconcile_backend_ssh_access(
        backends: list[str], _settings: Settings
    ) -> dict[str, object]:
        return {"ssh_backends": backends}

    async def fake_notify(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(cloudflare_sync, "run_command", fake_firewall_command)
    monkeypatch.setattr(
        cloudflare_sync,
        "apply_managed_env_updates",
        apply_managed_env_updates_preserving_temp_paths,
    )
    monkeypatch.setattr(self_audit, "run_command", fake_self_audit_command)
    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(apply_service, "run_command_checked_async", fake_run_checked)
    monkeypatch.setattr(
        apply_service,
        "reconcile_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "daemon_reloaded": False,
            "timer_states": {},
            "timer_reconciled": False,
        },
    )
    monkeypatch.setattr(
        apply_service, "reconcile_backend_ssh_access", fake_reconcile_backend_ssh_access
    )
    monkeypatch.setattr(
        apply_service,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(apply_service, "send_pushover_notification_async", fake_notify)

    try:
        payload = await cloudflare_sync.run_cloudflare_sync(settings)
        conf_path = nginx_dir / "cnc-host-web-example-com.conf"
        conf_text = conf_path.read_text(encoding="utf-8")
        refreshed_settings = settings.model_copy(
            update={"nginx_cloudflare_ips": "173.245.48.0/20,103.21.244.0/22"}
        )
        audit_payload = self_audit.run_control_plane_self_audit(refreshed_settings)

        assert payload["ok"] is True
        assert payload["changed"] is True
        assert payload["preview"]["checked"] is True
        assert payload["preview"]["nginx"]["checked"] is True
        assert payload["preview"]["firewall"]["checked"] is True
        assert payload["firewall"]["live_rule_count"] == 4
        assert payload["apply"]["status"] == "success"
        assert payload["apply"]["details"]["control_plane_self_audit"]["ok"] is True
        assert managed_env_path.read_text(encoding="utf-8").splitlines() == [
            "NGINX_CLOUDFLARE_IPS=173.245.48.0/20,103.21.244.0/22",
        ]
        assert conf_path.exists()
        assert "allow 127.0.0.1;" in conf_text
        assert "allow ::1;" in conf_text
        assert "allow 173.245.48.0/20;" in conf_text
        assert "allow 103.21.244.0/22;" in conf_text
        assert "deny all;" in conf_text
        assert audit_payload["firewall_restrictions"]["cidr_count"] == 2
        assert audit_payload["nginx_admin_reference"]["checked_files"] == 1
        assert ["nginx", "-t"] in commands
        assert ["systemctl", "reload", "nginx"] in commands
    finally:
        from app.config import get_settings

        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_run_cloudflare_sync_rolls_back_apply_and_firewall_when_env_update_fails(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []
    firewall_settings: list[str] = []
    apply_settings: list[str] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    async def fake_preview(_settings, *, cidrs):
        return _preview_payload(cidrs=cidrs)

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)

    def fake_reconcile(_settings, *, cidrs):
        firewall_settings.append(_settings.nginx_cloudflare_ips)
        return {"skipped": False, "live_rule_count": len(cidrs) * 2}

    monkeypatch.setattr(
        cloudflare_sync, "reconcile_cloudflare_http_firewall", fake_reconcile
    )

    def fake_apply_managed_env_updates(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        cloudflare_sync, "apply_managed_env_updates", fake_apply_managed_env_updates
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    class _DummySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", lambda: _DummySessionContext())

    async def fake_run_apply(_session, resolved_settings):
        apply_settings.append(resolved_settings.nginx_cloudflare_ips)
        return _DummyApplyResponse(status="success", run_id=len(apply_settings))

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is False
    assert payload["apply"]["status"] == "success"
    assert payload["managed_env_updated"] is False
    assert payload["error"] == "disk full"
    assert payload["rollback"]["ok"] is True
    assert payload["rollback"]["firewall_restored"] is True
    assert payload["rollback"]["apply_restored"] is True
    assert firewall_settings == [
        "173.245.48.0/20,103.21.244.0/22",
        "173.245.48.0/20",
    ]
    assert apply_settings == [
        "173.245.48.0/20,103.21.244.0/22",
        "173.245.48.0/20",
    ]
    assert state_writes[-1]["ok"] is False


@pytest.mark.asyncio
async def test_run_cloudflare_sync_preview_failure_prevents_live_mutation(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
        cloudflare_sync_state_path=tmp_path / "cloudflare-sync.json",
    )
    state_writes: list[dict[str, object]] = []
    observed_steps: list[str] = []

    monkeypatch.setattr(
        cloudflare_sync,
        "fetch_cloudflare_cidrs",
        lambda _settings: ("173.245.48.0/20", "103.21.244.0/22"),
    )

    async def fake_preview(_settings, *, cidrs):
        observed_steps.append("preview")
        raise RuntimeError(f"preview mismatch for {','.join(cidrs)}")

    monkeypatch.setattr(cloudflare_sync, "_preview_cloudflare_sync", fake_preview)
    monkeypatch.setattr(
        cloudflare_sync,
        "reconcile_cloudflare_http_firewall",
        lambda *_args, **_kwargs: pytest.fail(
            "firewall reconcile should not run after preview failure"
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "apply_managed_env_updates",
        lambda *_args, **_kwargs: pytest.fail(
            "managed env should not update after preview failure"
        ),
    )
    monkeypatch.setattr(
        cloudflare_sync,
        "write_cloudflare_sync_state",
        lambda _settings, payload: state_writes.append(dict(payload)),
    )

    class _DummySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    import app.database as database
    import app.services.apply_service as apply_service

    monkeypatch.setattr(database, "SessionLocal", lambda: _DummySessionContext())

    async def fake_run_apply(_session, _settings):
        raise AssertionError("apply should not run after preview failure")

    monkeypatch.setattr(apply_service, "run_apply", fake_run_apply)

    payload = await cloudflare_sync.run_cloudflare_sync(settings)

    assert payload["ok"] is False
    assert payload["error"] == "preview mismatch for 173.245.48.0/20,103.21.244.0/22"
    assert observed_steps == ["preview"]
    assert state_writes[-1]["ok"] is False
    assert state_writes[-1]["failed_phase"] == "preview"
