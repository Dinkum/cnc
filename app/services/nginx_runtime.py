from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.services.nginx_contract import (
    BRIDGE_NAME,
    CONTAINER_IP,
    CONTAINER_IP_V6,
    GATEWAY,
    GATEWAY_V6,
    HTTP_PORT,
    HTTPS_PORT,
    NETWORK_NAME,
    SUBNET,
    SUBNET_V6,
    TUNNEL_PORT,
    container_ingress_marker,
)
from app.services.validators import resolve_nginx_static_root

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.apply_core import DesiredState
    from app.services.tools.core import Plan


NGINX_IMAGE = (
    "docker.io/library/nginx:1.30.7-alpine@"
    "sha256:b24de00298eb7fb23839442d46c780991be7bc7ddb081b6c0876ac777012e352"
)
NGINX_CONTAINER = "cnc-nginx"
NGINX_UNIT = "cnc-nginx.container"
NGINX_SERVICE = "cnc-nginx.service"
NGINX_NETWORK_UNIT = "cnc-ingress.network"
NGINX_NETWORK_SERVICE = "cnc-ingress-network.service"
LEGACY_SERVICE = "nginx.service"

_USER = "101:101"
_NGINX = "/usr/sbin/nginx"
_CONFIG = "/etc/nginx/cnc-runtime/nginx.conf"
_HEALTH_URL = "http://127.0.0.1:18080/health"
_CERTIFICATE = re.compile(r'^\s*ssl_certificate "([^"\n]+)";', re.MULTILINE)
_TMPFS = "/tmp:rw,noexec,nosuid,nodev,mode=1777,size=256m"
_SECURITY = "--security-opt=no-new-privileges"
_TLS_IDMAP = "idmap=uids=@0-101-1;gids=@0-101-1"


def runtime_directory(control_dir: Path) -> Path:
    return control_dir / "nginx-runtime"


def config_path(control_dir: Path) -> Path:
    return runtime_directory(control_dir) / "config" / "nginx.conf"


def container_routes_directory(control_dir: Path) -> Path:
    return runtime_directory(control_dir) / "routes"


def active_routes_directory(settings: Settings) -> Path:
    if container_ingress_marker(settings).is_file():
        return container_routes_directory(settings.app_control_dir)
    return settings.nginx_generated_dir


def tls_directory(control_dir: Path) -> Path:
    return runtime_directory(control_dir) / "certificates"


def nginx_static_target(source: str | Path) -> str:
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:24]
    return f"/srv/cnc-static/{digest}"


def origin_certificate_assets(
    files: dict[str, str], control_dir: Path
) -> dict[str, str]:
    origin_dir = control_dir / "origin-tls"
    assets = {}
    for content in files.values():
        for value in _CERTIFICATE.findall(content):
            path = Path(value)
            if path.parent != origin_dir or path.suffix != ".pem" or path.is_symlink():
                raise ValueError(f"Invalid NGINX origin certificate path: {path}")
            assets[path.name] = path.read_text(encoding="ascii")
    return assets


def _volume(source: Path, target: str, options: str = "ro,nosuid,nodev,noexec") -> str:
    if not source.is_absolute() or any(char in str(source) for char in ':%\n\r\t"\\ '):
        raise ValueError(f"Cannot bind mount NGINX path {source}")
    return f"{source}:{target}:{options}"


def runtime_volumes(settings: Settings, desired: DesiredState) -> list[str]:
    return [
        _volume(
            config_path(settings.app_control_dir).parent,
            "/etc/nginx/cnc-runtime",
            f"ro,nosuid,nodev,noexec,{_TLS_IDMAP}",
        ),
        _volume(
            container_routes_directory(settings.app_control_dir),
            "/etc/nginx/cnc-generated",
            f"ro,nosuid,nodev,noexec,{_TLS_IDMAP}",
        ),
        _volume(
            tls_directory(settings.app_control_dir),
            str(settings.app_control_dir / "origin-tls"),
            f"ro,nosuid,nodev,noexec,{_TLS_IDMAP}",
        ),
        *(
            _volume(resolve_nginx_static_root(source, settings), nginx_static_target(source))
            for source in desired.nginx_static_roots
        ),
    ]


def render_nginx_config() -> str:
    return f"""worker_processes auto;
pid /tmp/nginx.pid;
error_log stderr warn;

events {{
    worker_connections 4096;
}}

http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout combined buffer=256k flush=1s;
    sendfile on;
    tcp_nopush on;
    keepalive_requests 1000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    open_file_cache max=20000 inactive=60s;
    open_file_cache_valid 30s;
    open_file_cache_min_uses 2;
    client_header_timeout 10s;
    client_body_timeout 10s;
    send_timeout 10s;
    reset_timedout_connection on;
    log_not_found off;
    client_body_temp_path /tmp/client-body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;

    server {{
        listen 127.0.0.1:18080;
        access_log off;
        location = /health {{ return 204; }}
        location / {{ return 404; }}
    }}

    server {{
        listen {HTTP_PORT} default_server;
        listen [::]:{HTTP_PORT} default_server;
        server_name _;
        return 444;
    }}

    include /etc/nginx/cnc-generated/cnc-*.conf;
}}
"""


def render_nginx_network() -> str:
    return f"""[Unit]
Description=CNC public ingress network
Requires=cnc-ingress-firewall.service
After=cnc-ingress-firewall.service

[Network]
NetworkName={NETWORK_NAME}
InterfaceName={BRIDGE_NAME}
Driver=bridge
Subnet={SUBNET}
Gateway={GATEWAY}
Subnet={SUBNET_V6}
Gateway={GATEWAY_V6}
IPv6=true
DisableDNS=true
Label=io.cnc.managed=true
"""


