from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ApplyRun, Backend, BackendResourceSample, HostApplyState
from app.services.commands import CommandResult
from app.services import auto_size, control_events
from app.schemas.apply import ApplyResponse


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}",
        auto_resource_limits=False,
    )


def _patch_container_stats(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cpu_percent: float,
    memory_percent: float,
    network_rx_bytes: int | None = None,
    network_tx_bytes: int | None = None,
) -> None:
    memory_limit_mib = 256
    memory_current_mib = memory_limit_mib * memory_percent / 100
    network_part = (
        f"|{network_rx_bytes}B / {network_tx_bytes}B"
        if network_rx_bytes is not None and network_tx_bytes is not None
        else ""
    )

    async def fake_read_container_stats(*_args, **_kwargs):
        return CommandResult(
            command=["podman", "stats"],
            returncode=0,
            stdout=f"{cpu_percent}%|{memory_current_mib}MiB / {memory_limit_mib}MiB{network_part}",
            stderr="",
        )

    monkeypatch.setattr(auto_size, "read_container_stats", fake_read_container_stats)
    monkeypatch.setattr(
        auto_size,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                command=["podman", "inspect"],
                returncode=1,
                stdout="",
                stderr="unavailable",
            ),
            None,
        ),
    )


def test_decide_backend_size_handles_naive_model_timestamps() -> None:
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)
    backend = Backend(
        name="web",
        kind="app",
        enabled=True,
        resource_mode="auto",
        resource_size="small",
        created_at=(now - timedelta(days=10)).replace(tzinfo=None),
        resource_size_updated_at=(now - timedelta(days=10)).replace(tzinfo=None),
    )
    samples = [
        BackendResourceSample(
            backend_id=1,
            bucket_start=now - timedelta(hours=index + 1),
            cpu_percent_of_entitlement=92,
            memory_percent=88,
        )
        for index in range(auto_size.AUTO_SIZE_MIN_SAMPLES)
    ]

    decision = auto_size._decide_backend_size(backend, samples, now)

    assert decision.decision == "promote"
    assert decision.next_size == "medium"


@pytest.mark.parametrize(
    ("memory_events", "expected_trigger"),
    [
        ({"max": 1, "oom_kill": 0}, "memory.max"),
        ({"max": 1, "oom_kill": 1}, "oom_kill"),
    ],
)
@pytest.mark.asyncio
async def test_auto_size_tick_promotes_once_for_new_memory_event(
    monkeypatch,
    tmp_path: Path,
    caplog,
    memory_events: dict[str, int],
    expected_trigger: str,
) -> None:
    caplog.set_level(logging.INFO)
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
    apply_calls: list[str] = []

    _patch_container_stats(monkeypatch, cpu_percent=20.0, memory_percent=30.0)
    monkeypatch.setattr(
        auto_size,
        "collect_container_cgroup_diagnostics",
        lambda _payload, **_kwargs: {
            "available": True,
            "memory": {
                "peak": 409403392,
                "events": memory_events,
            },
        },
    )

    async def fake_run_apply(*_args, **_kwargs):
        apply_calls.append("apply")
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={},
            run_id=17,
        )

    async def no_op(*_args, **_kwargs):
        return None

    async def no_notification(*_args, **_kwargs):
        return {"sent": False, "reason": "test"}

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)
    monkeypatch.setattr(auto_size, "_complete_deferred_apply_operation", no_op)
    monkeypatch.setattr(auto_size, "_emit_deferred_apply_completed_event", no_op)
    monkeypatch.setattr(auto_size, "_notify_auto_size_changes", no_notification)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id
        session.add(
            BackendResourceSample(
                backend_id=backend_id,
                bucket_start=now - timedelta(minutes=1),
                memory_events_max=0,
                memory_events_oom_kill=0,
            )
        )
        await session.commit()

        first = await auto_size.run_auto_size_tick(
            session, _settings(tmp_path), now=now
        )
        second = await auto_size.run_auto_size_tick(
            session,
            _settings(tmp_path),
            now=now + timedelta(minutes=1),
        )
        refreshed = await session.get(Backend, backend_id)

    assert first["changed_backends"] == ["web"]
    assert first["decisions"][0] == {
        "backend_id": backend_id,
        "backend": "web",
        "previous_size": "small",
        "next_size": "medium",
        "decision": "promote",
        "reason": f"memory_event_{expected_trigger}",
        "cpu_p95": None,
        "memory_p95": None,
        "sample_count": 0,
        "memory_peak_bytes": 409403392,
        "trigger": expected_trigger,
        "trigger_count": 1,
        "previous_counter": 0,
        "current_counter": 1,
        "memory_high_bytes": 268435456,
        "memory_max_bytes": 268435456,
    }
    assert second["evaluated"] is False
    assert apply_calls == ["apply"]
    assert refreshed is not None
    assert refreshed.resource_size == "medium"

    detected = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        == "auto.size.emergency.promotion.requested"
    )
    completed = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "auto.size.emergency.promoted"
    )
    for record in (detected, completed):
        assert record.context["backend_id"] == backend_id
        assert record.context["old_size"] == "small"
        assert record.context["new_size"] == "medium"
        assert record.context["memory_peak_bytes"] == 409403392
        assert record.context["trigger"] == expected_trigger
        assert record.context["previous_counter"] == 0
        assert record.context["current_counter"] == 1
        assert record.context["counter_delta"] == 1
        assert record.context["memory_peak_bytes"] == 409403392
    assert completed.context["apply_run_id"] == 17


