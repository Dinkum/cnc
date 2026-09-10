import asyncio
import base64
from pathlib import Path
import re

import pytest

from app.config import Settings
from app.models.entities import Backend, BackendSshKey
from app.services import backend_ssh_keys as keys
from app.services.ssh_access import BackendSshAccess
from ui_routes.support import _make_session


def public_key(byte: int = 1) -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " device"


@pytest.fixture
async def key_store(monkeypatch, tmp_path: Path):
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
    )
    applied = []

    async def reconcile(backends, _settings):
        applied.append(
            {
                backend.name: backend.public_key
                if isinstance(backend, BackendSshAccess)
                else backend.ssh_public_key
                for backend in backends
            }
        )
        return {}

    monkeypatch.setattr(keys, "reconcile_backend_ssh_access", reconcile)
    async with maker() as session:
        session.add_all(
            [
                Backend(name="web", kind="app", port=12000),
                Backend(name="worker", kind="app", port=12001),
            ]
        )
        await session.commit()
    return maker, settings, applied


async def test_create_download_is_once_and_revoke_is_output_scoped(key_store):
    maker, settings, applied = key_store
    async with maker() as session:
        created = await keys.mutate_backend_ssh_key(session, settings, 1, name="Laptop")
        assert created["private_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert re.fullmatch(r"cnc-web-ssh-[a-z0-9]{6}", created["key"]["filename"])
        first_id = created["key"]["id"]
        imported = await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Runner", public_key=public_key()
        )
        await keys.mutate_backend_ssh_key(
            session, settings, 2, name="Runner", public_key=public_key()
        )
        assert "private_key" not in imported
        assert len(applied[-1]["web"].splitlines()) == 2
        assert applied[-1]["worker"] == " ".join(public_key().split()[:2])
        backend = await session.get(Backend, 1)
        assert backend.ssh_private_key is None
        rows = await keys.list_backend_ssh_keys(session, 1)
        assert len(rows) == 2
        assert all(
            "private" not in field
            for row in rows
            for field in keys.ssh_key_metadata(row)
        )
        await keys.mutate_backend_ssh_key(session, settings, 1, revoke_id=first_id)
        assert applied[-1]["web"] == applied[-1]["worker"]
        assert (await session.get(BackendSshKey, first_id)).revoked_at is not None
        # Repeated revoke is safe, and a different output cannot revoke the key.
        await keys.mutate_backend_ssh_key(session, settings, 1, revoke_id=first_id)
        with pytest.raises(LookupError, match="not found"):
            await keys.mutate_backend_ssh_key(session, settings, 2, revoke_id=first_id)


async def test_names_and_fingerprints_are_unique_per_output(key_store):
    maker, settings, _ = key_store
    async with maker() as session:
        await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )
        with pytest.raises(ValueError, match="name already"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, name=" laptop ", public_key=public_key(2)
            )
        with pytest.raises(ValueError, match="already has access"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, name="Other", public_key=public_key()
            )
        with pytest.raises(ValueError, match="control characters"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, name="Bad\nname", public_key=public_key(2)
            )
        assert len(await keys.list_backend_ssh_keys(session, 1)) == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        "PRIVATE KEY",
        "ssh-rsa AAAA",
        "ssh-ed25519 !!!!",
        "ssh-ed25519 AAAA",
        'command="id" ' + public_key(),
        public_key() + "\n" + public_key(2),
        public_key() + "\rmalicious",
    ],
)
def test_import_rejects_invalid_keys_and_authorized_keys_options(value):
    with pytest.raises(ValueError):
        keys.parse_public_key(value)


def test_generated_key_fingerprint_matches_openssh():
    import subprocess

    public, private = keys.generate_backend_ssh_keypair("test")
    normalized, fingerprint = keys.parse_public_key(public)
    result = subprocess.run(
        ["ssh-keygen", "-lf", "-"],
        input=public,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.split()[1] == fingerprint
    assert normalized in public
    assert "PRIVATE KEY" in private


async def test_id_collision_retries_even_for_revoked_keys(key_store, monkeypatch):
    maker, settings, _ = key_store
    async with maker() as session:
        sequence = iter("aaaaaa" + "aaaaaa" + "bbbbbb")
        monkeypatch.setattr(keys.secrets, "choice", lambda _alphabet: next(sequence))
        first = await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )
        await keys.mutate_backend_ssh_key(
            session, settings, 1, revoke_id=first["key"]["id"]
        )
        second = await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )
        assert first["key"]["id"] == "aaaaaa"
        assert second["key"]["id"] == "bbbbbb"


async def test_reconcile_failure_rolls_back_created_key(key_store, monkeypatch):
    maker, settings, _ = key_store

    async def fail(*_args):
        raise RuntimeError("sshd failed")

    monkeypatch.setattr(keys, "reconcile_backend_ssh_access", fail)
    async with maker() as session:
        with pytest.raises(RuntimeError, match="sshd failed"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, name="Laptop", public_key=public_key()
            )
        assert not await keys.list_backend_ssh_keys(session, 1)
        assert (await session.get(Backend, 1)).ssh_public_key is None


