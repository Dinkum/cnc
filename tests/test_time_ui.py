import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

from app.config import Settings
from app.ui.operations import inputs as input_operations
from app.ui.operations import output_lifecycle as output_lifecycle_operations
from app.ui.operations import output_save as output_save_operations
from app.ui.routes import backup_mutations as ui_backup_mutations
from app.ui.routes import input_mutations as ui_input_mutations
from app.ui.routes import output_mutations as ui_output_mutations

_TIME_UI_SPEC = importlib.util.spec_from_file_location(
    "cnc_time_ui", Path(__file__).parents[1] / "scripts" / "time_ui.py"
)
assert _TIME_UI_SPEC is not None and _TIME_UI_SPEC.loader is not None
time_ui = importlib.util.module_from_spec(_TIME_UI_SPEC)
sys.modules[_TIME_UI_SPEC.name] = time_ui
_TIME_UI_SPEC.loader.exec_module(time_ui)


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeClient:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        del headers
        self.requests.append(url)
        response = self.responses.get(url)
        if response is None:
            return httpx.Response(404, text="not found")
        return response


async def test_runtime_patches_cover_worker_local_apply_references() -> None:
    settings = Settings()
    state = time_ui._install_runtime_patches(settings)
    patched = input_operations.commit_and_apply
    try:
        assert output_lifecycle_operations.commit_and_apply is patched
        assert output_save_operations.commit_and_apply is patched
        assert ui_backup_mutations.commit_and_apply is patched
        assert ui_input_mutations.commit_and_apply is patched
        assert ui_output_mutations.commit_and_apply is patched

        session = _Session()
        result = await output_save_operations.commit_and_apply(
            session,
            settings,
            operation="benchmark-test",
        )

        assert session.commits == 1
        assert result.apply_response.status == "success"
        assert state.fake_apply_calls == 1
    finally:
        time_ui._restore_runtime_patches(state)

    assert (
        input_operations.commit_and_apply is state.originals["input_commit_and_apply"]
    )
    assert (
        output_lifecycle_operations.commit_and_apply
        is state.originals["output_lifecycle_commit_and_apply"]
    )
    assert (
        output_save_operations.commit_and_apply
        is state.originals["output_save_commit_and_apply"]
    )
    assert (
        ui_backup_mutations.commit_and_apply
        is state.originals["ui_backup_commit_and_apply"]
    )
    assert (
        ui_input_mutations.commit_and_apply
        is state.originals["ui_input_commit_and_apply"]
    )
    assert (
        ui_output_mutations.commit_and_apply
        is state.originals["ui_output_commit_and_apply"]
    )


@pytest.mark.asyncio
async def test_discover_prod_output_prefers_metrics_capable_backend() -> None:
    client = FakeClient(
        {
            "/api/backends": httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "static-site", "kind": "static", "enabled": True},
                    {"id": 2, "name": "app-site", "kind": "app", "enabled": True},
                ],
            )
        }
    )

    target = await time_ui._discover_prod_output(client)

    assert target.id == 2
    assert target.kind == "app"
    assert client.requests == ["/api/backends"]


@pytest.mark.asyncio
async def test_discover_prod_output_skips_html_fallback_when_api_has_no_metrics_output() -> (
    None
):
    client = FakeClient(
        {
            "/api/backends": httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "static-site", "kind": "static", "enabled": True},
                ],
            ),
            "/?tab=outputs": httpx.Response(
                200,
                text='<a href="/outputs/1">static-site</a>',
            ),
        }
    )

    target = await time_ui._discover_prod_output(client)

    assert target is None
    assert client.requests == ["/api/backends"]


@pytest.mark.asyncio
async def test_prod_output_metrics_case_fails_when_series_is_not_drawable() -> None:
    target = time_ui.OutputTarget(id=2, name="app-site", kind="app", enabled=True)
    case = [
        item
        for item in time_ui._prod_readonly_cases(target)
        if item.name == "output_metrics_history"
    ][0]
    client = FakeClient(
        {
            "/api/backends/2/metrics-history?metric=cpu&timeframe=1h&width=900": httpx.Response(
                200,
                json={"available": True, "series": []},
            )
        }
    )

    result = await time_ui._time_case(
        client,
        case,
        iterations=1,
        warmups=0,
        csrf_token="",
    )

    assert result["status"] == "failed"
    assert "no drawable series" in result["error"]


@pytest.mark.asyncio
async def test_dashboard_routing_case_fails_when_tab_is_not_rendered() -> None:
    case = time_ui._dashboard_case("routing")
    client = FakeClient(
        {
            "/?tab=routing": httpx.Response(
                200,
                text='<body data-initial-tab="home"></body>',
            )
        }
    )

    result = await time_ui._time_case(
        client,
        case,
        iterations=1,
        warmups=0,
        csrf_token="",
    )

    assert result["status"] == "failed"
    assert "routing tab is not enabled" in result["error"]


def test_prod_readonly_cases_cover_all_metric_history_paths() -> None:
    target = time_ui.OutputTarget(id=2, name="app-site", kind="app", enabled=True)

    names = {case.name for case in time_ui._prod_readonly_cases(target)}

    assert {
        "host_metrics_history",
        "host_metrics_history_memory",
        "host_metrics_history_disk",
        "host_metrics_history_network",
        "output_metrics_history",
        "output_metrics_history_memory",
        "output_metrics_history_disk",
        "output_metrics_history_network",
    }.issubset(names)


def test_raise_for_failed_results_exits_nonzero(tmp_path: Path) -> None:
    record_path = tmp_path / "record.json"
    record_path.write_text(
        '{"results": [{"name": "ok", "status": "ok"}, {"name": "slow", "status": "partial"}]}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        time_ui._raise_for_failed_results(record_path)

    assert exc_info.value.code
    assert "slow" in str(exc_info.value)


def test_raise_for_failed_results_exits_nonzero_on_latency_budget(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "record.json"
    record_path.write_text(
        '{"results": [{"name": "home", "status": "ok", "median_ms": 125.0, "p95_ms": 180.0}]}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        time_ui._raise_for_failed_results(record_path, max_median_ms=100.0)

    assert exc_info.value.code
    assert "over budget" in str(exc_info.value)
    assert "home" in str(exc_info.value)