@pytest.mark.asyncio
async def test_emergency_promotion_retries_after_apply_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
    apply_calls = 0

    _patch_container_stats(monkeypatch, cpu_percent=20.0, memory_percent=30.0)
    monkeypatch.setattr(
        auto_size,
        "collect_container_cgroup_diagnostics",
        lambda _payload, **_kwargs: {
            "available": True,
            "memory": {"peak": 390 * 1024 * 1024, "events": {"max": 1, "oom_kill": 0}},
        },
    )

    async def fake_run_apply(*_args, **_kwargs):
        nonlocal apply_calls
        apply_calls += 1
        return ApplyResponse(
            status="error" if apply_calls == 1 else "success",
            message="apply failed" if apply_calls == 1 else "apply completed",
            details={},
            run_id=apply_calls,
        )

    async def no_op(*_args, **_kwargs):
        return None

    async def no_notification(*_args, **_kwargs):
        return {"sent": False, "reason": "test"}

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)
    monkeypatch.setattr(auto_size, "_emit_emergency_promotion_failed_event", no_op)
    monkeypatch.setattr(auto_size, "_complete_deferred_apply_operation", no_op)
    monkeypatch.setattr(auto_size, "_emit_deferred_apply_completed_event", no_op)
    monkeypatch.setattr(auto_size, "_notify_auto_size_changes", no_notification)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id
        session.add(
            BackendResourceSample(
                backend_id=backend_id,
                bucket_start=now - timedelta(minutes=1),
                memory_events_max=0,
                memory_events_oom_kill=0,
            )
        )
        await session.commit()

        first = await auto_size.run_auto_size_tick(
            session, _settings(tmp_path), now=now
        )
        after_failure = await session.get(Backend, backend_id)
        size_after_failure = after_failure.resource_size if after_failure else None
        second = await auto_size.run_auto_size_tick(
            session, _settings(tmp_path), now=now + timedelta(minutes=1)
        )
        after_success = await session.get(Backend, backend_id)

    assert first["apply"]["status"] == "error"
    assert first["apply"]["emergency_retry_prepared"] is True
    assert size_after_failure == "small"
    assert second["apply"]["status"] == "success"
    assert after_success is not None
    assert after_success.resource_size == "medium"
    assert apply_calls == 2


