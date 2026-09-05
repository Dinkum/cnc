from __future__ import annotations

import ipaddress

from app.config import Settings
from app.services.commands import run_command


TAILSCALE_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def is_tailnet_host(value: str) -> bool:
    host = str(value or "").strip().lower().rstrip(".")
    if host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host) in TAILSCALE_IPV4_NETWORK
    except ValueError:
        return False


def leader_tailnet_ip(settings: Settings) -> str:
    result = run_command(
        ["tailscale", "ip", "-4"],
        timeout_sec=settings.command_timeout_status_sec,
    )
    if not result.ok:
        raise ValueError("leader is not connected to Tailscale")
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if is_tailnet_host(candidate):
            return candidate
    raise ValueError("leader Tailscale IPv4 address is unavailable")
