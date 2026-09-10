from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import statistics
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.ui.dashboard.context as ui_dashboard_context
import app.ui.dashboard.data as ui_dashboard_data
import app.ui.outputs.data as ui_outputs_data
from app import main as app_main
from app.config import Settings
from app.database import Base
from app.dependencies import db_session_dependency, settings_dependency
from app.main import app
from app.models.entities import (
    ApplyRun,
    Backend,
    BackendBackup,
    BackendResourceSample,
    HostResourceSample,
    Input,
    Operation,
)
from app.routes import status as status_routes
from app.schemas.apply import ApplyResponse
from app.security import get_csrf_token
from app.services.mutation_apply import MutationApplyResult
from app.services.renderers import container_name
from app.ui.operations import inputs as input_operations
from app.ui.operations import output_lifecycle as output_lifecycle_operations
from app.ui.operations import output_save as output_save_operations
from app.ui.routes import backup_mutations as ui_backup_mutations
from app.ui.routes import input_mutations as ui_input_mutations
from app.ui.routes import output_mutations as ui_output_mutations
from app.ui.routes import reads as ui_reads

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "ignore" / "perf"
MAIN_TABS = ("home", "inputs", "routing", "outputs", "settings")
HTML_HEADERS = {"accept": "text/html"}
JSON_HEADERS = {"accept": "application/json", "x-requested-with": "fetch"}
OUTPUT_LINK_RE = re.compile(r'href=["\']/outputs/(\d+)(?:["\'/?#])')
METRIC_HISTORY_METRICS = ("cpu", "memory", "disk", "network")


@dataclass(frozen=True)
class TimingCase:
    name: str
    group: str
    run: Callable[[httpx.AsyncClient, int, str], Awaitable[httpx.Response]]
    expected_statuses: tuple[int, ...] = (200,)


@dataclass(frozen=True)
class OutputTarget:
    id: int
    name: str
    kind: str
    enabled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
        }


@dataclass
class RuntimePatchState:
    originals: dict[str, object]
    fake_apply_calls: int = 0


async def _fake_commit_and_apply(
    session: AsyncSession,
    settings: Settings,
    *,
    operation: str,
    apply_runner: object = None,
    apply_services: object = None,
    operation_handle: object = None,
) -> MutationApplyResult:
    await session.commit()
    return MutationApplyResult(
        operation=operation,
        state="applied",
        apply_response=ApplyResponse(
            status="success",
            message="Benchmark apply skipped.",
            details={"benchmark": True},
            run_id=None,
        ),
        state_history=("pending", "applying", "applied"),
    )


def _runtime_asset_snapshot(_settings: Settings) -> dict[str, object]:
    return {
        "changed_units": [],
        "daemon_reload_needed": False,
        "timer_reconcile_needed": False,
        "timer_states": {},
    }