def test_memory_event_alerts_do_not_override_manual_custom_or_large_policy() -> None:
    manual = Backend(
        id=1,
        name="manual",
        kind="app",
        resource_mode="manual",
        resource_size="small",
    )
    custom = Backend(
        id=2,
        name="custom",
        kind="app",
        resource_mode="auto",
        resource_size="small",
        memory_max_override="1G",
    )
    large = Backend(
        id=3,
        name="large",
        kind="app",
        resource_mode="auto",
        resource_size="large",
    )
    triggers = tuple(
        auto_size.MemoryEventTrigger(
            backend_id=backend.id,
            trigger="memory.max",
            trigger_count=1,
            memory_peak_bytes=500,
            previous_counter=2,
            current_counter=3,
            memory_high_bytes=400,
            memory_max_bytes=500,
        )
        for backend in (manual, custom, large)
    )

    assert (
        auto_size._memory_event_promotion_decisions([manual, custom, large], triggers)
        == []
    )
    alerts = auto_size._memory_event_resource_alerts([manual, custom, large], triggers)
    assert [(backend.name, reason) for backend, _trigger, reason in alerts] == [
        ("manual", "manual_or_custom_policy"),
        ("custom", "manual_or_custom_policy"),
        ("large", "maximum_tier_reached"),
    ]


def test_memory_event_counter_reset_uses_new_generation_value() -> None:
    assert auto_size._counter_delta(2, 9) == 2
    assert auto_size._counter_delta(0, 9) == 0


def test_memory_event_counter_without_baseline_only_seeds_sample() -> None:
    assert auto_size._counter_delta(4, None) == 0


def test_disk_usage_walk_stops_at_entry_limit(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(3):
        (root / f"file-{index}.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(auto_size, "AUTO_SIZE_DISK_WALK_ENTRY_LIMIT", 2)

    assert auto_size._path_disk_usage_bytes(root) is None


@pytest.mark.asyncio
async def test_auto_size_tick_records_one_minute_rollup_sample_without_evaluation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)

    _patch_container_stats(monkeypatch, cpu_percent=40.0, memory_percent=30.0)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                enabled=True,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
                cpu_quota_override="100%",
                memory_max_override="256MiB",
            )
        )
        await session.commit()
        settings = _settings(tmp_path)
        result = await auto_size.run_auto_size_tick(session, settings, now=now)

        samples = (
            (
                await session.execute(
                    select(BackendResourceSample).order_by(
                        BackendResourceSample.id.asc()
                    )
                )
            )
            .scalars()
            .all()
        )

    assert result["evaluated"] is False
    assert result["samples_written"] == 1
    assert len(samples) == 1
    assert samples[0].bucket_start == datetime(2026, 4, 1, 12, 30)
    assert samples[0].sampled_at == datetime(2026, 4, 1, 12, 30)
    assert samples[0].cpu_percent_of_entitlement == 40
    assert samples[0].cpu_entitlement_percent_of_host == 100.0
    assert samples[0].cpu_limit_percent_of_host == 100.0
    assert samples[0].memory_percent == 30
    assert samples[0].memory_current_bytes_count == 1
    assert samples[0].cpu_percent_of_entitlement_count == 1
    assert samples[0].cpu_percent_of_entitlement_sum == 40.0
    assert samples[0].cpu_percent_of_entitlement_min == 40.0
    assert samples[0].cpu_percent_of_entitlement_max == 40.0
    assert samples[0].cpu_percent_of_entitlement_first == 40.0
    assert samples[0].cpu_percent_of_entitlement_last == 40.0
    assert samples[0].memory_percent_count == 1
    assert samples[0].memory_percent_sum == 30.0


