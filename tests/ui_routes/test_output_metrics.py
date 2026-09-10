from .support import (
    Backend,
    BackendResourceSample,
    ControlEvent,
    HostResourceSample,
    Path,
    Settings,
    _make_session,
    build_backend_metric_history,
    build_host_metric_history,
    datetime,
    json,
    metric_history,
    timedelta,
    timezone,
    ui_reads,
)


async def test_output_metric_history_skips_resource_profile_for_non_memory_metric(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        backend_backup_dir=tmp_path / "backend-backups",
    )

    async def fail_enabled_runtime_backends(*_args, **_kwargs):
        raise AssertionError("non-memory metrics should not load the app resource mix")

    monkeypatch.setattr(ui_reads, "peek_cached_status", lambda: {"services": []})
    monkeypatch.setattr(
        ui_reads,
        "enabled_runtime_backends_for_resource_profile",
        fail_enabled_runtime_backends,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="cpu",
            timeframe="day",
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["metric"]["key"] == "cpu"
    assert "soft_limit_value" not in payload


async def test_output_metric_history_returns_adaptive_series(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(
        ui_reads,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "metrics": {
                        "cpu_percent": 42.0,
                        "cpu_percent_of_host": 10.5,
                        "cpu_entitlement_percent_of_host": 25.0,
                        "memory_percent": 58.0,
                        "memory_current_bytes": 268_435_456,
                    },
                }
            ]
        },
    )

    now = datetime.now(timezone.utc)
    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            memory_high_override="512M",
            memory_max_override="1G",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(hours=3),
                    cpu_percent_of_host=4,
                    cpu_percent_of_entitlement=18,
                    cpu_entitlement_percent_of_host=25.0,
                    memory_percent=31,
                    memory_current_bytes=134_217_728,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(hours=2),
                    cpu_percent_of_host=7,
                    cpu_percent_of_entitlement=28,
                    cpu_entitlement_percent_of_host=25.0,
                    memory_percent=36,
                    memory_current_bytes=150_994_944,
                ),
            ]
        )
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="cpu",
            timeframe="day",
            width=10,
            settings=settings,
            session=session,
        )
        memory_payload = await ui_reads.output_metric_history(
            backend.id,
            metric="memory",
            timeframe="day",
            width=10,
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["metric"]["key"] == "cpu"
    assert payload["timeframe"]["key"] == "day"
    assert payload["range_start_at"]
    assert payload["range_end_at"]
    assert payload["summary"]["latest_value"] == 10.5
    assert payload["summary"]["peak_value"] == 10.5
    assert payload["summary"]["point_count"] >= 3
    assert payload["series"][-1]["is_live"] is True
    assert "smoothed" not in payload["note"]
    assert payload["limit_value"] == 100.0
    assert payload["y_axis_min"] == 0.0
    assert payload["y_axis_max"] == 50.0
    assert "soft_limit_value" not in payload
    assert memory_payload["available"] is True
    assert memory_payload["metric"]["key"] == "memory"
    assert memory_payload["y_axis_min"] == 0.0
    assert memory_payload["y_axis_max"] == 100.0
    assert memory_payload["soft_limit_value"] == 50.0
    assert memory_payload["soft_limit_bytes"] == 536_870_912
    assert memory_payload["memory_limit_bytes"] == 1_073_741_824
    assert memory_payload["soft_limit_label"] == "SOFT LIMIT (512 MB)"


def test_metric_axis_bounds_keep_low_cpu_readable() -> None:
    assert (
        metric_history._nice_axis_max(0.6, unit_kind="percent", metric_key="cpu") == 1.0
    )
    assert (
        metric_history._nice_axis_max(1.1, unit_kind="percent", metric_key="cpu") == 2.0
    )
    assert (
        metric_history._nice_axis_max(7.0, unit_kind="percent", metric_key="cpu")
        == 10.0
    )
    assert (
        metric_history._nice_axis_max(31.0, unit_kind="percent", metric_key="cpu")
        == 50.0
    )
    assert (
        metric_history._nice_axis_max(0.6, unit_kind="percent", metric_key="memory")
        == 100.0
    )
    assert (
        metric_history._nice_axis_max(640.0, unit_kind="rate", metric_key="network")
        == 1000.0
    )


async def test_output_metric_history_scales_flat_low_cpu_to_readable_axis(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(
        ui_reads,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "metrics": {"cpu_percent": 0.6, "cpu_percent_of_host": 0.6},
                }
            ]
        },
    )
    now = datetime.now(timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=2),
                    cpu_percent_of_host=0.6,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=1),
                    cpu_percent_of_host=0.6,
                ),
            ]
        )
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="cpu",
            timeframe="day",
            width=800,
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["summary"]["peak_value"] == 0.6
    assert payload["y_axis_min"] == 0.0
    assert payload["y_axis_max"] == 1.0


