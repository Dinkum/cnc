from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import importlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Literal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.models.entities import HostJournalCommit
from app.services.thread_workers import HostMutationLifecycle, run_to_completion


logger = get_logger("host_journal")

JournalState = Literal[
    "running", "committing", "committed", "rolled_back", "unknown"
]
CommitOutcome = Literal["persisted", "rolled_back", "unknown"]
StepState = Literal["started", "done", "undone", "undo_failed"]
ActionFn = Callable[[Settings, dict[str, Any]], Awaitable[None] | None]

# Recovery after a restart runs actions by name, so every module that registers
# actions must be importable from here without the workflow that wrote the journal.
ACTION_MODULES = ("app.services.backend_backup_service",)

_ACTIONS: dict[str, ActionFn] = {}


def journal_action(name: str) -> Callable[[ActionFn], ActionFn]:
    def register(fn: ActionFn) -> ActionFn:
        if name in _ACTIONS and _ACTIONS[name] is not fn:
            raise RuntimeError(f"journal action {name} registered twice")
        _ACTIONS[name] = fn
        return fn

    return register


async def _run_action(settings: Settings, name: str, args: dict[str, Any]) -> None:
    fn = _ACTIONS.get(name)
    if fn is None:
        raise RuntimeError(f"journal action {name} is not registered")
    if inspect.iscoroutinefunction(fn):
        await fn(settings, args)
    else:
        await asyncio.to_thread(fn, settings, args)


@dataclass
class JournalStep:
    name: str
    undo: str | None
    undo_args: dict[str, Any]
    state: StepState = "started"
    error: str = ""


@dataclass
class JournalFinalizer:
    action: str
    args: dict[str, Any]
    state: Literal["pending", "done", "failed"] = "pending"
    error: str = ""


@dataclass
class UnwindReport:
    undone: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    irreversible: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failed and not self.irreversible

    def details(self) -> dict[str, Any]:
        return {
            "unwound_steps": list(self.undone),
            "unwind_errors": dict(self.failed),
            "still_changed_steps": list(self.irreversible),
            "failure_mode": "clean" if self.clean else "partial",
            "manual_review_required": not self.clean,
        }


async def durable_row_exists(
    settings: Settings,
    model: type,
    key: Any,
    *,
    matches: Callable[[Any], bool] | None = None,
) -> CommitOutcome:
    """Read on a connection of its own, because a raised commit does not prove the write was discarded."""
    try:
        engine = create_configured_async_engine(settings.database_url)
        try:
            maker = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with maker() as probe:
                found = await probe.get(model, key)
                return "persisted" if found is not None and (matches is None or matches(found)) else "rolled_back"
        finally:
            await engine.dispose()
    except Exception as exc:
        logger.warning("host_journal.commit_outcome_unresolved", error=str(exc))
        return "unknown"


@dataclass(frozen=True)
class CommitResult:
    outcome: CommitOutcome
    error: str = ""
    rollback_error: str = ""


async def commit_verified(
    session: AsyncSession,
    settings: Settings,
    model: type,
    key: Any,
    *,
    matches: Callable[[Any], bool] | None = None,
) -> CommitResult:
    """The witness must be a new row written by this transaction."""

    async def commit_or_resolve() -> CommitResult:
        try:
            await session.commit()
        except Exception as exc:
            rollback_error = ""
            try:
                await session.rollback()
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
                try:
                    await session.reset()
                except Exception as reset_exc:
                    logger.warning(
                        "host_journal.session_reset_failed", error=str(reset_exc)
                    )
            outcome: CommitOutcome = "unknown"
            if key is not None:
                kwargs = {"matches": matches} if matches is not None else {}
                outcome = await durable_row_exists(settings, model, key, **kwargs)
            logger.warning(
                "host_journal.commit_raised",
                witness=model.__name__,
                key=key,
                outcome=outcome,
                error=str(exc),
                rollback_error=rollback_error,
            )
            return CommitResult(outcome, str(exc), rollback_error)
        return CommitResult("persisted")

    # Once COMMIT starts, the durable result decides whether a caller may undo.
    return await HostMutationLifecycle().finish(commit_or_resolve())


async def commit_with_marker(
    session: AsyncSession, settings: Settings, marker: str, kind: str
) -> tuple[CommitOutcome, str]:
    session.add(HostJournalCommit(journal_id=marker, kind=kind))
    result = await commit_verified(session, settings, HostJournalCommit, marker)
    return result.outcome, result.error


