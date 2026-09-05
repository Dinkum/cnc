import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import HostApplyState
from app.schemas.apply import ApplyResponse
from app.services import apply_service, mutation_apply
from app.services.mutation_apply import commit_and_apply
from app.services.operations import OperationHandle


class FakeSession:
    def __init__(self) -> None:
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class CommitFailSession(FakeSession):
    async def commit(self) -> None:
        self.commits += 1
        raise RuntimeError("database disk I/O error")


@pytest.mark.asyncio
async def test_commit_and_apply_reports_applied_after_success(
    monkeypatch, tmp_path
) -> None:
    cache_invalidations = 0

    def fake_invalidate_status_cache(*_args, **_kwargs) -> None:
        nonlocal cache_invalidations
        cache_invalidations += 1

    async def fake_apply_runner(_session, _settings):
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=101
        )

    monkeypatch.setattr(
        mutation_apply, "invalidate_status_cache", fake_invalidate_status_cache
    )
    session = FakeSession()

    result = await commit_and_apply(
        session,
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        operation="test.success",
        apply_runner=fake_apply_runner,
    )

    assert session.flushes == 1
    assert session.commits == 1
    assert session.rollbacks == 0
    assert cache_invalidations == 1
    assert result.state == "applied"
    assert result.state_history == ("pending", "applying", "applied")
    assert result.applied is True
    assert result.apply_response.run_id == 101