async def test_cpu_metric_history_axis_uses_bucket_peak_above_average(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    sample_at = now - timedelta(minutes=5)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=sample_at,
                cpu_percent_of_host_count=10,
                cpu_percent_of_host_sum=6.0,
                cpu_percent_of_host_min=0.4,
                cpu_percent_of_host_max=1.9,
                cpu_percent_of_host_first=0.5,
                cpu_percent_of_host_last=0.7,
            )
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert payload["summary"]["latest_value"] == 0.7
    assert payload["summary"]["peak_value"] == 1.9
    assert payload["y_axis_min"] == 0.0
    assert payload["y_axis_max"] == 5.0


async def test_host_cpu_metric_history_axis_uses_bucket_peak_above_average(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    sample_at = now - timedelta(minutes=5)

    async with maker() as session:
        session.add(
            HostResourceSample(
                bucket_start=sample_at,
                cpu_percent_count=10,
                cpu_percent_sum=6.0,
                cpu_percent_min=0.4,
                cpu_percent_max=1.9,
                cpu_percent_first=0.5,
                cpu_percent_last=0.7,
            )
        )
        await session.commit()

        payload = await build_host_metric_history(
            session,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert payload["summary"]["latest_value"] == 0.7
    assert payload["summary"]["peak_value"] == 1.9
    assert payload["y_axis_min"] == 0.0
    assert payload["y_axis_max"] == 5.0


async def test_output_metric_history_reports_related_resize_chart_events(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
    resized_at = now - timedelta(minutes=12)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        other_backend = Backend(
            name="fern",
            kind="app",
            port=12001,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([backend, other_backend])
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=now - timedelta(minutes=20),
                cpu_percent_of_host=6,
                cpu_percent_of_entitlement=24,
            )
        )
        session.add_all(
            [
                ControlEvent(
                    kind="auto_size_resized",
                    source="auto_size",
                    summary="Auto-size resized outputs",
                    severity="success",
                    scope="host",
                    related_backends_json=json.dumps(["web", "fern"]),
                    subevents_json=json.dumps(
                        [
                            {
                                "label": "changes",
                                "value": "web: small->medium, fern: small->medium",
                            }
                        ]
                    ),
                    details_json=json.dumps(
                        {
                            "changes": [
                                {
                                    "backend": "web",
                                    "previous_size": "small",
                                    "next_size": "medium",
                                },
                                {
                                    "backend": "fern",
                                    "previous_size": "small",
                                    "next_size": "medium",
                                },
                            ]
                        }
                    ),
                    created_at=resized_at,
                ),
                ControlEvent(
                    kind="auto_size_resized",
                    source="auto_size",
                    summary="Auto-size resized outputs",
                    severity="success",
                    scope="host",
                    related_backends_json=json.dumps(["fern"]),
                    subevents_json=json.dumps(
                        [{"label": "changes", "value": "fern: small->medium"}]
                    ),
                    created_at=now - timedelta(minutes=10),
                ),
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert len(payload["chart_events"]) == 1
    assert payload["chart_events"][0]["timestamp"] == resized_at.isoformat()
    assert payload["chart_events"][0]["label"] == "web"
    assert payload["chart_events"][0]["backend"] == "web"
    assert payload["chart_events"][0]["before"] == "small"
    assert payload["chart_events"][0]["after"] == "medium"
    assert payload["chart_events"][0]["rows"] == [
        {"label": "before", "value": "small"},
        {"label": "after", "value": "medium"},
    ]


async def test_output_metric_history_accepts_shield_live_metrics(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(
        ui_reads,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-shield.service",
                    "metrics": {
                        "cpu_percent": 2.5,
                        "cpu_percent_of_host": 2.5,
                        "memory_percent": 12.5,
                        "memory_current_bytes": 33_554_432,
                        "memory_max_bytes": 268_435_456,
                    },
                }
            ]
        },
    )

    async with maker() as session:
        backend = Backend(name="shield", kind="shield", enabled=True, volumes_json="[]")
        session.add(backend)
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="cpu",
            timeframe="hour",
            width=640,
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["series"][-1]["value"] == 2.5
    assert payload["series"][-1]["is_live"] is True


async def test_output_metric_history_uses_sample_rollup_fields(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_reads, "peek_cached_status", lambda: {"services": []})

    now = datetime.now(timezone.utc)
    sample_at = now - timedelta(minutes=5)
    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=sample_at,
                cpu_percent_of_host=15,
                cpu_percent_of_host_count=2,
                cpu_percent_of_host_sum=23.0,
                cpu_percent_of_host_min=8.0,
                cpu_percent_of_host_max=15.0,
                cpu_percent_of_host_first=8.0,
                cpu_percent_of_host_last=15.0,
                cpu_entitlement_percent_of_host=20.0,
                cpu_percent_of_entitlement_count=2,
                cpu_percent_of_entitlement_sum=115.0,
                cpu_percent_of_entitlement_min=40.0,
                cpu_percent_of_entitlement_max=75.0,
                cpu_percent_of_entitlement_first=40.0,
                cpu_percent_of_entitlement_last=75.0,
            )
        )
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="cpu",
            timeframe="day",
            width=10,
            settings=settings,
            session=session,
        )

    assert payload["series"] == [
        {
            "timestamp": sample_at.isoformat(),
            "value": 11.5,
            "avg": 11.5,
            "visual_value": 11.5,
            "min": 8.0,
            "max": 15.0,
            "visual_min": 8.0,
            "visual_max": 15.0,
            "last": 15.0,
            "count": 2,
            "is_live": False,
            "cpu_pressure": 57.5,
            "cpu_entitlement_percent_of_host": 20.0,
            "cpu_limit_percent_of_host": 20.0,
        }
    ]
    assert payload["summary"]["latest_value"] == 15.0
    assert payload["summary"]["peak_value"] == 15.0
    assert payload["summary"]["average_value"] == 11.5
    assert payload["target_points"] == 24
    assert payload["y_axis_max"] >= payload["series"][0]["max"]


