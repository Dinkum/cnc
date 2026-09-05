from __future__ import annotations

from pathlib import Path
import tarfile

from app.config import Settings
from app.services import cluster_ingress
from app.services.apply_core import DesiredState
from app.services.commands import CommandResult
from app.services.resource_profile import build_resource_profile


def _desired(settings: Settings) -> DesiredState:
    return DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[],
        resource_profile=build_resource_profile(settings, 0),
        route_contracts=[],
        backend_contracts=[],
        cluster_nodes=(
            {
                "node_uid": "node-a",
                "name": "follower-a",
                "tailnet_ip": "100.64.0.19",
                "state": "healthy",
            },
        ),
        cluster_node_app_nginx_files={
            "node-a": {
                "cnc-host-example-com.conf": "server { listen 80; server_name example.com; }\n",
            }
        },
        cluster_node_admin_nginx_files={
            "node-a": {
                "cnc-admin-proxy.conf": "server { listen 127.0.0.1:9091; }\n",
            }
        },
    )


def test_reconcile_cluster_followers_uses_safe_ssh_options(monkeypatch) -> None:
    settings = Settings(multi_node_enabled=True, command_timeout_apply_sec=42)
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_ingress, "run_command", fake_run_command)

    result = cluster_ingress.reconcile_cluster_followers(_desired(settings), settings)

    assert result["cluster_followers"][0]["node_uid"] == "node-a"
    assert commands[0][:15] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/tmp/cnc-ssh-known-hosts",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=5m",
        "-o",
        "ControlPath=/tmp/cnc-ssh-%C",
    ]
    assert commands[1][:15] == [
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/tmp/cnc-ssh-known-hosts",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=5m",
        "-o",
        "ControlPath=/tmp/cnc-ssh-%C",
    ]
    script = commands[-1][-1]
    assert "bash -lc " in script
    assert "find /etc/nginx/generated -maxdepth 1 -type f -name " in script
    assert "find /etc/nginx/cnc-admin-generated -maxdepth 1 -type f -name " in script
    assert " -delete" in script
    assert "systemctl reload nginx || systemctl restart nginx" in script
    assert "tailscale serve --bg --https=443 http://127.0.0.1:9091" in script
    assert "rm -rf /etc/nginx" not in script


def test_reconcile_cluster_followers_skips_unreachable_node(monkeypatch) -> None:
    settings = Settings(multi_node_enabled=True, command_timeout_apply_sec=42)
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        commands.append(command)
        return CommandResult(
            command=command,
            returncode=255,
            stdout="",
            stderr="ssh: connect to host 100.64.0.19 port 22: Connection timed out",
        )

    monkeypatch.setattr(cluster_ingress, "run_command", fake_run_command)

    result = cluster_ingress.reconcile_cluster_followers(_desired(settings), settings)

    assert result["cluster_followers"] == []
    assert result["cluster_followers_partial"] is True
    assert result["cluster_followers_unavailable"] == [
        {
            "node_uid": "node-a",
            "name": "follower-a",
            "tailnet_ip": "100.64.0.19",
            "error": "ssh: connect to host 100.64.0.19 port 22: Connection timed out",
        }
    ]
    assert len(commands) == 1


def test_reconcile_cluster_followers_keeps_remote_validation_hard_fail(
    monkeypatch,
) -> None:
    settings = Settings(multi_node_enabled=True, command_timeout_apply_sec=42)
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], timeout_sec: int) -> CommandResult:
        commands.append(command)
        if len(commands) == 3:
            return CommandResult(
                command=command,
                returncode=1,
                stdout="",
                stderr="nginx: configuration file /etc/nginx/nginx.conf test failed",
            )
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cluster_ingress, "run_command", fake_run_command)

    try:
        cluster_ingress.reconcile_cluster_followers(_desired(settings), settings)
    except RuntimeError as exc:
        assert "nginx.conf test failed" in str(exc)
    else:
        raise AssertionError("expected follower validation failure")


def test_build_node_bundle_includes_static_replica(tmp_path: Path) -> None:
    source_root = tmp_path / "site"
    source_root.mkdir()
    (source_root / "index.html").write_text("hello\n", encoding="utf-8")
    bundle_path = tmp_path / "node-a.tar.gz"

    cluster_ingress._build_node_bundle(
        bundle_path,
        app_nginx_files={"cnc-host-site-example-com.conf": "server {}\n"},
        admin_nginx_files={
            "cnc-admin-proxy.conf": "server { listen 127.0.0.1:9091; }\n"
        },
        static_replicas={
            "site": {
                "source_root": str(source_root),
                "replica_root": "/var/lib/cnc/static-replicas/site",
                "content_hash": "abc123",
            }
        },
    )

    with tarfile.open(bundle_path, "r:gz") as archive:
        names = set(archive.getnames())

    assert "app-nginx/cnc-host-site-example-com.conf" in names
    assert "admin-nginx/cnc-admin-proxy.conf" in names
    assert "static/site/index.html" in names
    assert "manifest.json" in names
