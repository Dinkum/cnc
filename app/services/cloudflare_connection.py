"""Cloudflare connection metadata; credentials stay in the managed environment."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any

from app.config import Settings
from app.services.cloudflare_api import CloudflareClient, CloudflareError
from app.services.host_state import (
    LockedStateSectionTransaction,
    read_host_state_unlocked,
)
from app.services.managed_env import apply_managed_env_updates


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


def section(settings: Settings, name: str) -> dict[str, Any]:
    value = read_host_state_unlocked(settings.host_state_path).get(name, {})
    return value if isinstance(value, dict) else {}


def write_section(settings: Settings, name: str, value: dict[str, Any]) -> None:
    with LockedStateSectionTransaction(settings.host_state_path, section=name) as state:
        state.replace(value)


def connection_summary(settings: Settings) -> dict[str, Any]:
    state = section(settings, "cloudflare_connection")
    token = settings.cloudflare_api_token
    matches = bool(token) and state.get("fingerprint") == token_fingerprint(token)
    connected = matches and state.get("status") == "connected"
    expiry = state.get("expires_on")
    if expiry:
        try:
            connected = connected and datetime.fromisoformat(
                expiry.replace("Z", "+00:00")
            ) > datetime.now(UTC)
        except ValueError:
            connected = False
    return {
        "configured": bool(token),
        "connected": bool(connected),
        "status": "Connected"
        if connected
        else "Reconnect"
        if token
        else "Not connected",
        "token_display": f"•••• {token[-4:]}" if token else "",
        "zones": state.get("zones", []) if matches or not token else [],
        "checked_at": state.get("checked_at", ""),
        "error": state.get("error", ""),
        "expires_on": expiry or "",
    }


def matching_zone(
    settings: Settings, hostname: str, *, require_connected: bool = True
) -> dict[str, str] | None:
    summary = connection_summary(settings)
    if require_connected and not summary["connected"]:
        return None
    zones = [
        zone
        for zone in summary["zones"]
        if hostname == zone["name"] or hostname.endswith("." + zone["name"])
    ]
    return max(zones, key=lambda zone: len(zone["name"]), default=None)


def test_connection(settings: Settings, token: str) -> dict[str, Any]:
    client = CloudflareClient(token)
    result = client.request("GET", "/user/tokens/verify").get("result", {})
    if result.get("status") != "active":
        raise CloudflareError("This Cloudflare token is not active.")
    zones = client.zones()
    if not zones:
        raise CloudflareError(
            "No zones are accessible. Add Zone → Zone → Read and select the domains CNC should manage."
        )
    return {
        "fingerprint": token_fingerprint(token),
        "status": "connected",
        "checked_at": datetime.now(UTC).isoformat(),
        "expires_on": result.get("expires_on"),
        "zones": zones,
        "error": "",
    }


def configure_connection(settings: Settings, action: str, token: str = "") -> Settings:
    if action == "disconnect":
        next_settings = apply_managed_env_updates(
            settings, {"CLOUDFLARE_API_TOKEN": None}
        )
        state = section(settings, "cloudflare_connection")
        write_section(
            settings,
            "cloudflare_connection",
            {**state, "status": "disconnected", "error": ""},
        )
        return next_settings
    candidate = token.strip() if action == "connect" else settings.cloudflare_api_token
    try:
        state = test_connection(settings, candidate)
    except CloudflareError as exc:
        if action == "retest":
            previous = section(settings, "cloudflare_connection")
            write_section(
                settings,
                "cloudflare_connection",
                {
                    **previous,
                    "status": "error",
                    "error": str(exc),
                    "checked_at": datetime.now(UTC).isoformat(),
                },
            )
        raise
    next_settings = (
        apply_managed_env_updates(settings, {"CLOUDFLARE_API_TOKEN": candidate})
        if action == "connect"
        else settings
    )
    if settings.managed_env_file_path.exists():
        settings.managed_env_file_path.chmod(0o600)
    write_section(settings, "cloudflare_connection", state)
    return next_settings