def test_metric_history_rollup_helpers_accept_scalar_row_mappings() -> None:
    sample_at = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    backend_row = {
        "bucket_start": sample_at,
        "cpu_percent_of_host": 9,
        "cpu_percent_of_host_count": 2,
        "cpu_percent_of_host_sum": 18.0,
        "cpu_percent_of_host_min": 7.0,
        "cpu_percent_of_host_max": 11.0,
        "cpu_percent_of_host_first": 7.0,
        "cpu_percent_of_host_last": 11.0,
        "cpu_percent_of_entitlement": 36,
        "cpu_entitlement_percent_of_host": 25.0,
        "cpu_percent_of_entitlement_count": 2,
        "cpu_percent_of_entitlement_sum": 72.0,
        "cpu_percent_of_entitlement_min": 28.0,
        "cpu_percent_of_entitlement_max": 44.0,
        "cpu_percent_of_entitlement_first": 28.0,
        "cpu_percent_of_entitlement_last": 44.0,
        "memory_percent": 50,
        "memory_percent_count": 1,
        "memory_percent_sum": 50.0,
        "memory_percent_min": 50.0,
        "memory_percent_max": 50.0,
        "memory_percent_first": 50.0,
        "memory_percent_last": 50.0,
        "disk_usage_bytes": 2048,
        "disk_usage_bytes_count": 1,
        "disk_usage_bytes_sum": 2048.0,
        "disk_usage_bytes_min": 2048.0,
        "disk_usage_bytes_max": 2048.0,
        "disk_usage_bytes_first": 2048.0,
        "disk_usage_bytes_last": 2048.0,
        "network_rx_bytes": 4000,
        "network_tx_bytes": 3000,
        "network_total_bps": 1200,
        "network_total_bps_count": 1,
        "network_total_bps_sum": 1200.0,
        "network_total_bps_min": 1200.0,
        "network_total_bps_max": 1200.0,
        "network_total_bps_first": 1200.0,
        "network_total_bps_last": 1200.0,
    }
    backend_object = BackendResourceSample(**backend_row)

    for key in ("cpu", "memory", "disk", "network"):
        spec = metric_history._metric_spec(key)
        assert metric_history._sample_rollup(
            backend_row, spec
        ) == metric_history._sample_rollup(backend_object, spec)

    assert metric_history._sample_cpu_pressure_rollup(
        backend_row
    ) == metric_history._sample_cpu_pressure_rollup(backend_object)

    previous_row = {
        "bucket_start": sample_at - timedelta(minutes=1),
        "network_rx_bytes": 1000,
        "network_tx_bytes": 600,
    }
    previous_object = BackendResourceSample(**previous_row)
    assert metric_history._network_direction_rates(
        previous_row, backend_row
    ) == metric_history._network_direction_rates(
        previous_object,
        backend_object,
    )

    host_row = {
        "bucket_start": sample_at,
        "cpu_percent": 13,
        "cpu_percent_count": 2,
        "cpu_percent_sum": 26.0,
        "cpu_percent_min": 10.0,
        "cpu_percent_max": 16.0,
        "cpu_percent_first": 10.0,
        "cpu_percent_last": 16.0,
        "memory_percent": 44,
        "memory_percent_count": 1,
        "memory_percent_sum": 44.0,
        "memory_percent_min": 44.0,
        "memory_percent_max": 44.0,
        "memory_percent_first": 44.0,
        "memory_percent_last": 44.0,
        "disk_percent": 68,
        "disk_percent_count": 1,
        "disk_percent_sum": 68.0,
        "disk_percent_min": 68.0,
        "disk_percent_max": 68.0,
        "disk_percent_first": 68.0,
        "disk_percent_last": 68.0,
        "network_total_bps": 900,
        "network_total_bps_count": 1,
        "network_total_bps_sum": 900.0,
        "network_total_bps_min": 900.0,
        "network_total_bps_max": 900.0,
        "network_total_bps_first": 900.0,
        "network_total_bps_last": 900.0,
    }
    host_object = HostResourceSample(**host_row)
    for key in ("cpu", "memory", "disk", "network"):
        spec = metric_history._host_metric_spec(key)
        assert metric_history._host_sample_rollup(
            host_row, spec
        ) == metric_history._host_sample_rollup(
            host_object,
            spec,
        )


