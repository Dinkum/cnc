from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, BackendBackup, ClusterNode
from app.services.commands import run_command
from app.services.renderers import safe_slug
from app.services.ssh_options import SSH_BATCH_OPTIONS


logger = get_logger("cluster.backups")

MIRROR_ROOT = "/var/lib/cnc/backend-backup-mirrors"


async def mirror_backend_backup_to_cluster(
    session: AsyncSession,
    backend: Backend,
    backup: BackendBackup,
    settings: Settings,
) -> dict[str, Any]:
    if not settings.multi_node_enabled:
        return {"skipped": True, "reason": "multi-node support is disabled"}
    if backup.status != "success" or not backup.bundle_path:
        return {"skipped": True, "reason": "backup did not produce a bundle"}

    bundle_path = Path(backup.bundle_path)
    if not bundle_path.exists():
        return {"skipped": True, "reason": "backup bundle is missing"}

    nodes = await _healthy_follower_nodes(session)
    if not nodes:
        return {"skipped": True, "reason": "no healthy follower nodes"}

    mirrored: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    expected_sha256 = str(backup.bundle_sha256 or "").strip()
    for node in nodes:
        node_ip = _node_tailnet_ip(node)
        if not node_ip:
            continue
        destination_dir = f"{MIRROR_ROOT}/{safe_slug(backend.name)}"
        destination_path = f"{destination_dir}/{bundle_path.name}"
        try:
            _ssh_checked(
                node_ip,
                settings,
                f"install -d -m 0755 {shlex.quote(destination_dir)}",
            )
            _scp_checked(
                str(bundle_path), f"root@{node_ip}:{destination_path}", settings
            )
            if expected_sha256:
                _ssh_checked(
                    node_ip,
                    settings,
                    _verify_sha256_script(destination_path, expected_sha256),
                )
            mirrored.append(
                {
                    "node_uid": node.node_uid,
                    "name": node.name,
                    "tailnet_ip": node_ip,
                    "path": destination_path,
                }
            )
        except Exception as exc:
            logger.warning(
                "cluster.backup_mirror.failed",
                backend=backend.name,
                backup_id=backup.id,
                node_uid=node.node_uid,
                error=str(exc),
            )
            failures.append(
                {
                    "node_uid": node.node_uid,
                    "name": node.name,
                    "tailnet_ip": node_ip,
                    "error": str(exc),
                }
            )

    return {
        "skipped": False,
        "mirrored": mirrored,
        "failures": failures,
    }


async def _healthy_follower_nodes(session: AsyncSession) -> list[ClusterNode]:
    rows = (
        await session.execute(
            select(ClusterNode)
            .where(ClusterNode.removed_at.is_(None))
            .where(ClusterNode.state == "healthy")
            .order_by(ClusterNode.name.asc(), ClusterNode.node_uid.asc())
        )
    ).scalars()
    return [node for node in rows if _node_tailnet_ip(node)]


def _node_tailnet_ip(node: ClusterNode) -> str:
    return str(node.tailnet_ip or node.wireguard_ip or "").strip()


def _verify_sha256_script(path: str, expected_sha256: str) -> str:
    quoted_path = shlex.quote(path)
    quoted_expected = shlex.quote(expected_sha256)
    return f"""set -Eeuo pipefail
actual="$(sha256sum {quoted_path} | cut -d' ' -f1)"
test "$actual" = {quoted_expected}
"""


def _ssh_checked(node_ip: str, settings: Settings, script: str) -> None:
    _run_checked(
        [
            "ssh",
            *SSH_BATCH_OPTIONS,
            f"root@{node_ip}",
            f"bash -lc {shlex.quote(script)}",
        ],
        settings,
    )


def _scp_checked(source: str, destination: str, settings: Settings) -> None:
    _run_checked(["scp", *SSH_BATCH_OPTIONS, source, destination], settings)


def _run_checked(command: list[str], settings: Settings) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if result.ok:
        return
    message = result.stderr or result.stdout or "command failed"
    raise RuntimeError(message.strip())