@dataclass
class HostJournal:
    settings: Settings
    id: str
    kind: str
    subject: str
    state: JournalState = "running"
    steps: list[JournalStep] = field(default_factory=list)
    finalizers: list[JournalFinalizer] = field(default_factory=list)
    started_at: str = ""
    error: str = ""

    @classmethod
    def begin(cls, settings: Settings, *, kind: str, subject: str) -> HostJournal:
        journal = cls(
            settings=settings,
            id=uuid.uuid4().hex,
            kind=kind,
            subject=subject,
            started_at=datetime.now(UTC).isoformat(),
        )
        journal._persist()
        return journal

    @property
    def path(self) -> Path:
        return journal_path(self.settings, self.id)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "state": self.state,
            "started_at": self.started_at,
            "error": self.error,
            "steps": [asdict(step) for step in self.steps],
            "finalizers": [asdict(item) for item in self.finalizers],
        }

    @classmethod
    def load(cls, settings: Settings, path: Path) -> HostJournal:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            settings=settings,
            id=str(payload["id"]),
            kind=str(payload["kind"]),
            subject=str(payload["subject"]),
            state=payload["state"],
            started_at=str(payload.get("started_at") or ""),
            error=str(payload.get("error") or ""),
            steps=[JournalStep(**item) for item in payload.get("steps") or []],
            finalizers=[
                JournalFinalizer(**item) for item in payload.get("finalizers") or []
            ],
        )

    def _persist(self) -> None:
        directory = self.settings.resolved_host_journal_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.to_payload(), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _discard(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def step(
        self,
        name: str,
        run: Callable[[], Any],
        *,
        undo: str | None = None,
        undo_args: dict[str, Any] | None = None,
    ) -> Any:
        if self.state != "running":
            raise RuntimeError(f"journal {self.id} is {self.state}, not running")
        if undo is not None and undo not in _ACTIONS:
            raise RuntimeError(f"journal action {undo} is not registered")
        entry = JournalStep(name=name, undo=undo, undo_args=dict(undo_args or {}))
        self.steps.append(entry)
        # The undo is recorded before the step runs; undo actions must tolerate a step that never finished.
        self._persist()
        result = run()
        if inspect.isawaitable(result):
            result = await run_to_completion(result)
        entry.state = "done"
        self._persist()
        return result

    def after_commit(self, action: str, args: dict[str, Any] | None = None) -> None:
        if action not in _ACTIONS:
            raise RuntimeError(f"journal action {action} is not registered")
        self.finalizers.append(JournalFinalizer(action=action, args=dict(args or {})))
        self._persist()

    async def commit(self, session: AsyncSession) -> CommitOutcome:
        self.state = "committing"
        self._persist()
        outcome, self.error = await commit_with_marker(
            session, self.settings, self.id, self.kind
        )
        if outcome == "persisted":
            await self.finalize()
        elif outcome == "unknown":
            self.state = "unknown"
            self._persist()
        else:
            self.state = "running"
        return outcome

    async def finalize(self) -> None:
        self.state = "committed"
        self._persist()
        for item in self.finalizers:
            if item.state == "done":
                continue
            try:
                await _run_action(self.settings, item.action, item.args)
                item.state = "done"
            except Exception as exc:
                item.state = "failed"
                item.error = str(exc)
                logger.warning(
                    "host_journal.finalizer_failed",
                    journal_id=self.id,
                    action=item.action,
                    error=str(exc),
                )
            self._persist()
        if all(item.state == "done" for item in self.finalizers):
            self._discard()

    async def unwind(self) -> UnwindReport:
        report = UnwindReport()
        for entry in reversed(self.steps):
            if entry.state in {"undone"}:
                continue
            if entry.undo is None:
                if entry.state == "done":
                    report.irreversible.append(entry.name)
                continue
            try:
                await _run_action(self.settings, entry.undo, entry.undo_args)
                entry.state = "undone"
                report.undone.append(entry.name)
            except Exception as exc:
                entry.state = "undo_failed"
                entry.error = str(exc)
                report.failed[entry.name] = str(exc)
                logger.warning(
                    "host_journal.undo_failed",
                    journal_id=self.id,
                    step=entry.name,
                    error=str(exc),
                )
            self._persist()
        self.state = "rolled_back"
        if report.clean:
            self._discard()
        else:
            self._persist()
        return report


def journal_path(settings: Settings, journal_id: str) -> Path:
    return settings.resolved_host_journal_dir / f"{journal_id}.json"


def pending_journals(settings: Settings) -> list[Path]:
    directory = settings.resolved_host_journal_dir
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


async def recover_interrupted_journals(settings: Settings) -> list[dict[str, Any]]:
    """Finish or undo journals left by a process that stopped mid-workflow; caller holds the host lock."""
    for module in ACTION_MODULES:
        importlib.import_module(module)
    results: list[dict[str, Any]] = []
    for path in pending_journals(settings):
        try:
            journal = HostJournal.load(settings, path)
        except Exception as exc:
            results.append({"path": str(path), "result": "unreadable", "error": str(exc)})
            continue
        result: dict[str, Any] = {
            "journal_id": journal.id,
            "kind": journal.kind,
            "subject": journal.subject,
            "found_state": journal.state,
        }
        if journal.state == "running":
            report = await journal.unwind()
            result.update(result="rolled_back", **report.details())
        elif journal.state in {"committing", "unknown"}:
            outcome = await durable_row_exists(settings, HostJournalCommit, journal.id)
            if outcome == "persisted":
                await journal.finalize()
                result["result"] = "committed"
            elif outcome == "rolled_back":
                report = await journal.unwind()
                result.update(result="rolled_back", **report.details())
            else:
                journal.state = "unknown"
                journal._persist()
                result.update(result="unknown", manual_review_required=True)
        elif journal.state == "committed":
            await journal.finalize()
            result["result"] = "committed"
        else:
            result.update(result="needs_review", manual_review_required=True)
        logger.warning("host_journal.recovered", **result)
        results.append(result)
    return results