async def test_output_cpu_metric_history_reports_configured_limit_series(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=50),
                    cpu_percent_of_host=10,
                    cpu_percent_of_entitlement=40,
                    cpu_entitlement_percent_of_host=25.0,
                    cpu_limit_percent_of_host=12.5,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=30),
                    cpu_percent_of_host=18,
                    cpu_percent_of_entitlement=40,
                    cpu_entitlement_percent_of_host=45.0,
                    cpu_limit_percent_of_host=22.5,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=10),
                    cpu_percent_of_host=20,
                    cpu_percent_of_entitlement=50,
                    cpu_entitlement_percent_of_host=40.0,
                    cpu_limit_percent_of_host=20.0,
                ),
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert [point["value"] for point in payload["series"]] == [10.0, 18.0, 20.0]
    assert [
        point["cpu_entitlement_percent_of_host"] for point in payload["series"]
    ] == [25.0, 45.0, 40.0]
    assert payload["cpu_limit_series"] == [
        {"timestamp": (now - timedelta(minutes=50)).isoformat(), "value": 12.5},
        {"timestamp": (now - timedelta(minutes=30)).isoformat(), "value": 12.5},
        {"timestamp": (now - timedelta(minutes=30)).isoformat(), "value": 22.5},
        {"timestamp": (now - timedelta(minutes=10)).isoformat(), "value": 22.5},
        {"timestamp": (now - timedelta(minutes=10)).isoformat(), "value": 20.0},
        {"timestamp": now.isoformat(), "value": 20.0},
    ]


async def test_output_cpu_metric_history_reports_flat_configured_limit_series(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=50),
                    cpu_percent_of_host=10,
                    cpu_percent_of_entitlement=40,
                    cpu_entitlement_percent_of_host=25.0,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=30),
                    cpu_percent_of_host=18,
                    cpu_percent_of_entitlement=72,
                    cpu_entitlement_percent_of_host=25.0,
                ),
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert payload["cpu_limit_series"] == [
        {"timestamp": (now - timedelta(minutes=50)).isoformat(), "value": 25.0},
        {"timestamp": now.isoformat(), "value": 25.0},
    ]


