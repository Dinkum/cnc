import argparse
import importlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from app import admin_cli
from app import cli as cli_package
from app.cli import alerts as alerts_cli
from app.cli import apply as apply_cli
from app.cli import cloudflare as cloudflare_cli
from app.cli import doctor as doctor_cli
from app.cli import fix as fix_cli
from app.cli import llm_help as llm_help_cli
from app.cli import logs as logs_cli
from app.cli import repair as repair_cli
from app.cli import restore as restore_cli
from app.cli import shell as shell_cli
from app.cli import host as host_cli


def test_load_env_defaults_sets_missing_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "cnc.env"
    env_file.write_text(
        'DATABASE_URL=sqlite+aiosqlite:////var/lib/cnc/data/app.db\nOPENAI_MODEL="gpt-5-mini"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    admin_cli._load_env_defaults(env_file)

    assert os.environ["DATABASE_URL"] == "sqlite+aiosqlite:////var/lib/cnc/data/app.db"
    assert os.environ["OPENAI_MODEL"] == "gpt-5-mini"


def test_load_env_defaults_preserves_existing_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "cnc.env"
    env_file.write_text(
        "DATABASE_URL=sqlite+aiosqlite:////var/lib/cnc/data/app.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:////tmp/existing.db")

    admin_cli._load_env_defaults(env_file)

    assert os.environ["DATABASE_URL"] == "sqlite+aiosqlite:////tmp/existing.db"


def test_register_subparsers_imports_only_requested_command_module(monkeypatch) -> None:
    imported: list[str] = []

    def fake_import_module(name: str):
        imported.append(name)
        return SimpleNamespace(
            register=lambda subparsers: subparsers.add_parser(name.rsplit(".", 1)[-1])
        )

    monkeypatch.setattr(cli_package.importlib, "import_module", fake_import_module)

    parser = argparse.ArgumentParser(prog="cnc-admin")
    cli_package.register_subparsers(parser, command_names=["backend-alerts"])

    assert imported == ["app.cli.alerts"]


def test_admin_cli_doctor_app_text(monkeypatch, capsys) -> None:
    async def fake_doctor_app_async(backend_name: str, settings):
        return (
            2,
            {
                "backend": backend_name,
                "container": "cnc-app-web",
                "ok": False,
                "diagnosis": "app_service_down",
                "sandbox_status": "ready",
                "guest_status": "ready",
                "app_handoff_status": "down",
                "runtime_owner": "legacy_bridge",
                "runtime_owner_label": "legacy Podman + reboot bridge",
                "backend_exec_available": False,
                "backend_exec_error": "timed out after 3s",
                "issues": ["bootstrap_stuck", "storage_orphan"],
                "maintenance_issues": ["legacy_direct_podman_runtime_owner"],
                "private_ip": "10.88.0.8",
                "private_reachable": False,
                "loopback_port": 12000,
                "loopback_reachable": False,
                "dns_servers": ["1.1.1.1", "1.0.0.1"],
                "bootstrap": {
                    "status": "stuck",
                    "lock_active": False,
                    "stale": True,
                    "state": {
                        "started_at": "2026-03-25T00:00:00Z",
                        "failure_phase": "bootstrap_stale",
                    },
                },
                "shield_status": {
                    "server_enabled": True,
                    "backend_exists": True,
                    "backend_enabled": False,
                    "service_active": True,
                    "output_state": "healthy",
                    "ready": False,
                },
            },
            None,
        )

    monkeypatch.setattr(doctor_cli, "doctor_app_async", fake_doctor_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["doctor", "app", "web"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "backend: web" in captured.out
    assert "diagnosis: guest systemd up but app service down" in captured.out
    assert "runtime_owner: legacy Podman + reboot bridge" in captured.out
    assert "backend_exec_available: no" in captured.out
    assert "backend_exec_error: timed out after 3s" in captured.out
    assert "issues: bootstrap_stuck, storage_orphan" in captured.out
    assert "maintenance_issues: legacy_direct_podman_runtime_owner" in captured.out
    assert "shield_status:" in captured.out
    assert "backend_enabled: no" in captured.out
    assert captured.err == ""


def test_admin_cli_doctor_app_json(monkeypatch, capsys) -> None:
    async def fake_doctor_app_async(backend_name: str, settings):
        return (
            0,
            {
                "backend": backend_name,
                "container": "cnc-app-web",
                "ok": True,
                "issues": [],
                "dns_servers": ["1.1.1.1"],
                "bootstrap": {
                    "status": "succeeded",
                    "lock_active": False,
                    "stale": False,
                    "state": {},
                },
                "shield_status": {
                    "server_enabled": True,
                    "backend_exists": True,
                    "backend_enabled": True,
                    "service_active": True,
                    "output_state": "healthy",
                    "ready": True,
                },
            },
            None,
        )

    monkeypatch.setattr(doctor_cli, "doctor_app_async", fake_doctor_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["doctor", "app", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert payload["ok"] is True
    assert payload["shield_status"]["output_state"] == "healthy"


def test_doctor_text_labels_backend_exec_unavailable() -> None:
    text = doctor_cli.format_doctor_text(
        {
            "backend": "sample-app",
            "container": "cnc-app-sample-app",
            "ok": False,
            "diagnosis": "backend_exec_unavailable",
            "sandbox_status": "ready",
            "guest_status": "exec_unavailable",
            "app_handoff_status": "unknown",
            "backend_exec_available": False,
            "backend_exec_status": "timeout",
            "backend_exec_error": "timed out after 3s",
            "issues": ["podman_exec_unavailable", "loopback_unreachable"],
            "bootstrap": {"status": "pending"},
        }
    )

    assert "diagnosis: backend exec unavailable" in text
    assert "backend_exec_available: no" in text
    assert "backend_exec_status: timeout" in text
    assert "backend_exec_error: timed out after 3s" in text
    assert "issues: podman_exec_unavailable, loopback_unreachable" in text


def test_admin_cli_fix_text(monkeypatch, capsys) -> None:
    async def fake_fix_app_async(backend_name: str, settings):
        return (
            0,
            {
                "backend": backend_name,
                "changed": True,
                "ok": True,
                "before": {
                    "diagnosis": "sandbox_publish_broken",
                    "issues": ["bridge_dns_enabled", "container_missing"],
                },
                "cleanup": {
                    "changed": True,
                    "actions": ["podman network rm cnc-net-web", "removed spec.json"],
                    "errors": [],
                },
                "reconcile": {"created": True, "recreated": True, "bootstrapped": True},
                "after": {"diagnosis": "healthy", "issues": []},
                "failure": None,
                "shield_status": {
                    "server_enabled": True,
                    "backend_exists": False,
                    "backend_enabled": False,
                    "service_active": False,
                    "output_state": "missing",
                    "ready": False,
                },
            },
            None,
        )

    monkeypatch.setattr(fix_cli, "fix_app_async", fake_fix_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["fix", "web"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "backend: web" in captured.out
    assert "changed: yes" in captured.out
    assert "before_diagnosis: sandbox_publish_broken" in captured.out
    assert "after_issues: none" in captured.out
    assert "shield_status:" in captured.out
    assert "backend_enabled: no" in captured.out


def test_admin_cli_app_migrate_runtime_uses_fix_pipeline(monkeypatch, capsys) -> None:
    calls: list[str] = []

    async def fake_fix_app_async(backend_name: str, settings):
        calls.append(backend_name)
        return (
            0,
            {
                "backend": backend_name,
                "changed": True,
                "ok": True,
                "before": {
                    "diagnosis": "healthy",
                    "issues": [],
                    "maintenance_issues": ["legacy_direct_podman_runtime_owner"],
                },
                "cleanup": {"changed": False, "actions": [], "errors": []},
                "reconcile": {
                    "created": True,
                    "recreated": True,
                    "bootstrapped": False,
                },
                "after": {
                    "diagnosis": "healthy",
                    "issues": [],
                    "maintenance_issues": [],
                },
                "failure": None,
            },
            None,
        )

    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")
    monkeypatch.setattr(fix_cli, "fix_app_async", fake_fix_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["app", "migrate-runtime", "web"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == ["web"]
    assert "backend: web" in captured.out
    assert "changed: yes" in captured.out
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")


def test_admin_cli_app_reseed_rootfs_requires_explicit_confirmation(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["app", "reseed-rootfs", "web"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "refusing to rebuild rootfs" in captured.err
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")


def test_admin_cli_app_reseed_rootfs_text(monkeypatch, capsys) -> None:
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")
    app_cli = importlib.import_module("app.cli.app")

    calls: list[str] = []

    async def fake_reseed_rootfs_app_async(backend_name: str, settings):
        calls.append(backend_name)
        return (
            0,
            {
                "backend": backend_name,
                "changed": True,
                "seeded": True,
                "rootfs": "/var/lib/cnc/sandboxes/web/rootfs",
                "backup_id": 42,
                "backup_path": "/var/lib/cnc/backend-backups/web/backup.tar.gz",
                "quarantine_rootfs": "/var/lib/cnc/sandboxes/web/rootfs.quarantine-20260506T000000Z",
                "seed_revision": 4,
                "preserved_steps": ["preserved_guest_systemd_state"],
                "provisioned_steps": [
                    "provision_package_catalog",
                    "provision_guest_runtime",
                    "provision_guest_tools",
                    "provision_package_cleanup",
                    "profile_self_test",
                ],
            },
            None,
        )

    monkeypatch.setattr(
        app_cli, "reseed_rootfs_app_async", fake_reseed_rootfs_app_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["app", "reseed-rootfs", "web", "--force"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == ["web"]
    assert "backend: web" in captured.out
    assert "changed: yes" in captured.out
    assert "seeded: yes" in captured.out
    assert "backup_id: 42" in captured.out
    assert (
        "quarantine_rootfs: /var/lib/cnc/sandboxes/web/rootfs.quarantine-20260506T000000Z"
        in captured.out
    )
    assert "preserved_steps: preserved_guest_systemd_state" in captured.out
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")


@pytest.mark.asyncio
async def test_reseed_rootfs_app_creates_backup_before_reseed(monkeypatch) -> None:
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")
    app_cli = importlib.import_module("app.cli.app")
    backend = SimpleNamespace(id=7, name="web", kind="app", enabled=True)
    backup = SimpleNamespace(
        id=42,
        status="success",
        bundle_path="/var/lib/cnc/backend-backups/web/backup.tar.gz",
        bundle_sha256="abc123",
        size_bytes=1234,
        error=None,
    )
    calls: list[str] = []

    class FakeResult:
        def scalar_one_or_none(self):
            return backend

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            calls.append("load")
            return FakeResult()

    async def fake_create_backup(session, loaded_backend, settings):
        assert isinstance(session, FakeSession)
        assert loaded_backend is backend
        calls.append("backup")
        return backup

    async def fake_reseed(
        loaded_backend, settings, *, backup_id=None, backup_path=None
    ):
        assert loaded_backend is backend
        assert backup_id == 42
        assert backup_path == "/var/lib/cnc/backend-backups/web/backup.tar.gz"
        calls.append("reseed")
        return {
            "seeded": True,
            "rootfs": "/var/lib/cnc/sandboxes/web/rootfs",
            "quarantine_rootfs": "/var/lib/cnc/sandboxes/web/rootfs.quarantine-20260506T000000Z",
            "seed_revision": 4,
            "preserved_steps": [],
            "provisioned_steps": [],
        }

    import app.database
    import app.services.app_runtime
    import app.services.backend_backup_service

    monkeypatch.setattr(app.database, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        app.services.backend_backup_service, "create_backend_backup", fake_create_backup
    )
    monkeypatch.setattr(
        app.services.app_runtime, "reseed_app_backend_rootfs", fake_reseed
    )

    exit_code, payload, error = await app_cli.reseed_rootfs_app_async("web", object())

    assert exit_code == 0
    assert error is None
    assert calls == ["load", "backup", "reseed"]
    assert payload is not None
    assert payload["backup_id"] == 42
    assert payload["backup_path"] == "/var/lib/cnc/backend-backups/web/backup.tar.gz"
    assert (
        payload["quarantine_rootfs"]
        == "/var/lib/cnc/sandboxes/web/rootfs.quarantine-20260506T000000Z"
    )
    sys.modules.pop("app.cli.app", None)
    if hasattr(cli_package, "app"):
        delattr(cli_package, "app")


def test_admin_cli_repair_app_json(monkeypatch, capsys) -> None:
    async def fake_repair_app_async(backend_name: str, settings):
        return (
            2,
            {
                "backend": backend_name,
                "changed": True,
                "ok": False,
                "before": {
                    "diagnosis": "sandbox_publish_broken",
                    "issues": ["loopback_unreachable"],
                },
                "cleanup": {
                    "changed": True,
                    "actions": ["podman restart cnc-app-web"],
                    "errors": ["socket start failed"],
                },
                "reconcile": {
                    "created": False,
                    "recreated": False,
                    "bootstrapped": False,
                },
                "after": {
                    "diagnosis": "sandbox_publish_broken",
                    "issues": ["loopback_unreachable"],
                },
                "failure": {
                    "phase": "app_healthcheck",
                    "error": "app backend web healthcheck failed",
                },
                "shield_status": {
                    "server_enabled": False,
                    "backend_exists": False,
                    "backend_enabled": False,
                    "service_active": False,
                    "output_state": "missing",
                    "ready": False,
                },
            },
            None,
        )

    monkeypatch.setattr(repair_cli, "repair_app_async", fake_repair_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["repair", "app", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["backend"] == "web"
    assert payload["changed"] is True
    assert payload["shield_status"]["ready"] is False


def test_admin_cli_reconcile_host_text(monkeypatch, capsys) -> None:
    async def fake_reconcile_host_async(settings):
        return (
            0,
            {
                "changed_units": ["cnc-auto-size.timer", "cnc-cloudflare-sync.service"],
                "daemon_reloaded": True,
                "timer_reconciled": True,
                "timer_states": {
                    "cnc-auto-size.timer": {"enabled": True, "active": True},
                    "cnc-cloudflare-sync.timer": {"enabled": True, "active": True},
                },
            },
            None,
        )

    monkeypatch.setattr(host_cli, "reconcile_host_async", fake_reconcile_host_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["reconcile-host"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert (
        "changed_units: cnc-auto-size.timer, cnc-cloudflare-sync.service"
        in captured.out
    )
    assert "daemon_reloaded: yes" in captured.out
    assert "admin_restart_required: no" in captured.out
    assert "cnc-cloudflare-sync.timer: enabled=yes active=yes" in captured.out


def test_admin_cli_reconcile_host_json(monkeypatch, capsys) -> None:
    async def fake_reconcile_host_async(settings):
        return (
            0,
            {
                "changed_units": [],
                "daemon_reloaded": False,
                "timer_reconciled": False,
                "timer_states": {
                    "cnc-auto-size.timer": {"enabled": True, "active": True},
                },
            },
            None,
        )

    monkeypatch.setattr(host_cli, "reconcile_host_async", fake_reconcile_host_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["reconcile-host", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["changed_units"] == []
    assert payload["daemon_reloaded"] is False
    assert payload["timer_states"]["cnc-auto-size.timer"]["active"] is True


def test_admin_cli_host_quadlet_cutover_plan_text(monkeypatch, capsys) -> None:
    async def fake_quadlet_cutover_plan_async(settings):
        return (
            0,
            {
                "needs_cutover_count": 1,
                "blocked_count": 0,
                "outputs": [
                    {
                        "backend": "web",
                        "status": "cutover_ready",
                        "runtime_owner": "legacy_bridge",
                        "diagnosis": "healthy",
                    },
                    {
                        "backend": "docs",
                        "status": "quadlet",
                        "runtime_owner": "quadlet",
                        "diagnosis": "healthy",
                    },
                ],
                "commands": ["cnc-admin app migrate-runtime web"],
            },
            None,
        )

    monkeypatch.setattr(
        host_cli, "quadlet_cutover_plan_async", fake_quadlet_cutover_plan_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["host", "quadlet-cutover-plan"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "scope: app runtime ownership" in captured.out
    assert "needs_cutover: 1" in captured.out
    assert (
        "web: status=cutover_ready owner=legacy_bridge health=healthy" in captured.out
    )
    assert "cnc-admin app migrate-runtime web" in captured.out


def test_admin_cli_reconcile_host_error(monkeypatch, capsys) -> None:
    async def fake_reconcile_host_async(settings):
        return 1, None, "systemctl daemon-reload failed"

    monkeypatch.setattr(host_cli, "reconcile_host_async", fake_reconcile_host_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["reconcile-host"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "systemctl daemon-reload failed" in captured.err


def test_admin_cli_host_apply_uses_canonical_namespace(monkeypatch, capsys) -> None:
    async def fake_apply_async(settings):
        return (
            0,
            {
                "status": "success",
                "message": "apply completed",
                "run_id": 7,
                "created_at": "-",
            },
            None,
        )

    monkeypatch.setattr(host_cli, "apply_async", fake_apply_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["host", "apply"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "scope: full converge" in captured.out
    assert "message: apply completed" in captured.out


def test_admin_cli_app_doctor_uses_canonical_namespace(monkeypatch, capsys) -> None:
    async def fake_doctor_app_async(backend_name: str, settings):
        return (
            2,
            {
                "backend": backend_name,
                "container": "cnc-app-web",
                "ok": False,
                "diagnosis": "app_service_down",
                "sandbox_status": "ready",
                "guest_status": "ready",
                "app_handoff_status": "down",
                "issues": ["loopback_unreachable"],
                "bootstrap": {
                    "status": "failed",
                    "lock_active": False,
                    "stale": False,
                    "state": {},
                },
                "recommended_actions": [
                    {
                        "label": "logs",
                        "command": "cnc-admin app logs web",
                        "note": "inspect startup failures",
                    }
                ],
            },
            None,
        )

    monkeypatch.setattr(doctor_cli, "doctor_app_async", fake_doctor_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["app", "doctor", "web"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "diagnosis: guest systemd up but app service down" in captured.out
    assert "recommended_next:" in captured.out
    assert "cnc-admin app logs web" in captured.out


def test_admin_cli_logs_app_text(monkeypatch, capsys) -> None:
    async def fake_logs_app_async(backend_name: str, lines: int, settings):
        return (
            0,
            {
                "backend": backend_name,
                "container": "cnc-app-web",
                "issues": ["bootstrap_failed"],
                "bootstrap_status": "failed",
                "requested_lines": lines,
                "sources_available": True,
                "sources": [
                    {
                        "source": "container",
                        "output": "apt-get update\ntemporary failure resolving",
                    },
                    {"source": "bootstrap-state", "output": "bootstrap phase failed"},
                ],
                "ok": True,
                "shield_status": {
                    "server_enabled": True,
                    "backend_exists": True,
                    "backend_enabled": True,
                    "service_active": True,
                    "output_state": "healthy",
                    "ready": True,
                },
            },
            None,
        )

    monkeypatch.setattr(logs_cli, "logs_app_async", fake_logs_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["logs", "app", "web", "--lines", "80"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "backend: web" in captured.out
    assert "requested_lines: 80" in captured.out
    assert "== container ==" in captured.out
    assert "temporary failure resolving" in captured.out
    assert "shield_status:" in captured.out
    assert "backend_enabled: yes" in captured.out


def test_admin_cli_logs_app_json(monkeypatch, capsys) -> None:
    async def fake_logs_app_async(backend_name: str, lines: int, settings):
        return (
            0,
            {
                "backend": backend_name,
                "container": "cnc-app-web",
                "issues": [],
                "bootstrap_status": "succeeded",
                "requested_lines": lines,
                "sources_available": False,
                "sources": [],
                "ok": True,
                "shield_status": {
                    "server_enabled": True,
                    "backend_exists": True,
                    "backend_enabled": True,
                    "service_active": True,
                    "output_state": "healthy",
                    "ready": True,
                },
            },
            None,
        )

    monkeypatch.setattr(logs_cli, "logs_app_async", fake_logs_app_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["logs", "app", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert payload["sources_available"] is False
    assert payload["shield_status"]["backend_enabled"] is True


def test_admin_cli_backend_alerts_check_text(monkeypatch, capsys) -> None:
    async def fake_backend_alerts_check_async(settings):
        return (
            0,
            {
                "checked_at": "2026-04-18T20:30:00+00:00",
                "backend_count": 1,
                "healthy_count": 0,
                "unhealthy_count": 1,
                "notifications_configured": True,
                "notifications": [{"backend": "web", "kind": "down", "sent": True}],
                "backends": [
                    {
                        "backend": "web",
                        "ok": False,
                        "issues": ["loopback_unreachable"],
                    }
                ],
            },
            None,
        )

    monkeypatch.setattr(
        alerts_cli, "backend_alerts_check_async", fake_backend_alerts_check_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["backend-alerts", "check"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "unhealthy_count: 1" in captured.out
    assert "web: down -> sent" in captured.out


def test_admin_cli_backend_alerts_test_json(monkeypatch, capsys) -> None:
    async def fake_backend_alerts_test_async(backend_name: str, settings):
        return (
            0,
            {
                "backend": backend_name,
                "notifications_configured": True,
                "notifications": [
                    {"backend": backend_name, "kind": "down", "sent": True},
                    {"backend": backend_name, "kind": "reminder", "sent": True},
                    {"backend": backend_name, "kind": "recovered", "sent": True},
                ],
                "ok": True,
            },
            None,
        )

    monkeypatch.setattr(
        alerts_cli, "backend_alerts_test_async", fake_backend_alerts_test_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["backend-alerts", "test", "--backend", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert [item["kind"] for item in payload["notifications"]] == [
        "down",
        "reminder",
        "recovered",
    ]


def test_admin_cli_notifications_test_text(monkeypatch, capsys) -> None:
    async def fake_notifications_test_async(backend_name: str, settings):
        return (
            0,
            {
                "backend": backend_name,
                "notifications_configured": True,
                "notifications": [
                    {
                        "kind": "host_drift_detected",
                        "title": "CNC host drift detected",
                        "sent": True,
                    },
                    {"kind": "apply_failed", "title": "CNC apply failed", "sent": True},
                    {
                        "kind": "down",
                        "title": f"CNC backend down: {backend_name}",
                        "sent": True,
                    },
                ],
                "ok": True,
            },
            None,
        )

    monkeypatch.setattr(
        alerts_cli, "notifications_test_async", fake_notifications_test_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["notifications", "test", "--backend", "web"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "notification_count: 3" in captured.out
    assert "CNC host drift detected: sent" in captured.out
    assert "CNC backend down: web: sent" in captured.out


def test_admin_cli_notifications_test_json(monkeypatch, capsys) -> None:
    async def fake_notifications_test_async(backend_name: str, settings):
        return (
            0,
            {
                "backend": backend_name,
                "notifications_configured": True,
                "notifications": [
                    {
                        "kind": "host_drift_detected",
                        "title": "CNC host drift detected",
                        "sent": True,
                    },
                    {
                        "kind": "host_drift_cleared",
                        "title": "CNC host drift cleared",
                        "sent": True,
                    },
                    {"kind": "apply_failed", "title": "CNC apply failed", "sent": True},
                    {
                        "kind": "update_completed",
                        "title": "CNC update completed",
                        "sent": True,
                    },
                    {
                        "kind": "backup_failed",
                        "title": f"CNC backup failed: {backend_name}",
                        "sent": True,
                    },
                    {
                        "kind": "restore_completed",
                        "title": f"CNC restore completed: {backend_name}",
                        "sent": True,
                    },
                    {
                        "kind": "down",
                        "title": f"CNC backend down: {backend_name}",
                        "sent": True,
                    },
                    {
                        "kind": "reminder",
                        "title": f"CNC backend still down: {backend_name}",
                        "sent": True,
                    },
                    {
                        "kind": "recovered",
                        "title": f"CNC backend recovered: {backend_name}",
                        "sent": True,
                    },
                ],
                "ok": True,
            },
            None,
        )

    monkeypatch.setattr(
        alerts_cli, "notifications_test_async", fake_notifications_test_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["notifications", "test", "--backend", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert [item["kind"] for item in payload["notifications"]] == [
        "host_drift_detected",
        "host_drift_cleared",
        "apply_failed",
        "update_completed",
        "backup_failed",
        "restore_completed",
        "down",
        "reminder",
        "recovered",
    ]


def test_admin_cli_logs_admin_text_filters_request_id(
    monkeypatch, capsys, tmp_path
) -> None:
    log_path = tmp_path / "cnc.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-04-05T12:00:00.000Z INFO cnc.admin http.request.started component=main request_id=req_a path=/",
                "2026-04-05T12:00:00.100Z ERROR cnc.admin http.request.unhandled component=main request_id=req_b error_code=CNC-09001 error_inst=7K2Q9M4D path=/api/status",
                "Traceback (most recent call last):",
                "RuntimeError: boom",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        admin_cli,
        "_resolve_settings",
        lambda: SimpleNamespace(log_path=log_path, log_app_id="cnc.admin"),
    )

    async def fake_shield_status_payload(_settings):
        return {
            "server_enabled": True,
            "backend_exists": True,
            "backend_enabled": True,
            "service_active": True,
            "output_state": "healthy",
            "ready": True,
        }

    monkeypatch.setattr(
        logs_cli, "load_shield_status_payload", fake_shield_status_payload
    )

    exit_code = admin_cli.main(["logs", "admin", "--request-id", "req_b"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "app_id: cnc.admin" in captured.out
    assert "request_id: req_b" in captured.out
    assert "entry_count: 1" in captured.out
    assert "http.request.unhandled" in captured.out
    assert "RuntimeError: boom" in captured.out
    assert "req_a" not in captured.out
    assert "shield_status:" in captured.out
    assert "server_enabled: yes" in captured.out
    assert "ready: yes" in captured.out


def test_admin_log_tail_reads_only_requested_tail(tmp_path) -> None:
    log_path = tmp_path / "admin.log"
    log_path.write_text(
        "\n".join(f"line-{index}" for index in range(200)) + "\n", encoding="utf-8"
    )

    assert logs_cli._tail_lines(log_path, 3) == ["line-197", "line-198", "line-199"]


def test_admin_log_tail_caps_single_large_line(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "admin.log"
    monkeypatch.setattr(logs_cli, "ADMIN_LOG_TAIL_MAX_BYTES", 32)
    log_path.write_bytes(b"a" * 128)

    assert logs_cli._tail_lines(log_path, 5) == ["a" * 32]


def test_admin_cli_logs_admin_json_filters_errors_by_component(
    monkeypatch, capsys, tmp_path
) -> None:
    log_path = tmp_path / "cnc.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-04-05T12:00:00.000Z INFO cnc.admin http.request.started component=main request_id=req_a path=/",
                "2026-04-05T12:00:00.100Z ERROR cnc.admin http.request.unhandled component=main request_id=req_b error_code=CNC-09001 error_inst=7K2Q9M4D path=/api/status",
                "2026-04-05T12:00:00.200Z ERROR cnc.admin apply.run.failed component=apply request_id=req_c error_code=CNC-02099 error_inst=0000000J phase=app_bootstrap",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        admin_cli,
        "_resolve_settings",
        lambda: SimpleNamespace(log_path=log_path, log_app_id="cnc.admin"),
    )

    async def fake_shield_status_payload(_settings):
        return {
            "server_enabled": False,
            "backend_exists": True,
            "backend_enabled": False,
            "service_active": True,
            "output_state": "unhealthy",
            "ready": False,
        }

    monkeypatch.setattr(
        logs_cli, "load_shield_status_payload", fake_shield_status_payload
    )

    exit_code = admin_cli.main(
        ["logs", "admin", "--errors", "--component", "apply", "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["errors_only"] is True
    assert payload["component"] == "apply"
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["level"] == "ERROR"
    assert "apply.run.failed" in payload["entries"][0]["text"]
    assert payload["shield_status"]["backend_enabled"] is False


def test_admin_cli_logs_admin_text_parses_block_format(
    monkeypatch, capsys, tmp_path
) -> None:
    log_path = tmp_path / "cnc.log"
    log_path.write_text(
        "\n".join(
            [
                "[2026-04-05] ----- 12:00:00.100 | ERROR    | cnc.admin | apply -----",
                "(*) Started apply run | request_id: req_c, component: apply",
                ">> verify               | (x) Failed API request | error_code: CNC-02099, error_inst: 0000000J, error_type: TimeoutError",
                "~~ Traceback (most recent call last):",
                "~~ TimeoutError: boom",
                ">> result               | (x) Failed | request_id: req_c, error_code: CNC-02099, error_inst: 0000000J",
                "|= 18.2ms total",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        admin_cli,
        "_resolve_settings",
        lambda: SimpleNamespace(log_path=log_path, log_app_id="cnc.admin"),
    )

    async def fake_shield_status_payload(_settings):
        return {
            "server_enabled": False,
            "backend_exists": False,
            "backend_enabled": False,
            "service_active": False,
            "output_state": "missing",
            "ready": False,
        }

    monkeypatch.setattr(
        logs_cli, "load_shield_status_payload", fake_shield_status_payload
    )

    exit_code = admin_cli.main(
        ["logs", "admin", "--errors", "--component", "apply", "--request-id", "req_c"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "entry_count: 1" in captured.out
    assert "app_id: cnc.admin" in captured.out
    assert "component: apply" in captured.out
    assert "Started apply run" in captured.out
    assert "TimeoutError: boom" in captured.out


def test_admin_cli_llm_help_text(monkeypatch, capsys) -> None:
    async def fake_llm_help_async(backend_name: str, settings):
        assert backend_name == "web"
        return (
            0,
            {
                "backend": "web",
                "kind": "app",
                "enabled": True,
                "status": {"value": "healthy", "service_state": "active / running"},
                "routing": {
                    "inputs": [{"kind": "domain", "value": "web.example.com"}],
                    "target": "http://127.0.0.1:12001",
                    "exposure": "127.0.0.1:12001:8337/tcp",
                },
                "runtime": {
                    "container": "cnc-app-web",
                    "network": "cnc-net-web",
                    "sandbox_profile": "ubuntu-24.04-systemd",
                    "port": 12001,
                    "handoff_port": 8337,
                    "healthcheck_mode": "tcp",
                    "healthcheck_path": "-",
                    "debug_toolbelt_version": "v3",
                    "debug_tools": [
                        "bash",
                        "curl",
                        "dig",
                        "ip",
                        "jq",
                        "nslookup",
                        "ping",
                        "ps",
                        "rg",
                        "sqlite3",
                        "ss",
                        "sudo",
                        "top",
                        "wget",
                    ],
                    "mounts": [{"host_path": "/srv/web-data", "target_path": "/data"}],
                },
                "commands": {
                    "llm_help": "llm-help web",
                    "ssh_shell": "ssh web@100.64.0.10",
                    "ssh_exec_example": "ssh web@100.64.0.10 'python3 --version'",
                    "ssh_llm_help": "ssh web@100.64.0.10 llm-help",
                    "host_llm_help_remote": "ssh root@100.64.0.10 llm-help",
                    "host_llm_help_backend_remote": "ssh root@100.64.0.10 'llm-help web'",
                    "host_doctor_app_remote": "ssh root@100.64.0.10 'cnc-admin app doctor web'",
                },
                "warnings": [
                    "Use the literal backend SSH path `ssh web@100.64.0.10` for app access; it supports ssh command mode, not scp or sftp."
                ],
            },
            None,
        )

    monkeypatch.setattr(llm_help_cli, "llm_help_async", fake_llm_help_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["llm-help", "web"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "# CNC Backend Context: web" in captured.out
    assert "## What CNC Is" in captured.out
    assert "## LLM Rules" in captured.out
    assert "## Sandbox Boundary" in captured.out
    assert "## Verify Next" in captured.out
    assert "## Escalate To Host" in captured.out
    assert "## Commands" in captured.out
    assert "backend: web" in captured.out
    assert "inputs: domain: web.example.com" in captured.out
    assert "debug_toolbelt: v3" in captured.out
    assert (
        "debug_tools: bash, curl, dig, ip, jq, nslookup, ping, ps, rg, sqlite3, ss, sudo, top, wget"
        in captured.out
    )
    assert "mounts: /srv/web-data:/data" in captured.out
    assert "```bash" in captured.out
    assert "# refresh this backend brief" in captured.out
    assert "ssh web@100.64.0.10 llm-help" in captured.out
    assert "# primary app shell inside the backend container" in captured.out
    assert "ssh web@100.64.0.10" in captured.out
    assert "cnc-admin shell web" not in captured.out
    assert "cnc-admin logs app web --lines 200" not in captured.out
    assert "cnc-admin apply" not in captured.out
    assert (
        "Use `ssh web@100.64.0.10 llm-help` once to refresh CNC context before making assumptions about this backend."
        in captured.out
    )
    assert (
        "DO NOT treat CNC metadata, apply, or fix as the source of truth for app env, startup, or process roles inside the sandbox."
        in captured.out
    )
    assert (
        "IF backend-local health passes and public traffic still fails, classify the incident as CNC/host-side until proven otherwise."
        in captured.out
    )
    assert (
        "If you need to change app env, update scripts, or guest supervisor state, do it through the app's own tooling inside the sandbox instead of CNC metadata."
        in captured.out
    )
    assert "## App Onboarding Inside The Guest" in captured.out
    assert (
        "Create the app's own systemd unit under `/etc/systemd/system/<app>.service` inside the guest."
        in captured.out
    )
    assert (
        "Do not create CNC-specific launcher hooks, wrapper scripts, or CNC-owned service metadata for app startup."
        in captured.out
    )
    assert "# sandbox-local health with CNC's configured contract" in captured.out
    assert (
        "python3 -c \"import socket; socket.create_connection(('127.0.0.1', 8337), 2).close()\""
        in captured.out
    )
    assert "# host-side CNC diagnosis for the published sandbox" in captured.out
    assert "# public route check when the backend has an enabled domain" in captured.out
    assert (
        "Escalate to host-level control only when the next required action is not possible through the container path."
        in captured.out
    )
    assert (
        "If the next action needs host/root access, stop backend-shell work and ask the user to run the root escalation command; include the exact command, why it is needed, and what output to return."
        in captured.out
    )
    assert (
        "Before interacting at the host level, run `ssh root@100.64.0.10 llm-help` to refresh host context and supported CNC actions."
        in captured.out
    )
    assert (
        "# Host-wide CNC brief: shows the host control-plane view, supported CNC workflows,"
        in captured.out
    )
    assert "ssh root@100.64.0.10 llm-help" in captured.out
    assert "ssh root@100.64.0.10 'llm-help web'" in captured.out
    assert "ssh root@100.64.0.10 'cnc-admin app doctor web'" in captured.out
    assert (
        "Stop backend-shell recovery if the app answers on `127.0.0.1:8337` but public routing still fails"
        in captured.out
    )
    assert (
        "switch to the host-level commands above rather than trying to smuggle host operations through the backend shell."
        in captured.out
    )
    assert (
        "Use the literal backend SSH path `ssh web@100.64.0.10` for app access; it supports ssh command mode, not scp or sftp."
        in captured.out
    )
    assert "<backend>" not in captured.out


def test_resolve_llm_help_host_prefers_configured_host(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.10 54321 100.64.0.10 22")

    host = admin_cli._resolve_llm_help_host(
        SimpleNamespace(ssh_advertise_host="203.0.113.10")
    )

    assert host == "203.0.113.10"


def test_resolve_llm_help_host_uses_ssh_connection(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.10 54321 100.64.0.10 22")

    host = admin_cli._resolve_llm_help_host(SimpleNamespace(ssh_advertise_host=""))

    assert host == "100.64.0.10"


def test_resolve_llm_help_host_falls_back_to_placeholder(monkeypatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)

    host = admin_cli._resolve_llm_help_host(SimpleNamespace(ssh_advertise_host=""))

    assert host == "SERVER_IP"


def test_admin_cli_llm_help_json(monkeypatch, capsys) -> None:
    async def fake_llm_help_async(backend_name: str, settings):
        assert backend_name == "web"
        return (
            0,
            {
                "backend": "web",
                "kind": "app",
                "enabled": True,
                "status": {"value": "healthy", "service_state": "active / running"},
                "routing": {
                    "inputs": [],
                    "target": "http://127.0.0.1:12001",
                    "exposure": "127.0.0.1:12001:8337/tcp",
                },
                "runtime": {
                    "container": "cnc-app-web",
                    "network": "cnc-net-web",
                    "env_keys": [],
                    "mounts": [],
                },
                "commands": {"llm_help": "llm-help web"},
                "warnings": [],
            },
            None,
        )

    monkeypatch.setattr(llm_help_cli, "llm_help_async", fake_llm_help_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["llm-help", "web", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert payload["commands"]["llm_help"] == "llm-help web"


def test_admin_cli_llm_help_host_text(monkeypatch, capsys) -> None:
    async def fake_llm_help_async(backend_name: str | None, settings):
        assert backend_name is None
        return (
            0,
            {
                "host": "100.64.0.10",
                "cnc": {
                    "admin_service": "cnc-admin.service",
                    "admin_bind": "127.0.0.1:9090",
                    "runtime_manager": "podman",
                    "backend_ssh_model": "ordinary OpenSSH restricted to tailnet source addresses; forced-command path into podman exec; not the Tailscale-managed SSH feature",
                    "app_control_dir": "/var/lib/cnc/app-control",
                    "backend_backup_dir": "/var/lib/cnc/backend-backups",
                    "updater_script_path": "/var/lib/cnc/current/scripts/update_from_github.sh",
                    "backend_count": 1,
                    "app_backend_count": 1,
                    "static_backend_count": 0,
                },
                "workflow": {
                    "start_here": [
                        "Run llm-help before guessing how this host is wired."
                    ],
                    "do": [
                        "Treat app outputs as Podman containers named cnc-app-<backend>."
                    ],
                    "do_not": [
                        "Do not use docker commands on this host; CNC app runtimes are managed with Podman.",
                        "Do not use podman exec, podman restart, or podman rm directly unless CNC guidance specifically requires it.",
                    ],
                    "tips": [
                        "If public traffic is failing, verify both the edge route and the backend doctor output."
                    ],
                },
                "commands": {
                    "llm_help": "llm-help",
                    "apply": "cnc-admin host apply",
                    "cnc_admin_shell": "cnc-admin app shell <backend>",
                    "cnc_admin_exec": "cnc-admin app exec <backend> -- <command>",
                    "fix_backend": "cnc-admin fix <backend>",
                    "shell_backend": "ssh <backend>@100.64.0.10",
                },
                "backends": [
                    {
                        "backend": "web",
                        "kind": "app",
                        "enabled": True,
                        "status": {
                            "value": "healthy",
                            "service_state": "active / running",
                        },
                        "routing": {
                            "inputs": [],
                            "target": "http://127.0.0.1:12001",
                            "exposure": "127.0.0.1:12001:8337/tcp",
                        },
                        "runtime": {
                            "container": "cnc-app-web",
                            "network": "cnc-net-web",
                            "env_keys": [],
                            "mounts": [],
                        },
                        "commands": {"llm_help": "llm-help web"},
                        "warnings": [],
                    }
                ],
            },
            None,
        )

    monkeypatch.setattr(llm_help_cli, "llm_help_async", fake_llm_help_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["llm-help"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "# CNC Host Context" in captured.out
    assert "## What CNC Is" in captured.out
    assert "## LLM Rules" in captured.out
    assert "runtime_manager: podman" in captured.out
    assert "## Workflow" in captured.out
    assert "## Do Not" in captured.out
    assert "## Backend Inventory" in captured.out
    assert (
        "MUST use `cnc-admin app doctor <backend>` before manual restart, repair, or rollback decisions."
        in captured.out
    )
    assert (
        "DO NOT assume a successful backend-side update means CNC runtime convergence is complete."
        in captured.out
    )
    assert (
        "IF the app can boot in a fresh one-shot process but the managed runtime is wedged, classify the primary fault as app/runtime and the immediate recovery path as CNC-owned."
        in captured.out
    )
    assert (
        "If public traffic is failing, verify both the edge route and the backend doctor output."
        in captured.out
    )
    assert "admin_bind:" not in captured.out
    assert "updater_script_path:" not in captured.out
    assert "podman ps -a" not in captured.out
    assert "cnc-admin host apply" in captured.out
    assert "ssh <backend>@100.64.0.10" in captured.out
    assert "`web`: status=healthy;" in captured.out


def test_admin_cli_apply_text(monkeypatch, capsys) -> None:
    async def fake_apply_async(settings):
        return (
            0,
            {
                "status": "success",
                "message": "apply completed",
                "run_id": 35,
                "created_at": "2026-03-30T23:19:50",
                "details": {
                    "nginx_files": ["cnc-host-web.conf"],
                    "app_containers_running": ["cnc-app-web"],
                },
            },
            None,
        )

    monkeypatch.setattr(apply_cli, "apply_async", fake_apply_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["apply"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "status: success" in captured.out
    assert "message: apply completed" in captured.out


def test_admin_cli_apply_text_partial_failure(monkeypatch, capsys) -> None:
    async def fake_apply_async(settings):
        return (
            1,
            {
                "status": "error",
                "message": "apply partially failed",
                "run_id": 36,
                "created_at": "2026-03-30T23:21:00",
                "details": {
                    "phase": "ssh_access",
                    "error": "ssh alias update exploded",
                    "failure_mode": "partial",
                    "manual_review_required": True,
                    "operator_action": "manual review required",
                    "completed_phases": [
                        "app_runtime",
                    ],
                    "live_mutation_phases": [
                        "app_runtime",
                        "ssh_access",
                    ],
                    "nginx_rollback": {
                        "mode": "remove_unloaded_candidate",
                        "attempted": False,
                        "status": "not_staged",
                    },
                },
            },
            None,
        )

    monkeypatch.setattr(apply_cli, "apply_async", fake_apply_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["apply"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "message: apply partially failed" in captured.out
    assert "phase: ssh_access" in captured.out
    assert "failure_mode: partial" in captured.out
    assert "manual_review_required: yes" in captured.out
    assert "completed_phases: app_runtime" in captured.out
    assert "live_mutation_phases: app_runtime, ssh_access" in captured.out
    assert (
        "nginx_rollback: mode=remove_unloaded_candidate status=not_staged"
        in captured.out
    )


@pytest.mark.asyncio
async def test_cloudflare_sync_async_returns_success_for_deferred_sync(
    monkeypatch,
) -> None:
    from app.services import cloudflare_sync as cloudflare_sync_service

    async def fake_run_cloudflare_sync(_settings):
        return {
            "ok": False,
            "status": "deferred",
            "deferred": True,
            "deferred_reason": "control_plane_update_running",
        }

    monkeypatch.setattr(
        cloudflare_sync_service,
        "run_cloudflare_sync",
        fake_run_cloudflare_sync,
    )

    exit_code, payload, error = await cloudflare_cli.cloudflare_sync_async(
        SimpleNamespace()
    )

    assert exit_code == 0
    assert payload is not None
    assert payload["status"] == "deferred"
    assert error is None


def test_admin_cli_cloudflare_sync_deferred_text(monkeypatch, capsys) -> None:
    async def fake_cloudflare_sync_async(settings):
        return (
            0,
            {
                "checked_at": "2026-07-13T21:02:48+00:00",
                "ok": False,
                "status": "deferred",
                "deferred": True,
                "deferred_reason": "control_plane_update_running",
                "cloudflare_only": True,
                "changed": False,
                "fetched_cidr_count": 22,
                "managed_env_updated": False,
                "firewall": {
                    "skipped": True,
                    "reason": "control_plane_update_running",
                },
                "blocker": {
                    "id": 27,
                    "kind": "update_control_plane",
                    "status": "running",
                    "phase": "background_update",
                },
            },
            None,
        )

    monkeypatch.setattr(
        cloudflare_cli, "cloudflare_sync_async", fake_cloudflare_sync_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["cloudflare", "sync"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "status: deferred" in captured.out
    assert "deferred: yes" in captured.out
    assert "deferred_reason: control_plane_update_running" in captured.out
    assert (
        "blocker: update_control_plane #27 (running, background_update)" in captured.out
    )
    assert "failed_phase:" not in captured.out


def test_admin_cli_cloudflare_sync_text(monkeypatch, capsys) -> None:
    async def fake_cloudflare_sync_async(settings):
        return (
            0,
            {
                "checked_at": "2026-04-11T12:00:00+00:00",
                "ok": True,
                "cloudflare_only": True,
                "changed": True,
                "fetched_cidr_count": 22,
                "managed_env_updated": True,
                "firewall": {"skipped": False, "live_rule_count": 44},
                "apply": {"status": "success"},
            },
            None,
        )

    monkeypatch.setattr(
        cloudflare_cli, "cloudflare_sync_async", fake_cloudflare_sync_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["cloudflare", "sync"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "cloudflare_only: yes" in captured.out
    assert "changed: yes" in captured.out
    assert "firewall_rule_count: 44" in captured.out
    assert "apply_status: success" in captured.out


def test_admin_cli_cloudflare_sync_json(monkeypatch, capsys) -> None:
    async def fake_cloudflare_sync_async(settings):
        return (
            2,
            {
                "checked_at": "2026-04-11T12:00:00+00:00",
                "ok": False,
                "cloudflare_only": True,
                "changed": False,
                "error": "ufw must be active",
            },
            None,
        )

    monkeypatch.setattr(
        cloudflare_cli, "cloudflare_sync_async", fake_cloudflare_sync_async
    )
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["cloudflare", "sync", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"] == "ufw must be active"


def test_admin_cli_llm_help_host_json(monkeypatch, capsys) -> None:
    async def fake_llm_help_async(backend_name: str | None, settings):
        assert backend_name is None
        return (
            0,
            {
                "host": "100.64.0.10",
                "cnc": {"runtime_manager": "podman"},
                "workflow": {},
                "commands": {"llm_help": "llm-help"},
                "backends": [],
            },
            None,
        )

    monkeypatch.setattr(llm_help_cli, "llm_help_async", fake_llm_help_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["llm-help", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["cnc"]["runtime_manager"] == "podman"
    assert payload["commands"]["llm_help"] == "llm-help"


def test_admin_cli_restore_verify_text(monkeypatch, capsys) -> None:
    async def fake_restore_verify_app_async(backend_name: str, backup_id: int | None):
        assert backup_id is None
        return (
            0,
            {
                "backend": backend_name,
                "backup_id": 7,
                "ok": True,
                "scope": "full_state",
                "bundle_path": "/var/lib/cnc/backend-backups/web/20260330-7.tar.gz",
                "bundle_sha256": "abc123",
                "bundle_size_bytes": 4096,
                "bundle_format_version": 1,
                "mount_entries_archived": 2,
                "mount_entries_total": 2,
                "container_snapshot_included": True,
                "verification_status": "verified",
                "restore_readiness": "ready",
                "restore_readiness_label": "ready",
                "risk_flags": [],
                "risk_summary": "no known restore risks",
                "covered_paths": ["/srv/web-data", "/srv/web-cache"],
                "notes": "bundle with metadata plus 2 mounted path(s) plus container snapshot",
            },
            None,
        )

    monkeypatch.setattr(
        restore_cli, "restore_verify_app_async", fake_restore_verify_app_async
    )

    exit_code = admin_cli.main(["restore-verify", "app", "web"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "backend: web" in captured.out
    assert "backup_id: 7" in captured.out
    assert "container_snapshot_included: yes" in captured.out
    assert "verification_status: verified" in captured.out
    assert "restore_readiness: ready" in captured.out
    assert "covered_paths: /srv/web-data, /srv/web-cache" in captured.out


def test_admin_cli_restore_verify_json(monkeypatch, capsys) -> None:
    async def fake_restore_verify_app_async(backend_name: str, backup_id: int | None):
        assert backup_id == 9
        return (
            0,
            {
                "backend": backend_name,
                "backup_id": 9,
                "ok": True,
                "scope": "mounted_data",
                "bundle_path": "/tmp/web.tar.gz",
                "bundle_sha256": "def456",
                "bundle_size_bytes": 2048,
                "bundle_format_version": 1,
                "mount_entries_archived": 1,
                "mount_entries_total": 1,
                "container_snapshot_included": False,
                "verification_status": "verified",
                "restore_readiness": "review",
                "restore_readiness_label": "review before restore",
                "risk_flags": ["app_without_container_snapshot"],
                "risk_summary": "app bundle does not include a container snapshot",
                "covered_paths": ["/srv/web-data"],
                "notes": "bundle with metadata plus 1 mounted path(s)",
            },
            None,
        )

    monkeypatch.setattr(
        restore_cli, "restore_verify_app_async", fake_restore_verify_app_async
    )

    exit_code = admin_cli.main(
        ["restore-verify", "app", "web", "--backup-id", "9", "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backup_id"] == 9
    assert payload["container_snapshot_included"] is False
    assert payload["restore_readiness"] == "review"
    assert payload["covered_paths"] == ["/srv/web-data"]


def test_admin_cli_restore_create_text(monkeypatch, capsys) -> None:
    async def fake_restore_create_async(
        bundle_path: str, backend_name: str | None, settings
    ):
        assert bundle_path == "/tmp/web.tar.gz"
        assert backend_name is None
        return (
            0,
            {
                "backend": "web",
                "backup_id": 14,
                "bundle_path": "/tmp/web.tar.gz",
                "restored_paths": ["/srv/web-data"],
                "ok": True,
            },
            None,
        )

    monkeypatch.setattr(restore_cli, "restore_create_async", fake_restore_create_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(["restore-create", "/tmp/web.tar.gz"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "backend: web" in captured.out
    assert "backup_id: 14" in captured.out
    assert "restored_paths: 1" in captured.out


def test_admin_cli_restore_create_json(monkeypatch, capsys) -> None:
    async def fake_restore_create_async(
        bundle_path: str, backend_name: str | None, settings
    ):
        assert bundle_path == "/tmp/web.tar.gz"
        assert backend_name == "web-restored"
        return (
            0,
            {
                "backend": "web-restored",
                "backup_id": 21,
                "bundle_path": "/tmp/web.tar.gz",
                "restored_paths": [],
                "ok": True,
            },
            None,
        )

    monkeypatch.setattr(restore_cli, "restore_create_async", fake_restore_create_async)
    monkeypatch.setattr(admin_cli, "_resolve_settings", lambda: object())

    exit_code = admin_cli.main(
        [
            "restore-create",
            "/tmp/web.tar.gz",
            "--backend-name",
            "web-restored",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web-restored"
    assert payload["backup_id"] == 21


def test_admin_cli_backup_verify_uses_canonical_namespace(monkeypatch, capsys) -> None:
    async def fake_restore_verify_app_async(backend_name: str, backup_id: int | None):
        assert backup_id == 9
        return (
            0,
            {"backend": backend_name, "backup_id": 9, "ok": True, "covered_paths": []},
            None,
        )

    monkeypatch.setattr(
        restore_cli, "restore_verify_app_async", fake_restore_verify_app_async
    )

    exit_code = admin_cli.main(
        ["backup", "verify", "web", "--backup-id", "9", "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["backend"] == "web"
    assert payload["backup_id"] == 9


def test_admin_cli_shell_runs_resolved_container(monkeypatch) -> None:
    async def fake_resolve_shell_app_async(backend_name: str):
        return 0, f"cnc-app-{backend_name}", None

    called: dict[str, object] = {}

    def fake_run_shell(container: str) -> int:
        called["container"] = container
        return 0

    monkeypatch.setattr(
        shell_cli, "resolve_shell_app_async", fake_resolve_shell_app_async
    )
    monkeypatch.setattr(shell_cli, "run_shell", fake_run_shell)

    exit_code = admin_cli.main(["shell", "web"])

    assert exit_code == 0
    assert called["container"] == "cnc-app-web"


def test_admin_cli_shell_reports_resolution_error(monkeypatch, capsys) -> None:
    async def fake_resolve_shell_app_async(backend_name: str):
        return 1, None, f"shell only supports app backends: {backend_name}"

    monkeypatch.setattr(
        shell_cli, "resolve_shell_app_async", fake_resolve_shell_app_async
    )

    exit_code = admin_cli.main(["shell", "static-site"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "shell only supports app backends: static-site" in captured.err


def test_admin_cli_exec_runs_resolved_container(monkeypatch) -> None:
    async def fake_resolve_shell_app_async(backend_name: str):
        return 0, f"cnc-app-{backend_name}", None

    called: dict[str, object] = {}

    def fake_run_exec(container: str, command_args: list[str]) -> int:
        called["container"] = container
        called["command_args"] = command_args
        return 0

    monkeypatch.setattr(
        shell_cli, "resolve_shell_app_async", fake_resolve_shell_app_async
    )
    monkeypatch.setattr(shell_cli, "run_exec", fake_run_exec)

    exit_code = admin_cli.main(["exec", "web", "--", "bash", "-lc", "pwd"])

    assert exit_code == 0
    assert called["container"] == "cnc-app-web"
    assert called["command_args"] == ["bash", "-lc", "pwd"]


def test_admin_cli_exec_requires_command(monkeypatch, capsys) -> None:
    exit_code = admin_cli.main(["exec", "web"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "exec requires a command after --" in captured.err


def test_admin_cli_backend_alerts_test_requires_backend(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        admin_cli.main(["backend-alerts", "test"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "the following arguments are required: --backend" in captured.err