@pytest.mark.asyncio
async def test_auto_size_tick_handles_metric_bucket_commit_collision(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)

    _patch_container_stats(monkeypatch, cpu_percent=40.0, memory_percent=30.0)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                enabled=True,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
                cpu_quota_override="100%",
                memory_max_override="256MiB",
            )
        )
        await session.commit()

        async def fail_commit_once() -> None:
            raise IntegrityError(
                "INSERT INTO backend_resource_samples",
                {},
                Exception(
                    "UNIQUE constraint failed: backend_resource_samples.backend_id, bucket_start"
                ),
            )

        monkeypatch.setattr(session, "commit", fail_commit_once)
        result = await auto_size.run_auto_size_tick(
            session, _settings(tmp_path), now=now
        )

    assert result["evaluated"] is False
    assert result["sample_write_status"] == "collision"
    assert "UNIQUE constraint failed" in result["sample_write_error"]

    terminal = [
        record
        for record in caplog.records
        if getattr(record, "log_kind", None) == "result" and record.name == "auto.size"
    ]
    assert len(terminal) == 1
    assert terminal[0].levelname == "WARNING"
    assert terminal[0].context["status"] == "partial"


@pytest.mark.asyncio
async def test_auto_size_tick_continues_nightly_evaluation_after_metric_collision(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)
    evaluated: list[datetime] = []

    _patch_container_stats(monkeypatch, cpu_percent=40.0, memory_percent=30.0)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                enabled=True,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
                cpu_quota_override="100%",
                memory_max_override="256MiB",
            )
        )
        await session.commit()

        async def fake_evaluate(_session, sample_now: datetime):
            evaluated.append(sample_now)
            return []

        monkeypatch.setattr(auto_size, "_evaluate_auto_sizes", fake_evaluate)
        real_commit = session.commit
        commit_count = 0

        async def fail_first_commit():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 1:
                raise IntegrityError(
                    "INSERT INTO backend_resource_samples",
                    {},
                    Exception(
                        "UNIQUE constraint failed: backend_resource_samples.backend_id, bucket_start"
                    ),
                )
            await real_commit()

        monkeypatch.setattr(session, "commit", fail_first_commit)
        result = await auto_size.run_auto_size_tick(
            session,
            _settings(tmp_path).model_copy(update={"auto_size_nightly_hour_utc": 3}),
            now=now,
        )

    assert result["evaluated"] is True
    assert result["sample_write_status"] == "collision"
    assert evaluated == [now]


@pytest.mark.asyncio
async def test_auto_size_tick_records_network_rate_from_counter_delta(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    previous = datetime(2026, 4, 1, 12, 29, tzinfo=UTC)
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)

    _patch_container_stats(
        monkeypatch,
        cpu_percent=20.0,
        memory_percent=25.0,
        network_rx_bytes=1_720_000,
        network_tx_bytes=2_480_000,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=previous,
                network_rx_bytes=1_000_000,
                network_tx_bytes=2_000_000,
            )
        )
        await session.commit()

        await auto_size.run_auto_size_tick(session, _settings(tmp_path), now=now)

        samples = (
            (
                await session.execute(
                    select(BackendResourceSample).order_by(
                        BackendResourceSample.bucket_start.asc()
                    )
                )
            )
            .scalars()
            .all()
        )

    sample = samples[-1]
    assert sample.network_rx_bytes == 1_720_000
    assert sample.network_tx_bytes == 2_480_000
    assert sample.network_rx_bps == 12000
    assert sample.network_tx_bps == 8000
    assert sample.network_total_bps == 20000
    assert sample.network_rx_bps_count == 1
    assert sample.network_tx_bps_count == 1
    assert sample.network_total_bps_count == 1
    assert sample.network_total_bps_sum == 20000.0


@pytest.mark.asyncio
async def test_auto_size_tick_records_app_disk_usage(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    _patch_container_stats(monkeypatch, cpu_percent=20.0, memory_percent=25.0)
    monkeypatch.setattr(
        auto_size, "_backend_disk_usage_bytes", lambda *_args: 1_234_567
    )

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                enabled=True,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                env_json="{}",
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
                cpu_quota_override="100%",
                memory_max_override="256MiB",
            )
        )
        await session.commit()

        await auto_size.run_auto_size_tick(session, _settings(tmp_path), now=now)

        sample = (
            await session.execute(
                select(BackendResourceSample).order_by(BackendResourceSample.id.asc())
            )
        ).scalar_one()

    assert sample.disk_usage_bytes == 1_234_567
    assert sample.disk_usage_complete is True
    assert sample.disk_usage_skipped_paths == 0
    assert sample.disk_usage_bytes_count == 1
    assert sample.disk_usage_bytes_sum == 1_234_567.0
    assert sample.disk_usage_bytes_min == 1_234_567.0
    assert sample.disk_usage_bytes_max == 1_234_567.0
    assert sample.disk_usage_bytes_first == 1_234_567.0
    assert sample.disk_usage_bytes_last == 1_234_567.0


