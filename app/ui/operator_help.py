"""Request-aware SSH destinations and operator command presentation."""

from __future__ import annotations

import ipaddress
import socket

from fastapi import Request

from app.config import Settings
from app.models.entities import Backend
from app.services.llm_help import (
    build_backend_llm_help_payload,
    build_backend_operator_commands,
)
from app.services.tailscale_urls import (
    configured_tailnet_admin_host,
    tailscale_service_url,
)


def _is_tailscale_host(hostname: str) -> bool:
    normalized = hostname.strip().rstrip(".").lower()
    if not normalized:
        return False
    if normalized.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def ssh_destination_for_request(request: Request) -> str:
    hostname = (request.url.hostname or "").strip()
    if not hostname:
        return "SERVER_IP"
    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        resolved: list[str] = []
        try:
            addrinfos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return "SERVER_IP"
        for _family, _socktype, _proto, _canonname, sockaddr in addrinfos:
            host = sockaddr[0] if sockaddr else ""
            if not host:
                continue
            try:
                parsed = ipaddress.ip_address(host)
            except ValueError:
                continue
            if parsed.version == 4:
                return str(parsed)
            resolved.append(str(parsed))
        return resolved[0] if resolved else "SERVER_IP"


def _page_ssh_destination(request: Request) -> str:
    # Keep output first paint free of synchronous DNS lookups.
    host = (request.url.hostname or "").strip()
    return host or "SERVER_IP"


def backend_ssh_destination(
    settings: Settings,
    request: Request,
    *,
    resolve_dns: bool,
) -> str:
    hostname = (request.url.hostname or "").strip()
    if hostname and _is_tailscale_host(hostname):
        return hostname
    tailnet_host = configured_tailnet_admin_host(settings)
    if tailnet_host:
        return tailnet_host
    configured = str(settings.ssh_advertise_host or "").strip()
    if configured:
        return configured
    if not hostname:
        return "SERVER_IP"
    return (
        ssh_destination_for_request(request)
        if resolve_dns
        else _page_ssh_destination(request)
    )


def _backend_operator_commands(
    backend: Backend, request: Request
) -> list[dict[str, str]]:
    if backend.kind == "app" and backend.enabled is False:
        return []
    host = ssh_destination_for_request(request)
    payload = build_backend_llm_help_payload(
        backend,
        host=host,
        llm_help_url=output_llm_help_url(request, backend.id),
    )
    return build_backend_operator_commands(payload)


def backend_operator_commands_for_page(
    backend: Backend,
    *,
    settings: Settings,
    host: str,
    llm_help_url: str,
) -> list[dict[str, str]]:
    if backend.kind == "app" and backend.enabled is False:
        return []
    payload = build_backend_llm_help_payload(
        backend,
        host=host,
        llm_help_url=llm_help_url,
    )
    commands = build_backend_operator_commands(payload)
    loaded_inputs = backend.__dict__.get("inputs")
    if isinstance(loaded_inputs, list):
        for item in sorted(
            loaded_inputs,
            key=lambda entry: (str(entry.kind or ""), str(entry.hostname or "")),
        ):
            if str(item.kind or "domain").strip().lower() != "tailnet_service":
                continue
            url = tailscale_service_url(str(item.hostname or ""), settings)
            if not url:
                continue
            commands.insert(
                0,
                {
                    "label": "tailscale url",
                    "command": url,
                    "note": "Root-path tailnet URL for this output.",
                },
            )
    return commands


def host_llm_help_url(request: Request) -> str:
    try:
        return str(request.url_for("host_llm_help"))
    except RuntimeError:
        host = request.headers.get("host") or request.url.netloc or "SERVER_IP"
        scheme = request.url.scheme or "http"
        return f"{scheme}://{host}/llm.txt"


def output_llm_help_url(request: Request, backend_id: int | None) -> str:
    if isinstance(backend_id, int):
        try:
            return str(request.url_for("output_llm_help", backend_id=backend_id))
        except RuntimeError:
            pass
    host = request.headers.get("host") or request.url.netloc or "SERVER_IP"
    scheme = request.url.scheme or "http"
    backend_segment = str(backend_id) if isinstance(backend_id, int) else "BACKEND_ID"
    return f"{scheme}://{host}/outputs/{backend_segment}/llm.txt"
