from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.security import CNCTrustedHostMiddleware, enforce_origin_policy


def _request(
    *,
    headers: dict[str, str] | None = None,
    scheme: str = "http",
    client_host: str = "127.0.0.1",
) -> SimpleNamespace:
    return SimpleNamespace(
        headers=headers or {},
        url=SimpleNamespace(scheme=scheme),
        client=SimpleNamespace(host=client_host),
    )


def test_enforce_origin_policy_allows_loopback_admin_origin() -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(headers={"origin": "http://127.0.0.1:9090"})
    enforce_origin_policy(request, settings)


def test_enforce_origin_policy_allows_tailscale_serve_same_origin() -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(
        headers={
            "origin": "https://cnc-node.example.ts.net",
            "host": "cnc-node.example.ts.net",
            "x-forwarded-proto": "https",
        },
        client_host="100.64.0.12",
    )
    enforce_origin_policy(request, settings)


def test_enforce_origin_policy_allows_same_host_without_forwarded_proto() -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(
        headers={
            "origin": "https://demo.example.com",
            "host": "demo.example.com",
        }
    )
    enforce_origin_policy(request, settings)


def test_enforce_origin_policy_allows_current_tailscale_origin_without_forwarded_host(
    monkeypatch,
) -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    monkeypatch.setattr(
        "app.security.current_tailscale_admin_hostnames",
        lambda _settings: ("admin.tailnet.example",),
    )
    request = _request(
        headers={
            "origin": "https://admin.tailnet.example",
        },
        client_host="100.64.0.12",
    )
    enforce_origin_policy(request, settings)


def test_enforce_origin_policy_rejects_forwarded_host_origin_spoof() -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(
        headers={
            "origin": "https://evil.example",
            "host": "admin.tailnet.example",
            "x-forwarded-host": "evil.example",
            "x-forwarded-proto": "https",
        },
        client_host="100.64.0.12",
    )
    with pytest.raises(HTTPException):
        enforce_origin_policy(request, settings)


def test_enforce_origin_policy_rejects_unrelated_tailscale_origin(
    monkeypatch,
) -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    monkeypatch.setattr(
        "app.security.current_tailscale_admin_hostnames",
        lambda _settings: ("admin.tailnet.example",),
    )
    request = _request(
        headers={
            "origin": "https://other.tailnet.example",
            "host": "admin.tailnet.example",
            "x-forwarded-proto": "https",
        },
        client_host="100.64.0.12",
    )
    with pytest.raises(HTTPException):
        enforce_origin_policy(request, settings)


def test_enforce_origin_policy_rejects_cross_origin_host() -> None:
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(
        headers={
            "origin": "https://evil.example",
            "host": "cnc-node.example.ts.net",
            "x-forwarded-proto": "https",
        }
    )
    with pytest.raises(HTTPException):
        enforce_origin_policy(request, settings)


def test_enforce_origin_policy_rejects_non_default_port_without_forwarded_proto() -> (
    None
):
    settings = Settings(admin_host="127.0.0.1", admin_port=9090)
    request = _request(
        headers={
            "origin": "https://demo.example.com:444",
            "host": "demo.example.com",
        }
    )
    with pytest.raises(HTTPException):
        enforce_origin_policy(request, settings)


def test_settings_trusted_host_list_includes_loopback_and_configured_hosts() -> None:
    settings = Settings(
        admin_allowed_hosts="cnc-node.example.ts.net,admin.internal.example",
    )

    assert "127.0.0.1" in settings.trusted_host_list
    assert "localhost" in settings.trusted_host_list
    assert "::1" in settings.trusted_host_list
    assert "cnc-node.example.ts.net" in settings.trusted_host_list
    assert "admin.internal.example" in settings.trusted_host_list


def test_settings_trusted_host_list_accepts_explicit_private_ip_hosts() -> None:
    settings = Settings(admin_allowed_hosts="10.0.0.5,192.168.1.20")

    assert "10.0.0.5" in settings.trusted_host_list
    assert "192.168.1.20" in settings.trusted_host_list


def test_settings_rejects_invalid_trusted_host_entries() -> None:
    with pytest.raises(ValueError):
        Settings(admin_allowed_hosts="bad host value")


def test_settings_rejects_noncanonical_admin_port() -> None:
    assert Settings().admin_port == 9090
    with pytest.raises(ValueError):
        Settings(admin_port=19090)


def test_cnc_trusted_host_middleware_allows_current_tailscale_admin_hostname(
    monkeypatch,
) -> None:
    guest_app = FastAPI()

    @guest_app.get("/")
    def read_root():
        return {"ok": True}

    monkeypatch.setattr(
        "app.security.current_tailscale_admin_hostnames",
        lambda _settings: ("cnc-admin.example.ts.net",),
    )
    guest_app.add_middleware(
        CNCTrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
        settings_getter=lambda: Settings(),
    )

    client = TestClient(guest_app)
    response = client.get("/", headers={"host": "cnc-admin.example.ts.net"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_cnc_trusted_host_middleware_rejects_join_bootstrap_on_untrusted_host() -> None:
    guest_app = FastAPI()

    @guest_app.get("/join/install.sh")
    def read_join_script():
        return {"ok": True}

    guest_app.add_middleware(
        CNCTrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
        settings_getter=lambda: Settings(),
    )

    client = TestClient(guest_app)
    response = client.get("/join/install.sh", headers={"host": "203.0.113.10:19091"})

    assert response.status_code == 400


@pytest.mark.parametrize(
    "host",
    (
        "localhost/static/x?ignored=",
        "localhost,evil.example",
        "operator@localhost",
        "localhost:",
        "localhost:0",
        "localhost:99999",
    ),
)
def test_cnc_trusted_host_middleware_rejects_malformed_authority(host: str) -> None:
    guest_app = FastAPI()

    @guest_app.get("/static/x")
    def read_static():
        return {"ok": True}

    guest_app.add_middleware(
        CNCTrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
        settings_getter=lambda: Settings(),
    )

    response = TestClient(guest_app).get("/static/x", headers={"host": host})

    assert response.status_code == 400


def test_cnc_trusted_host_middleware_accepts_bracketed_loopback_ipv6() -> None:
    guest_app = FastAPI()

    @guest_app.get("/")
    def read_root():
        return {"ok": True}

    guest_app.add_middleware(
        CNCTrustedHostMiddleware,
        allowed_hosts=["::1"],
        settings_getter=lambda: Settings(),
    )

    response = TestClient(guest_app).get("/", headers={"host": "[::1]:9090"})

    assert response.status_code == 200
