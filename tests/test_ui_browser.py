"""Browser regressions use real rendered pages; host effects are scripted at HTTP boundaries."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.dependencies import db_session_dependency, settings_dependency
from app.models.entities import Backend, BackendResourceSample
from app.services.backend_metric_history import build_backend_metric_history
from app.ui.dashboard import data as dashboard_data
from app.ui.outputs import data as output_data
from app.ui.routes.pages import router

ROOT = Path(__file__).resolve().parents[1]


async def fixture_status(
    session: AsyncSession, _settings: Settings, **_kwargs: object
) -> dict[str, object]:
    backends = (await session.scalars(select(Backend))).all()
    return {
        "services": [
            {
                "backend": backend.name,
                "service": f"cnc-app-{backend.name}",
                "ok": True,
                "data": {"ActiveState": "active", "SubState": "running"},
                "metrics": {"cpu_percent": 5.0, "memory_percent": 20.0},
            }
            for backend in backends
            if backend.kind == "app" and backend.enabled
        ],
        "resource_profile": {"host_cpu_count": 4, "host_memory_bytes": 8 * 1024**3},
    }


async def seed_browser_database(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        session.add_all(
            [
                Backend(id=1, name="demo-app", kind="app", port=12001, enabled=True),
                Backend(
                    id=2,
                    name="static-site",
                    kind="static",
                    static_root="/srv/example",
                    enabled=True,
                ),
                Backend(id=3, name="survivor", kind="app", port=12002, enabled=True),
            ]
        )
        await session.flush()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for index in range(30):
            session.add(
                BackendResourceSample(
                    backend_id=1,
                    bucket_start=now - timedelta(minutes=30 - index),
                    cpu_percent_of_host=5 + index % 3,
                    memory_percent=20 + index % 4,
                    memory_current_bytes=100 * 1024**2,
                    memory_max_bytes=500 * 1024**2,
                    network_rx_bytes=10000 * index,
                    network_tx_bytes=20000 * index,
                    network_rx_bps=100,
                    network_tx_bps=200,
                    network_total_bps=300,
                )
            )
        await session.commit()


async def render_browser_pages(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{work_dir / 'app.db'}",
        access_key_hash="",
        csrf_token="ui-regression-token",
        app_control_dir=work_dir / "control",
        app_sandbox_dir=work_dir / "sandboxes",
        backend_backup_dir=work_dir / "backups",
        apply_lock_path=work_dir / "apply.lock",
        beta_hardening=False,
        multi_node_enabled=False,
    )
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await seed_browser_database(maker)

        # Page reads use only saved state. A cold cache is intentional after deletion.
        monkeypatch.setattr(dashboard_data, "peek_cached_status", lambda: None)
        monkeypatch.setattr(output_data, "peek_cached_status", lambda: None)
        monkeypatch.setattr(dashboard_data, "collect_status", fixture_status)

        async def session_dependency() -> AsyncIterator[AsyncSession]:
            async with maker() as session:
                yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[db_session_dependency] = session_dependency
        app.dependency_overrides[settings_dependency] = lambda: settings
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://example.test"
        ) as client:

            async def save_page(path: str, name: str) -> None:
                response = await client.get(path)
                assert response.status_code == 200, response.text
                (work_dir / name).write_text(response.text)

            await save_page("/outputs/1", "output.html")
            await save_page("/?tab=outputs", "outputs.html")
            async with maker() as session:
                backend = await session.get(Backend, 1)
                assert backend is not None
                metrics = {}
                for key in ("cpu", "memory", "network"):
                    metrics[key] = await build_backend_metric_history(
                        session,
                        backend,
                        metric_key=key,
                        timeframe_key="day",
                        chart_width_px=1000,
                        live_metrics=None,
                    )
                (work_dir / "metrics.json").write_text(json.dumps(metrics))
                # The route suite exercises actual deletion/rollback; this is its resulting saved state.
                await session.delete(backend)
                await session.commit()
                status = await fixture_status(session, settings)
                (work_dir / "status.json").write_text(json.dumps(status))
            await save_page("/?tab=outputs&defer_status=1", "outputs-deleted.html")
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("CNC_PLAYWRIGHT_MODULE"),
    reason="Use scripts/test_ui.py with CNC_PLAYWRIGHT_MODULE for browser coverage",
)
async def test_ui_browser_regressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = time.perf_counter()
    await render_browser_pages(tmp_path, monkeypatch)
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(ROOT / "tests/browser/ui-regressions.mjs"), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    print(result.stdout, end="")
    print(f"UI browser total including fixtures: {time.perf_counter() - started:.2f}s")
    assert result.returncode == 0, result.stdout + result.stderr
