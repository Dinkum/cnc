from pathlib import Path
from itertools import product
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, Input
from app.schemas.backends import BackendCloneIn, BackendIn, BackendUpdate
from app.services import backend_commands, clone_defaults, port_preflight
from app.services import backend_backup_service, validators
from app.services.validators import ValidationError


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        port_range_start=12000,
        port_range_end=12004,
    )


def _backend_in(*, name: str = "web", port: int | None = 12000) -> BackendIn:
    return BackendIn(
        name=name,
        kind="app",
        port=port,
        sandbox_profile="ubuntu-24.04-systemd",
        handoff_port=8337,
        volumes_json="[]",
    )


@pytest.mark.asyncio
async def test_create_backend_rejects_explicit_port_that_is_not_host_free(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    monkeypatch.setattr(port_preflight, "is_loopback_port_free", lambda _port: False)

    async with maker() as session:
        with pytest.raises(ValidationError, match="already in use on 127.0.0.1"):
            await backend_commands.create_backend(session, _backend_in(), commit=False)


@pytest.mark.asyncio
async def test_update_backend_checks_only_changed_ports(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
            )
        )
        await session.commit()
        backend = (await session.execute(select(Backend))).scalar_one()

        def fail_if_called(_port: int) -> bool:
            raise AssertionError("unchanged ports should not be host-checked")

        monkeypatch.setattr(port_preflight, "is_loopback_port_free", fail_if_called)
        await backend_commands.update_backend(
            session, backend.id, BackendUpdate(notes="same port"), commit=False
        )

        monkeypatch.setattr(
            port_preflight, "is_loopback_port_free", lambda _port: False
        )
        with pytest.raises(ValidationError, match="already in use on 127.0.0.1"):
            await backend_commands.update_backend(
                session, backend.id, BackendUpdate(port=12001), commit=False
            )


@pytest.mark.asyncio
async def test_clone_default_port_skips_db_used_and_host_used_ports(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        clone_defaults, "is_loopback_port_free", lambda port: port != 12002
    )

    async with maker() as session:
        session.add_all(
            [
                Backend(
                    name="web",
                    kind="app",
                    port=12000,
                    sandbox_profile="ubuntu-24.04-systemd",
                    handoff_port=8337,
                    volumes_json="[]",
                ),
                Backend(
                    name="other",
                    kind="app",
                    port=12001,
                    sandbox_profile="ubuntu-24.04-systemd",
                    handoff_port=8338,
                    volumes_json="[]",
                ),
            ]
        )
        await session.commit()

        port = await clone_defaults.next_clone_port(session, 12000, settings)

    assert port == 12003


@pytest.mark.asyncio
async def test_clone_rejects_reserved_netdata_port(monkeypatch, tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    monkeypatch.setattr(clone_defaults, "is_loopback_port_free", lambda _port: True)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                sandbox_profile="ubuntu-24.04-systemd",
                handoff_port=8337,
                volumes_json="[]",
            )
        )
        await session.commit()
        backend = (await session.execute(select(Backend))).scalar_one()

        with pytest.raises(ValidationError, match="reserved host port"):
            await backend_commands.clone_backend(
                session,
                backend.id,
                BackendCloneIn(name="web-copy", port=19999),
                settings,
            )


async def test_clone_port_uses_all_available_capacity(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    for source in range(12000, 12005):
        others = [port for port in range(12000, 12005) if port != source]
        for states in product(("free", "db", "host"), repeat=4):
            used = {source} | {p for p, state in zip(others, states) if state == "db"}
            busy = {p for p, state in zip(others, states) if state == "host"}
            free = {p for p, state in zip(others, states) if state == "free"}

            async def execute(_statement):
                return SimpleNamespace(scalars=lambda: used)

            monkeypatch.setattr(
                clone_defaults, "is_loopback_port_free", lambda p: p not in busy
            )
            actual = await clone_defaults.next_clone_port(
                SimpleNamespace(execute=execute), source, settings
            )
            higher = free & set(range(source + 1, 12005))
            expected = min(higher or free) if free else None
            assert actual == expected


async def test_clone_name_collisions_use_one_query(monkeypatch, tmp_path):
    maker = await _make_session(tmp_path / "app.db")
    source = "a" * 63
    async with maker() as session:
        session.add_all(
            [
                Backend(name="clone-" + source[:57], kind="static"),
                Backend(name="clone-" + source[:55] + "-2", kind="static"),
            ]
        )
        await session.commit()
        statements = []

        def record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(session.bind.sync_engine, "before_cursor_execute", record)
        try:
            name = await clone_defaults.next_clone_name(session, source)
        finally:
            event.remove(session.bind.sync_engine, "before_cursor_execute", record)
        assert name == "clone-" + source[:55] + "-3"
        assert len(statements) == 1


async def test_static_clone_copies_files_without_app_port_capacity(
    monkeypatch, tmp_path
):
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path).model_copy(
        update={
            "host_state_path": tmp_path / "host-state.json",
            "apply_lock_path": tmp_path / "mutation.lock",
        }
    )
    site = tmp_path / "site"
    site.mkdir()
    content = b"<h1>Static site</h1>"
    (site / "index.html").write_bytes(content)

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "static clone must not allocate a port or execute host commands"
        )

    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("static clone must not send external notifications")

    monkeypatch.setattr(clone_defaults, "is_loopback_port_free", forbidden)
    monkeypatch.setattr(backend_backup_service, "run_command", forbidden)
    monkeypatch.setattr(
        backend_backup_service, "send_pushover_notification_async", forbidden_async
    )
    monkeypatch.setattr(backend_backup_service, "get_settings", lambda: settings)
    monkeypatch.setattr(validators, "get_settings", lambda: settings)
    async with maker() as session:
        backend = Backend(
            name="site", kind="static", static_root=str(site), enabled=True
        )
        backend.inputs = [Input(kind="domain", hostname="site.example.com")]
        session.add(backend)
        await session.commit()
        result = await backend_commands.clone_backend(
            session, backend.id, BackendCloneIn(name="site-copy"), settings
        )
        clone = await backend_commands.require_backend(session, result["backend"].id)
        assert clone.kind == "static"
        assert clone.port is None
        assert clone.enabled is False
        assert clone.inputs == []
        assert clone.static_root != str(site)
        assert (Path(clone.static_root) / "index.html").read_bytes() == content
        assert (site / "index.html").read_bytes() == content
        assert len(result["post_clone_checks"]) == 4
        assert all(check["status"] == "passed" for check in result["post_clone_checks"])
