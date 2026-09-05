from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend
from app.schemas.backends import BackendCloneIn, BackendIn, BackendUpdate
from app.services import backend_commands, port_preflight
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
        backend_commands, "is_loopback_port_free", lambda port: port != 12002
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

        port = await backend_commands._next_clone_backend_port(session, 12000, settings)

    assert port == 12003


@pytest.mark.asyncio
async def test_clone_rejects_reserved_netdata_port(monkeypatch, tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _settings(tmp_path)
    monkeypatch.setattr(backend_commands, "is_loopback_port_free", lambda _port: True)

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