@pytest.mark.asyncio
async def test_commit_failure_durably_invalidates_apply_convergence(
    monkeypatch, tmp_path
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    engine = create_async_engine(database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as recovery_session:
        recovery_session.add(
            HostApplyState(
                id=1,
                last_applied_state_hash="applied-hash",
                last_successful_operation_id=7,
                last_successful_apply_run_id=8,
            )
        )
        await recovery_session.commit()

    async def fake_apply_runner(_session, _settings):
        return ApplyResponse(
            status="success",
            message="host changed",
            details={},
            run_id=8,
        )

    async def fake_emit_control_event(*_args, **_kwargs):
        return {"sent": False}

    monkeypatch.setattr(mutation_apply, "emit_control_event", fake_emit_control_event)
    session = CommitFailSession()
    result = await commit_and_apply(
        session,
        Settings(database_url=database_url),
        operation="test.commit_failure",
        apply_runner=fake_apply_runner,
    )

    async with maker() as recovery_session:
        marker = await recovery_session.get(HostApplyState, 1)
        (
            hashes,
            reason,
            desired_hash,
        ) = await apply_service._load_previous_successful_slice_hashes(recovery_session)
    await engine.dispose()

    assert session.commits == 1
    assert session.rollbacks == 1
    assert result.state == "apply_failed"
    assert result.apply_response.details["failure_mode"] == "partial"
    assert result.apply_response.details["convergence_invalidated"] is True
    assert marker is not None
    assert marker.last_applied_state_hash is None
    assert marker.last_successful_operation_id is None
    assert marker.last_successful_apply_run_id is None
    assert hashes == {}
    assert reason == "no_previous_successful_apply"
    assert desired_hash == ""


@pytest.mark.asyncio
async def test_commit_and_apply_passes_existing_operation_handle(
    monkeypatch, tmp_path
) -> None:
    seen_handle = None
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    operation_handle = OperationHandle(id=11, kind="test.operation", settings=settings)

    def fake_invalidate_status_cache(*_args, **_kwargs) -> None:
        return None

    async def fake_apply_runner(_session, _settings, *, operation_handle=None):
        nonlocal seen_handle
        seen_handle = operation_handle
        return ApplyResponse(
            status="success", message="apply completed", details={}, run_id=101
        )

    monkeypatch.setattr(
        mutation_apply, "invalidate_status_cache", fake_invalidate_status_cache
    )
    session = FakeSession()

    result = await commit_and_apply(
        session,
        settings,
        operation="test.success",
        apply_runner=fake_apply_runner,
        operation_handle=operation_handle,
    )

    assert result.state == "applied"
    assert seen_handle is operation_handle


@pytest.mark.asyncio
async def test_deferred_apply_completed_event_uses_response_summary(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    async def fake_emit_control_event(_settings, **kwargs):
        captured.update(kwargs)
        return {"sent": False}

    monkeypatch.setattr(mutation_apply, "emit_control_event", fake_emit_control_event)
    response = ApplyResponse(
        status="success",
        message="apply completed",
        details={
            "apply_event_summary": "Output created",
            "apply_event_subevents": [
                {"label": "run", "value": "#101"},
                {"label": "output", "value": "web"},
                {"label": "status", "value": "success"},
            ],
            "outputs_total": 1,
            "outputs_created": 1,
            "outputs_updated": 0,
            "outputs_deleted": 0,
            "output_created_names": ["web"],
            "app_healthcheck_status": "ok",
        },
        run_id=101,
    )

    await mutation_apply._emit_deferred_apply_completed_event(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        response,
    )

    assert captured["summary"] == "Output created"
    assert captured["subevents"] == [
        {"label": "run", "value": "#101"},
        {"label": "output", "value": "web"},
        {"label": "status", "value": "success"},
    ]
    assert captured["details"]["outputs_created"] == 1
    assert captured["details"]["output_created_names"] == ["web"]


@pytest.mark.asyncio
async def test_commit_and_apply_rejects_incomplete_operation_handle(
    monkeypatch, tmp_path
) -> None:
    cache_invalidations = 0

    class IncompleteOperationHandle:
        id = 12
        kind = "test.incomplete"
        completed = False

        async def update(self, **_kwargs):
            return None

        async def complete(self, *_args, **_kwargs):
            return None

    def fake_invalidate_status_cache(*_args, **_kwargs) -> None:
        nonlocal cache_invalidations
        cache_invalidations += 1

    async def fail_apply_runner(*_args, **_kwargs):
        raise AssertionError("invalid operation handle should fail before apply runs")

    monkeypatch.setattr(
        mutation_apply, "invalidate_status_cache", fake_invalidate_status_cache
    )
    session = FakeSession()

    result = await commit_and_apply(
        session,
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        operation="test.invalid_handle",
        apply_runner=fail_apply_runner,
        operation_handle=IncompleteOperationHandle(),
    )

    assert session.flushes == 1
    assert session.commits == 0
    assert session.rollbacks == 1
    assert cache_invalidations == 1
    assert result.state == "apply_failed"
    assert result.apply_response.details["error_code"] == "CNC-02099"
    assert result.apply_response.details["error_name"] == "APPLY_FAILED"
    assert (
        "missing required attribute: settings" in result.apply_response.details["error"]
    )


@pytest.mark.asyncio
async def test_commit_and_apply_reports_apply_failed_without_rollback(
    monkeypatch, tmp_path
) -> None:
    cache_invalidations = 0

    def fake_invalidate_status_cache(*_args, **_kwargs) -> None:
        nonlocal cache_invalidations
        cache_invalidations += 1

    async def fake_apply_runner(_session, _settings):
        return ApplyResponse(
            status="error",
            message="apply failed",
            details={"phase": "nginx_validate", "error": "bad config"},
            run_id=102,
        )

    monkeypatch.setattr(
        mutation_apply, "invalidate_status_cache", fake_invalidate_status_cache
    )
    session = FakeSession()

    result = await commit_and_apply(
        session,
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        operation="test.failure",
        apply_runner=fake_apply_runner,
    )

    assert session.flushes == 1
    assert session.commits == 0
    assert session.rollbacks == 1
    assert cache_invalidations == 1
    assert result.state == "apply_failed"
    assert result.state_history == ("pending", "applying", "apply_failed")
    assert result.applied is False
    assert result.apply_response.details["phase"] == "nginx_validate"


@pytest.mark.asyncio
async def test_commit_and_apply_converts_runner_exception_to_apply_failed(
    monkeypatch, tmp_path
) -> None:
    cache_invalidations = 0

    def fake_invalidate_status_cache(*_args, **_kwargs) -> None:
        nonlocal cache_invalidations
        cache_invalidations += 1

    async def exploding_apply_runner(_session, _settings):
        raise RuntimeError("apply worker exploded")

    monkeypatch.setattr(
        mutation_apply, "invalidate_status_cache", fake_invalidate_status_cache
    )
    session = FakeSession()

    result = await commit_and_apply(
        session,
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        operation="test.exception",
        apply_runner=exploding_apply_runner,
    )

    assert session.flushes == 1
    assert session.commits == 0
    assert session.rollbacks == 1
    assert cache_invalidations == 1
    assert result.state == "apply_failed"
    assert result.state_history == ("pending", "applying", "apply_failed")
    assert result.apply_response.details["phase"] == "apply"
    assert result.apply_response.details["error"] == "apply worker exploded"
    assert result.apply_response.details["error_code"] == "CNC-02099"
    assert result.apply_response.details["error_name"] == "APPLY_FAILED"
    assert result.apply_response.details["error_inst"]