async def _fake_collect_status(
    session: AsyncSession,
    settings: Settings,
    cache_ttl_seconds: int | None = None,
    force_refresh: bool = False,
    allow_stale: bool = False,
) -> dict[str, Any]:
    del cache_ttl_seconds, force_refresh, allow_stale
    backends = list(
        (
            await session.execute(Backend.__table__.select().order_by(Backend.id.asc()))
        ).mappings()
    )
    services: list[dict[str, object]] = []
    for backend in backends:
        if backend["kind"] != "app" or not backend["enabled"]:
            continue
        services.append(
            {
                "service": container_name(str(backend["name"])),
                "backend": backend["name"],
                "ok": True,
                "data": {
                    "ActiveState": "active",
                    "SubState": "running",
                    "BootstrapState": "ready",
                    "PrivateReachable": "yes",
                    "ProxyReachable": "yes",
                },
                "metrics": {
                    "cpu_percent": 18.5,
                    "cpu_percent_of_host": 7.2,
                    "cpu_entitlement_percent_of_host": 25.0,
                    "memory_percent": 42.0,
                    "memory_current_bytes": 180 * 1024 * 1024,
                    "memory_max_bytes": 384 * 1024 * 1024,
                    "network_rx_bytes": 12_000_000,
                    "network_tx_bytes": 4_500_000,
                    "network_rx_bps": 82_000,
                    "network_tx_bps": 31_000,
                    "network_total_bps": 113_000,
                },
            }
        )
    return {
        "services": services,
        "nginx": {"ok": True, "data": {"ActiveState": "active", "SubState": "running"}},
        "resource_profile": {
            "mode": "auto",
            "resource_size": "small",
            "backend_count": len(
                [item for item in backends if item["kind"] == "app" and item["enabled"]]
            ),
            "host_cpu_count": 4,
            "host_memory_bytes": 8 * 1024 * 1024 * 1024,
        },
        "resource_profile_drift": {"changed": False},
        "host_metrics": {
            "cpu_percent": 22.0,
            "memory_percent": 51.0,
            "disk_percent": 37.0,
            "network_rx_bps": 140_000,
            "network_tx_bps": 58_000,
            "network_total_bps": 198_000,
        },
        "last_apply": {
            "status": "success",
            "message": "Applied.",
            "created_at": datetime.now(UTC).isoformat(),
        },
        "last_update": {},
        "update_version": {"current_version": "benchmark", "has_update": False},
    }


def _install_runtime_patches(settings: Settings) -> RuntimePatchState:
    state = RuntimePatchState(
        originals={
            "app_main_get_settings": app_main.get_settings,
            "app_main_build_access_required_response": app_main.build_access_required_response,
            "ui_dashboard_collect_status": ui_dashboard_data.collect_status,
            "status_collect_status": status_routes.collect_status,
            "ui_dashboard_peek_cached_status": ui_dashboard_data.peek_cached_status,
            "ui_help_peek_cached_status": ui_dashboard_context.peek_cached_status,
            "ui_output_peek_cached_status": ui_outputs_data.peek_cached_status,
            "ui_reads_peek_cached_status": ui_reads.peek_cached_status,
            "input_commit_and_apply": input_operations.commit_and_apply,
            "output_lifecycle_commit_and_apply": output_lifecycle_operations.commit_and_apply,
            "output_save_commit_and_apply": output_save_operations.commit_and_apply,
            "ui_backup_commit_and_apply": ui_backup_mutations.commit_and_apply,
            "ui_input_commit_and_apply": ui_input_mutations.commit_and_apply,
            "ui_output_commit_and_apply": ui_output_mutations.commit_and_apply,
            "app_main_inspect_managed_systemd_assets": (
                app_main.inspect_managed_systemd_assets
            ),
        }
    )

    async def fake_commit_and_apply(*args, **kwargs) -> MutationApplyResult:
        state.fake_apply_calls += 1
        return await _fake_commit_and_apply(*args, **kwargs)

    app_main.get_settings = lambda: settings
    app_main.build_access_required_response = lambda request, settings: None
    ui_dashboard_data.collect_status = _fake_collect_status
    status_routes.collect_status = _fake_collect_status
    ui_dashboard_data.peek_cached_status = lambda: {}
    ui_dashboard_context.peek_cached_status = lambda: {}
    ui_outputs_data.peek_cached_status = lambda: {}
    ui_reads.peek_cached_status = lambda: {}
    input_operations.commit_and_apply = fake_commit_and_apply
    output_lifecycle_operations.commit_and_apply = fake_commit_and_apply
    output_save_operations.commit_and_apply = fake_commit_and_apply
    ui_backup_mutations.commit_and_apply = fake_commit_and_apply
    ui_input_mutations.commit_and_apply = fake_commit_and_apply
    ui_output_mutations.commit_and_apply = fake_commit_and_apply
    app_main.inspect_managed_systemd_assets = _runtime_asset_snapshot
    return state


