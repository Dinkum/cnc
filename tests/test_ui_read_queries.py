from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import Base, create_configured_async_engine
from app.models.entities import ApplyRun
from app.ui.read_models import latest_successful_apply_at


@pytest.mark.asyncio
async def test_apply_timestamp_query_skips_snapshots_and_selects_latest_success(
    tmp_path,
):
    engine = create_configured_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        assert await latest_successful_apply_at(session) is None
        session.add_all(
            [
                ApplyRun(
                    status="success", message="old", created_at=datetime(2026, 1, 1)
                ),
                ApplyRun(
                    status="success",
                    message="latest",
                    created_at=datetime(2026, 1, 2),
                    desired_state_json="x" * 1024 * 1024,
                ),
                ApplyRun(
                    status="failed", message="failure", created_at=datetime(2026, 1, 3)
                ),
            ]
        )
        await session.commit()
    returned_columns = []

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def capture_columns(_conn, cursor, statement, *_args):
        if "FROM apply_runs" in statement:
            returned_columns.append([item[0] for item in cursor.description])

    async with maker() as session:
        assert await latest_successful_apply_at(session) == datetime(2026, 1, 2)
    assert returned_columns == [["created_at"]]
    await engine.dispose()
