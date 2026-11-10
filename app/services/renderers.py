from __future__ import annotations

from app.services import cloudflare_ingress
from app.services.cloudflare_tunnel import TUNNEL_ORIGIN_PORT

SHIELD_BACKEND_NAME = "shield"
SHIELD_PROFILE = "shield"
SHIELD_PORT = 1026
SHIELD_PUBLIC_PATH_PREFIX = "/shield"
SHIELD_AUTH_PATH = "/shield/check"


def safe_slug(raw: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch == "-" else "-" for ch in raw.lower()
    ).strip("-")


def nginx_filename(hostname: str) -> str:
    return f"cnc-host-{safe_slug(hostname)}.conf"


def container_name(backend_name: str) -> str:
    return f"cnc-app-{safe_slug(backend_name)}"


def network_name(backend_name: str) -> str:
    return f"cnc-net-{safe_slug(backend_name)}"


def upstream_name(hostname: str) -> str:
    return f"cnc_upstream_{safe_slug(hostname).replace('-', '_')}"


def _cloudflare_acl_lines(enabled: bool, cloudflare_ips: tuple[str, ...]) -> list[str]:
    if not enabled:
        return []

    policy = cloudflare_ingress.build_cloudflare_ingress_policy(cloudflare_ips)
    return cloudflare_ingress.render_cloudflare_nginx_acl(policy)


def _shield_location_lines(
    shield_upstream: str, *, shield_output_key: str, cloudflare_only: bool
) -> list[str]:
    # Shield limits guesses per client, so the client must be named by nginx,
    # never by the request. Behind the Cloudflare-only ACL every peer is a
    # Cloudflare edge, which overwrites CF-Connecting-IP; anywhere else that
    # header is client-supplied and the peer address is the only fact.
    client_ip = "$http_cf_connecting_ip" if cloudflare_only else "$remote_addr"
    forwarded = [
        f"        proxy_set_header X-Shield-Output {shield_output_key};",
        f"        proxy_set_header X-CNC-Client-IP {client_ip};",
        "        proxy_set_header X-Real-IP $remote_addr;",
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;",
    ]
    return [
        f"    location = {SHIELD_AUTH_PATH} {{",
        "        internal;",
        f"        proxy_pass http://{shield_upstream}/check;",
        "        proxy_pass_request_body off;",
        '        proxy_set_header Content-Length "";',
        "        proxy_set_header X-Original-URI $request_uri;",
        "        proxy_set_header X-Original-Host $host;",
        *forwarded,
        "    }",
        "",
        "    location @cnc_shield_login {",
        f"        return 302 {SHIELD_PUBLIC_PATH_PREFIX}/access?next=$request_uri;",
        "    }",
        "",
        f"    location {SHIELD_PUBLIC_PATH_PREFIX}/ {{",
        "        proxy_http_version 1.1;",
        '        proxy_set_header Connection "";',
        f"        proxy_pass http://{shield_upstream}/;",
        "        proxy_set_header Host $host;",
        *forwarded,
        "    }",
        "",
    ]


def _tunnel_scheme_map(hostname: str) -> list[str]:
    variable = f"$cnc_tunnel_proto_{safe_slug(hostname).replace('-', '_')}"
    return [
        f"map $http_x_forwarded_proto {variable} {{",
        "    default https;",
        "    http http;",
        "    https https;",
        "}",
        "",
    ]


def _tunnel_headers(config: str, hostname: str) -> str:
    variable = f"$cnc_tunnel_proto_{safe_slug(hostname).replace('-', '_')}"
    return (
        config.replace("X-Forwarded-Proto $scheme", f"X-Forwarded-Proto {variable}")
        .replace(
            "X-Forwarded-For $proxy_add_x_forwarded_for", "X-Forwarded-For $remote_addr"
        )
        .replace(
            "proxy_set_header X-Real-IP $remote_addr;",
            'proxy_set_header Forwarded "";\n        proxy_set_header X-Real-IP $remote_addr;',
        )
    )


