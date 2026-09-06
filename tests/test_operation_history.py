import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.dependencies import db_session_dependency, settings_dependency
from app.models.entities import Operation
from app.routes.status import router


@pytest.mark.asyncio
async def test_history_is_bounded_filtered_and_matches_detail():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    statuses = ["success", "failed", "partial", "cancelled"]
    async with maker() as session:
        session.add_all(
            [
                Operation(
                    kind="apply_host",
                    status=statuses[i % 4],
                    details_json=json.dumps({"value": i}),
                )
                for i in range(20)
            ]
        )
        session.add(Operation(kind="apply_host", status="running"))
        await session.commit()

    async def dependency():
        async with maker() as session:
            yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[db_session_dependency] = dependency
    app.dependency_overrides[settings_dependency] = lambda: Settings(_env_file=None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            ids = []
            cursor = None
            for _ in range(3):
                params = {"limit": 8}
                if cursor:
                    params["before"] = cursor
                response = await client.get("/api/operations", params=params)
                assert response.status_code == 200
                payload = response.json()
                for operation in payload["operations"]:
                    ids.append(operation["id"])
                    detail = await client.get(f"/api/operations/{operation['id']}")
                    assert operation == detail.json()
                cursor = payload["next_cursor"]
            assert ids == list(range(20, 0, -1))
            assert cursor is None
            filtered = (
                await client.get("/api/operations?status=failed&limit=50")
            ).json()["operations"]
            assert len(filtered) == 5
            assert all(item["status"] == "failed" for item in filtered)
            for query in ("limit=51", "before=0", "status=bogus"):
                assert (await client.get(f"/api/operations?{query}")).status_code == 422
    finally:
        await engine.dispose()
