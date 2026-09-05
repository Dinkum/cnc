from __future__ import annotations

import json
from pathlib import Path
import shlex
import tarfile
import tempfile
from typing import Any

from app.config import Settings
from app.services.apply_core import DesiredState
from app.services.commands import (
    CommandResult,
    command_result_is_retryable,
    run_command,
)
from app.services.ssh_options import SSH_BATCH_OPTIONS


class ClusterFollowerUnavailable(RuntimeError):
    def __init__(self, node_uid: str, node_ip: str, result: CommandResult) -> None:
        message = result.stderr or result.stdout or "follower unavailable"
        super().__init__(message.strip())
        self.node_uid = node_uid
        self.node_ip = node_ip
        self.result = result


def reconcile_cluster_followers(
    desired: DesiredState, settings: Settings
) -> dict[str, Any]:
    if not settings.multi_node_enabled:
        return {
            "cluster_followers": [],
            "cluster_followers_skipped": True,
            "cluster_followers_skip_reason": "multi-node support is disabled",
        }
    if not desired.cluster_nodes:
        return {
            "cluster_followers": [],
            "cluster_followers_skipped": True,
            "cluster_followers_skip_reason": "no healthy follower nodes",
        }

    synced_nodes: list[dict[str, Any]] = []
    unavailable_nodes: list[dict[str, Any]] = []
    static_replicas = desired.static_replicas
    with tempfile.TemporaryDirectory(prefix="cnc-cluster-ingress-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        for node in desired.cluster_nodes:
            node_uid = str(node.get("node_uid") or "").strip()
            node_ip = str(node.get("tailnet_ip") or "").strip()
            if not node_uid or not node_ip:
                continue
            app_files = desired.cluster_node_app_nginx_files.get(node_uid, {})
            admin_files = desired.cluster_node_admin_nginx_files.get(node_uid, {})
            bundle_path = temp_dir / f"{node_uid}.tar.gz"
            _build_node_bundle(
                bundle_path,
                app_nginx_files=app_files,
                admin_nginx_files=admin_files,
                static_replicas=static_replicas,
            )
            remote_path = f"/var/lib/cnc/cluster-ingress/{node_uid}.tar.gz"
            try:
                _ssh_checked(
                    node_uid,
                    node_ip,
                    settings,
                    "set -Eeuo pipefail\ninstall -d -m 0755 /var/lib/cnc/cluster-ingress",
                )
                _scp_checked(
                    node_uid,
                    node_ip,
                    str(bundle_path),
                    f"root@{node_ip}:{remote_path}",
                    settings,
                )
                _ssh_checked(
                    node_uid, node_ip, settings, _remote_sync_script(remote_path)
                )
            except ClusterFollowerUnavailable as exc:
                unavailable_nodes.append(
                    {
                        "node_uid": node_uid,
                        "name": node.get("name") or node_uid,
                        "tailnet_ip": node_ip,
                        "error": str(exc),
                    }
                )
                continue
            synced_nodes.append(
                {
                    "node_uid": node_uid,
                    "name": node.get("name") or node_uid,
                    "tailnet_ip": node_ip,
                    "app_nginx_files": sorted(app_files),
                    "admin_nginx_files": sorted(admin_files),
                    "static_replicas": sorted(static_replicas),
                }
            )

    return {
        "cluster_followers": synced_nodes,
        "cluster_followers_unavailable": unavailable_nodes,
        "cluster_followers_partial": bool(unavailable_nodes),
        "cluster_static_replicas": [
            {
                "backend": backend,
                "replica_root": replica["replica_root"],
                "content_hash": replica["content_hash"],
            }
            for backend, replica in sorted(static_replicas.items())
        ],
    }


def _build_node_bundle(
    bundle_path: Path,
    *,
    app_nginx_files: dict[str, str],
    admin_nginx_files: dict[str, str] | None = None,
    static_replicas: dict[str, dict[str, str]],
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cnc-cluster-ingress-bundle-"
    ) as stage_name:
        stage = Path(stage_name)
        app_nginx_dir = stage / "app-nginx"
        admin_nginx_dir = stage / "admin-nginx"
        app_nginx_dir.mkdir(parents=True)
        admin_nginx_dir.mkdir(parents=True)
        admin_nginx_files = admin_nginx_files or {}
        for filename, content in sorted(app_nginx_files.items()):
            if not filename.startswith("cnc-") or "/" in filename:
                raise ValueError(f"invalid generated app nginx filename: {filename}")
            (app_nginx_dir / filename).write_text(content, encoding="utf-8")
        for filename, content in sorted(admin_nginx_files.items()):
            if not filename.startswith("cnc-") or "/" in filename:
                raise ValueError(f"invalid generated admin nginx filename: {filename}")
            (admin_nginx_dir / filename).write_text(content, encoding="utf-8")

        manifest = {
            "app_nginx_files": sorted(app_nginx_files),
            "admin_nginx_files": sorted(admin_nginx_files),
            "static_replicas": [
                {
                    "backend": backend,
                    "replica_root": replica["replica_root"],
                    "content_hash": replica["content_hash"],
                }
                for backend, replica in sorted(static_replicas.items())
            ],
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with tarfile.open(bundle_path, "w:gz") as archive:
            archive.add(app_nginx_dir, arcname="app-nginx")
            archive.add(admin_nginx_dir, arcname="admin-nginx")
            archive.add(stage / "manifest.json", arcname="manifest.json")
            for backend, replica in sorted(static_replicas.items()):
                replica_root = str(replica["replica_root"]).strip().rstrip("/")
                replica_name = Path(replica_root).name
                if not replica_name or replica_name != _safe_replica_name(replica_name):
                    raise ValueError(f"invalid static replica path for {backend}")
                archive.add(replica["source_root"], arcname=f"static/{replica_name}")


def _safe_replica_name(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch == "-")


def _remote_sync_script(remote_path: str) -> str:
    quoted_remote_path = shlex.quote(remote_path)
    return f"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v nginx >/dev/null 2>&1; then
  apt-get -qq update
  apt-get -qq install -y nginx ca-certificates curl tar gzip >/dev/null
fi
install -d -m 0755 /etc/nginx/generated /etc/nginx/cnc-admin-generated /etc/nginx/conf.d /var/lib/cnc/static-replicas
cat > /etc/nginx/conf.d/cnc-generated.conf <<'EOF'
# Managed by CNC. Do not edit by hand.
include /etc/nginx/generated/*.conf;
EOF
cat > /etc/nginx/conf.d/cnc-admin-generated.conf <<'EOF'
# Managed by CNC. Do not edit by hand.
include /etc/nginx/cnc-admin-generated/*.conf;
EOF
stage="$(mktemp -d /tmp/cnc-ingress.XXXXXX)"
app_nginx_backup="$(mktemp -d /tmp/cnc-nginx-generated.XXXXXX)"
admin_nginx_backup="$(mktemp -d /tmp/cnc-admin-nginx-generated.XXXXXX)"
cleanup() {{
  rm -rf "$stage"
  rm -rf "$app_nginx_backup"
  rm -rf "$admin_nginx_backup"
  rm -f {quoted_remote_path}
}}
trap cleanup EXIT
tar -C "$stage" -xzf {quoted_remote_path}
if [ -d "$stage/static" ]; then
  for source in "$stage"/static/*; do
    [ -d "$source" ] || continue
    name="$(basename "$source")"
    case "$name" in
      ""|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
        echo "invalid static replica name: $name" >&2
        exit 2
        ;;
    esac
    target="/var/lib/cnc/static-replicas/$name"
    temp_target="$(mktemp -d "/var/lib/cnc/static-replicas/.${{name}}.XXXXXX")"
    cp -a "$source/." "$temp_target/"
    if [ -e "$target" ]; then
      old_target="$(mktemp -d "/var/lib/cnc/static-replicas/.old-${{name}}.XXXXXX")"
      rmdir "$old_target"
      mv "$target" "$old_target"
    else
      old_target=""
    fi
    mv "$temp_target" "$target"
    if [ -n "$old_target" ]; then
      rm -rf "$old_target"
    fi
  done
fi
find /etc/nginx/generated -maxdepth 1 -type f -name 'cnc-*.conf' -exec cp -a {{}} "$app_nginx_backup"/ \\;
find /etc/nginx/cnc-admin-generated -maxdepth 1 -type f -name 'cnc-*.conf' -exec cp -a {{}} "$admin_nginx_backup"/ \\;
find /etc/nginx/generated -maxdepth 1 -type f -name 'cnc-*.conf' -delete
find /etc/nginx/cnc-admin-generated -maxdepth 1 -type f -name 'cnc-*.conf' -delete
if [ -d "$stage/app-nginx" ]; then
  find "$stage/app-nginx" -maxdepth 1 -type f -name 'cnc-*.conf' -exec install -m 0644 {{}} /etc/nginx/generated/ \\;
fi
if [ -d "$stage/admin-nginx" ]; then
  find "$stage/admin-nginx" -maxdepth 1 -type f -name 'cnc-*.conf' -exec install -m 0644 {{}} /etc/nginx/cnc-admin-generated/ \\;
fi
if ! nginx -t; then
  find /etc/nginx/generated -maxdepth 1 -type f -name 'cnc-*.conf' -delete
  find /etc/nginx/cnc-admin-generated -maxdepth 1 -type f -name 'cnc-*.conf' -delete
  find "$app_nginx_backup" -maxdepth 1 -type f -name 'cnc-*.conf' -exec install -m 0644 {{}} /etc/nginx/generated/ \\;
  find "$admin_nginx_backup" -maxdepth 1 -type f -name 'cnc-*.conf' -exec install -m 0644 {{}} /etc/nginx/cnc-admin-generated/ \\;
  nginx -t || true
  exit 1
fi
systemctl enable --now nginx >/dev/null
systemctl reload nginx || systemctl restart nginx
if command -v tailscale >/dev/null 2>&1 && [ -f /etc/nginx/cnc-admin-generated/cnc-admin-proxy.conf ]; then
  tailscale serve --bg --https=443 http://127.0.0.1:9091 >/dev/null || true
fi
"""


def _ssh_checked(node_uid: str, node_ip: str, settings: Settings, script: str) -> None:
    _run_checked(
        [
            "ssh",
            *SSH_BATCH_OPTIONS,
            f"root@{node_ip}",
            f"bash -lc {shlex.quote(script)}",
        ],
        settings,
        node_uid=node_uid,
        node_ip=node_ip,
    )


def _scp_checked(
    node_uid: str, node_ip: str, source: str, destination: str, settings: Settings
) -> None:
    _run_checked(
        ["scp", *SSH_BATCH_OPTIONS, source, destination],
        settings,
        node_uid=node_uid,
        node_ip=node_ip,
    )


def _run_checked(
    command: list[str],
    settings: Settings,
    *,
    node_uid: str,
    node_ip: str,
) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if result.ok:
        return
    if command_result_is_retryable(result):
        raise ClusterFollowerUnavailable(node_uid, node_ip, result)
    message = result.stderr or result.stdout or "command failed"
    raise RuntimeError(message.strip())