async def test_failed_revoke_commit_restores_access_and_database(
    key_store, monkeypatch
):
    maker, settings, applied = key_store
    async with maker() as session:
        created = await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )

        async def fail_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, revoke_id=created["key"]["id"]
            )
        assert applied[-2]["web"] is None
        assert applied[-1]["web"] == " ".join(public_key().split()[:2])
        assert len(await keys.list_backend_ssh_keys(session, 1)) == 1


async def test_disabled_output_stores_keys_without_enabling_access(key_store):
    maker, settings, applied = key_store
    async with maker() as session:
        backend = await session.get(Backend, 1)
        backend.enabled = False
        await session.commit()
        await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )
        assert applied == []
        assert len(await keys.list_backend_ssh_keys(session, 1)) == 1


async def test_cancellation_waits_for_access_transaction(key_store, monkeypatch):
    maker, settings, _ = key_store
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_reconcile(*_args):
        started.set()
        await finish.wait()
        return {}

    monkeypatch.setattr(keys, "reconcile_backend_ssh_access", slow_reconcile)
    async with maker() as session:
        mutation = asyncio.create_task(
            keys.mutate_backend_ssh_key(
                session, settings, 1, name="Laptop", public_key=public_key()
            )
        )
        await started.wait()
        mutation.cancel()
        await asyncio.sleep(0)
        assert not mutation.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await mutation
        assert len(await keys.list_backend_ssh_keys(session, 1)) == 1


async def test_http_key_routes_require_auth_and_csrf_and_never_redownload(key_store):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import Response
    from app.access import (
        build_access_required_response,
        hash_access_key,
        set_access_cookie,
        ACCESS_COOKIE_NAME,
    )
    from app.dependencies import db_session_dependency, settings_dependency
    from app.ui.routes.ssh_keys import router

    maker, settings, _ = key_store
    settings = settings.model_copy(
        update={
            "access_key_hash": hash_access_key("test-login-password"),
            "csrf_token": "test-csrf",
        }
    )
    app = FastAPI()
    app.include_router(router)

    async def session_dependency():
        async with maker() as session:
            yield session

    app.dependency_overrides[db_session_dependency] = session_dependency
    app.dependency_overrides[settings_dependency] = lambda: settings

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        rejected = build_access_required_response(request, settings)
        return rejected if rejected is not None else await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        endpoint = "/api/backends/1/ssh-keys"
        assert (await client.get(endpoint)).status_code == 401
        assert (
            await client.post(
                endpoint, data={"name": "Laptop", "csrf_token": "test-csrf"}
            )
        ).status_code == 401
        cookie_response = Response()
        set_access_cookie(cookie_response, settings)
        cookie = cookie_response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        client.cookies.set(ACCESS_COOKIE_NAME, cookie)
        assert (
            await client.post(endpoint, data={"name": "Laptop", "csrf_token": "bad"})
        ).status_code == 403
        created = await client.post(
            endpoint, data={"name": "Laptop", "csrf_token": "test-csrf"}
        )
        assert created.status_code == 200
        payload = created.json()
        assert "PRIVATE KEY" in payload["private_key"]
        assert created.headers["cache-control"] == "no-store"
        listed = await client.get(endpoint)
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        assert "private_key" not in listed.text and "PRIVATE KEY" not in listed.text
        assert (await client.get("/api/backends/1/ssh-key")).status_code == 404
        assert (
            await client.post(
                "/api/backends/1/ssh-key", data={"csrf_token": "test-csrf"}
            )
        ).status_code == 404
        assert (
            await client.post(
                f"{endpoint}/{payload['key']['id']}/revoke", data={"csrf_token": "bad"}
            )
        ).status_code == 403
        revoked = await client.post(
            f"{endpoint}/{payload['key']['id']}/revoke",
            data={"csrf_token": "test-csrf"},
        )
        assert revoked.status_code == 200
        assert (await client.get(endpoint)).json() == {"keys": []}


async def test_uncertain_commit_uses_committed_keys_instead_of_regranting_revoked_access(
    key_store, monkeypatch
):
    maker, settings, applied = key_store
    async with maker() as session:
        created = await keys.mutate_backend_ssh_key(
            session, settings, 1, name="Laptop", public_key=public_key()
        )
        commit = session.commit

        async def commit_then_fail():
            await commit()
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(session, "commit", commit_then_fail)
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            await keys.mutate_backend_ssh_key(
                session, settings, 1, revoke_id=created["key"]["id"]
            )
        assert applied[-1]["web"] is None
        assert not await keys.list_backend_ssh_keys(session, 1)
