from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import Settings
from app.security import enforce_origin_policy, parse_host_port, secure_cookie_required
from app.services.backend_ssh_audit import (
    BACKEND_SSH_INTERNAL_PORT,
    backend_ssh_internal_token_matches,
)


ACCESS_COOKIE_NAME = "cnc_access"
ACCESS_ROUTE_PATH = "/access"
_ACCESS_COOKIE_VERSION = "v1"
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16
)
_ATTEMPT_WINDOW_SEC = 600
_ATTEMPT_LOCK = threading.Lock()


@dataclass
class AccessAttemptState:
    failures: int
    blocked_until: float
    last_seen: float


_ATTEMPTS: dict[str, AccessAttemptState] = {}


def access_key_is_configured(settings: Settings) -> bool:
    return bool(str(settings.access_key_hash or "").strip())


def normalize_access_key(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) < 12:
        raise ValueError("access key must be at least 12 characters")
    if len(normalized) > 256:
        raise ValueError("access key must be 256 characters or fewer")
    if any(ch in normalized for ch in ("\r", "\n", "\x00")):
        raise ValueError("access key cannot contain control characters")
    return normalized


def hash_access_key(value: str) -> str:
    return _PASSWORD_HASHER.hash(normalize_access_key(value))


def verify_access_key(settings: Settings, value: str) -> bool:
    stored_hash = str(settings.access_key_hash or "").strip()
    if not stored_hash:
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(stored_hash, normalize_access_key(value)))
    except (InvalidHashError, VerificationError, VerifyMismatchError, ValueError):
        return False


def access_key_needs_rehash(settings: Settings) -> bool:
    stored_hash = str(settings.access_key_hash or "").strip()
    if not stored_hash:
        return False
    try:
        return _PASSWORD_HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False


def _cookie_secret(settings: Settings) -> bytes:
    return str(settings.access_key_hash or "").encode("utf-8")


def _cookie_signature(payload: str, settings: Settings) -> str:
    return hmac.new(
        _cookie_secret(settings), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def build_access_cookie_value(settings: Settings, *, now: int | None = None) -> str:
    issued_at = int(now or time.time())
    expires_at = issued_at + settings.access_session_ttl_sec
    nonce = secrets.token_urlsafe(8)
    payload = f"{_ACCESS_COOKIE_VERSION}.{expires_at}.{nonce}"
    signature = _cookie_signature(payload, settings)
    return f"{payload}.{signature}"


def access_cookie_is_valid(
    settings: Settings, cookie_value: str | None, *, now: int | None = None
) -> bool:
    if not access_key_is_configured(settings):
        return False
    if not cookie_value:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 4:
        return False
    version, expires_raw, nonce, signature = parts
    if (
        version != _ACCESS_COOKIE_VERSION
        or not expires_raw.isdigit()
        or not nonce
        or not signature
    ):
        return False
    payload = ".".join((version, expires_raw, nonce))
    expected = _cookie_signature(payload, settings)
    if not hmac.compare_digest(signature, expected):
        return False
    return int(expires_raw) >= int(now or time.time())


def set_access_cookie(
    response: Response, settings: Settings, *, request: Request | None = None
) -> None:
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        build_access_cookie_value(settings),
        max_age=settings.access_session_ttl_sec,
        httponly=True,
        secure=True if request is None else secure_cookie_required(request),
        samesite="lax",
        path="/",
    )


def clear_access_cookie(response: Response, *, request: Request | None = None) -> None:
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        secure=True if request is None else secure_cookie_required(request),
    )


def access_request_is_authenticated(request: Request, settings: Settings) -> bool:
    if not access_key_is_configured(settings):
        return False
    return access_cookie_is_valid(settings, request.cookies.get(ACCESS_COOKIE_NAME))


def _cleanup_attempts(now: float) -> None:
    stale = [
        key
        for key, state in _ATTEMPTS.items()
        if now - state.last_seen > _ATTEMPT_WINDOW_SEC
    ]
    for key in stale:
        _ATTEMPTS.pop(key, None)