async def test_output_cpu_metric_history_preserves_raw_limit_changes_when_downsampled(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        for index in range(60):
            sample_at = now - timedelta(minutes=60 - index)
            cpu_limit = 12.5 if index < 20 else 22.5 if index < 40 else 15.0
            session.add(
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=sample_at,
                    cpu_percent_of_host=10,
                    cpu_percent_of_entitlement=40,
                    cpu_limit_percent_of_host=cpu_limit,
                )
            )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=1,
            live_metrics=None,
            now=now,
        )

    assert len(payload["series"]) < 60
    assert {
        "timestamp": (now - timedelta(minutes=40)).isoformat(),
        "value": 22.5,
    } in payload["cpu_limit_series"]
    assert {
        "timestamp": (now - timedelta(minutes=20)).isoformat(),
        "value": 15.0,
    } in payload["cpu_limit_series"]


async def test_output_cpu_metric_history_does_not_infer_limit_from_pressure(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=50),
                    cpu_percent_of_host=10,
                    cpu_percent_of_entitlement=40,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=30),
                    cpu_percent_of_host=18,
                    cpu_percent_of_entitlement=40,
                ),
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert "cpu_entitlement_percent_of_host" not in payload["series"][0]
    assert payload["cpu_limit_series"] == []


async def test_metric_history_width_bucket_preserves_spike_extrema(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(hours=24)
    values = [45] * 60
    values[17] = 98
    values[42] = 12

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=start + timedelta(minutes=index),
                    memory_percent=value,
                )
                for index, value in enumerate(values)
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="memory",
            timeframe_key="day",
            chart_width_px=10,
            live_metrics=None,
            now=now,
        )

    assert payload["target_points"] == 24
    assert payload["range_start_at"] == (now - timedelta(hours=24)).isoformat()
    assert payload["range_end_at"] == now.isoformat()
    assert payload["summary"]["peak_value"] == 98.0
    bucket = payload["series"][0]
    assert bucket["count"] == 60
    assert bucket["avg"] == 45.33
    assert bucket["visual_value"] == 45.33
    assert bucket["min"] == 12.0
    assert bucket["max"] == 98.0
    assert bucket["visual_min"] == 12.0
    assert bucket["visual_max"] == 98.0


async def test_memory_metric_history_switches_to_bytes_only_when_limit_changes(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=50),
                    memory_percent=50,
                    memory_current_bytes=512 * 1024 * 1024,
                    memory_max_bytes=1024 * 1024 * 1024,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=30),
                    memory_percent=55,
                    memory_current_bytes=1126 * 1024 * 1024,
                    memory_max_bytes=2048 * 1024 * 1024,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=10),
                    memory_percent=60,
                    memory_current_bytes=922 * 1024 * 1024,
                    memory_max_bytes=1536 * 1024 * 1024,
                ),
            ]
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="memory",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert payload["metric"]["unit_kind"] == "bytes"
    assert [point["value"] for point in payload["series"]] == [
        float(512 * 1024 * 1024),
        float(1126 * 1024 * 1024),
        float(922 * 1024 * 1024),
    ]
    assert [point["memory_percent"] for point in payload["series"]] == [
        50.0,
        55.0,
        60.0,
    ]
    assert payload["memory_limit_series"] == [
        {
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "value": 1024 * 1024 * 1024,
        },
        {
            "timestamp": (now - timedelta(minutes=30)).isoformat(),
            "value": 1024 * 1024 * 1024,
        },
        {
            "timestamp": (now - timedelta(minutes=30)).isoformat(),
            "value": 2048 * 1024 * 1024,
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "value": 2048 * 1024 * 1024,
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "value": 1536 * 1024 * 1024,
        },
        {"timestamp": now.isoformat(), "value": 1536 * 1024 * 1024},
    ]
    assert payload["limit_value"] is None


async def test_metric_history_collapses_current_bucket_visual_extrema(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=24)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        older_samples = [
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=cutoff + timedelta(hours=index),
                memory_percent=40,
            )
            for index in range(23)
        ]
        current_samples = [
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=now - timedelta(minutes=40),
                memory_percent=90,
            ),
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=now - timedelta(minutes=20),
                memory_percent=10,
            ),
        ]
        session.add_all([*older_samples, *current_samples])
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="memory",
            timeframe_key="day",
            chart_width_px=10,
            live_metrics={"memory_percent": 50},
            now=now,
        )

    latest = payload["series"][-1]
    assert latest["is_live"] is True
    assert latest["min"] == 10.0
    assert latest["max"] == 90.0
    assert latest["visual_value"] == payload["series"][-2]["visual_value"]
    assert latest["visual_min"] == latest["visual_value"]
    assert latest["visual_max"] == latest["visual_value"]
    assert payload["summary"]["peak_value"] == 90.0