@pytest.mark.asyncio
async def test_auto_size_tick_skips_disk_value_between_hourly_crawls(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    previous = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)

    _patch_container_stats(monkeypatch, cpu_percent=20.0, memory_percent=25.0)

    def fail_disk_usage(*_args):
        raise AssertionError("disk usage should not be crawled between hourly buckets")

    monkeypatch.setattr(auto_size, "_backend_disk_usage_bytes", fail_disk_usage)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=previous,
                disk_usage_bytes=1_234_567,
            )
        )
        await session.commit()

        await auto_size.run_auto_size_tick(session, _settings(tmp_path), now=now)

        sample = (
            await session.execute(
                select(BackendResourceSample).where(
                    BackendResourceSample.bucket_start == now.replace(tzinfo=None)
                )
            )
        ).scalar_one()

    assert sample.disk_usage_bytes is None
    assert sample.disk_usage_bytes_count == 0


@pytest.mark.asyncio
async def test_auto_size_sample_writer_batches_backend_sample_queries(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
    previous = now - timedelta(minutes=1)

    async with maker() as session:
        backends = [
            Backend(
                name=f"web-{index}",
                kind="app",
                enabled=True,
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
            )
            for index in range(3)
        ]
        session.add_all(backends)
        await session.flush()
        for backend in backends:
            session.add(
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=previous,
                    network_rx_bytes=1_000,
                    network_tx_bytes=2_000,
                )
            )
        await session.commit()

        statements: list[str] = []

        def record_statement(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ):
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "backend_resource_samples" in statement
            ):
                statements.append(statement)

        event.listen(
            session.bind.sync_engine, "before_cursor_execute", record_statement
        )
        try:
            await auto_size._write_metric_samples(
                session,
                backends,
                {
                    backend.name: {
                        "cpu_percent_of_host": 10.0,
                        "cpu_percent": 20.0,
                        "memory_percent": 30.0,
                        "network_rx_bytes": 1_600,
                        "network_tx_bytes": 2_600,
                    }
                    for backend in backends
                },
                now,
            )
        finally:
            event.remove(
                session.bind.sync_engine, "before_cursor_execute", record_statement
            )

    assert len(statements) == 2


