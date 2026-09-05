import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, Input
from app.services.commands import CommandResult
from app.services.resource_profile import ResourceProfile
from app.services import status_service
from app.services.status_service import (
    collect_status,
    invalidate_status_cache,
    tailscale_service_url,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _settings(db_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        shield_enabled=False,
        netdata_enabled=False,
    )


def test_tailscale_service_url_uses_configured_tailnet_dns_name(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        tailscale_tailnet_dns_name="example.ts.net",
    )

    assert (
        tailscale_service_url("app-dev", settings) == "https://app-dev.example.ts.net"
    )


def test_dashboard_overview_counts_match_route_conflict_contract() -> None:
    app_a = Backend(name="app-a", kind="app", enabled=True)
    app_b = Backend(name="app-b", kind="app", enabled=True)
    disabled = Backend(name="disabled", kind="static", enabled=False)
    active_domain = Input(kind="domain", hostname="a.example.com", enabled=True)
    active_domain.backends.append(app_a)
    conflicting_path = Input(kind="tailnet_path", hostname="/apps", enabled=True)
    conflicting_path.backends.extend([app_a, app_b])
    unattached = Input(kind="domain", hostname="empty.example.com", enabled=True)

    counts = status_service.dashboard_overview_counts(
        [app_a, app_b, disabled],
        [active_domain, conflicting_path, unattached],
    )

    assert counts == {
        "inputs_total": 3,
        "inputs_enabled": 3,
        "backends_total": 3,
        "backends_enabled": 2,
        "app_backends_enabled": 2,
        "routes_total": 4,
        "routes_active": 1,
    }


def test_podman_stats_parser_accepts_json_output() -> None:
    metrics = status_service._parse_podman_stats_metrics(
        '[{"CPUPerc":"12.5%","MemUsage":"64MiB / 512MiB","NetIO":"1MiB / 2MiB"}]',
        fallback_memory_max_bytes=None,
    )

    assert metrics["cpu_percent_of_host"] == 12.5
    assert metrics["memory_percent"] == 12.5
    assert metrics["memory_current_bytes"] == 67_108_864
    assert metrics["memory_max_bytes"] == 536_870_912
    assert metrics["network_rx_bytes"] == 1_048_576
    assert metrics["network_tx_bytes"] == 2_097_152


def test_cpu_entitlement_pressure_is_not_capped_at_100() -> None:
    assert status_service._derive_cpu_percent_of_entitlement(30.0, 10.0) == 300.0


def test_host_network_scope_excludes_container_virtual_interfaces() -> None:
    assert status_service._host_network_interface_in_scope("eth0") is True
    assert status_service._host_network_interface_in_scope("vethabc") is False
    assert status_service._host_network_interface_in_scope("podman0") is False
    assert status_service._host_network_interface_in_scope("lo") is False


@pytest.mark.asyncio
async def test_status_cache_reuses_recent_result(monkeypatch, tmp_path: Path) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    calls: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        calls.append(command)
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": False,
            "container_state": {
                "ActiveState": "inactive",
                "SubState": "configured",
                "ActiveEnterTimestamp": "",
            },
            "bootstrap": {
                "status": "missing",
                "marker_present": False,
                "lock_active": False,
                "stale": False,
                "state": {},
            },
            "private_ip": None,
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        first = await collect_status(session, settings, cache_ttl_seconds=60)
        second = await collect_status(session, settings, cache_ttl_seconds=60)

    assert first == second
    assert len(calls) == 1  # nginx only; cached second call does not re-run


@pytest.mark.asyncio
async def test_status_cache_can_return_stale_payload_without_blocking_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    calls: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        calls.append(command)
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": False,
            "container_state": {
                "ActiveState": "inactive",
                "SubState": "configured",
                "ActiveEnterTimestamp": "",
            },
            "bootstrap": {
                "status": "missing",
                "marker_present": False,
                "lock_active": False,
                "stale": False,
                "state": {},
            },
            "private_ip": None,
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        first = await collect_status(session, settings, cache_ttl_seconds=60)
        status_service._status_cache_expires_at = 0.0
        second = await collect_status(
            session, settings, cache_ttl_seconds=60, allow_stale=True
        )

    assert first == second
    assert len(calls) == 1


def test_status_cache_prefill_uses_forced_refresh(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path / "app.db")
    scheduled: list[bool] = []

    def fake_prefill(
        _settings: Settings, *, force_refresh: bool = False, delay_seconds: float = 0.05
    ) -> bool:
        del delay_seconds
        scheduled.append(force_refresh)
        return True

    monkeypatch.setattr(status_service, "schedule_status_cache_prefill", fake_prefill)

    invalidate_status_cache(settings, prefill=True)

    assert scheduled == [True]


@pytest.mark.asyncio
async def test_forced_status_prefill_runs_after_existing_prefill(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path / "app.db")
    release_existing_prefill = asyncio.Event()
    calls: list[bool] = []

    status_service._status_cache_prefill_task = None
    status_service._status_cache_prefill_loop = None
    status_service._status_cache_prefill_pending = None
    status_service._status_cache_prefill_reschedule_attached = False

    async def fake_warm_status_cache(
        _settings: Settings, *, force_refresh: bool = False
    ) -> None:
        calls.append(force_refresh)
        if not force_refresh:
            await release_existing_prefill.wait()

    monkeypatch.setattr(status_service, "warm_status_cache", fake_warm_status_cache)

    assert status_service.schedule_status_cache_prefill(
        settings, force_refresh=False, delay_seconds=0
    )
    await asyncio.sleep(0)

    assert not status_service.schedule_status_cache_prefill(
        settings, force_refresh=True, delay_seconds=0
    )
    assert calls == [False]

    release_existing_prefill.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if calls == [False, True]:
            break

    assert calls == [False, True]
    status_service._status_cache_prefill_task = None
    status_service._status_cache_prefill_loop = None
    status_service._status_cache_prefill_pending = None
    status_service._status_cache_prefill_reschedule_attached = False


@pytest.mark.asyncio
async def test_status_uncached_refresh_reuses_network_isolation_canary_cache(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)
    canary_calls = 0

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    def fake_canary(backends, _settings):
        nonlocal canary_calls
        canary_calls += 1
        return {
            "checked": True,
            "backend_count": len(backends),
            "pair_count": 2,
            "ok": True,
            "leaks": [],
            "unknown": [],
            "checks": [],
        }

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_network_isolation_canary", fake_canary
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings, **_kwargs: {
            "backend": backend.name,
            "container": f"cnc-app-{backend.name}",
            "container_exists": False,
            "container_state": {"ActiveState": "inactive", "SubState": "configured"},
            "bootstrap": {
                "status": "missing",
                "marker_present": False,
                "lock_active": False,
                "stale": False,
                "state": {},
            },
            "private_ip": None,
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_diagnostics_snapshot",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=2,
            effective_backend_count=2,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service._collect_host_metrics", lambda _profile: {}
    )

    async with maker() as session:
        session.add_all(
            [
                Backend(
                    name="web-a",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
                Backend(
                    name="web-b",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
            ]
        )
        await session.commit()

        first = await collect_status(session, settings, cache_ttl_seconds=0)
        status_service._status_cache_expires_at = 0.0
        second = await collect_status(session, settings, cache_ttl_seconds=0)

    assert first["app_network_isolation"]["cache"] == "miss"
    assert second["app_network_isolation"]["cache"] == "hit"
    assert canary_calls == 1


@pytest.mark.asyncio
async def test_status_force_refresh_bypasses_network_isolation_canary_cache(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)
    canary_calls = 0

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    def fake_canary(backends, _settings):
        nonlocal canary_calls
        canary_calls += 1
        return {
            "checked": True,
            "backend_count": len(backends),
            "pair_count": 2,
            "ok": True,
            "leaks": [],
            "unknown": [],
            "checks": [],
        }

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_network_isolation_canary", fake_canary
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings, **_kwargs: {
            "backend": backend.name,
            "container": f"cnc-app-{backend.name}",
            "container_exists": False,
            "container_state": {"ActiveState": "inactive", "SubState": "configured"},
            "bootstrap": {"status": "missing", "state": {}},
            "private_ip": None,
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_diagnostics_snapshot",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=2,
            effective_backend_count=2,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service._collect_host_metrics", lambda _profile: {}
    )

    async with maker() as session:
        session.add_all(
            [
                Backend(
                    name="web-a",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
                Backend(
                    name="web-b",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
            ]
        )
        await session.commit()

        first = await collect_status(session, settings, force_refresh=True)
        second = await collect_status(session, settings, force_refresh=True)

    assert first["app_network_isolation"]["cache"] == "refresh"
    assert second["app_network_isolation"]["cache"] == "refresh"
    assert canary_calls == 2


@pytest.mark.asyncio
async def test_status_degrades_when_network_isolation_canary_fails(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    def fail_canary(*_args, **_kwargs):
        raise RuntimeError("canary failed")

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_network_isolation_canary", fail_canary
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings, **_kwargs: {
            "backend": backend.name,
            "container": f"cnc-app-{backend.name}",
            "container_exists": False,
            "container_state": {"ActiveState": "inactive", "SubState": "configured"},
            "bootstrap": {"status": "missing", "state": {}},
            "private_ip": None,
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_diagnostics_snapshot",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=2,
            effective_backend_count=2,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service._collect_host_metrics", lambda _profile: {}
    )

    async with maker() as session:
        session.add_all(
            [
                Backend(
                    name="web-a",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
                Backend(
                    name="web-b",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
            ]
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)

    assert payload["app_network_isolation"]["ok"] is False
    assert payload["app_network_isolation"]["error"] == "canary failed"
    assert len(payload["services"]) == 2


@pytest.mark.asyncio
async def test_network_isolation_canary_exception_emits_drift_event(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    settings = _settings(tmp_path / "app.db")
    backends = [
        Backend(
            name="web-a",
            kind="app",
            enabled=True,
            volumes_json="[]",
            handoff_port=8337,
        ),
        Backend(
            name="web-b",
            kind="app",
            enabled=True,
            volumes_json="[]",
            handoff_port=8337,
        ),
    ]
    events: list[dict[str, object]] = []

    def fail_canary(*_args, **_kwargs):
        raise RuntimeError("canary failed")

    async def fake_emit_control_event(_settings, **kwargs):
        events.append(kwargs)
        return {"sent": False}

    monkeypatch.setattr(
        "app.services.status_service.collect_app_network_isolation_canary",
        fail_canary,
    )
    monkeypatch.setattr(
        "app.services.status_service.emit_control_event",
        fake_emit_control_event,
    )

    payload = await status_service._network_isolation_status(
        backends, settings, force_refresh=True
    )

    assert payload["ok"] is False
    assert payload["cache"] == "error"
    assert payload["error"] == "canary failed"
    assert len(events) == 1
    assert events[0]["kind"] == "output_isolation_drift"
    assert events[0]["related_backends"] == ["web-a", "web-b"]
    assert events[0]["subevents"][0] == {"label": "status", "value": "unknown"}


@pytest.mark.asyncio
async def test_status_emits_network_isolation_drift_event(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)
    events: list[dict[str, object]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    def fake_canary(backends, _settings):
        return {
            "checked": True,
            "backend_count": len(backends),
            "pair_count": 2,
            "ok": False,
            "leaks": [
                {
                    "source": "web-a",
                    "target": "web-b",
                    "check": "private_connect",
                    "detail": "200",
                }
            ],
            "unknown": [],
            "checks": [],
        }

    async def fake_emit_control_event(_settings, **kwargs):
        events.append(kwargs)
        return {"sent": False}

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_network_isolation_canary", fake_canary
    )
    monkeypatch.setattr(
        "app.services.status_service.emit_control_event", fake_emit_control_event
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings, **_kwargs: {
            "backend": backend.name,
            "container": f"cnc-app-{backend.name}",
            "container_exists": True,
            "container_state": {"ActiveState": "active", "SubState": "running"},
            "bootstrap": {"status": "succeeded", "state": {}},
            "private_ip": "10.88.0.8",
            "private_reachable": True,
            "loopback_reachable": True,
            "dns_servers": [],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_diagnostics_snapshot",
        lambda _settings: None,
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=2,
            effective_backend_count=2,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service._collect_host_metrics", lambda _profile: {}
    )

    async with maker() as session:
        session.add_all(
            [
                Backend(
                    name="web-a",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
                Backend(
                    name="web-b",
                    kind="app",
                    enabled=True,
                    volumes_json="[]",
                    handoff_port=8337,
                ),
            ]
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)
        status_service._status_cache_expires_at = 0.0
        await collect_status(session, settings, force_refresh=True)

    assert payload["app_network_isolation"]["ok"] is False
    assert len(events) == 1
    assert events[0]["kind"] == "output_isolation_drift"
    assert events[0]["notify"] is False
    assert events[0]["related_backends"] == ["web-a", "web-b"]
    assert events[0]["subevents"][0] == {"label": "status", "value": "warning"}


def test_collect_host_metrics_reports_cpu_memory_disk_and_network(monkeypatch) -> None:
    status_service._host_cpu_sample = None
    status_service._host_network_sample = None

    monotonic_ticks = [10.0, 12.0]
    tick_index = {"value": 0}

    def fake_monotonic() -> float:
        idx = tick_index["value"]
        tick_index["value"] = min(idx + 1, len(monotonic_ticks) - 1)
        return monotonic_ticks[idx]

    monkeypatch.setattr(status_service.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(status_service, "_read_linux_cpu_totals", lambda: (1000, 400))
    monkeypatch.setattr(
        status_service, "_read_linux_memory", lambda: (8 * 1024, 3 * 1024)
    )
    monkeypatch.setattr(
        status_service, "_read_linux_network_bytes", lambda: (1_000_000, 2_000_000)
    )

    class FakeDisk:
        total = 1000
        used = 250
        free = 750

    monkeypatch.setattr(status_service.shutil, "disk_usage", lambda _path: FakeDisk())
    monkeypatch.setattr(status_service.os, "getloadavg", lambda: (0.5, 0.4, 0.3))

    profile = ResourceProfile(
        mode="auto",
        backend_count=1,
        effective_backend_count=1,
        host_cpu_count=4,
        host_memory_bytes=8 * 1024,
        memory_high="256M",
        memory_max="384M",
        cpu_quota="50%",
        reason=None,
    )

    first = status_service._collect_host_metrics(profile)
    monkeypatch.setattr(status_service, "_read_linux_cpu_totals", lambda: (1200, 440))
    monkeypatch.setattr(
        status_service, "_read_linux_network_bytes", lambda: (1_500_000, 2_750_000)
    )
    second = status_service._collect_host_metrics(profile)

    assert first["cpu_percent"] == 12.5
    assert first["memory_percent"] == 37.5
    assert first["disk_percent"] == 25.0
    assert first["network_total_bps"] == 0.0
    assert second["cpu_percent"] == 80.0
    assert second["network_rx_bps"] == 250000.0
    assert second["network_tx_bps"] == 375000.0


def test_app_action_hints_include_fix_for_network_orphan() -> None:
    backend = Backend(
        name="web",
        kind="app",
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    hints = status_service.app_action_hints(backend, {"issues": ["network_orphan"]})

    assert hints == [
        {
            "label": "fix",
            "command": "cnc-admin fix web",
            "note": "repair CNC-owned backend drift and restore the managed sandbox/publication path",
        },
        {
            "label": "doctor",
            "command": "cnc-admin app doctor web",
            "note": "re-check runtime state after any fix or apply attempt",
        },
    ]


def test_app_action_hints_include_fix_for_legacy_loopback_proxy_state() -> None:
    backend = Backend(
        name="web",
        kind="app",
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    hints = status_service.app_action_hints(
        backend,
        {"issues": ["loopback_publish_missing", "legacy_loopback_proxy_present"]},
    )

    assert hints == [
        {
            "label": "fix",
            "command": "cnc-admin fix web",
            "note": "repair CNC-owned backend drift and restore the managed sandbox/publication path",
        },
        {
            "label": "doctor",
            "command": "cnc-admin app doctor web",
            "note": "re-check runtime state after any fix or apply attempt",
        },
    ]


def test_app_action_hints_include_fix_for_unavailable_backend_exec() -> None:
    backend = Backend(
        name="web",
        kind="app",
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    hints = status_service.app_action_hints(
        backend,
        {"issues": ["podman_exec_unavailable"]},
    )

    assert hints == [
        {
            "label": "fix",
            "command": "cnc-admin fix web",
            "note": "restart the managed backend container from the host side when the backend exec path is unavailable",
        },
        {
            "label": "doctor",
            "command": "cnc-admin app doctor web",
            "note": "re-check runtime state after any fix or apply attempt",
        },
    ]


def test_app_action_hints_do_not_repair_deferred_guest_observation() -> None:
    backend = Backend(
        name="web",
        kind="app",
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    hints = status_service.app_action_hints(
        backend,
        {"issues": [], "observation_issues": ["podman_exec_busy"]},
    )

    assert hints == [
        {
            "label": "doctor",
            "command": "cnc-admin app doctor web",
            "note": "re-check runtime state after any fix or apply attempt",
        }
    ]


@pytest.mark.asyncio
async def test_status_calculates_service_cpu_and_memory_percent(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    monotonic_ticks = [101.0, 101.0, 101.0]
    monotonic_index = {"value": 0}

    def fake_monotonic() -> float:
        idx = monotonic_index["value"]
        monotonic_index["value"] = idx + 1
        if idx < len(monotonic_ticks):
            return monotonic_ticks[idx]
        return monotonic_ticks[-1]

    monkeypatch.setattr("app.services.status_service.time.monotonic", fake_monotonic)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_read_container_stats(
        _container: str, timeout_sec: int
    ) -> CommandResult:
        return CommandResult(
            command=["podman", "stats", "--no-stream", "cnc-app-web"],
            returncode=0,
            stdout="12.5%|200MiB / 384MiB",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.read_container_stats", fake_read_container_stats
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": True,
            "container_state": {
                "ActiveState": "active",
                "SubState": "running",
                "ActiveEnterTimestamp": "2026-03-25T00:00:00Z",
            },
            "bootstrap": {
                "status": "succeeded",
                "marker_present": True,
                "lock_active": False,
                "stale": False,
                "state": {},
            },
            "private_ip": "10.88.0.8",
            "private_reachable": True,
            "loopback_reachable": True,
            "dns_servers": ["1.1.1.1", "1.0.0.1"],
            "issues": [],
            "ok": True,
        },
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        second = await collect_status(session, settings, force_refresh=True)

    service_payload = second["services"][0]
    metrics = service_payload["metrics"]
    assert metrics["cpu_percent"] == 25.0
    assert metrics["memory_percent"] == 52.1


@pytest.mark.asyncio
async def test_status_reports_enabled_app_containers(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    calls: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        calls.append(command)
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_state": {
                "ActiveState": "active",
                "SubState": "running",
                "ActiveEnterTimestamp": "2026-03-25T00:00:00Z",
            },
            "bootstrap": {
                "status": "succeeded",
                "marker_present": True,
                "lock_active": False,
                "stale": False,
                "state": {
                    "started_at": "2026-03-25T00:00:00Z",
                    "finished_at": "2026-03-25T00:01:00Z",
                    "reconcile_phase": "steady_state",
                    "reconcile_phase_status": "succeeded",
                },
            },
            "private_ip": "10.88.0.8",
            "private_reachable": True,
            "loopback_reachable": True,
            "dns_servers": ["1.1.1.1", "1.0.0.1"],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                healthcheck_path="/",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)

    assert len(calls) == 1  # nginx only; app diagnostics are handled separately
    assert payload["services"][0]["service"] == "cnc-app-web"
    assert payload["services"][0]["data"]["ActiveState"] == "active"
    assert payload["services"][0]["data"]["BootstrapState"] == "succeeded"
    assert payload["services"][0]["data"]["ReconcilePhase"] == "steady_state"
    assert payload["services"][0]["data"]["ReconcilePhaseStatus"] == "succeeded"
    assert payload["services"][0]["data"]["PrivateReachable"] == "yes"
    assert payload["services"][0]["data"]["ProxyReachable"] == "yes"
    assert payload["services"][0]["data"]["DnsServers"] == "1.1.1.1, 1.0.0.1"
    assert payload["services"][0]["action_hints"] == []


@pytest.mark.asyncio
async def test_status_reports_app_container_metrics_from_podman_stats(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_read_container_stats(
        _container: str, timeout_sec: int
    ) -> CommandResult:
        return CommandResult(
            command=["podman", "stats", "--no-stream", "cnc-app-web"],
            returncode=0,
            stdout="12.5%|128MiB / 512MiB|1MiB / 2MiB",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.read_container_stats", fake_read_container_stats
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": True,
            "container_state": {
                "ActiveState": "active",
                "SubState": "running",
                "ActiveEnterTimestamp": "2026-03-25T00:00:00Z",
            },
            "bootstrap": {
                "status": "succeeded",
                "marker_present": True,
                "lock_active": False,
                "stale": False,
                "state": {
                    "started_at": "2026-03-25T00:00:00Z",
                    "finished_at": "2026-03-25T00:01:00Z",
                    "reconcile_phase": "steady_state",
                    "reconcile_phase_status": "succeeded",
                },
            },
            "private_ip": "10.88.0.8",
            "private_reachable": True,
            "loopback_reachable": True,
            "dns_servers": ["1.1.1.1", "1.0.0.1"],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                healthcheck_path="/",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)

    metrics = payload["services"][0]["metrics"]
    assert metrics["cpu_percent"] == 25.0
    assert metrics["memory_percent"] == 25.0
    assert metrics["memory_current_bytes"] == 134217728
    assert metrics["memory_max_bytes"] == 536870912
    assert metrics["network_rx_bytes"] == 1048576
    assert metrics["network_tx_bytes"] == 2097152


@pytest.mark.asyncio
async def test_status_reports_shield_container_metrics_from_podman_stats(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_read_container_stats(
        container: str, timeout_sec: int
    ) -> CommandResult:
        assert container == "cnc-shield"
        return CommandResult(
            command=["podman", "stats", "--no-stream", container],
            returncode=0,
            stdout="2.5%|32MiB / 256MiB|512KiB / 1MiB",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.read_container_stats", fake_read_container_stats
    )

    async with maker() as session:
        session.add(
            Backend(name="shield", kind="shield", enabled=True, volumes_json="[]")
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)

    shield_service = next(
        item for item in payload["services"] if item["service"] == "cnc-shield.service"
    )
    metrics = shield_service["metrics"]
    assert shield_service["metric_service"] == "cnc-shield"
    assert metrics["cpu_percent"] == 2.5
    assert metrics["memory_percent"] == 12.5
    assert metrics["memory_current_bytes"] == 33554432
    assert metrics["memory_max_bytes"] == 268435456


@pytest.mark.asyncio
async def test_status_calculates_app_network_throughput_from_podman_stats(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    status_service._network_usage_samples = {}
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)
    network_totals = [(1_000_000, 2_000_000), (1_750_000, 2_450_000)]

    monkeypatch.setattr("app.services.status_service.time.monotonic", lambda: 105.0)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_read_container_stats(
        _container: str, timeout_sec: int
    ) -> CommandResult:
        rx_bytes, tx_bytes = network_totals.pop(0)
        return CommandResult(
            command=["podman", "stats", "--no-stream", "cnc-app-web"],
            returncode=0,
            stdout=f"1.0%|64MiB / 512MiB|{rx_bytes}B / {tx_bytes}B",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.read_container_stats", fake_read_container_stats
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": True,
            "container_state": {"ActiveState": "active", "SubState": "running"},
            "private_reachable": True,
            "loopback_reachable": True,
            "issues": [],
            "ok": True,
            "cgroup": {
                "available": True,
                "memory": {
                    "current": 96 * 1024 * 1024,
                    "max": 256 * 1024 * 1024,
                    "events": {},
                },
            },
        },
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        await collect_status(session, settings, force_refresh=True)
        status_service._network_usage_samples = {
            "cnc-app-web": (100.0, 1_000_000, 2_000_000)
        }
        second = await collect_status(session, settings, force_refresh=True)

    metrics = second["services"][0]["metrics"]
    assert metrics["network_rx_bps"] == 150000.0
    assert metrics["network_tx_bps"] == 90000.0
    assert metrics["network_total_bps"] == 240000.0
    assert metrics["memory_current_bytes"] == 96 * 1024 * 1024
    assert metrics["memory_max_bytes"] == 256 * 1024 * 1024
    assert metrics["memory_percent"] == 37.5


@pytest.mark.asyncio
async def test_status_falls_back_to_container_scope_metrics_when_podman_stats_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    cpu_usage_nsec = 2_000_000_000

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal cpu_usage_nsec
        if command[:3] == ["systemctl", "show", "libpod-abc123.scope"]:
            payload = (
                "ActiveState=active\n"
                "SubState=running\n"
                "MemoryCurrent=134217728\n"
                f"CPUUsageNSec={cpu_usage_nsec}"
            )
            cpu_usage_nsec += 500_000_000
            return CommandResult(
                command=command, returncode=0, stdout=payload, stderr=""
            )
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    async def fake_read_container_stats(
        _container: str, timeout_sec: int
    ) -> CommandResult:
        return CommandResult(
            command=["podman", "stats", "--no-stream", "cnc-app-web"],
            returncode=125,
            stdout="",
            stderr='Error: unknown FS magic on "/run/netns/netns-123"',
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.read_container_stats", fake_read_container_stats
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_exists": True,
            "container_id": "abc123",
            "container_state": {
                "ActiveState": "active",
                "SubState": "running",
                "ActiveEnterTimestamp": "2026-03-25T00:00:00Z",
            },
            "bootstrap": {
                "status": "succeeded",
                "marker_present": True,
                "lock_active": False,
                "stale": False,
                "state": {
                    "started_at": "2026-03-25T00:00:00Z",
                    "finished_at": "2026-03-25T00:01:00Z",
                    "reconcile_phase": "steady_state",
                    "reconcile_phase_status": "succeeded",
                },
            },
            "private_ip": "10.88.0.8",
            "private_reachable": True,
            "loopback_reachable": True,
            "dns_servers": ["1.1.1.1", "1.0.0.1"],
            "issues": [],
            "ok": True,
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.status_service._collect_host_metrics",
        lambda _profile: {"cpu_percent": None, "memory_percent": None},
    )
    monkeypatch.setattr("app.services.status_service.time.monotonic", lambda: 105.0)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                healthcheck_path="/",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        first = await collect_status(session, settings, force_refresh=True)
        status_service._cpu_usage_samples = {
            "libpod-abc123.scope": (100.0, 2_000_000_000)
        }
        second = await collect_status(session, settings, force_refresh=True)

    first_metrics = first["services"][0]["metrics"]
    second_metrics = second["services"][0]["metrics"]
    assert first_metrics["memory_percent"] == 33.3
    assert first_metrics["memory_current_bytes"] == 134217728
    assert first_metrics["cpu_percent"] is None
    assert second_metrics["memory_percent"] == 33.3
    assert second_metrics["cpu_percent_of_host"] == 2.5
    assert second_metrics["cpu_percent"] == 5.0


@pytest.mark.asyncio
async def test_status_reports_stuck_bootstrap_issue(
    monkeypatch, tmp_path: Path
) -> None:
    invalidate_status_cache()
    db_path = tmp_path / "app.db"
    maker = await _make_session(db_path)
    settings = _settings(db_path)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(
            command=command,
            returncode=0,
            stdout="ActiveState=active\nSubState=running",
            stderr="",
        )

    monkeypatch.setattr(
        "app.services.status_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.status_service.collect_app_backend_diagnostics",
        lambda backend, settings: {
            "backend": backend.name,
            "container": "cnc-app-web",
            "container_state": {
                "ActiveState": "active",
                "SubState": "running",
                "ActiveEnterTimestamp": "2026-03-25T00:00:00Z",
            },
            "bootstrap": {
                "status": "stuck",
                "marker_present": False,
                "lock_active": False,
                "stale": True,
                "state": {
                    "started_at": "2026-03-25T00:00:00Z",
                    "failure_phase": "bootstrap_stale",
                },
            },
            "private_ip": "10.88.0.8",
            "private_reachable": False,
            "loopback_reachable": False,
            "dns_servers": ["1.1.1.1"],
            "issues": ["bootstrap_stuck", "loopback_unreachable"],
            "ok": False,
            "inspect_error": "",
        },
    )
    monkeypatch.setattr(
        "app.services.status_service.build_resource_profile",
        lambda _settings, _count: ResourceProfile(
            mode="auto",
            backend_count=1,
            effective_backend_count=1,
            host_cpu_count=4,
            host_memory_bytes=8 * 1024 * 1024 * 1024,
            memory_high="256M",
            memory_max="384M",
            cpu_quota="50%",
            reason=None,
        ),
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                port=12000,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                enabled=True,
            )
        )
        await session.commit()

        payload = await collect_status(session, settings, force_refresh=True)

    assert payload["services"][0]["ok"] is False
    assert payload["services"][0]["data"]["BootstrapState"] == "stuck"
    assert payload["services"][0]["data"]["BootstrapStale"] == "yes"
    assert (
        payload["services"][0]["data"]["RuntimeIssues"]
        == "bootstrap_stuck, loopback_unreachable"
    )
    assert payload["services"][0]["action_hints"] == [
        {
            "label": "logs",
            "command": "cnc-admin app logs web",
            "note": "inspect the latest sandbox bootstrap and app-runtime failure context",
        },
        {
            "label": "fix",
            "command": "cnc-admin fix web",
            "note": "clear failed sandbox bootstrap state and rerun backend reconciliation",
        },
        {
            "label": "fix",
            "command": "cnc-admin fix web",
            "note": "repair the sandbox path and restore loopback publication",
        },
        {
            "label": "doctor",
            "command": "cnc-admin app doctor web",
            "note": "re-check runtime state after any fix or apply attempt",
        },
    ]