async def test_metric_history_live_point_does_not_drive_visual_axis(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=now - timedelta(minutes=1),
                cpu_percent_of_host=4.0,
            )
        )
        await session.commit()

        payload = await build_backend_metric_history(
            session,
            backend,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics={"cpu_percent_of_host": 40.0},
            now=now,
        )

    latest = payload["series"][-1]
    assert latest["is_live"] is True
    assert latest["value"] == 40.0
    assert latest["visual_value"] == 4.0
    assert latest["visual_min"] == 4.0
    assert latest["visual_max"] == 4.0
    assert payload["summary"]["latest_value"] == 40.0
    assert payload["summary"]["peak_value"] == 40.0
    assert payload["y_axis_max"] == 5.0


async def test_host_metric_history_reports_persisted_series(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
    start = now - timedelta(hours=2)

    async with maker() as session:
        session.add_all(
            [
                HostResourceSample(
                    bucket_start=start + timedelta(minutes=index),
                    cpu_percent=value,
                    cpu_percent_count=1,
                    cpu_percent_sum=float(value),
                    cpu_percent_min=float(value),
                    cpu_percent_max=float(value),
                    cpu_percent_first=float(value),
                    cpu_percent_last=float(value),
                )
                for index, value in enumerate([11, 18, 44])
            ]
        )
        await session.commit()

        payload = await build_host_metric_history(
            session,
            metric_key="cpu",
            timeframe_key="day",
            chart_width_px=800,
            live_metrics={"cpu_percent": 22.0},
            now=now,
        )

    assert payload["available"] is True
    assert payload["metric"]["key"] == "cpu"
    assert payload["series"][-1]["is_live"] is True
    assert payload["summary"]["peak_value"] == 44.0
    assert payload["summary"]["latest_value"] == 22.0


async def test_host_metric_history_reports_resize_chart_events(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
    resized_at = now - timedelta(minutes=25)

    async with maker() as session:
        session.add(
            HostResourceSample(
                bucket_start=now - timedelta(minutes=30),
                cpu_percent=18,
                cpu_percent_count=1,
                cpu_percent_sum=18.0,
                cpu_percent_min=18.0,
                cpu_percent_max=18.0,
                cpu_percent_first=18.0,
                cpu_percent_last=18.0,
            )
        )
        session.add_all(
            [
                ControlEvent(
                    kind="auto_size_resized",
                    source="auto_size",
                    summary="Auto-size resized outputs",
                    severity="success",
                    scope="host",
                    related_backends_json=json.dumps(["web"]),
                    subevents_json=json.dumps(
                        [
                            {"label": "outputs", "value": "1"},
                            {"label": "changes", "value": "web: small->medium"},
                        ]
                    ),
                    details_json=json.dumps(
                        {
                            "changes": [
                                {
                                    "backend": "web",
                                    "previous_size": "small",
                                    "next_size": "medium",
                                }
                            ]
                        }
                    ),
                    created_at=resized_at,
                ),
                ControlEvent(
                    kind="apply_completed",
                    source="apply",
                    summary="Changes applied",
                    severity="success",
                    scope="host",
                    affects_all=True,
                    created_at=now - timedelta(minutes=20),
                ),
            ]
        )
        await session.commit()

        payload = await build_host_metric_history(
            session,
            metric_key="cpu",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    assert payload["chart_events"] == [
        {
            "timestamp": resized_at.isoformat(),
            "kind": "auto_size_resized",
            "label": "web",
            "summary": "web",
            "severity": "success",
            "source": "auto_size",
            "backend": "web",
            "related_backends": ["web"],
            "before": "small",
            "after": "medium",
            "rows": [
                {"label": "before", "value": "small"},
                {"label": "after", "value": "medium"},
            ],
        }
    ]


async def test_host_metric_history_reports_network_directions(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")
    now = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)

    async with maker() as session:
        session.add(
            HostResourceSample(
                bucket_start=now - timedelta(minutes=1),
                network_total_bps=8192,
                network_rx_bps=6144,
                network_tx_bps=2048,
                network_total_bps_count=1,
                network_total_bps_sum=8192.0,
                network_total_bps_min=8192.0,
                network_total_bps_max=8192.0,
                network_total_bps_first=8192.0,
                network_total_bps_last=8192.0,
            )
        )
        await session.commit()

        payload = await build_host_metric_history(
            session,
            metric_key="network",
            timeframe_key="hour",
            chart_width_px=800,
            live_metrics=None,
            now=now,
        )

    point = payload["series"][0]
    assert payload["metric"]["unit_kind"] == "rate"
    assert point["network_rx_bps"] == 6144.0
    assert point["network_tx_bps"] == 2048.0


async def test_output_metric_history_reports_network_series(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(
        ui_reads,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "metrics": {
                        "network_total_bps": 24_000.0,
                        "network_rx_bps": 20_000.0,
                        "network_tx_bps": 4_000.0,
                    },
                }
            ]
        },
    )
    now = datetime.now(timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=6),
                    network_rx_bytes=1_000_000,
                    network_tx_bytes=500_000,
                    network_total_bps=12_000,
                    network_total_bps_count=1,
                    network_total_bps_sum=12_000.0,
                    network_total_bps_min=12_000.0,
                    network_total_bps_max=12_000.0,
                    network_total_bps_first=12_000.0,
                    network_total_bps_last=12_000.0,
                ),
                BackendResourceSample(
                    backend_id=backend.id,
                    bucket_start=now - timedelta(minutes=5),
                    network_rx_bytes=1_600_000,
                    network_tx_bytes=620_000,
                    network_total_bps=12_000,
                    network_total_bps_count=1,
                    network_total_bps_sum=12_000.0,
                    network_total_bps_min=12_000.0,
                    network_total_bps_max=12_000.0,
                    network_total_bps_first=12_000.0,
                    network_total_bps_last=12_000.0,
                ),
            ]
        )
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="network",
            timeframe="day",
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["metric"]["key"] == "network"
    assert payload["metric"]["unit_kind"] == "rate"
    assert payload["series"][1]["network_rx_bps"] == 10000.0
    assert payload["series"][1]["network_tx_bps"] == 2000.0
    assert payload["series"][-1]["is_live"] is True
    assert payload["series"][-1]["network_rx_bps"] == 20000.0
    assert payload["series"][-1]["network_tx_bps"] == 4000.0
    assert payload["summary"]["latest_value"] == 24000.0
    assert payload["summary"]["peak_value"] == 24000.0