@pytest.mark.asyncio
async def test_auto_size_evaluation_batches_checkpoint_sample_query(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)
    bucket = now.replace(minute=0, second=0, microsecond=0)

    async with maker() as session:
        backends = [
            Backend(
                name=f"web-{index}",
                kind="app",
                enabled=True,
                resource_mode="auto",
                resource_size="small",
                volumes_json="[]",
                created_at=now - timedelta(days=10),
            )
            for index in range(3)
        ]
        session.add_all(backends)
        await session.flush()
        for backend in backends:
            for index in range(auto_size.AUTO_SIZE_MIN_SAMPLES):
                session.add(
                    BackendResourceSample(
                        backend_id=backend.id,
                        bucket_start=bucket - timedelta(hours=index + 1),
                        cpu_percent_of_host=15,
                        cpu_percent_of_entitlement=20,
                        memory_percent=25,
                    )
                )
        await session.commit()

        statements: list[str] = []

        def record_statement(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ):
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "backend_resource_samples" in statement
            ):
                statements.append(statement)

        event.listen(
            session.bind.sync_engine, "before_cursor_execute", record_statement
        )
        try:
            decisions = await auto_size._evaluate_auto_sizes(session, now)
        finally:
            event.remove(
                session.bind.sync_engine, "before_cursor_execute", record_statement
            )

    assert len(decisions) == 3
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_auto_size_tick_rolls_up_repeated_samples_in_the_same_minute(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, 10, tzinfo=UTC)

    async with maker() as session:
        session.add(
            Backend(
                name="web",
                kind="app",
                enabled=True,
                base_image="docker.io/library/ubuntu:24.04",
                internal_port=8337,
                workdir="/srv/web",
                install_command="apt-get update",
                start_command="python3 -m http.server 8337",
                env_json="{}",
                volumes_json="[]",
                resource_mode="auto",
                resource_size="small",
                cpu_quota_override="100%",
                memory_max_override="256MiB",
            )
        )
        await session.commit()
        settings = _settings(tmp_path)

        _patch_container_stats(monkeypatch, cpu_percent=40.0, memory_percent=30.0)
        await auto_size.run_auto_size_tick(session, settings, now=now)
        _patch_container_stats(monkeypatch, cpu_percent=75.0, memory_percent=55.0)
        await auto_size.run_auto_size_tick(
            session, settings, now=now.replace(second=45)
        )

        sample = (
            await session.execute(
                select(BackendResourceSample).order_by(BackendResourceSample.id.asc())
            )
        ).scalar_one()

    assert sample.bucket_start == datetime(2026, 4, 1, 12, 30)
    assert sample.cpu_percent_of_entitlement_count == 2
    assert sample.cpu_percent_of_entitlement_sum == 115.0
    assert sample.cpu_percent_of_entitlement_min == 40.0
    assert sample.cpu_percent_of_entitlement_max == 75.0
    assert sample.cpu_percent_of_entitlement_first == 40.0
    assert sample.cpu_percent_of_entitlement_last == 75.0
    assert sample.cpu_percent_of_entitlement == 75
    assert sample.cpu_entitlement_percent_of_host == 100.0
    assert sample.memory_percent_count == 2
    assert sample.memory_percent_min == 30.0
    assert sample.memory_percent_max == 55.0
    assert sample.memory_percent == 55


