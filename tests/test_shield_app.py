from starlette.testclient import TestClient
import json

from app.services.renderers import SHIELD_PUBLIC_PATH_PREFIX
from app.services.shield_service import SESSION_COOKIE_NAME, make_code_hash
from app.shield import ShieldAppConfig, create_app


def _client(tmp_path) -> TestClient:
    secret = "test-secret"
    app = create_app(
        ShieldAppConfig(
            db_path=tmp_path / "shield.db",
            secret_key=secret,
            access_code_hash=make_code_hash(secret, "let me in"),
        )
    )
    return TestClient(app, base_url="https://shield.test")


def test_shield_access_page_is_minimal_and_posts_to_reserved_path(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/access?next=/app")

    assert response.status_code == 200
    assert "Access Code:" in response.text
    assert f'action="{SHIELD_PUBLIC_PATH_PREFIX}/submit"' in response.text
    assert 'action="/shield/submit"' in response.text
    assert "__cnc" not in response.text
    assert 'name="next" value="/app"' in response.text
    visible_text = response.text.replace(SHIELD_PUBLIC_PATH_PREFIX, "")
    assert "cnc" not in visible_text.lower()


def test_shield_public_domain_root_and_reserved_submit_work(tmp_path) -> None:
    client = _client(tmp_path)

    page = client.get("/")
    response = client.post(
        f"{SHIELD_PUBLIC_PATH_PREFIX}/submit",
        data={"access_code": "let me in", "next": "/app"},
        follow_redirects=False,
        headers={"cf-connecting-ip": "203.0.113.20"},
    )

    assert page.status_code == 200
    assert "Access Code:" in page.text
    assert response.status_code == 303


def test_shield_submit_sets_session_cookie_and_check_accepts_it(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/submit",
        data={"access_code": "let me in", "next": "/app"},
        follow_redirects=False,
        headers={"cf-connecting-ip": "203.0.113.10"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert SESSION_COOKIE_NAME in response.cookies
    assert "cnc" not in SESSION_COOKIE_NAME.lower()
    check = client.get("/check")
    assert check.status_code == 204


def test_shield_uses_output_specific_access_code_hash(tmp_path) -> None:
    secret = "test-secret"
    config_path = tmp_path / "shield-config.json"
    config_path.write_text(
        json.dumps(
            {"outputs": {"app-one": {"code_hash": make_code_hash(secret, "app one")}}}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            ShieldAppConfig(
                db_path=tmp_path / "shield.db",
                secret_key=secret,
                access_code_hash=make_code_hash(secret, "global"),
                config_path=config_path,
            )
        ),
        base_url="https://shield.test",
    )

    denied = client.post(
        "/submit",
        data={"access_code": "global", "next": "/app"},
        headers={"x-shield-output": "app-one", "cf-connecting-ip": "203.0.113.11"},
    )
    granted = client.post(
        "/submit",
        data={"access_code": "app one", "next": "/app"},
        follow_redirects=False,
        headers={"x-shield-output": "app-one", "cf-connecting-ip": "203.0.113.12"},
    )

    assert denied.status_code == 200
    assert granted.status_code == 303


def test_shield_submit_uses_generic_failure_for_wrong_code(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/submit",
        data={"access_code": "bad", "next": "/app"},
        headers={"x-forwarded-for": "203.0.113.10"},
    )

    assert response.status_code == 200
    assert "Try again." in response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    assert client.get("/check").status_code == 401


def test_shield_submit_rejects_oversized_body_before_form_parsing(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/submit",
        content=b"access_code=" + (b"x" * 4096),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert response.text == "Request body too large"


def test_shield_next_rejects_external_redirect(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/submit",
        data={"access_code": "let me in", "next": "https://example.com"},
        follow_redirects=False,
        headers={"cf-connecting-ip": "203.0.113.10"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