async def test_output_metric_history_reports_disk_usage_series(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(
        ui_reads,
        "peek_cached_status",
        lambda: {
            "services": [
                {
                    "service": "cnc-app-web",
                    "metrics": {
                        "disk_usage_bytes": 3 * 1024 * 1024,
                    },
                }
            ]
        },
    )
    now = datetime.now(timezone.utc)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            sandbox_profile="ubuntu-24.04-systemd",
            handoff_port=8337,
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendResourceSample(
                backend_id=backend.id,
                bucket_start=now - timedelta(minutes=5),
                disk_usage_bytes=2 * 1024 * 1024,
                disk_usage_bytes_count=1,
                disk_usage_bytes_sum=float(2 * 1024 * 1024),
                disk_usage_bytes_min=float(2 * 1024 * 1024),
                disk_usage_bytes_max=float(2 * 1024 * 1024),
                disk_usage_bytes_first=float(2 * 1024 * 1024),
                disk_usage_bytes_last=float(2 * 1024 * 1024),
            )
        )
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="disk",
            timeframe="day",
            settings=settings,
            session=session,
        )

    assert payload["available"] is True
    assert payload["metric"]["key"] == "disk"
    assert payload["metric"]["unit_kind"] == "bytes"
    assert payload["sample_cadence"] == "1h"
    assert payload["series"][-1]["is_live"] is True
    assert payload["summary"]["latest_value"] == float(3 * 1024 * 1024)
    assert payload["summary"]["peak_value"] == float(3 * 1024 * 1024)
    assert payload["limit_value"] is None


async def test_output_metric_history_reports_static_outputs_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    monkeypatch.setattr(ui_reads, "peek_cached_status", lambda: {"services": []})

    async with maker() as session:
        backend = Backend(
            name="docs",
            kind="static",
            static_root="/srv/docs",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        payload = await ui_reads.output_metric_history(
            backend.id,
            metric="memory",
            timeframe="week",
            settings=settings,
            session=session,
        )

    assert payload["available"] is False
    assert payload["series"] == []
    assert (
        payload["note"]
        == "Resource history is available for app and shield outputs only."
    )