def access_attempt_backoff_seconds(
    client_host: str | None, *, now: float | None = None
) -> int:
    if not client_host:
        return 0
    current_time = float(now or time.time())
    with _ATTEMPT_LOCK:
        _cleanup_attempts(current_time)
        state = _ATTEMPTS.get(client_host)
        if state is None or state.blocked_until <= current_time:
            return 0
        return max(0, math.ceil(state.blocked_until - current_time))


def record_access_attempt(
    client_host: str | None, *, success: bool, now: float | None = None
) -> None:
    if not client_host:
        return
    current_time = float(now or time.time())
    with _ATTEMPT_LOCK:
        _cleanup_attempts(current_time)
        if success:
            _ATTEMPTS.pop(client_host, None)
            return
        current = _ATTEMPTS.get(client_host)
        failures = 1 if current is None else current.failures + 1
        delay = min(30, 2 ** min(failures - 1, 5))
        _ATTEMPTS[client_host] = AccessAttemptState(
            failures=failures,
            blocked_until=current_time + delay,
            last_seen=current_time,
        )


def access_origin_guard(request: Request, settings: Settings) -> None:
    enforce_origin_policy(request, settings)


def _request_scope_path(request: Request) -> str:
    path = request.scope.get("path")
    if isinstance(path, str) and path.startswith("/"):
        return path
    return "/"


def access_redirect_target(request: Request) -> str:
    path = _request_scope_path(request)
    raw_query = request.scope.get("query_string", b"")
    query = (
        raw_query.decode("latin-1")
        if isinstance(raw_query, bytes)
        else str(raw_query or "")
    )
    if query:
        path = f"{path}?{query}"
    return path


def sanitize_next_path(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate.startswith("/"):
        return "/"
    decoded = candidate
    for _attempt in range(3):
        next_decoded = unquote(decoded, errors="replace")
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if decoded.startswith("//") or "\\" in decoded:
        return "/"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
        return "/"
    if decoded.startswith(f"{ACCESS_ROUTE_PATH}/") or decoded == ACCESS_ROUTE_PATH:
        return "/"
    return candidate or "/"


def access_route_is_exempt(path: str) -> bool:
    if path == ACCESS_ROUTE_PATH:
        return True
    if path.startswith("/join/"):
        return True
    if path in {"/api/health", "/live", "/ready"}:
        return True
    return path.startswith("/static/")


def internal_backend_ssh_request_is_authorized(request: Request) -> bool:
    if not _request_scope_path(request).startswith("/internal/backend-ssh/"):
        return False
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            return False
    except ValueError:
        return False
    if any(
        request.headers.get(name)
        for name in (
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-real-ip",
        )
    ):
        return False
    host, port = parse_host_port(request.headers.get("host"))
    host = host or ""
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    if port not in {None, BACKEND_SSH_INTERNAL_PORT}:
        return False
    expected_token = str(
        getattr(request.app.state, "backend_ssh_internal_token", "") or ""
    )
    return backend_ssh_internal_token_matches(
        expected_token,
        request.headers.get("x-cnc-internal-token"),
    )


def build_access_required_response(request: Request, settings: Settings):
    path = _request_scope_path(request)
    if access_route_is_exempt(path):
        return None
    if internal_backend_ssh_request_is_authorized(request):
        return None
    if path.startswith("/internal/backend-ssh/"):
        return Response(status_code=404)
    if access_key_is_configured(settings) and access_request_is_authenticated(
        request, settings
    ):
        return None
    if path.startswith("/api/"):
        detail = (
            "access key required"
            if access_key_is_configured(settings)
            else "access key setup required"
        )
        return JSONResponse(status_code=401, content={"detail": detail})
    if path == "/llm.txt" or path.endswith("/llm.txt"):
        return Response(status_code=401)
    if (
        "text/html" in request.headers.get("accept", "")
        or request.headers.get("accept", "") == ""
    ):
        next_path = quote(access_redirect_target(request), safe="")
        return RedirectResponse(
            url=f"{ACCESS_ROUTE_PATH}?next={next_path}", status_code=303
        )
    next_path = quote(access_redirect_target(request), safe="")
    return RedirectResponse(
        url=f"{ACCESS_ROUTE_PATH}?next={next_path}", status_code=303
    )