def _tunnel_client_ip() -> list[str]:
    return [
        "    set_real_ip_from 127.0.0.1;",
        "    real_ip_header CF-Connecting-IP;",
        "",
    ]


def render_nginx_http(
    hostname: str,
    ports: int | str | list[int | str],
    *,
    cloudflare_only: bool = False,
    cloudflare_ips: tuple[str, ...] = (),
    tunnel: bool = False,
    upstream_suffix: str = "",
    tls_certificate: str | None = None,
    shield_enabled: bool = False,
    shield_port: int = SHIELD_PORT,
    shield_target: int | str | None = None,
    shield_output_key: str = "global",
) -> str:
    upstream = upstream_name(hostname) + upstream_suffix
    if isinstance(ports, int | str):
        upstream_servers = [_nginx_upstream_server(ports)]
    else:
        upstream_servers = [_nginx_upstream_server(port) for port in ports]
    shield_upstream = f"{upstream}_shield"
    lines = [
        f"upstream {upstream} {{",
    ]
    lines.extend(f"    server {server};" for server in upstream_servers)
    lines.extend(
        [
            "    keepalive 64;",
            "}",
            "",
        ]
    )
    if shield_enabled:
        shield_server = _nginx_upstream_server(
            shield_target if shield_target is not None else shield_port
        )
        lines.extend(
            [
                f"upstream {shield_upstream} {{",
                f"    server {shield_server};",
                "    keepalive 16;",
                "}",
                "",
            ]
        )
    lines.extend(
        [
            "server {",
            f"    listen 127.0.0.1:{TUNNEL_ORIGIN_PORT};"
            if tunnel
            else "    listen 80;",
            f"    server_name {hostname};",
            "",
        ]
    )
    lines.extend(
        _tunnel_client_ip()
        if tunnel
        else _cloudflare_acl_lines(cloudflare_only, cloudflare_ips)
    )
    if tls_certificate and not tunnel:
        lines.extend(
            [
                "    listen 443 ssl;",
                f'    ssl_certificate "{tls_certificate}";',
                f'    ssl_certificate_key "{tls_certificate}";',
                "    ssl_protocols TLSv1.2 TLSv1.3;",
            ]
        )
    if shield_enabled:
        lines.extend(
            _shield_location_lines(
                shield_upstream,
                shield_output_key=shield_output_key,
                cloudflare_only=cloudflare_only,
            )
        )
    lines.extend(
        [
            "    location / {",
            *(
                [
                    f"        auth_request {SHIELD_AUTH_PATH};",
                    "        error_page 401 = @cnc_shield_login;",
                ]
                if shield_enabled
                else []
            ),
            "        proxy_http_version 1.1;",
            '        proxy_set_header Connection "";',
            f"        proxy_pass http://{upstream};",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
            "}",
            "",
        ]
    )
    config = "\n".join((_tunnel_scheme_map(hostname) if tunnel else []) + lines)
    return _tunnel_headers(config, hostname) if tunnel else config


def _nginx_upstream_server(value: int | str) -> str:
    if isinstance(value, int):
        return f"127.0.0.1:{value}"
    normalized = str(value or "").strip()
    if not normalized:
        return "127.0.0.1:0"
    if normalized.startswith(("http://", "https://")):
        normalized = normalized.split("://", 1)[1].rstrip("/")
    if ":" not in normalized:
        return f"127.0.0.1:{normalized}"
    return normalized