@pytest.mark.asyncio
async def test_auto_size_tick_promotes_hot_backend_and_runs_apply(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)
    apply_calls: list[dict[str, object]] = []
    apply_entity_sizes: list[str | None] = []
    apply_database_sizes: list[str | None] = []
    completed_apply_runs: list[int | None] = []
    apply_completed_events: list[int | None] = []
    notifications: list[dict[str, object]] = []

    _patch_container_stats(monkeypatch, cpu_percent=92.0, memory_percent=88.0)

    async def fake_run_apply(
        session,
        settings,
        *,
        operation_kind="apply_host",
        actor="system",
        commit_on_success=True,
        emit_success_event=True,
    ):
        backend_entity = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()
        apply_entity_sizes.append(backend_entity.resource_size)
        apply_database_sizes.append(
            (
                await session.execute(
                    select(Backend.resource_size).where(Backend.id == backend_id)
                )
            ).scalar_one()
        )
        apply_calls.append(
            {
                "operation_kind": operation_kind,
                "actor": actor,
                "commit_on_success": commit_on_success,
                "emit_success_event": emit_success_event,
                "autoflush": session.autoflush,
            }
        )
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={
                "deferred_operation_id": 41,
                "deferred_operation_kind": "auto_size",
            },
            run_id=9,
        )

    async def fake_complete_deferred_apply_operation(_settings, apply_response):
        completed_apply_runs.append(apply_response.run_id)

    async def fake_emit_deferred_apply_completed_event(_settings, apply_response):
        apply_completed_events.append(apply_response.run_id)

    async def fake_send_notification(_settings, **kwargs):
        notifications.append(kwargs)
        return True

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        auto_size,
        "_complete_deferred_apply_operation",
        fake_complete_deferred_apply_operation,
    )
    monkeypatch.setattr(
        auto_size,
        "_emit_deferred_apply_completed_event",
        fake_emit_deferred_apply_completed_event,
    )
    monkeypatch.setattr(
        control_events, "send_pushover_notification_async", fake_send_notification
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
            created_at=now - timedelta(days=10),
            resource_size_updated_at=now - timedelta(days=10),
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id
        bucket = now.replace(minute=0, second=0, microsecond=0)
        for index in range(auto_size.AUTO_SIZE_MIN_SAMPLES):
            session.add(
                BackendResourceSample(
                    backend_id=backend_id,
                    bucket_start=bucket - timedelta(hours=index + 1),
                    cpu_percent_of_host=46,
                    cpu_percent_of_entitlement=92,
                    memory_percent=88,
                    memory_current_bytes=200 * 1024 * 1024,
                    memory_max_bytes=256 * 1024 * 1024,
                )
            )
        await session.commit()

        settings = _settings(tmp_path)
        result = await auto_size.run_auto_size_tick(session, settings, now=now)
        refreshed = await session.get(Backend, backend_id)

    assert result["evaluated"] is True
    assert result["changed_backends"] == ["web"]
    assert result["apply"]["status"] == "success"
    assert result["notification"] == {
        "sent": True,
        "event": "auto_size_resized",
        "reason": "policy_match",
    }
    assert apply_calls == [
        {
            "operation_kind": "auto_size",
            "actor": "auto_size",
            "commit_on_success": False,
            "emit_success_event": False,
            "autoflush": False,
        }
    ]
    assert apply_entity_sizes == ["medium"]
    assert apply_database_sizes == ["small"]
    assert completed_apply_runs == [9]
    assert apply_completed_events == [9]
    assert notifications
    assert notifications[0]["title"] == "CNC auto-size resized outputs"
    assert notifications[0]["event"] == "auto_size_resized"
    message = str(notifications[0]["message"])
    assert "title: Auto-size resized outputs" in message
    assert "status: success" in message
    assert "values:" in message
    assert "web: small->medium" in message
    assert "cpu p95 92%" in message
    assert "memory p95 88%" in message
    assert 'details: {"affects_all":false' in message
    assert refreshed is not None
    assert refreshed.resource_size == "medium"


@pytest.mark.asyncio
async def test_auto_size_tick_rolls_back_size_change_when_apply_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)

    _patch_container_stats(monkeypatch, cpu_percent=92.0, memory_percent=88.0)

    async def fake_run_apply(session, settings):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={"phase": "nginx_validate"},
            run_id=9,
        )

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
            created_at=now - timedelta(days=10),
            resource_size_updated_at=now - timedelta(days=10),
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id
        bucket = now.replace(minute=0, second=0, microsecond=0)
        for index in range(auto_size.AUTO_SIZE_MIN_SAMPLES):
            session.add(
                BackendResourceSample(
                    backend_id=backend_id,
                    bucket_start=bucket - timedelta(hours=index + 1),
                    cpu_percent_of_host=46,
                    cpu_percent_of_entitlement=92,
                    memory_percent=88,
                    memory_current_bytes=200 * 1024 * 1024,
                    memory_max_bytes=256 * 1024 * 1024,
                )
            )
        await session.commit()

        settings = _settings(tmp_path)
        result = await auto_size.run_auto_size_tick(session, settings, now=now)
        refreshed = await session.get(Backend, backend_id)

    assert result["apply"]["status"] == "error"
    assert refreshed is not None
    assert refreshed.resource_size == "small"


