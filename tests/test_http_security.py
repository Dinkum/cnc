from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.http_security import SecurityHeadersMiddleware
from app.main import app as main_app
from app.services.shield_service import make_code_hash
from app.shield import ShieldAppConfig, create_app


EXPECTED_HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def test_security_headers_apply_without_changing_response() -> None:
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"ok": True}

    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_main_access_gate_response_has_security_headers() -> None:
    response = TestClient(main_app).get(
        "/api/status",
        headers={"host": "localhost", "accept": "application/json"},
    )

    assert response.status_code == 401
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value


def test_shield_page_has_security_headers(tmp_path) -> None:
    secret = "test-secret"
    app = create_app(
        ShieldAppConfig(
            db_path=tmp_path / "shield.db",
            secret_key=secret,
            access_code_hash=make_code_hash(secret, "let me in"),
        )
    )

    response = TestClient(app, base_url="https://shield.test").get("/access")

    assert response.status_code == 200
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers[name] == value