def render_nginx_shield_closed(
    hostname: str,
    *,
    cloudflare_only: bool = False,
    cloudflare_ips: tuple[str, ...] = (),
) -> str:
    lines = [
        "server {",
        "    listen 80;",
        f"    server_name {hostname};",
        "",
    ]
    lines.extend(_cloudflare_acl_lines(cloudflare_only, cloudflare_ips))
    lines.extend(
        [
            "    location / {",
            "        return 404;",
            "    }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def render_nginx_static(
    hostname: str,
    static_root: str,
    *,
    cloudflare_only: bool = False,
    cloudflare_ips: tuple[str, ...] = (),
    tunnel: bool = False,
    upstream_suffix: str = "",
    tls_certificate: str | None = None,
    shield_enabled: bool = False,
    shield_port: int = SHIELD_PORT,
    shield_target: int | str | None = None,
    shield_output_key: str = "global",
) -> str:
    upstream = upstream_name(hostname) + upstream_suffix
    shield_upstream = f"{upstream}_shield"
    lines: list[str] = []
    if shield_enabled:
        shield_server = _nginx_upstream_server(
            shield_target if shield_target is not None else shield_port
        )
        lines.extend(
            [
                f"upstream {shield_upstream} {{",
                f"    server {shield_server};",
                "    keepalive 16;",
                "}",
                "",
            ]
        )
    lines.extend(
        [
            "server {",
            f"    listen 127.0.0.1:{TUNNEL_ORIGIN_PORT};"
            if tunnel
            else "    listen 80;",
            f"    server_name {hostname};",
            f"    root {static_root};",
            "    index index.html;",
            "",
        ]
    )
    lines.extend(
        _tunnel_client_ip()
        if tunnel
        else _cloudflare_acl_lines(cloudflare_only, cloudflare_ips)
    )
    if tls_certificate and not tunnel:
        lines.extend(
            [
                "    listen 443 ssl;",
                f'    ssl_certificate "{tls_certificate}";',
                f'    ssl_certificate_key "{tls_certificate}";',
                "    ssl_protocols TLSv1.2 TLSv1.3;",
            ]
        )
    if shield_enabled:
        lines.extend(
            _shield_location_lines(
                shield_upstream,
                shield_output_key=shield_output_key,
                cloudflare_only=cloudflare_only,
            )
        )
    lines.extend(
        [
            "    location / {",
            *(
                [
                    f"        auth_request {SHIELD_AUTH_PATH};",
                    "        error_page 401 = @cnc_shield_login;",
                ]
                if shield_enabled
                else []
            ),
            "        try_files $uri $uri/ =404;",
            "    }",
            "}",
            "",
        ]
    )
    config = "\n".join((_tunnel_scheme_map(hostname) if tunnel else []) + lines)
    return _tunnel_headers(config, hostname) if tunnel else config


def render_follower_admin_proxy(
    leader_admin_host: str,
    *,
    node_uid: str,
    listen: str = "127.0.0.1:9091",
    server_names: tuple[str, ...] = ("_",),
) -> str:
    server_name = " ".join(dict.fromkeys(name for name in server_names if name)) or "_"
    return "\n".join(
        [
            "server {",
            f"    listen {listen};",
            f"    server_name {server_name};",
            "",
            "    allow 127.0.0.1;",
            "    allow ::1;",
            "    allow 100.64.0.0/10;",
            "    deny all;",
            "",
            "    location = /node-healthz {",
            "        access_log off;",
            f"        add_header X-CNC-Node {node_uid};",
            "        return 200 'ok\\n';",
            "    }",
            "",
            "    location / {",
            "        proxy_http_version 1.1;",
            '        proxy_set_header Connection "";',
            f"        proxy_pass https://{leader_admin_host};",
            "        proxy_ssl_server_name on;",
            f"        proxy_ssl_name {leader_admin_host};",
            f"        proxy_set_header Host {leader_admin_host};",
            f"        proxy_set_header X-CNC-Follower-Node {node_uid};",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto https;",
            "    }",
            "}",
            "",
        ]
    )


def render_nginx_https_default(
    *, cloudflare_only: bool = False, cloudflare_ips: tuple[str, ...] = ()
) -> str:
    lines = [
        "server {",
        "    listen 443 ssl default_server;",
        "    ssl_reject_handshake on;",
        "    ssl_protocols TLSv1.2 TLSv1.3;",
    ]
    lines.extend(_cloudflare_acl_lines(cloudflare_only, cloudflare_ips))
    lines.extend(["    return 404;", "}", ""])
    return "\n".join(lines)