def _restore_runtime_patches(state: RuntimePatchState) -> None:
    originals = state.originals
    app_main.get_settings = originals["app_main_get_settings"]
    app_main.build_access_required_response = originals[
        "app_main_build_access_required_response"
    ]
    ui_dashboard_data.collect_status = originals["ui_dashboard_collect_status"]
    status_routes.collect_status = originals["status_collect_status"]
    ui_dashboard_data.peek_cached_status = originals["ui_dashboard_peek_cached_status"]
    ui_dashboard_context.peek_cached_status = originals["ui_help_peek_cached_status"]
    ui_outputs_data.peek_cached_status = originals["ui_output_peek_cached_status"]
    ui_reads.peek_cached_status = originals["ui_reads_peek_cached_status"]
    input_operations.commit_and_apply = originals["input_commit_and_apply"]
    output_lifecycle_operations.commit_and_apply = originals[
        "output_lifecycle_commit_and_apply"
    ]
    output_save_operations.commit_and_apply = originals["output_save_commit_and_apply"]
    ui_backup_mutations.commit_and_apply = originals["ui_backup_commit_and_apply"]
    ui_input_mutations.commit_and_apply = originals["ui_input_commit_and_apply"]
    ui_output_mutations.commit_and_apply = originals["ui_output_commit_and_apply"]
    app_main.inspect_managed_systemd_assets = originals[
        "app_main_inspect_managed_systemd_assets"
    ]


async def _make_settings(work_dir: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{work_dir / 'app.db'}",
        log_path=work_dir / "app.log",
        managed_env_file_path=work_dir / "cnc.env",
        nginx_generated_dir=work_dir / "nginx",
        systemd_generated_dir=work_dir / "systemd",
        apply_backup_dir=work_dir / "apply-backups",
        backend_backup_dir=work_dir / "backend-backups",
        app_control_dir=work_dir / "app-control",
        app_sandbox_dir=work_dir / "sandboxes",
        app_quadlet_dir=work_dir / "quadlet",
        tailscale_serve_state_path=work_dir / "tailscale-serve-state.json",
        host_state_path=work_dir / "host-state.json",
        apply_lock_path=work_dir / "apply.lock",
        update_log_dir=work_dir / "updates",
        updater_script_path=work_dir / "update_from_github.sh",
        access_key_hash="",
        csrf_token="benchmark-csrf-token",
        beta_routing=True,
        status_cache_ttl_sec=60,
        static_files_dir=ROOT / "app" / "static",
        templates_dir=ROOT / "app" / "templates",
    )