@pytest.mark.asyncio
async def test_auto_size_tick_records_partial_failure_when_commit_after_apply_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 0, tzinfo=UTC)
    commit_failures: list[str] = []

    _patch_container_stats(monkeypatch, cpu_percent=92.0, memory_percent=88.0)

    async def fake_run_apply(*_args, **_kwargs):
        return ApplyResponse(
            status="success",
            message="apply completed",
            details={
                "deferred_operation_id": 51,
                "deferred_operation_kind": "auto_size",
            },
            run_id=11,
        )

    async def fake_record_commit_failure(_settings, apply_response, exc):
        commit_failures.append(f"{apply_response.run_id}:{exc}")

    async def fake_complete_deferred_apply_operation(*_args, **_kwargs):
        raise AssertionError(
            "deferred operation should not complete after commit failure"
        )

    async def fake_emit_deferred_apply_completed_event(*_args, **_kwargs):
        raise AssertionError(
            "apply completion event should not emit after commit failure"
        )

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        auto_size, "_record_auto_size_commit_failure", fake_record_commit_failure
    )
    monkeypatch.setattr(
        auto_size,
        "_complete_deferred_apply_operation",
        fake_complete_deferred_apply_operation,
    )
    monkeypatch.setattr(
        auto_size,
        "_emit_deferred_apply_completed_event",
        fake_emit_deferred_apply_completed_event,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
            created_at=now - timedelta(days=10),
            resource_size_updated_at=now - timedelta(days=10),
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id
        bucket = now.replace(minute=0, second=0, microsecond=0)
        for index in range(auto_size.AUTO_SIZE_MIN_SAMPLES):
            session.add(
                BackendResourceSample(
                    backend_id=backend_id,
                    bucket_start=bucket - timedelta(hours=index + 1),
                    cpu_percent_of_host=46,
                    cpu_percent_of_entitlement=92,
                    memory_percent=88,
                    memory_current_bytes=200 * 1024 * 1024,
                    memory_max_bytes=256 * 1024 * 1024,
                )
            )
        await session.commit()

        previous_run = ApplyRun(
            status="success",
            message="previous apply",
            desired_state_hash="previous",
            details_json="{}",
        )
        session.add(previous_run)
        await session.flush()
        session.add(
            HostApplyState(
                id=1,
                last_applied_state_hash="previous",
                last_successful_apply_run_id=previous_run.id,
            )
        )
        await session.commit()

        real_commit = session.commit
        commit_count = 0

        async def flaky_commit():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 2:
                raise RuntimeError("sqlite commit failed")
            await real_commit()

        monkeypatch.setattr(session, "commit", flaky_commit)

        result = await auto_size.run_auto_size_tick(
            session, _settings(tmp_path), now=now
        )
        refreshed = await session.get(Backend, backend_id)
        apply_state = await session.get(HostApplyState, 1)

    assert result["apply"]["status"] == "error"
    assert result["apply"]["message"] == "apply completed but auto-size commit failed"
    assert result["apply"]["run_id"] is None
    assert result["apply"]["uncommitted_run_id"] == 11
    assert result["apply"]["phase"] == "database_commit"
    assert result["apply"]["convergence_invalidated"] is True
    assert apply_state is not None
    assert apply_state.last_successful_apply_run_id is None
    assert commit_failures == ["11:sqlite commit failed"]
    assert refreshed is not None
    assert refreshed.resource_size == "small"


@pytest.mark.asyncio
async def test_auto_size_tick_only_evaluates_on_hour_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 2, 3, 5, tzinfo=UTC)
    apply_calls: list[str] = []

    _patch_container_stats(monkeypatch, cpu_percent=92.0, memory_percent=88.0)

    async def fake_run_apply(session, settings):
        apply_calls.append("apply")
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=9
        )

    monkeypatch.setattr(auto_size, "run_apply", fake_run_apply)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            enabled=True,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            resource_mode="auto",
            resource_size="small",
            cpu_quota_override="100%",
            memory_max_override="256MiB",
            created_at=now - timedelta(days=10),
            resource_size_updated_at=now - timedelta(days=10),
        )
        session.add(backend)
        await session.commit()

        settings = _settings(tmp_path)
        result = await auto_size.run_auto_size_tick(session, settings, now=now)
        refreshed = await session.get(Backend, backend.id)

    assert result["evaluated"] is False
    assert result["samples_written"] == 1
    assert apply_calls == []
    assert refreshed is not None
    assert refreshed.resource_size == "small"
