from __future__ import annotations

import json

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from app.config import Settings
from app.models.entities import Backend, BackendHardeningRun, Operation
from app.schemas.apply import ApplyResponse
from app.services.hardening_policy import hardening_preview, read_hardening_policy
from app.services.mutation_apply import commit_and_apply
from app.ui.operations import hardening as worker
from app.ui.routes import hardening_mutations as routes
from .support import _PostedRequest, _make_session


@pytest.fixture
async def hardening_app(monkeypatch, tmp_path):
    settings = Settings(
        beta_hardening=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        app_control_dir=tmp_path / "control",
        apply_lock_path=tmp_path / "apply.lock",
    )
    maker = await _make_session(tmp_path / "app.db")
    monkeypatch.setattr(routes, "enforce_csrf", lambda *_args: None)
    async with maker() as session:
        app = Backend(
            name="web",
            kind="app",
            port=12000,
            handoff_port=8000,
            sandbox_profile="ubuntu-24.04-systemd",
            volumes_json="[]",
            enabled=True,
        )
        session.add(app)
        await session.flush()
        session.add(
            BackendHardeningRun(
                backend_id=app.id,
                phase="phase1",
                status="success",
                ratings_json='{"no_new_privileges":"likely_safe"}',
                details_json="{}",
            )
        )
        await session.commit()
        backend_id = app.id
    return settings, maker, backend_id


async def preview(settings, session, backend_id, **kwargs):
    return await routes.preview_hardening_configuration(
        backend_id,
        _PostedRequest(path=f"/ui/backends/{backend_id}/hardening/preview"),
        csrf_token="token",
        mode="manual",
        configuration='{"no_new_privileges":true}',
        settings=settings,
        session=session,
        **kwargs,
    )


async def apply(settings, session, backend_id, plan, tasks, *, reviewed=True):
    return await routes.apply_hardening_configuration(
        backend_id,
        _PostedRequest(path=f"/ui/backends/{backend_id}/hardening/apply"),
        tasks,
        csrf_token="token",
        mode=plan["mode"],
        configuration=json.dumps(plan["configuration"]),
        revision=plan["revision"],
        reviewed=reviewed,
        settings=settings,
        session=session,
    )


async def test_gate_and_non_app_requests_are_rejected(hardening_app):
    settings, maker, backend_id = hardening_app
    async with maker() as session:
        with pytest.raises(HTTPException) as blocked:
            await routes.read_hardening_configuration(
                backend_id,
                settings=settings.model_copy(update={"beta_hardening": False}),
                session=session,
            )
        assert blocked.value.status_code == 403
        app = await session.get(Backend, backend_id)
        app.kind = "static"
        await session.commit()
        with pytest.raises(HTTPException) as unsupported:
            await preview(settings, session, backend_id)
        assert unsupported.value.status_code == 400


async def test_review_acknowledgment_is_required_before_queueing(hardening_app):
    settings, maker, backend_id = hardening_app
    async with maker() as session:
        plan = await preview(settings, session, backend_id)
        tasks = BackgroundTasks()
        with pytest.raises(HTTPException, match="Review"):
            await apply(settings, session, backend_id, plan, tasks, reviewed=False)
        assert tasks.tasks == []
        assert list((await session.execute(select(Operation))).scalars()) == []


async def test_changed_output_invalidates_the_review(hardening_app):
    settings, maker, backend_id = hardening_app
    async with maker() as session:
        plan = await preview(settings, session, backend_id)
        app = await session.get(Backend, backend_id)
        app.handoff_port = 8080
        await session.commit()
        with pytest.raises(HTTPException) as changed:
            await apply(settings, session, backend_id, plan, BackgroundTasks())
        assert changed.value.status_code == 409


@pytest.mark.parametrize("outcome", ["success", "error"])
async def test_apply_commits_only_after_convergence_and_keeps_previous(
    hardening_app, monkeypatch, outcome
):
    settings, maker, backend_id = hardening_app
    observed = []

    async def fake_apply(session, _settings):
        app = await session.get(Backend, backend_id)
        observed.append(read_hardening_policy(app).no_new_privileges)
        return ApplyResponse(
            status=outcome,
            message="fixture convergence",
            details={"phase": "app_runtime"},
        )

    async def converge(session, settings, operation, **_kwargs):
        result = await commit_and_apply(
            session, settings, operation="hardening test", apply_runner=fake_apply
        )
        return result.apply_response

    monkeypatch.setattr(worker, "_commit_output_save_apply", converge)
    async with maker() as session:
        plan = await preview(settings, session, backend_id)
        tasks = BackgroundTasks()
        result = await apply(settings, session, backend_id, plan, tasks)
        assert result.status_code == 202
        operation_id = json.loads(result.body)["operation_id"]
    await tasks()
    async with maker() as session:
        app = await session.get(Backend, backend_id)
        operation = await session.get(Operation, operation_id)
        assert observed == [True]
        assert read_hardening_policy(app).no_new_privileges is (outcome == "success")
        assert app.hardening_previous_json == ("{}" if outcome == "success" else None)
        assert operation.status == ("success" if outcome == "success" else "failed")


async def test_queued_apply_rechecks_revision_under_host_lock(
    hardening_app, monkeypatch
):
    settings, maker, backend_id = hardening_app

    async def unexpected_apply(*_args, **_kwargs):
        pytest.fail("A stale review reached runtime convergence")

    monkeypatch.setattr(worker, "_commit_output_save_apply", unexpected_apply)
    async with maker() as session:
        plan = await preview(settings, session, backend_id)
        tasks = BackgroundTasks()
        result = await apply(settings, session, backend_id, plan, tasks)
        operation_id = json.loads(result.body)["operation_id"]
    async with maker() as session:
        app = await session.get(Backend, backend_id)
        app.notes = "Changed after queueing"
        await session.commit()
    await tasks()
    async with maker() as session:
        operation = await session.get(Operation, operation_id)
        assert operation.status == "failed"
        assert "changed" in operation.error
        assert (
            read_hardening_policy(await session.get(Backend, backend_id)).persisted()
            == "{}"
        )


async def test_previous_policy_can_be_reviewed_without_an_advisor_run(hardening_app):
    settings, maker, backend_id = hardening_app
    async with maker() as session:
        app = await session.get(Backend, backend_id)
        app.hardening_config_json = '{"no_new_privileges":true}'
        app.hardening_previous_json = "{}"
        await session.commit()
        await session.refresh(app)
        plan = hardening_preview(app, {}, "previous")
        assert plan["configuration"]["no_new_privileges"] is False
        assert plan["changes"][0]["before"] is True


async def test_invalid_csrf_is_rejected_before_reading_or_queueing():
    with pytest.raises(HTTPException) as blocked:
        await routes.preview_hardening_configuration(
            1,
            _PostedRequest(path="/ui/backends/1/hardening/preview"),
            csrf_token="invalid",
            mode="manual",
            configuration="{}",
            settings=Settings(beta_hardening=True, csrf_token="expected"),
            session=None,
        )
    assert blocked.value.status_code == 403
