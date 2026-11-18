from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.change_scope import Scope


NETWORK_NAME = "cnc-ingress"
BRIDGE_NAME = "cnc-ingress0"
SUBNET = "10.89.254.0/24"
GATEWAY = "10.89.254.1"
CONTAINER_IP = "10.89.254.2"
SUBNET_V6 = "fd9d:8be8:8b66:3e31::/64"
GATEWAY_V6 = "fd9d:8be8:8b66:3e31::1"
CONTAINER_IP_V6 = "fd9d:8be8:8b66:3e31::2"
HTTP_PORT = 8080
HTTPS_PORT = 8443
TUNNEL_PORT = 18081


def container_ingress_marker(settings: Settings) -> Path:
    return settings.app_control_dir / "nginx-container-active"


def nginx_gateway_enabled(settings: Settings, *, container_mode: bool = False) -> bool:
    return container_mode or container_ingress_marker(settings).exists()


def container_ingress_enabled(settings: Settings, scope: Scope | None = None) -> bool:
    if settings.multi_node_enabled:
        return False
    return container_ingress_marker(settings).exists() or (
        scope is not None
        and scope.is_full
        and scope.tools.includes("podman")
        and scope.tools.includes("nginx")
    )
