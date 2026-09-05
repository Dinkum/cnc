import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import Backend, BackendHardeningRun, ControlEvent
from app.services import app_hardening


@pytest.mark.asyncio
async def test_phase1_stop_request_rates_collected_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        app_control_dir=tmp_path / "app-control",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with app_hardening._session(settings) as session:
        backend = Backend(
            name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
        )
        session.add(backend)
        await session.flush()
        run = await app_hardening.create_hardening_run(
            session,
            backend,
            settings,
            phase="phase1",
            details={"message": "Phase 1 monitor queued.", "duration_sec": 3600},
        )
        backend_id = backend.id
        run_id = run.id

    def fake_collect(_container, evidence_dir, _duration_sec, _settings, _run_id):
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "inspect.json").write_text("[]", encoding="utf-8")
        (evidence_dir / "diff.start").write_text("", encoding="utf-8")
        (evidence_dir / "diff.end").write_text("", encoding="utf-8")
        (evidence_dir / "logs.snapshot").write_text("", encoding="utf-8")
        (evidence_dir / "events.snapshot").write_text("", encoding="utf-8")
        (evidence_dir / "stats.log").write_text("", encoding="utf-8")
        (evidence_dir / "inside-sample.log").write_text("", encoding="utf-8")
        (evidence_dir / "fds.log").write_text("", encoding="utf-8")

        async def request_stop() -> None:
            async with app_hardening._session(settings) as session:
                await app_hardening.request_phase1_monitor_stop(session, backend_id)

        asyncio.run(request_stop())

    monkeypatch.setattr(
        app_hardening, "container_exists", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(app_hardening, "_collect_phase1_evidence", fake_collect)

    await app_hardening._run_phase1_monitor_async(settings, run_id, backend_id, 3600)

    async with app_hardening._session(settings) as session:
        stored = await session.get(BackendHardeningRun, run_id)
        event = (
            await session.execute(
                select(ControlEvent).where(
                    ControlEvent.kind == "hardening_phase1_complete"
                )
            )
        ).scalar_one()

    assert stored is not None
    assert stored.status == "success"
    assert json.loads(stored.ratings_json)["read_only_rootfs"] == "likely_safe"
    details = json.loads(stored.details_json)
    assert details["stopped_early"] is True
    assert details["message"] == "Phase 1 monitor stopped early."
    assert event.scope == "backend"
    assert event.backend_name == "demo"
    assert event.summary == "Security baseline complete"
    assert event.severity == "success"
    assert {"label": "run", "value": f"#{run_id}"} in event.subevents

    await engine.dispose()


@pytest.mark.asyncio
async def test_phase2_clones_output_without_user_input_and_removes_clone(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_control_dir=tmp_path / "app-control",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with app_hardening._session(settings) as session:
        source = Backend(
            name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
        )
        session.add(source)
        await session.flush()
        phase1_dir = (
            tmp_path / "app-control" / "demo" / "hardening" / "run-1" / "phase1"
        )
        phase1_dir.mkdir(parents=True)
        (phase1_dir / "values.json").write_text(
            json.dumps(
                {
                    "pids_limit": 512,
                    "memory_limit": "512M",
                    "nofile_limit": 4096,
                    "shm_size": "64M",
                }
            ),
            encoding="utf-8",
        )
        phase1 = BackendHardeningRun(
            backend_id=source.id,
            phase="phase1",
            status="success",
            evidence_dir=str(phase1_dir),
            ratings_json=json.dumps({"privileged_false": "likely_safe"}),
            details_json="{}",
        )
        phase2 = BackendHardeningRun(
            backend_id=source.id,
            phase="phase2",
            status="queued",
            evidence_dir=str(
                tmp_path / "app-control" / "demo" / "hardening" / "run-2" / "phase2"
            ),
            ratings_json="{}",
            details_json="{}",
        )
        session.add_all([phase1, phase2])
        await session.commit()
        source_id = source.id
        run_id = phase2.id

    clone_payloads = []
    clone_names = []

    async def fake_clone_backend(session, backend_id, payload, settings):
        clone_payloads.append(
            SimpleNamespace(name=payload.name, port=payload.port, backend_id=backend_id)
        )
        clone = Backend(
            name=payload.name,
            kind="app",
            port=payload.port,
            enabled=False,
            volumes_json="[]",
        )
        session.add(clone)
        await session.flush()
        (settings.app_sandbox_dir / clone.name).mkdir(parents=True)
        (settings.app_control_dir / clone.name).mkdir(parents=True)
        clone_names.append(clone.name)
        return {"backend": clone}

    monkeypatch.setattr(
        app_hardening.backend_commands, "clone_backend", fake_clone_backend
    )
    monkeypatch.setattr(
        app_hardening,
        "STRICT_SETTINGS",
        (app_hardening.StrictSetting("privileged_false", ()),),
    )
    monkeypatch.setattr(
        app_hardening, "_create_test_network", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_hardening, "_test_strict_setting", lambda *_args, **_kwargs: "certain_safe"
    )
    monkeypatch.setattr(
        app_hardening,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, stdout="", stderr=""),
    )

    await app_hardening._run_phase2_test_async(settings, run_id, source_id)

    assert clone_payloads
    assert clone_payloads[0].backend_id == source_id
    assert clone_payloads[0].name.startswith("hardening-demo-")
    assert clone_payloads[0].port != 12000

    async with app_hardening._session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        clones = (
            (
                await session.execute(
                    select(Backend).where(Backend.name.in_(clone_names))
                )
            )
            .scalars()
            .all()
        )

    assert run is not None
    assert run.status == "success"
    assert json.loads(run.ratings_json) == {"privileged_false": "certain_safe"}
    progress = json.loads(run.details_json)["progress"]
    assert progress == {
        "completed": 1,
        "total": 1,
        "state": "finished",
        "substate": "complete",
        "current_setting": "",
        "current_label": "",
    }
    assert clones == []
    for clone_name in clone_names:
        assert not (settings.app_sandbox_dir / clone_name).exists()
        assert not (settings.app_control_dir / clone_name).exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_phase2_resume_skips_completed_settings(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_control_dir=tmp_path / "app-control",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with app_hardening._session(settings) as session:
        source = Backend(
            name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
        )
        session.add(source)
        await session.flush()
        phase1_dir = (
            tmp_path / "app-control" / "demo" / "hardening" / "run-1" / "phase1"
        )
        phase1_dir.mkdir(parents=True)
        (phase1_dir / "values.json").write_text("{}", encoding="utf-8")
        phase1 = BackendHardeningRun(
            backend_id=source.id,
            phase="phase1",
            status="success",
            evidence_dir=str(phase1_dir),
            ratings_json=json.dumps(
                {"privileged_false": "likely_safe", "cap_drop_all": "likely_safe"}
            ),
            details_json="{}",
        )
        interrupted = BackendHardeningRun(
            backend_id=source.id,
            phase="phase2",
            status="failed",
            evidence_dir=str(
                tmp_path / "app-control" / "demo" / "hardening" / "run-2" / "phase2"
            ),
            ratings_json=json.dumps({"privileged_false": "certain_safe"}),
            details_json=json.dumps(
                {
                    "interrupted": True,
                    "tested": [
                        {"setting": "privileged_false", "rating": "certain_safe"}
                    ],
                }
            ),
            error=app_hardening.HARDENING_RESTART_ERROR,
        )
        session.add_all([phase1, interrupted])
        await session.flush()
        resumed = await app_hardening.create_resumed_phase2_run(
            session,
            source,
            settings,
            interrupted,
        )
        source_id = source.id
        run_id = resumed.id

    clone_names = []

    async def fake_clone_backend(session, backend_id, payload, settings):
        clone = Backend(
            name=payload.name,
            kind="app",
            port=payload.port,
            enabled=False,
            volumes_json="[]",
        )
        session.add(clone)
        await session.flush()
        (settings.app_sandbox_dir / clone.name).mkdir(parents=True)
        (settings.app_control_dir / clone.name).mkdir(parents=True)
        clone_names.append(clone.name)
        return {"backend": clone}

    tested_settings = []

    def fake_test_strict_setting(
        _backend, setting, _evidence_dir, _settings, _publish_substate
    ):
        tested_settings.append(setting.name)
        return "certain_safe"

    monkeypatch.setattr(
        app_hardening.backend_commands, "clone_backend", fake_clone_backend
    )
    monkeypatch.setattr(
        app_hardening,
        "STRICT_SETTINGS",
        (
            app_hardening.StrictSetting("privileged_false", ()),
            app_hardening.StrictSetting("cap_drop_all", ()),
        ),
    )
    monkeypatch.setattr(
        app_hardening, "_create_test_network", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(app_hardening, "_test_strict_setting", fake_test_strict_setting)
    monkeypatch.setattr(
        app_hardening,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, stdout="", stderr=""),
    )

    await app_hardening._run_phase2_test_async(settings, run_id, source_id)

    async with app_hardening._session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)

    assert tested_settings == ["cap_drop_all"]
    assert run is not None
    assert run.status == "success"
    assert json.loads(run.ratings_json) == {
        "cap_drop_all": "certain_safe",
        "privileged_false": "certain_safe",
    }
    details = json.loads(run.details_json)
    assert details["resumed_from_run_id"] == interrupted.id
    assert details["progress"]["completed"] == 2
    assert details["progress"]["total"] == 2
    assert [item["setting"] for item in details["tested"]] == [
        "privileged_false",
        "cap_drop_all",
    ]
    for clone_name in clone_names:
        assert not (settings.app_sandbox_dir / clone_name).exists()
        assert not (settings.app_control_dir / clone_name).exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_fail_interrupted_hardening_runs_marks_run_and_removes_phase2_clone(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_control_dir=tmp_path / "app-control",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with app_hardening._session(settings) as session:
        source = Backend(
            name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
        )
        session.add(source)
        await session.flush()
        phase2 = BackendHardeningRun(
            backend_id=source.id,
            phase="phase2",
            status="running",
            ratings_json=json.dumps({"cap_drop_all": "certain_safe"}),
            details_json=json.dumps(
                {
                    "message": "Tested cap_drop_all.",
                    "progress": {
                        "completed": 1,
                        "total": 2,
                        "state": "tested",
                        "substate": "complete",
                    },
                }
            ),
        )
        session.add(phase2)
        await session.flush()
        clone_name = f"hardening-demo-{phase2.id}"
        clone = Backend(
            name=clone_name, kind="app", port=13000, enabled=False, volumes_json="[]"
        )
        session.add(clone)
        await session.commit()
        run_id = phase2.id
        clone_id = clone.id

    (settings.app_sandbox_dir / clone_name).mkdir(parents=True)
    (settings.app_control_dir / clone_name).mkdir(parents=True)
    commands = []

    def fake_run_command(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_hardening, "run_command", fake_run_command)

    count = await app_hardening.fail_interrupted_hardening_runs(settings)

    async with app_hardening._session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        clone = await session.get(Backend, clone_id)

    assert count == 1
    assert run is not None
    assert run.status == "failed"
    assert run.error == app_hardening.HARDENING_RESTART_ERROR
    details = json.loads(run.details_json)
    assert details["interrupted"] is True
    assert details["message"] == "Phase 2 clone test interrupted by CNC restart."
    assert details["progress"]["state"] == "failed"
    assert details["progress"]["substate"] == "interrupted"
    assert clone is None
    assert ["podman", "rm", "-f", app_hardening.container_name(clone_name)] in commands
    assert [
        "podman",
        "network",
        "rm",
        "-f",
        app_hardening.network_name(clone_name),
    ] in commands
    assert not (settings.app_sandbox_dir / clone_name).exists()
    assert not (settings.app_control_dir / clone_name).exists()

    await engine.dispose()


@pytest.mark.asyncio
async def test_fail_interrupted_hardening_runs_preserves_clone_row_when_runtime_cleanup_fails(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_control_dir=tmp_path / "app-control",
    )
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with app_hardening._session(settings) as session:
        source = Backend(
            name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
        )
        session.add(source)
        await session.flush()
        phase2 = BackendHardeningRun(
            backend_id=source.id,
            phase="phase2",
            status="running",
            ratings_json="{}",
            details_json="{}",
        )
        session.add(phase2)
        await session.flush()
        clone_name = f"hardening-demo-{phase2.id}"
        clone = Backend(
            name=clone_name, kind="app", port=13000, enabled=False, volumes_json="[]"
        )
        session.add(clone)
        await session.commit()
        clone_id = clone.id

    def fake_run_command(command, **_kwargs):
        if command[:3] == ["podman", "network", "rm"]:
            return SimpleNamespace(ok=False, returncode=1, stdout="", stderr="busy")
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_hardening, "run_command", fake_run_command)

    count = await app_hardening.fail_interrupted_hardening_runs(settings)

    async with app_hardening._session(settings) as session:
        clone = await session.get(Backend, clone_id)

    assert count == 1
    assert clone is not None

    await engine.dispose()


def test_strict_setting_removes_container_when_start_fails(
    monkeypatch, tmp_path: Path
) -> None:
    backend = Backend(
        name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
    )
    settings = Settings(
        app_sandbox_dir=tmp_path / "sandboxes", app_control_dir=tmp_path / "app-control"
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "phase1-values.json").write_text("{}", encoding="utf-8")
    commands = []

    def fake_run_command(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["podman", "create"]:
            return SimpleNamespace(ok=True, stdout="", stderr="", returncode=0)
        if command[:2] == ["podman", "start"]:
            return SimpleNamespace(
                ok=False, stdout="", stderr="permission denied", returncode=126
            )
        return SimpleNamespace(ok=True, stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_hardening, "run_command", fake_run_command)
    monkeypatch.setattr(
        app_hardening,
        "_strict_create_command",
        lambda *_args, **_kwargs: ["podman", "create", "--name", "cnc-demo"],
    )

    rating = app_hardening._test_strict_setting(
        backend,
        app_hardening.StrictSetting("read_only_rootfs", ("--read-only",)),
        evidence_dir,
        settings,
    )

    assert rating == "certain_unsafe"
    assert commands[-1] == ["podman", "rm", "-f", "cnc-app-demo"]


def test_phase2_network_create_failure_is_fatal(monkeypatch, tmp_path: Path) -> None:
    settings = Settings()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    def fake_run_command(command, **_kwargs):
        if command[:3] == ["podman", "network", "create"]:
            return SimpleNamespace(
                command=command,
                ok=False,
                stdout="",
                stderr="network failed",
                returncode=1,
            )
        return SimpleNamespace(
            command=command, ok=True, stdout="", stderr="", returncode=0
        )

    monkeypatch.setattr(app_hardening, "run_command", fake_run_command)

    with pytest.raises(app_hardening.CommandError):
        app_hardening._create_test_network("demo", evidence_dir, settings)


def test_strict_setting_reports_progress_substates(monkeypatch, tmp_path: Path) -> None:
    backend = Backend(
        name="demo", kind="app", port=12000, enabled=True, volumes_json="[]"
    )
    settings = Settings(
        app_sandbox_dir=tmp_path / "sandboxes", app_control_dir=tmp_path / "app-control"
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "phase1-values.json").write_text("{}", encoding="utf-8")
    substates = []

    def fake_run_command(command, **_kwargs):
        if command[:2] == ["podman", "create"]:
            return SimpleNamespace(ok=True, stdout="", stderr="", returncode=0)
        if command[:2] == ["podman", "start"]:
            return SimpleNamespace(ok=True, stdout="", stderr="", returncode=0)
        return SimpleNamespace(ok=True, stdout="", stderr="", returncode=0)

    monkeypatch.setattr(app_hardening, "run_command", fake_run_command)
    monkeypatch.setattr(
        app_hardening,
        "_strict_create_command",
        lambda *_args, **_kwargs: ["podman", "create", "--name", "cnc-demo"],
    )
    monkeypatch.setattr(
        app_hardening, "_watch_container", lambda *_args, **_kwargs: (True, "")
    )

    rating = app_hardening._test_strict_setting(
        backend,
        app_hardening.StrictSetting("read_only_rootfs", ("--read-only",)),
        evidence_dir,
        settings,
        substates.append,
    )

    assert rating == "certain_safe"
    assert substates == ["preclean", "create", "start", "watch", "cleanup"]


def test_feature_rows_are_grouped_and_hide_unrated_settings() -> None:
    phase1 = BackendHardeningRun(
        backend_id=2,
        phase="phase1",
        status="success",
        evidence_dir="/tmp/hardening/run-1/phase1",
        ratings_json=json.dumps({"cap_drop_all": "likely_safe"}),
        details_json="{}",
    )
    phase2 = BackendHardeningRun(
        backend_id=2,
        phase="phase2",
        status="success",
        evidence_dir="/tmp/hardening/run-2/phase2",
        ratings_json=json.dumps({"network_none": "certain_unsafe"}),
        details_json="{}",
    )

    rows = app_hardening._feature_rows(phase1, phase2)

    assert [row["setting"] for row in rows] == ["cap_drop_all", "network_none"]
    assert rows[0]["group"] == "privilege"
    assert rows[0]["group_label"] == "Privilege"
    assert (
        rows[0]["description"]
        == "Drops all Linux capabilities by default so the container starts from the smallest privilege set."
    )
    assert "Phase 1" in rows[0]["evidence"]
    assert rows[1]["group"] == "network"
    assert rows[1]["group_label"] == "Network"
    assert rows[1]["description"] == "Runs the container with all networking removed."
    assert "Phase 2" in rows[1]["evidence"]
    assert "--network=none" in rows[1]["evidence"]
    assert "DNS failures" in rows[1]["evidence"]
    assert "Artifacts:" not in rows[1]["evidence"]
    assert "settings/network_none" not in rows[1]["evidence"]


def test_feature_evidence_is_specific_without_artifact_paths() -> None:
    phase2 = BackendHardeningRun(
        backend_id=2,
        phase="phase2",
        status="success",
        evidence_dir="/tmp/hardening/run-2/phase2",
        ratings_json=json.dumps(
            {"seccomp_custom_profile": "uncertain", "cap_drop_all": "certain_safe"}
        ),
        details_json="{}",
    )

    rows = {row["setting"]: row for row in app_hardening._feature_rows(None, phase2)}

    seccomp_evidence = rows["seccomp_custom_profile"]["evidence"]
    assert "seccomp.json" in seccomp_evidence
    assert "no syscall allowlist" in seccomp_evidence
    assert "Artifacts:" not in seccomp_evidence
    assert "run-2" not in seccomp_evidence

    cap_evidence = rows["cap_drop_all"]["evidence"]
    assert "--cap-drop=ALL" in cap_evidence
    assert "capability-specific failures" in cap_evidence
    assert "Artifacts:" not in cap_evidence


def test_uncertain_rating_surfaces_as_uncertain_recommendation() -> None:
    phase2 = BackendHardeningRun(
        backend_id=2,
        phase="phase2",
        status="success",
        evidence_dir="/tmp/hardening/run-2/phase2",
        ratings_json=json.dumps({"seccomp_custom_profile": "uncertain"}),
        details_json="{}",
    )

    rows = app_hardening._feature_rows(None, phase2)

    assert rows[0]["setting"] == "seccomp_custom_profile"
    assert rows[0]["recommendation"] == "uncertain"


def test_all_hardening_settings_have_user_facing_descriptions() -> None:
    assert set(app_hardening.HARDENING_SETTING_DESCRIPTIONS) == set(
        app_hardening.HARDENING_SETTINGS
    )
    assert all(
        description.endswith(".")
        for description in app_hardening.HARDENING_SETTING_DESCRIPTIONS.values()
    )
