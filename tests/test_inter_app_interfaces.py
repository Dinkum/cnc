import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.entities import Backend
from app.services.inter_app_interfaces import (
    ConcurrentInterfaceUpdateError,
    apply_inbound_inter_app_interface_statuses,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_inbound_interface_update_rejects_stale_json_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    async with maker() as session:
        source = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            inter_app_interfaces_json="[]",
            enabled=True,
        )
        target = Backend(
            name="api",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([source, target])
        await session.flush()
        source.inter_app_interfaces_json = json.dumps(
            [
                {
                    "name": "cnc0",
                    "target_backend_id": target.id,
                    "status": "pending",
                    "direction": "out",
                }
            ]
        )
        await session.commit()
        source_id = source.id
        target_id = target.id

    async with maker() as first, maker() as stale:
        assert await first.get(Backend, source_id) is not None
        stale_source = await stale.get(Backend, source_id)
        assert stale_source is not None

        await apply_inbound_inter_app_interface_statuses(
            first,
            target_backend_id=target_id,
            source_backend_ids=[str(source_id)],
            names=["cnc0"],
            statuses=["accepted"],
            directions=["out"],
        )
        await first.commit()

        original_execute = stale.execute
        returned_stale_read = False

        async def execute_with_stale_read(statement, *args, **kwargs):
            nonlocal returned_stale_read
            if not returned_stale_read and bool(getattr(statement, "is_select", False)):
                returned_stale_read = True
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [stale_source])
                )
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(stale, "execute", execute_with_stale_read)

        with pytest.raises(
            ConcurrentInterfaceUpdateError, match="refresh and try again"
        ):
            await apply_inbound_inter_app_interface_statuses(
                stale,
                target_backend_id=target_id,
                source_backend_ids=[str(source_id)],
                names=["cnc0"],
                statuses=["rejected"],
                directions=["out"],
            )
        await stale.rollback()

    async with maker() as session:
        stored = await session.get(Backend, source_id)

    assert stored is not None
    assert json.loads(stored.inter_app_interfaces_json)[0]["status"] == "accepted"
