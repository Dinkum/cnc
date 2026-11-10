from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import urllib.error
import urllib.request
from uuid import UUID

from app.config import Settings
from app.services.cloudflare_api import CloudflareError
from app.services.cloudflare_connection import section, token_fingerprint, write_section
from app.services.commands import run_command
from app.services.host_state import write_text_atomic
from app.services.managed_env import apply_managed_env_updates


CLOUDFLARED_IMAGE = "docker.io/cloudflare/cloudflared:2026.10.0"
TUNNEL_ORIGIN_PORT = 18081
TUNNEL_METRICS_PORT = 18082
SERVICE = "cnc-cloudflare-tunnel.service"
CONTAINER = "cnc-cloudflare-tunnel"
DASHBOARD_URL = "https://dash.cloudflare.com/?to=/:account/tunnels"


def tunnel_identity(token: str) -> str:
    try:
        if not token or len(token) > 16384:
            raise ValueError
        payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or not all(
            isinstance(payload.get(key), str) for key in ("t", "a", "s")
        ):
            raise ValueError
        identity = UUID(payload["t"])
        if not identity.int or not re.fullmatch(r"[a-fA-F0-9]{32}", payload["a"]):
            raise ValueError
        secret = base64.b64decode(payload["s"], validate=True)
        if len(secret) != 32:
            raise ValueError
        return str(identity)
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise CloudflareError(
            "Paste the Tunnel token from Cloudflare's connector installation command."
        ) from None


def token_path(settings: Settings) -> Path:
    return settings.app_control_dir.parent / "cloudflare-tunnel" / "token"


def tunnel_summary(settings: Settings) -> dict[str, Any]:
    token = settings.cloudflare_tunnel_token
    state = section(settings, "cloudflare_tunnel")
    matches = bool(token) and state.get("fingerprint") == token_fingerprint(token)
    connected = matches and state.get("status") == "connected"
    return {
        "configured": bool(token),
        "connected": connected,
        "status": "Last check: connected"
        if connected
        else "Check connector"
        if token
        else "Not configured",
        "token_display": f"•••• {token[-4:]}" if token else "",
        "tunnel_id": state.get("tunnel_id", "") if matches else "",
        "error": state.get("error", "") if matches else "",
        "checked_at": state.get("checked_at", "") if matches else "",
        "service_url": f"http://127.0.0.1:{TUNNEL_ORIGIN_PORT}",
        "dashboard_url": DASHBOARD_URL,
    }


def connector_ready() -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{TUNNEL_METRICS_PORT}/ready")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _command(settings: Settings, command: list[str]) -> None:
    result = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
    if not result.ok:
        # Upstream output may contain connector credentials. Keep it out of errors and operation records.
        raise CloudflareError(
            "The Tunnel connector could not start. Check its service and outbound connectivity, then retry."
        )


def _write_token(settings: Settings, token: str) -> None:
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    write_text_atomic(path, token, encoding="utf-8")
    path.chmod(0o600)


def _install_service(settings: Settings) -> None:
    from app.services.runtime_assets import PACKAGING_DIR

    source = PACKAGING_DIR / SERVICE
    target = settings.systemd_generated_dir / SERVICE
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_bytes() != source.read_bytes():
        write_text_atomic(target, source.read_text(), encoding="utf-8")
        target.chmod(0o644)
        _command(settings, ["systemctl", "daemon-reload"])


def _wait_ready(settings: Settings) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if connector_ready():
            return
        time.sleep(0.25)
    raise CloudflareError(
        "The connector has not reached Cloudflare. Check outbound access and the Tunnel token, then retry."
    )


def configure_tunnel(settings: Settings, action: str, token: str = "") -> Settings:
    previous = settings.cloudflare_tunnel_token
    if action == "disconnect":
        _command(settings, ["systemctl", "disable", "--now", SERVICE])
        next_settings = apply_managed_env_updates(
            settings, {"CLOUDFLARE_TUNNEL_TOKEN": None}
        )
        _write_token(settings, "")
        write_section(settings, "cloudflare_tunnel", {})
        return next_settings
    candidate = token.strip() if action == "connect" else previous
    identity = tunnel_identity(candidate)
    _install_service(settings)
    _command(settings, ["podman", "pull", CLOUDFLARED_IMAGE])
    try:
        _write_token(settings, candidate)
        _command(settings, ["systemctl", "enable", SERVICE])
        _command(settings, ["systemctl", "restart", SERVICE])
        _wait_ready(settings)
        next_settings = apply_managed_env_updates(
            settings, {"CLOUDFLARE_TUNNEL_TOKEN": candidate}
        )
    except (CloudflareError, OSError):
        _write_token(settings, previous)
        if previous:
            _command(settings, ["systemctl", "restart", SERVICE])
        else:
            _command(settings, ["systemctl", "disable", "--now", SERVICE])
        if action == "retest":
            write_section(
                settings,
                "cloudflare_tunnel",
                {
                    "fingerprint": token_fingerprint(previous),
                    "tunnel_id": identity,
                    "status": "error",
                    "error": "The connector could not be verified. Retest after checking the service.",
                    "checked_at": datetime.now(UTC).isoformat(),
                },
            )
        raise
    write_section(
        settings,
        "cloudflare_tunnel",
        {
            "fingerprint": token_fingerprint(candidate),
            "tunnel_id": identity,
            "status": "connected",
            "error": "",
            "checked_at": datetime.now(UTC).isoformat(),
        },
    )
    return next_settings


def reconcile_tunnel(settings: Settings) -> bool:
    if not settings.cloudflare_tunnel_token:
        return False
    _install_service(settings)
    path = token_path(settings)
    changed = not path.exists() or path.read_text() != settings.cloudflare_tunnel_token
    if changed:
        _write_token(settings, settings.cloudflare_tunnel_token)
    active = run_command(
        ["systemctl", "is-active", "--quiet", SERVICE],
        timeout_sec=settings.command_timeout_status_sec,
    )
    if changed or not active.ok:
        _command(settings, ["systemctl", "enable", SERVICE])
        _command(settings, ["systemctl", "restart", SERVICE])
        _wait_ready(settings)
        return True
    return False


def connector_command(settings: Settings) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "--replace",
        "--pull=never",
        "--name",
        CONTAINER,
        "--network",
        "host",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "0:0",
        "--volume",
        f"{token_path(settings)}:/run/tunnel-token:ro",
        CLOUDFLARED_IMAGE,
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        f"127.0.0.1:{TUNNEL_METRICS_PORT}",
        "run",
        "--token-file",
        "/run/tunnel-token",
    ]


if __name__ == "__main__":
    from app.config import get_settings

    settings = get_settings()
    tunnel_identity(token_path(settings).read_text())
    command = connector_command(settings)
    os.execvp(command[0], command)