async def _seed_database(session_factory: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        app_backend = Backend(
            name="demo-app",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8000,
            healthcheck_mode="http",
            healthcheck_path="/health",
            resource_mode="auto",
            resource_size="small",
            volumes_json='["/srv/demo-data:/srv/data"]',
            notes="Benchmark app output",
            enabled=True,
            created_at=now - timedelta(days=3),
            updated_at=now - timedelta(days=2),
        )
        static_backend = Backend(
            name="static-site",
            kind="static",
            port=None,
            static_root="/srv/static-site",
            resource_mode="auto",
            resource_size="small",
            volumes_json="[]",
            notes="Benchmark static output",
            enabled=True,
            created_at=now - timedelta(days=3),
            updated_at=now - timedelta(days=2),
        )
        app_input = Input(kind="domain", hostname="app.example.test", enabled=True)
        static_input = Input(kind="tailnet_path", hostname="/static", enabled=True)
        app_input.backends.append(app_backend)
        static_input.backends.append(static_backend)
        session.add_all([app_backend, static_backend, app_input, static_input])
        await session.flush()
        session.add(
            ApplyRun(
                status="success",
                message="Benchmark baseline apply.",
                details_json="{}",
                created_at=now - timedelta(days=1),
            )
        )
        session.add(
            Operation(
                kind="apply_host",
                status="success",
                phase="complete",
                actor="benchmark",
                details_json='{"progress": 100, "message": "Applied"}',
                started_at=now - timedelta(days=1, minutes=2),
                finished_at=now - timedelta(days=1),
            )
        )
        session.add(
            BackendBackup(
                backend_id=1,
                status="success",
                scope="mounted_data",
                bundle_path="/tmp/demo-app.tar.gz",
                size_bytes=2_400_000,
                notes="benchmark backup",
                created_at=now - timedelta(hours=6),
            )
        )
        for offset in range(48):
            bucket = now - timedelta(minutes=5 * (48 - offset))
            session.add(
                BackendResourceSample(
                    backend_id=1,
                    bucket_start=bucket,
                    cpu_percent_of_host=5 + offset % 9,
                    cpu_percent_of_entitlement=20 + offset % 17,
                    memory_percent=35 + offset % 12,
                    memory_current_bytes=(160 + offset % 24) * 1024 * 1024,
                    memory_max_bytes=384 * 1024 * 1024,
                    disk_usage_bytes=(240 + offset % 28) * 1024 * 1024,
                    disk_usage_complete=True,
                    network_rx_bytes=10_000_000 + offset * 25_000,
                    network_tx_bytes=4_000_000 + offset * 12_000,
                    network_rx_bps=56_000 + offset * 650,
                    network_tx_bps=29_000 + offset * 350,
                    network_total_bps=85_000 + offset * 1_000,
                )
            )
            session.add(
                HostResourceSample(
                    bucket_start=bucket,
                    cpu_percent=18 + offset % 11,
                    memory_percent=48 + offset % 8,
                    disk_percent=37,
                    network_rx_bytes=80_000_000 + offset * 40_000,
                    network_tx_bytes=30_000_000 + offset * 16_000,
                    network_rx_bps=130_000 + offset * 1_500,
                    network_tx_bps=55_000 + offset * 700,
                    network_total_bps=185_000 + offset * 2_200,
                )
            )
        await session.commit()


def _dashboard_case(tab: str) -> TimingCase:
    async def run(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        response = await client.get(f"/?tab={tab}", headers=HTML_HEADERS)
        if (
            tab == "routing"
            and response.status_code == 200
            and 'data-initial-tab="routing"' not in response.text
        ):
            raise RuntimeError("routing tab is not enabled")
        return response

    return TimingCase(name=f"dashboard_{tab}_load", group="main_tab_page_load", run=run)


def _metric_history_case_name(scope: str, metric: str) -> str:
    base_name = f"{scope}_metrics_history"
    return base_name if metric == "cpu" else f"{base_name}_{metric}"


async def _validated_metric_history(
    client: httpx.AsyncClient,
    url: str,
    *,
    label: str,
) -> httpx.Response:
    response = await client.get(url, headers=JSON_HEADERS)
    if response.status_code == 200:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} returned invalid metric payload")
        if payload.get("available") is not True:
            raise RuntimeError(f"{label} metric history is unavailable")
        if not payload.get("series"):
            raise RuntimeError(f"{label} metric history returned no drawable series")
    return response


def _host_metric_history_case(metric: str) -> TimingCase:
    async def run(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await _validated_metric_history(
            client,
            f"/api/host/metrics-history?metric={metric}&timeframe=1h&width=900",
            label=f"host {metric}",
        )

    return TimingCase(
        name=_metric_history_case_name("host", metric),
        group="common_operation",
        run=run,
    )


def _output_metric_history_case(output_id: int, metric: str) -> TimingCase:
    async def run(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await _validated_metric_history(
            client,
            f"/api/backends/{output_id}/metrics-history?metric={metric}&timeframe=1h&width=900",
            label=f"output #{output_id} {metric}",
        )

    return TimingCase(
        name=_metric_history_case_name("output", metric),
        group="common_operation",
        run=run,
    )


def _local_fixture_cases() -> list[TimingCase]:
    async def output_page(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await client.get("/outputs/1", headers=HTML_HEADERS)

    async def api_status(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await client.get("/api/status", headers=JSON_HEADERS)

    async def create_input(
        client: httpx.AsyncClient, iteration: int, csrf_token: str
    ) -> httpx.Response:
        return await client.post(
            "/ui/inputs",
            data={
                "csrf_token": csrf_token,
                "kind": "domain",
                "value": f"bench-{iteration}-{time.monotonic_ns()}.example.test",
                "enabled": "true",
                "backend_ids": "1",
            },
            headers={**HTML_HEADERS, "x-cnc-dashboard-refresh": "1"},
        )

    async def toggle_output(
        client: httpx.AsyncClient, iteration: int, csrf_token: str
    ) -> httpx.Response:
        action = "disable" if iteration % 2 == 0 else "enable"
        return await client.post(
            "/ui/backends/1/state",
            data={"csrf_token": csrf_token, "action": action},
            headers=JSON_HEADERS,
        )

    async def attach_inputs(
        client: httpx.AsyncClient, _iteration: int, csrf_token: str
    ) -> httpx.Response:
        return await client.post(
            "/ui/backends/1/inputs",
            data={"csrf_token": csrf_token, "input_ids": ["1"]},
            headers=JSON_HEADERS,
        )

    async def create_static_output(
        client: httpx.AsyncClient, iteration: int, csrf_token: str
    ) -> httpx.Response:
        name = f"bench-static-{iteration}-{time.monotonic_ns()}"
        return await client.post(
            "/ui/backends",
            data={
                "csrf_token": csrf_token,
                "name": name,
                "kind": "static",
                "static_root": f"/srv/{name}",
                "enabled": "true",
                "volumes_json": "[]",
            },
            headers=HTML_HEADERS,
        )

    return [
        *[_dashboard_case(tab) for tab in MAIN_TABS],
        TimingCase(
            name="output_detail_load", group="main_tab_page_load", run=output_page
        ),
        TimingCase(name="api_status_cached", group="common_operation", run=api_status),
        *[_host_metric_history_case(metric) for metric in METRIC_HISTORY_METRICS],
        *[_output_metric_history_case(1, metric) for metric in METRIC_HISTORY_METRICS],
        TimingCase(
            name="input_create_dashboard_refresh",
            group="common_operation",
            run=create_input,
            expected_statuses=(201,),
        ),
        TimingCase(
            name="output_toggle_json",
            group="common_operation",
            run=toggle_output,
            expected_statuses=(202,),
        ),
        TimingCase(
            name="output_attach_inputs_json",
            group="common_operation",
            run=attach_inputs,
            expected_statuses=(202,),
        ),
        TimingCase(
            name="static_output_create_render",
            group="common_operation",
            run=create_static_output,
            expected_statuses=(201,),
        ),
    ]


def _output_target_from_api_item(item: object) -> OutputTarget | None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), int):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in {"app", "shield"}:
        return None
    return OutputTarget(
        id=int(item["id"]),
        name=str(item.get("name") or item["id"]),
        kind=kind,
        enabled=bool(item.get("enabled", True)),
    )


async def _discover_prod_output(client: httpx.AsyncClient) -> OutputTarget | None:
    response = await client.get("/api/backends", headers=JSON_HEADERS)
    if response.status_code == 200:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            candidates = [
                target
                for target in (_output_target_from_api_item(item) for item in payload)
                if target is not None
            ]
            for target in candidates:
                if target.enabled:
                    return target
            if candidates:
                return candidates[0]
            return None

    response = await client.get("/?tab=outputs", headers=HTML_HEADERS)
    if response.status_code == 200:
        match = OUTPUT_LINK_RE.search(response.text)
        if match:
            return OutputTarget(
                id=int(match.group(1)), name="", kind="unknown", enabled=True
            )
    return None


async def _authenticate_prod_client(client: httpx.AsyncClient, access_key: str) -> None:
    response = await client.post(
        "/access",
        data={"access_key": access_key, "next_path": "/"},
        headers=HTML_HEADERS,
    )
    if response.status_code != 303:
        raise RuntimeError(
            f"access login returned HTTP {response.status_code}: {response.text[:300]}"
        )


def _prod_readonly_cases(output: OutputTarget | None) -> list[TimingCase]:
    async def api_status(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await client.get("/api/status", headers=JSON_HEADERS)

    cases = [
        *[_dashboard_case(tab) for tab in MAIN_TABS],
        TimingCase(name="api_status_cached", group="common_operation", run=api_status),
        *[_host_metric_history_case(metric) for metric in METRIC_HISTORY_METRICS],
    ]
    if output is None:
        return cases
    output_id = output.id

    async def output_page(
        client: httpx.AsyncClient, _iteration: int, _csrf_token: str
    ) -> httpx.Response:
        return await client.get(f"/outputs/{output_id}", headers=HTML_HEADERS)

    return [
        *cases,
        TimingCase(
            name="output_detail_load", group="main_tab_page_load", run=output_page
        ),
        *[
            _output_metric_history_case(output_id, metric)
            for metric in METRIC_HISTORY_METRICS
        ],
    ]


def _summarize_ms(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * 0.95)))
    return {
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
    }


async def _time_case(
    client: httpx.AsyncClient,
    case: TimingCase,
    *,
    iterations: int,
    warmups: int,
    csrf_token: str,
) -> dict[str, object]:
    for iteration in range(warmups):
        try:
            response = await case.run(client, -1 - iteration, csrf_token)
        except Exception as exc:
            return {
                "name": case.name,
                "group": case.group,
                "iterations": iterations,
                "status": "failed",
                "error": f"warmup error: {type(exc).__name__}: {exc}",
                "samples_ms": [],
            }
        if response.status_code not in case.expected_statuses:
            return {
                "name": case.name,
                "group": case.group,
                "iterations": iterations,
                "status": "failed",
                "error": f"warmup returned HTTP {response.status_code}: {response.text[:300]}",
                "samples_ms": [],
            }

    durations_ms: list[float] = []
    error = ""
    for iteration in range(iterations):
        started_at = time.perf_counter()
        try:
            response = await case.run(client, iteration, csrf_token)
        except Exception as exc:
            error = f"iteration {iteration} error: {type(exc).__name__}: {exc}"
            break
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        if response.status_code not in case.expected_statuses:
            error = f"iteration {iteration} returned HTTP {response.status_code}: {response.text[:300]}"
            break
        durations_ms.append(elapsed_ms)

    if not durations_ms:
        return {
            "name": case.name,
            "group": case.group,
            "iterations": iterations,
            "status": "failed",
            "error": error or "no timing samples collected",
            "samples_ms": [],
        }

    return {
        "name": case.name,
        "group": case.group,
        "iterations": iterations,
        "status": "partial" if error else "ok",
        "error": error,
        **_summarize_ms(durations_ms),
        "samples_ms": [round(value, 3) for value in durations_ms],
    }


def _write_record(output_dir: Path, record: dict[str, object]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = str(record["run_id"])
    record_path = output_dir / f"{timestamp}.json"
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary_path = output_dir / "history.jsonl"
    summary_rows = [
        {
            "run_id": record["run_id"],
            "recorded_at": record["recorded_at"],
            "name": result["name"],
            "group": result["group"],
            "status": result.get("status", "ok"),
            "median_ms": result.get("median_ms"),
            "p95_ms": result.get("p95_ms"),
            "max_ms": result.get("max_ms"),
            "error": result.get("error", ""),
        }
        for result in record["results"]
    ]
    with summary_path.open("a", encoding="utf-8") as handle:
        for row in summary_rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return record_path


def _print_summary(record_path: Path, results: list[dict[str, object]]) -> None:
    name_width = max(len(str(result["name"])) for result in results)
    print(f"wrote {record_path}")
    print(f"{'case'.ljust(name_width)}  status      median    p95    max")
    for result in results:
        if result.get("status") == "failed":
            print(f"{str(result['name']).ljust(name_width)}  failed")
            continue
        print(
            f"{str(result['name']).ljust(name_width)}  "
            f"{str(result.get('status', 'ok')).ljust(8)}  "
            f"{float(result['median_ms']):7.1f}  {float(result['p95_ms']):5.1f}  {float(result['max_ms']):5.1f}"
        )


def _raise_for_failed_results(
    record_path: Path,
    *,
    max_median_ms: float | None = None,
    max_p95_ms: float | None = None,
) -> None:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    failed_results = [
        result
        for result in record.get("results", [])
        if result.get("status", "ok") != "ok"
    ]
    budget_failures = [
        result
        for result in record.get("results", [])
        if result.get("status", "ok") == "ok"
        and (
            (
                max_median_ms is not None
                and float(result.get("median_ms") or 0) > max_median_ms
            )
            or (
                max_p95_ms is not None and float(result.get("p95_ms") or 0) > max_p95_ms
            )
        )
    ]
    if not failed_results and not budget_failures:
        return
    blocked_results = [*failed_results, *budget_failures]
    names = ", ".join(
        str(result.get("name", "unknown")) for result in blocked_results[:8]
    )
    if len(blocked_results) > 8:
        names = f"{names}, ..."
    reasons: list[str] = []
    if failed_results:
        reasons.append(f"{len(failed_results)} failed or partial")
    if budget_failures:
        budget_parts: list[str] = []
        if max_median_ms is not None:
            budget_parts.append(f"median>{max_median_ms:g}ms")
        if max_p95_ms is not None:
            budget_parts.append(f"p95>{max_p95_ms:g}ms")
        reasons.append(
            f"{len(budget_failures)} over budget ({', '.join(budget_parts)})"
        )
    raise SystemExit(
        f"Timing guard failed: {'; '.join(reasons)}: {names}. See {record_path} for details."
    )


async def run_local_fixture_benchmark(
    *, iterations: int, warmups: int, output_dir: Path
) -> Path:
    run_started = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="cnc-ui-perf-") as tmp:
        work_dir = Path(tmp)
        settings = await _make_settings(work_dir)
        runtime_patches = _install_runtime_patches(settings)
        try:
            engine = create_async_engine(settings.database_url, future=True)
            session_factory = async_sessionmaker(
                bind=engine, expire_on_commit=False, class_=AsyncSession
            )
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                await _seed_database(session_factory)

                async def override_session():
                    async with session_factory() as session:
                        yield session

                app.dependency_overrides[settings_dependency] = lambda: settings
                app.dependency_overrides[db_session_dependency] = override_session

                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://127.0.0.1:9090"
                ) as client:
                    csrf_token = get_csrf_token(settings)
                    results = [
                        await _time_case(
                            client,
                            case,
                            iterations=iterations,
                            warmups=warmups,
                            csrf_token=csrf_token,
                        )
                        for case in _local_fixture_cases()
                    ]
                if runtime_patches.fake_apply_calls == 0:
                    raise RuntimeError(
                        "local fixture mutations did not use the benchmark apply stub"
                    )
            finally:
                app.dependency_overrides.pop(settings_dependency, None)
                app.dependency_overrides.pop(db_session_dependency, None)
                await engine.dispose()
        finally:
            _restore_runtime_patches(runtime_patches)

    recorded_at = datetime.now(UTC)
    record = {
        "run_id": recorded_at.strftime("%Y-%m-%dT%H%M%SZ"),
        "recorded_at": recorded_at.isoformat(),
        "duration_sec": round((recorded_at - run_started).total_seconds(), 3),
        "iterations": iterations,
        "warmups": warmups,
        "mode": "local_fixture",
        "target": "in-process ASGI seeded temporary database",
        "non_destructive": False,
        "description": "CNC dashboard timings against a seeded temporary database for local development only.",
        "results": results,
    }
    record_path = _write_record(output_dir, record)
    _print_summary(record_path, results)
    return record_path


async def run_prod_readonly_benchmark(
    *,
    base_url: str,
    cookie_header: str,
    access_key: str,
    iterations: int,
    warmups: int,
    output_dir: Path,
) -> Path:
    run_started = datetime.now(UTC)
    headers = {"cookie": cookie_header} if cookie_header else {}
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=30.0
    ) as client:
        if access_key:
            await _authenticate_prod_client(client, access_key)
        output = await _discover_prod_output(client)
        cases = _prod_readonly_cases(output)
        results = [
            await _time_case(
                client,
                case,
                iterations=iterations,
                warmups=warmups,
                csrf_token="",
            )
            for case in cases
        ]

    recorded_at = datetime.now(UTC)
    record = {
        "run_id": recorded_at.strftime("%Y-%m-%dT%H%M%SZ"),
        "recorded_at": recorded_at.isoformat(),
        "duration_sec": round((recorded_at - run_started).total_seconds(), 3),
        "iterations": iterations,
        "warmups": warmups,
        "mode": "prod_readonly",
        "target": base_url.rstrip("/"),
        "non_destructive": True,
        "description": "CNC production dashboard timings using GET page/API requests; optional access-key login is setup and excluded from timed samples.",
        "discovered_output_id": output.id if output else None,
        "discovered_output": output.as_dict() if output else None,
        "results": results,
    }
    record_path = _write_record(output_dir, record)
    _print_summary(record_path, results)
    return record_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Time CNC dashboard page loads and common UI operations."
    )
    parser.add_argument(
        "--mode",
        choices=("prod-readonly", "local-fixture"),
        default="prod-readonly",
        help="Benchmark production with safe GET requests by default; local fixture mode is only for development.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("CNC_PERF_BASE_URL", ""),
        help="Production CNC admin base URL. Can also be set with CNC_PERF_BASE_URL.",
    )
    parser.add_argument(
        "--cookie-header",
        default=os.getenv("CNC_PERF_COOKIE_HEADER", ""),
        help="Optional Cookie header for authenticated production reads. Can also be set with CNC_PERF_COOKIE_HEADER.",
    )
    parser.add_argument(
        "--access-key",
        default=os.getenv("CNC_PERF_ACCESS_KEY", ""),
        help="Optional admin access key used only to obtain a temporary session. Can also be set with CNC_PERF_ACCESS_KEY.",
    )
    parser.add_argument(
        "--access-key-stdin",
        action="store_true",
        help="Read the admin access key from stdin instead of argv/env so it is not stored in shell history.",
    )
    parser.add_argument(
        "--iterations", type=int, default=5, help="Measured iterations per case."
    )
    parser.add_argument(
        "--warmups", type=int, default=1, help="Warmup iterations per case."
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use a short smoke timing run with two measured iterations and no warmups.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for dated timing records.",
    )
    parser.add_argument(
        "--max-median-ms",
        type=float,
        default=None,
        help="Exit nonzero when any successful case median exceeds this budget.",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=None,
        help="Exit nonzero when any successful case p95 exceeds this budget.",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Write timing records and exit 0 even when one or more cases fail or only partially complete.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.quick:
        args.iterations = 2
        args.warmups = 0
    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1")
    if args.warmups < 0:
        raise SystemExit("--warmups cannot be negative")
    if args.mode == "local-fixture":
        record_path = asyncio.run(
            run_local_fixture_benchmark(
                iterations=args.iterations,
                warmups=args.warmups,
                output_dir=args.output_dir,
            )
        )
        if not args.allow_failures:
            _raise_for_failed_results(
                record_path,
                max_median_ms=args.max_median_ms,
                max_p95_ms=args.max_p95_ms,
            )
        return
    if not args.base_url:
        raise SystemExit(
            "--base-url or CNC_PERF_BASE_URL is required for prod-readonly timing"
        )
    access_key = args.access_key
    if args.access_key_stdin:
        access_key = getpass.getpass("").strip()
    record_path = asyncio.run(
        run_prod_readonly_benchmark(
            base_url=args.base_url,
            cookie_header=args.cookie_header,
            access_key=access_key,
            iterations=args.iterations,
            warmups=args.warmups,
            output_dir=args.output_dir,
        )
    )
    if not args.allow_failures:
        _raise_for_failed_results(
            record_path,
            max_median_ms=args.max_median_ms,
            max_p95_ms=args.max_p95_ms,
        )


if __name__ == "__main__":
    main()
