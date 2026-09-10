from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, Input
from app.ui.dashboard.data import load_dashboard_data
from app.ui.dashboard.presentation import build_dashboard_context
from app.ui.outputs.data import load_output_page_data
from app.ui.outputs.presentation import build_output_page_context


@pytest.fixture
async def page_data_store(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ui.db'}",
        auto_resource_limits=False,
        csrf_token="fixture-csrf-token",
    )
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            route = Input(kind="domain", hostname="docs.example.com", enabled=True)
            backend = Backend(
                name="docs",
                kind="static",
                static_root="/srv/docs",
                enabled=True,
                inputs=[route],
            )
            session.add(backend)
            await session.commit()
        yield settings, sessions
    finally:
        await engine.dispose()


@pytest.mark.parametrize("tab", ["home", "inputs", "routing", "outputs", "settings"])
async def test_dashboard_presentation_can_reuse_data_after_session_closes(
    page_data_store, tab: str
) -> None:
    settings, sessions = page_data_store
    async with sessions() as session:
        data = await load_dashboard_data(
            session, settings, active_tab=tab, defer_status=True
        )

    # A detached read result must be sufficient for repeated presentation:
    # rendering cannot depend on a live session or trigger lazy ORM queries.
    def render() -> dict[str, object]:
        return build_dashboard_context(
            settings,
            scope=data.scope,
            rows=data.rows,
            status=data.status,
            cluster_nodes=data.cluster_nodes,
            resource_profile=data.resource_profile,
            current_version=data.app_version,
            asset_version=data.asset_version,
        )

    context = render()
    assert render() == context
    assert context["active_tab"] == tab
    assert context["csrf_token"] == "fixture-csrf-token"
    assert context["backend_map"] == {1: "docs"}
    assert bool(context["inputs"]) is (tab in {"inputs", "outputs"})
    assert bool(context["routing_rows"]) is (tab == "routing")


async def test_output_presentation_keeps_attachment_and_backup_information_detached(
    page_data_store,
) -> None:
    settings, sessions = page_data_store
    async with sessions() as session:
        data = await load_output_page_data(
            session, settings, 1, prefer_cached_runtime=True
        )

    context = build_output_page_context(data, settings, prefer_cached_runtime=True)
    assert context == build_output_page_context(
        data, settings, prefer_cached_runtime=True
    )
    assert context["selected_backend"].name == "docs"
    assert context["input_attach_options"][0]["value"] == "docs.example.com"
    assert context["input_attach_options"][0]["attached"] is True
    assert context["input_attach_lazy_available"] is False
    assert context["backup_signals_pending"] is True
    assert context["backup_summary"]
    assert context["clone_defaults"]["name"] == "clone-docs"
    assert context["csrf_token"] == "fixture-csrf-token"
