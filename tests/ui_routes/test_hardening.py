from .support import (
    Backend,
    BackendHardeningRun,
    BackgroundTasks,
    Path,
    Request,
    Settings,
    _PostedRequest,
    _make_session,
    json,
    select,
    ui_hardening,
    ui_reads,
)


async def test_resume_hardening_phase2_seeds_new_run(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "app-control",
    )
    monkeypatch.setattr(ui_hardening, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web", kind="app", port=12000, volumes_json="[]", enabled=True
        )
        session.add(backend)
        await session.flush()
        phase1 = BackendHardeningRun(
            backend_id=backend.id,
            phase="phase1",
            status="success",
            ratings_json=json.dumps({"cap_drop_all": "likely_safe"}),
            details_json="{}",
        )
        interrupted = BackendHardeningRun(
            backend_id=backend.id,
            phase="phase2",
            status="failed",
            ratings_json=json.dumps({"cap_drop_all": "certain_safe"}),
            details_json=json.dumps(
                {
                    "interrupted": True,
                    "tested": [{"setting": "cap_drop_all", "rating": "certain_safe"}],
                }
            ),
        )
        session.add_all([phase1, interrupted])
        await session.commit()

        background_tasks = BackgroundTasks()
        response = await ui_hardening.resume_hardening_phase2(
            backend.id,
            _PostedRequest(path=f"/ui/backends/{backend.id}/hardening/phase2/resume"),
            background_tasks,
            settings=settings,
            csrf_token="token",
            session=session,
        )

        runs = (
            (
                await session.execute(
                    select(BackendHardeningRun).order_by(BackendHardeningRun.id.asc())
                )
            )
            .scalars()
            .all()
        )

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["message"] == "Phase 2 resume started."
    assert len(background_tasks.tasks) == 1
    resumed = runs[-1]
    assert resumed.status == "queued"
    assert json.loads(resumed.ratings_json) == {"cap_drop_all": "certain_safe"}
    details = json.loads(resumed.details_json)
    assert details["resumed_from_run_id"] == interrupted.id
    assert details["tested"] == [{"setting": "cap_drop_all", "rating": "certain_safe"}]


async def test_resume_hardening_phase2_reuses_active_run(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setattr(ui_hardening, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web", kind="app", port=12000, volumes_json="[]", enabled=True
        )
        session.add(backend)
        await session.flush()
        phase1 = BackendHardeningRun(
            backend_id=backend.id,
            phase="phase1",
            status="success",
            ratings_json="{}",
            details_json="{}",
        )
        active = BackendHardeningRun(
            backend_id=backend.id,
            phase="phase2",
            status="running",
            ratings_json="{}",
            details_json="{}",
        )
        interrupted = BackendHardeningRun(
            backend_id=backend.id,
            phase="phase2",
            status="failed",
            ratings_json="{}",
            details_json=json.dumps({"interrupted": True}),
        )
        session.add_all([phase1, active, interrupted])
        await session.commit()

        background_tasks = BackgroundTasks()
        response = await ui_hardening.resume_hardening_phase2(
            backend.id,
            _PostedRequest(path=f"/ui/backends/{backend.id}/hardening/phase2/resume"),
            background_tasks,
            settings=settings,
            csrf_token="token",
            session=session,
        )
        run_count = (await session.execute(select(BackendHardeningRun))).scalars().all()

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["message"] == "Phase 2 clone test already running."
    assert payload["run_id"] == active.id
    assert len(background_tasks.tasks) == 0
    assert len(run_count) == 3


async def test_hardening_phase2_events_streams_latest_summary(tmp_path: Path) -> None:
    maker = await _make_session(tmp_path / "app.db")

    async with maker() as session:
        backend = Backend(
            name="web", kind="app", port=12000, volumes_json="[]", enabled=True
        )
        session.add(backend)
        await session.flush()
        session.add(
            BackendHardeningRun(
                backend_id=backend.id,
                phase="phase1",
                status="success",
                ratings_json=json.dumps({"cap_drop_all": "likely_safe"}),
                details_json="{}",
            )
        )
        session.add(
            BackendHardeningRun(
                backend_id=backend.id,
                phase="phase2",
                status="success",
                ratings_json=json.dumps({"cap_drop_all": "certain_safe"}),
                details_json=json.dumps(
                    {
                        "message": "Phase 2 finished.",
                        "progress": {
                            "completed": 1,
                            "total": 44,
                            "state": "tested",
                            "substate": "complete",
                            "current_setting": "cap_drop_all",
                            "current_label": "cap drop all",
                        },
                    }
                ),
            )
        )
        await session.commit()

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/api/backends/{backend.id}/hardening/phase2/events",
                "headers": [],
            },
            receive,
        )
        response = await ui_reads.output_hardening_phase2_events(
            backend.id, request=request, session=session
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        body = "".join(chunks)

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert "event: hardening\n" in body
    assert '"phase":"phase2","status":"success"' in body
    assert '"setting":"cap_drop_all"' in body
    assert '"progress":{"completed":1,"total":44' in body
