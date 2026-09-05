from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.app_quadlet import QUADLET_HEADER
from app.services.cluster_topology import leader_tailnet_ip
from app.services.renderers import SHIELD_BACKEND_NAME, SHIELD_PORT


SHIELD_CONTAINER_NAME = "cnc-shield"
SHIELD_CONTAINER_UNIT = "cnc-shield.container"
SHIELD_CONTAINER_SERVICE = "cnc-shield.service"


def shield_quadlet_path(settings: Settings) -> Path:
    return settings.app_quadlet_dir / SHIELD_CONTAINER_UNIT


def render_shield_quadlet(settings: Settings) -> str:
    env_file = settings.shield_env_file_path
    state_dir = settings.shield_state_dir
    port = settings.shield_port or SHIELD_PORT
    publish_ports = [f"PublishPort=127.0.0.1:{port}:1026/tcp"]
    if settings.multi_node_enabled:
        publish_ports.append(
            f"PublishPort={leader_tailnet_ip(settings)}:{port}:1026/tcp"
        )
    lines = [
        QUADLET_HEADER,
        "",
        "[Unit]",
        "Description=CNC Shield access gate",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Container]",
        f"ContainerName={SHIELD_CONTAINER_NAME}",
        f"HostName={SHIELD_CONTAINER_NAME}",
        f"Image={settings.shield_container_image}",
        *publish_ports,
        f"Volume={state_dir}:/var/lib/cnc/shield:Z",
        f"EnvironmentFile={env_file}",
        "Environment=SHIELD_DB_PATH=/var/lib/cnc/shield/shield.db",
        "Environment=SHIELD_CONFIG_PATH=/var/lib/cnc/shield/shield-config.json",
        "Environment=SHIELD_SESSION_TTL_SEC=604800",
        "LogDriver=journald",
        "Label=io.cnc.managed=true",
        f"Label=io.cnc.backend={SHIELD_BACKEND_NAME}",
        "Exec=cnc-shield",
        "PodmanArgs=--read-only --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m --cap-drop=ALL --security-opt=no-new-privileges",
        "HealthCmd=/usr/bin/python3 -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:1026/health\", timeout=3).read()'",
        "HealthInterval=30s",
        "HealthTimeout=5s",
        "HealthRetries=3",
        "HealthStartPeriod=10s",
        "",
        "[Service]",
        "Restart=always",
        "RestartSec=5",
        f"TimeoutStartSec={max(30, int(settings.command_timeout_apply_sec))}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_shield_env(settings: Settings) -> str:
    # Podman EnvironmentFile does not unquote values — write bare values only.
    values = {
        "SHIELD_SECRET_KEY": settings.shield_secret_key,
        "SHIELD_ACCESS_CODE_HASH": settings.shield_access_code_hash,
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def render_shield_config(output_code_hashes: dict[str, str]) -> str:
    payload = {
        "outputs": {
            name: {"code_hash": code_hash}
            for name, code_hash in sorted(output_code_hashes.items())
            if name and code_hash
        }
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def write_shield_config_asset(
    settings: Settings,
    *,
    output_code_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    changed = _write_text_atomic(
        settings.shield_config_path,
        render_shield_config(output_code_hashes or {}),
        mode=0o600,
    )
    return {
        "config_path": str(settings.shield_config_path),
        "changed_files": [str(settings.shield_config_path)] if changed else [],
    }


def write_shield_runtime_assets(
    settings: Settings,
    *,
    output_code_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    changed_files: list[str] = []
    settings.shield_state_dir.mkdir(parents=True, exist_ok=True)
    if _write_text_atomic(
        shield_quadlet_path(settings), render_shield_quadlet(settings), mode=0o644
    ):
        changed_files.append(str(shield_quadlet_path(settings)))
    if _write_text_atomic(
        settings.shield_env_file_path, render_shield_env(settings), mode=0o600
    ):
        changed_files.append(str(settings.shield_env_file_path))
    config_result = write_shield_config_asset(
        settings, output_code_hashes=output_code_hashes
    )
    changed_files.extend(config_result["changed_files"])
    return {
        "container_unit": SHIELD_CONTAINER_UNIT,
        "container_service": SHIELD_CONTAINER_SERVICE,
        "changed_files": changed_files,
    }


def remove_shield_runtime_assets(settings: Settings) -> list[str]:
    removed: list[str] = []
    for path in (
        shield_quadlet_path(settings),
        settings.shield_env_file_path,
        settings.shield_config_path,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(str(path))
    return removed


def _write_text_atomic(path: Path, content: str, *, mode: int) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        path.chmod(mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(encoded)
    temp_path.chmod(mode)
    temp_path.replace(path)
    path.chmod(mode)
    return True