def render_nginx_quadlet(
    settings: Settings, desired: DesiredState, volumes: list[str]
) -> str:
    publications = []
    for enabled, host, guest in (
        (desired.nginx_public_http, 80, HTTP_PORT),
        (desired.nginx_public_https, 443, HTTPS_PORT),
    ):
        if enabled:
            publications.extend(
                (f"0.0.0.0:{host}:{guest}/tcp", f"[::]:{host}:{guest}/tcp")
            )
    if desired.nginx_tunnel:
        publications.append(f"127.0.0.1:{TUNNEL_PORT}:{TUNNEL_PORT}/tcp")
    return "\n".join(
        [
            "[Unit]",
            "Description=CNC public NGINX ingress",
            "After=network-online.target cnc-ingress-firewall.service",
            "Wants=network-online.target",
            "Requires=cnc-ingress-firewall.service",
            f"ConditionPathExists={container_ingress_marker(settings)}",
            "",
            "[Container]",
            f"ContainerName={NGINX_CONTAINER}",
            f"Image={NGINX_IMAGE}",
            "Pull=missing",
            f"Network={NGINX_NETWORK_UNIT}:ip={CONTAINER_IP},ip6={CONTAINER_IP_V6}",
            *[f"PublishPort={item}" for item in publications],
            *[f"Volume={item}" for item in volumes],
            f"User={_USER}",
            "UserNS=auto:size=65536",
            "DropCapability=all",
            "ReadOnly=true",
            "ReadOnlyTmpfs=false",
            f"Tmpfs={_TMPFS}",
            f"PodmanArgs={_SECURITY}",
            f"Entrypoint={_NGINX}",
            f"Exec=-c {_CONFIG} -g 'daemon off;'",
            "LogDriver=journald",
            "Label=io.cnc.managed=true",
            f"HealthCmd=wget -q -O /dev/null {_HEALTH_URL}",
            "HealthInterval=30s",
            "HealthTimeout=3s",
            "HealthRetries=3",
            "",
            "[Service]",
            "Slice=cnc-edge-public-ingress.slice",
            "Restart=on-failure",
            "RestartSec=2s",
            "LimitNOFILE=262144",
            "TimeoutStartSec=300",
            "EnvironmentFile=-/etc/cnc.env",
            "ExecStartPre=/var/lib/cnc/current/.venv/bin/python -m app.services.nginx_firewall",
            f"ExecReload=podman exec {NGINX_CONTAINER} {_NGINX} -c {_CONFIG} -s reload",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def candidate_command(volumes: list[str]) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "--pull=missing",
        "--network=none",
        "--read-only",
        "--read-only-tmpfs=false",
        f"--tmpfs={_TMPFS}",
        "--userns=auto:size=65536",
        f"--user={_USER}",
        "--cap-drop=all",
        _SECURITY,
        f"--entrypoint={_NGINX}",
        *(arg for volume in volumes for arg in ("--volume", volume)),
        NGINX_IMAGE,
        "-c",
        _CONFIG,
        "-t",
    ]


def validate_command(container: bool) -> list[str]:
    return (
        ["podman", "exec", NGINX_CONTAINER, _NGINX, "-c", _CONFIG, "-t"]
        if container
        else ["nginx", "-t"]
    )


def reload_command(container: bool) -> list[str]:
    return ["systemctl", "reload", NGINX_SERVICE if container else "nginx"]


def health_command() -> list[str]:
    return [
        "podman",
        "exec",
        NGINX_CONTAINER,
        "wget",
        "-q",
        "-O",
        "/dev/null",
        _HEALTH_URL,
    ]


async def ensure_nginx_network(plan: Plan) -> bool:
    from app.services.container_runtime import Present
    from app.services.nginx_firewall import bootstrap_nginx_firewall
    from app.services.tools.core import managed_file

    settings = plan.settings
    services = plan.services.runtime_services
    timeout = settings.command_timeout_apply_sec
    await asyncio.to_thread(bootstrap_nginx_firewall, settings, services=services)
    path = settings.app_quadlet_dir / NGINX_NETWORK_UNIT
    with managed_file(path, render_nginx_network()) as changed:
        if changed:
            await services.run_command_checked_async(
                ["systemctl", "daemon-reload"], timeout
            )
        active = await asyncio.to_thread(
            services.run_command,
            ["systemctl", "is-active", "--quiet", NGINX_NETWORK_SERVICE],
            timeout,
        )
        if not active.ok:
            await services.run_command_checked_async(
                ["systemctl", "start", NGINX_NETWORK_SERVICE], timeout
            )
        inspected = await asyncio.to_thread(
            services.inspect_network, NETWORK_NAME, settings.command_timeout_status_sec
        )
        if not isinstance(inspected, Present) or not _network_matches(inspected.value):
            raise RuntimeError(
                "NGINX Podman network differs from the ingress firewall contract"
            )
    return changed or not active.ok


def _network_matches(network: dict) -> bool:
    addresses = {
        (item.get("subnet"), item.get("gateway"))
        for item in network.get("subnets", ())
        if isinstance(item, dict)
    }
    return (
        network.get("driver") == "bridge"
        and network.get("network_interface") == BRIDGE_NAME
        and network.get("dns_enabled") is False
        and addresses == {(SUBNET, GATEWAY), (SUBNET_V6, GATEWAY_V6)}
    )
