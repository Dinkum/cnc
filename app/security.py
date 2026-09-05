from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from collections.abc import Callable
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.logger import get_logger
from app.services.tailscale_admin import current_tailscale_admin_hostnames


_RUNTIME_CSRF_TOKEN = secrets.token_urlsafe(32)
_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
logger = get_logger("security")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def get_csrf_token(settings: Settings) -> str:
    return settings.csrf_token or _RUNTIME_CSRF_TOKEN


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_host(host: str | None) -> str | None:
    if not host:
        return None
    return host.strip().lower().rstrip(".")


def _default_port_for_scheme(scheme: str | None) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _parse_ip(host: str | None) -> ipaddress._BaseAddress | None:
    normalized = _normalize_host(host)
    if not normalized:
        return None
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        return None


def _is_tailscale_client_host(host: str | None) -> bool:
    ip = _parse_ip(host)
    if ip is None:
        return False
    return ip in _TAILSCALE_IPV4 or ip in _TAILSCALE_IPV6


def _is_tailscale_dns_host(host: str | None) -> bool:
    normalized = _normalize_host(host)
    return bool(normalized and normalized.endswith(".ts.net"))


def parse_host_port(value: str | None) -> tuple[str | None, int | None]:
    if not value:
        return None, None
    candidate = value.strip()
    if (
        not candidate
        or candidate.endswith(":")
        or any(character in candidate for character in ",/\\?#@")
        or any(character.isspace() or ord(character) < 0x20 for character in candidate)
    ):
        return None, None
    try:
        parsed = urlparse(f"//{candidate}")
        host = _normalize_host(parsed.hostname)
        port = parsed.port
    except ValueError:
        return None, None
    if (
        not host
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not _host_is_valid(host)
        or port == 0
    ):
        return None, None
    return host, port


def _host_is_valid(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if len(host) > 253:
        return False
    labels = host.split(".")
    return bool(labels) and all(_HOST_LABEL_RE.fullmatch(label) for label in labels)


def _source_matches_request(
    *,
    source_scheme: str,
    source_host: str | None,
    source_port: int | None,
    request_host: str | None,
    request_port: int | None,
) -> bool:
    if source_host != request_host:
        return False
    if request_port is None:
        return source_port == _default_port_for_scheme(source_scheme)
    return source_port == request_port


def _effective_request_host_port(request: Request) -> tuple[str | None, int | None]:
    host = request.headers.get("host")
    effective_host, effective_port = parse_host_port(host)
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if effective_port is None and forwarded_proto:
        scheme = forwarded_proto.split(",", 1)[0].strip().lower()
        effective_port = _default_port_for_scheme(scheme)
    return effective_host, effective_port


def _trusted_hosts_for_request(
    settings: Settings,
    *,
    allowed_hosts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    hosts = {host for host in settings.trusted_host_list}
    hosts.update(
        _normalize_host(host) for host in allowed_hosts if _normalize_host(host)
    )
    hosts.update(current_tailscale_admin_hostnames(settings))
    return tuple(sorted(hosts))


class CNCTrustedHostMiddleware:
    def __init__(
        self,
        app,
        *,
        allowed_hosts: list[str] | tuple[str, ...] | None = None,
        settings_getter: Callable[[], Settings] = get_settings,
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(
            normalized
            for host in (allowed_hosts or [])
            if (normalized := _normalize_host(host))
        )
        self.settings_getter = settings_getter

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        request_host, _request_port = parse_host_port(Headers(scope=scope).get("host"))
        if request_host is None:
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return
        try:
            settings = self.settings_getter()
            trusted_hosts = _trusted_hosts_for_request(
                settings, allowed_hosts=self.allowed_hosts
            )
        except Exception:  # pragma: no cover - defensive path
            trusted_hosts = self.allowed_hosts

        if request_host in trusted_hosts:
            await self.app(scope, receive, send)
            return

        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)


def enforce_origin_policy(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source = origin or referer
    request_client = getattr(request, "client", None)
    client_host = request_client.host if request_client else None

    if source:
        try:
            parsed = urlparse(source)
            source_port = parsed.port or _default_port_for_scheme(parsed.scheme)
        except ValueError:
            parsed = None
            source_port = None
        if parsed is None or parsed.scheme not in {"http", "https"}:
            logger.warning(
                "origin.reject.invalid_scheme",
                source=source,
                host=request.headers.get("host"),
                forwarded_host=request.headers.get("x-forwarded-host"),
                forwarded_proto=request.headers.get("x-forwarded-proto"),
                client_host=client_host,
            )
            raise HTTPException(status_code=403, detail="invalid request origin")
        source_host = _normalize_host(parsed.hostname)
        request_host, request_port = _effective_request_host_port(request)
        if _source_matches_request(
            source_scheme=parsed.scheme,
            source_host=source_host,
            source_port=source_port,
            request_host=request_host,
            request_port=request_port,
        ):
            return
        if (
            parsed.scheme == "https"
            and source_host in current_tailscale_admin_hostnames(settings)
            and _is_tailscale_client_host(client_host)
        ):
            return
        if _is_loopback_host(source_host) and source_port == settings.admin_port:
            return
        logger.warning(
            "origin.reject.mismatch",
            source=source,
            source_host=source_host,
            source_port=source_port,
            host=request.headers.get("host"),
            forwarded_host=request.headers.get("x-forwarded-host"),
            forwarded_proto=request.headers.get("x-forwarded-proto"),
            request_host=request_host,
            request_port=request_port,
            client_host=client_host,
        )
        raise HTTPException(status_code=403, detail="invalid request origin")

    if not _is_loopback_host(client_host):
        logger.warning(
            "origin.reject.source",
            host=request.headers.get("host"),
            forwarded_host=request.headers.get("x-forwarded-host"),
            forwarded_proto=request.headers.get("x-forwarded-proto"),
            client_host=client_host,
        )
        raise HTTPException(status_code=403, detail="invalid request source")


def secure_cookie_required(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    request_url = getattr(request, "url", None)
    scheme = (
        forwarded_proto.split(",", 1)[0].strip().lower()
        if forwarded_proto
        else str(getattr(request_url, "scheme", "") or "").lower()
    )
    if scheme == "https":
        return True
    host, _port = parse_host_port(request.headers.get("host"))
    request_client = getattr(request, "client", None)
    client_host = request_client.host if request_client else None
    return not (_is_loopback_host(host) or _is_loopback_host(client_host))


def enforce_csrf(
    request: Request, settings: Settings, submitted_token: str | None
) -> None:
    enforce_origin_policy(request, settings)
    expected = get_csrf_token(settings)
    if not submitted_token or not hmac.compare_digest(submitted_token, expected):
        raise HTTPException(status_code=403, detail="invalid csrf token")
