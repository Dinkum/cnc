from types import SimpleNamespace

from starlette.requests import Request

from app.access import (
    ACCESS_COOKIE_NAME,
    access_attempt_backoff_seconds,
    access_cookie_is_valid,
    access_key_is_configured,
    build_access_cookie_value,
    build_access_required_response,
    hash_access_key,
    record_access_attempt,
    sanitize_next_path,
    verify_access_key,
)
from app.config import Settings


def _request(
    *,
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
    cookies: dict[str, str] | None = None,
    backend_ssh_token: str = "",
) -> Request:
    raw_headers = list(headers or [])
    if cookies:
        cookie_value = "; ".join(
            f"{key}={value}" for key, value in cookies.items()
        ).encode()
        raw_headers.append((b"cookie", cookie_value))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 9090),
            "app": SimpleNamespace(
                state=SimpleNamespace(backend_ssh_internal_token=backend_ssh_token)
            ),
        }
    )


def test_access_key_hash_round_trip() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))

    assert access_key_is_configured(settings) is True
    assert verify_access_key(settings, "correct horse battery staple") is True
    assert verify_access_key(settings, "wrong key") is False


def test_access_cookie_invalidates_when_hash_changes() -> None:
    old_settings = Settings(access_key_hash=hash_access_key("alpha access key"))
    cookie_value = build_access_cookie_value(old_settings, now=1_700_000_000)

    assert access_cookie_is_valid(old_settings, cookie_value, now=1_700_000_100) is True

    new_settings = Settings(access_key_hash=hash_access_key("beta access key"))
    assert (
        access_cookie_is_valid(new_settings, cookie_value, now=1_700_000_100) is False
    )


def test_access_required_redirects_html_when_cookie_missing() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(headers=[(b"accept", b"text/html")])

    response = build_access_required_response(request, settings)

    assert response is not None
    assert response.status_code == 303
    assert response.headers["location"].startswith("/access?next=")


def test_access_required_allows_valid_cookie() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    cookie_value = build_access_cookie_value(settings)
    request = _request(
        headers=[(b"accept", b"text/html")],
        cookies={ACCESS_COOKIE_NAME: cookie_value},
    )

    response = build_access_required_response(request, settings)

    assert response is None


def test_access_required_returns_json_for_api() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(path="/api/status")

    response = build_access_required_response(request, settings)

    assert response is not None
    assert response.status_code == 401
    assert response.body == b'{"detail":"access key required"}'


def test_access_gate_uses_raw_scope_path_when_host_header_contains_path() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(
        path="/api/status",
        headers=[(b"host", b"localhost/static/x?ignored=")],
    )

    # Starlette 1.0 reconstructed request.url.path as /static/x for this malformed
    # authority. The access decision must always use the server-provided ASGI path.
    response = build_access_required_response(request, settings)

    assert response is not None
    assert response.status_code == 401
    assert response.body == b'{"detail":"access key required"}'


def test_join_bootstrap_routes_bypass_access_key_gate() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(path="/join/install.sh")

    response = build_access_required_response(request, settings)

    assert response is None


def test_internal_backend_ssh_route_requires_runtime_token() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(
        path="/internal/backend-ssh/llm-help/worker",
        headers=[
            (b"host", b"127.0.0.1:9090"),
            (b"x-cnc-internal-token", b"runtime-secret"),
        ],
        backend_ssh_token="runtime-secret",
    )

    assert build_access_required_response(request, settings) is None

    missing = _request(
        path="/internal/backend-ssh/llm-help/worker",
        headers=[(b"host", b"127.0.0.1:9090")],
        backend_ssh_token="runtime-secret",
    )
    missing_response = build_access_required_response(missing, settings)
    assert missing_response is not None
    assert missing_response.status_code == 404


def test_internal_backend_ssh_route_rejects_proxied_loopback_request() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    request = _request(
        path="/internal/backend-ssh/llm-help/worker",
        headers=[
            (b"host", b"127.0.0.1:9090"),
            (b"x-cnc-internal-token", b"runtime-secret"),
            (b"x-forwarded-for", b"100.64.0.12"),
        ],
        backend_ssh_token="runtime-secret",
    )

    response = build_access_required_response(request, settings)
    assert response is not None
    assert response.status_code == 404


def test_access_attempt_backoff_grows_after_failures() -> None:
    client_host = "127.0.0.1"
    record_access_attempt(client_host, success=True, now=0)

    record_access_attempt(client_host, success=False, now=10)
    first = access_attempt_backoff_seconds(client_host, now=10.1)
    record_access_attempt(client_host, success=False, now=12)
    second = access_attempt_backoff_seconds(client_host, now=12.1)

    assert first >= 1
    assert second >= 2


def test_sanitize_next_path_rejects_external_targets() -> None:
    for unsafe in (
        "//evil.example",
        "/\\evil.example",
        "/%5Cevil.example",
        "/%2F%2Fevil.example",
        "/%252F%252Fevil.example",
        "/%61ccess",
        "/outputs/7\tevil",
        "/outputs/7%09evil",
        "/outputs/7\x7fevil",
    ):
        assert sanitize_next_path(unsafe) == "/"

    assert sanitize_next_path("/access") == "/"
    assert sanitize_next_path("/outputs/7") == "/outputs/7"
    assert sanitize_next_path("/outputs/7?tab=metrics") == "/outputs/7?tab=metrics"
