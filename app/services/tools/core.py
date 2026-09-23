"""Compare what is on the box against what we want, change only the difference.

The whole convergence model is three things:

``managed``
    A context manager. It writes the files whose bytes differ, deletes the ones
    that are no longer wanted, and puts everything back if the body raises. Every
    file-shaped tool uses it, so no tool ships its own copy of
    write, validate, activate, roll back.

a tool
    A plain function ``tool(plan) -> bool``. It returns whether it changed the
    host. Failure is an exception. Each tool owns its own mess and nothing else
    needs to understand it.

``converge``
    Runs the tools in list order and says what changed, or raises naming the
    tool that failed.

Nothing here caches host state. Reading a config file to compare it is far
cheaper than writing it and reloading the daemon that consumes it, so there is
never a reason to guess from a remembered hash whether the box is already
right. Ask the box.
"""

from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from app.logger import get_logger

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.apply_core import DesiredState

logger = get_logger("tools")


@dataclass(frozen=True)
class Scope:
    """Which outputs a change is allowed to touch.

    A save that renames one output has no business rebuilding another that
    happens to have drifted. Scope is derived from the requested change, not
    from a comparison against the last successful apply, so a small edit has a
    small justified blast radius.

    Shared resources are a deliberate exception. nginx renders its whole
    managed tree and the firewall renders its whole rule set, because those
    files are one artifact; what scope forbids is reaching into an unrelated
    output's runtime, accounts or guest filesystem.
    """

    outputs: frozenset[str] | None = None
    reason: str = "full reconcile"

    @property
    def is_full(self) -> bool:
        """True when every output is in scope, as for an explicit reconcile."""
        return self.outputs is None

    def covers(self, output: str) -> bool:
        return self.is_full or output in (self.outputs or frozenset())

    def limit(self, names: set[str]) -> set[str]:
        """Narrow a candidate set to what this change is allowed to touch."""
        if self.is_full:
            return set(names)
        return {name for name in names if self.covers(name)}

    def as_details(self) -> dict[str, Any]:
        return {
            "scope": "full" if self.is_full else sorted(self.outputs or ()),
            "scope_reason": self.reason,
        }

    @classmethod
    def everything(cls, reason: str = "full reconcile") -> "Scope":
        return cls(outputs=None, reason=reason)

    @classmethod
    def outputs_named(cls, names: set[str], reason: str) -> "Scope":
        return cls(outputs=frozenset(names), reason=reason)


@dataclass(frozen=True)
class Plan:
    """What the box should look like. The only argument a tool receives."""

    desired: "DesiredState"
    settings: "Settings"
    services: Any = None
    #: What this change may touch. Defaults to everything so an explicit
    #: whole-host reconcile needs no ceremony and a caller that forgets to
    #: narrow gets the safe, wide behaviour rather than a silent no-op.
    scope: Scope = field(default_factory=Scope.everything)
    extras: Mapping[str, Any] = field(default_factory=dict)
    #: Where tools record what they did, for the durable ApplyRun payload.
    #: Mutable on purpose: a tool writes its own key and nothing reads another
    #: tool's, so there is no shared structure to get out of step.
    details: dict[str, Any] = field(default_factory=dict)
    #: Tools that have begun changing the host. Recorded before the first
    #: mutation rather than after the last, so a tool that fails halfway is
    #: still counted. Reporting a half-changed box as untouched is the
    #: dangerous direction to be wrong in.
    touched: set[str] = field(default_factory=set)
    #: How to put each tool's change back, newest last. A tool registers this
    #: once it has actually changed something, so a later tool's failure can
    #: return the host to the state the database is about to roll back to.
    undo: list[tuple[str, Callable[[], Any]]] = field(default_factory=list)

    def touching(self, tool: str) -> None:
        """Declare that this tool is about to change the host."""
        self.touched.add(tool)

    def on_unwind(self, tool: str, restore: Callable[[], Any]) -> None:
        """Register how to undo what this tool just did.

        Only meaningful for changes that can be put back. Podman does not
        register one, because a container that has been removed and rebuilt is
        not restored by rewinding a file.
        """
        self.undo.append((tool, restore))

    def extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    async def report(self, phase: str, substate: str) -> None:
        """Tell the operator what this tool is doing right now.

        The text is user-facing and is matched against the progress card's step
        headlines, so it stays exactly as it was before the tool moved. A
        callback that raises must never fail the apply.
        """
        callback = getattr(self.services, "progress_callback", None)
        if callback is None:
            return
        try:
            result = callback(phase, substate)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "tools.progress_callback_failed", phase=phase, substate=substate
            )


# ---------------------------------------------------------------------------
# managed
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ensure_dir(directory: Path) -> None:
    """Make sure the managed directory exists before comparing against it.

    A tool owns its directory even when it currently wants no files there, so
    an unwritable location is a failure to report rather than a silent no-op.
    """
    directory.mkdir(parents=True, exist_ok=True)


def _owned(directory: Path, prefix: str) -> dict[Path, str]:
    """Current contents of every file in ``directory`` starting with ``prefix``."""
    if not directory.is_dir():
        return {}
    found: dict[Path, str] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.startswith(prefix) or not path.is_file():
            continue
        content = _read(path)
        if content is not None:
            found[path] = content
    return found


@contextmanager
def managed(
    directory: Path,
    prefix: str,
    wanted: Mapping[str, str],
    *,
    mode: int = 0o644,
    plan: "Plan | None" = None,
    tool: str = "",
    after_restore: Callable[[], Any] | None = None,
) -> Iterator[list[str]]:
    """Make ``directory`` hold exactly ``wanted`` for files starting with ``prefix``.

    Yields the list of filenames that actually changed, which is empty when the
    box was already right. If the body raises, every file is restored to the
    bytes it had on entry, including re-creating ones that were deleted and
    removing ones that were added.

    The prefix matters. It is what makes a deletion work: when an output goes
    away its rendered file simply stops appearing in ``wanted``, and this
    removes it. A tool that only ever wrote files would leave the old config
    behind forever.
    """
    _ensure_dir(directory)
    before = _owned(directory, prefix)
    changed: list[str] = []

    def restore() -> None:
        """Put the directory back exactly as it was found."""
        for path in list(directory.iterdir()) if directory.is_dir() else []:
            if path.name.startswith(prefix) and path not in before and path.is_file():
                path.unlink(missing_ok=True)
        for path, content in before.items():
            _write_atomic(path, content, mode)

    # The whole reconcile is inside the guard, not just the caller's body. A
    # failure partway through the writes used to leave the files it had already
    # replaced in place, so a save that could not finish still changed the box.
    try:
        for name in sorted(wanted):
            if not name.startswith(prefix):
                raise ValueError(
                    f"{name} does not start with the managed prefix {prefix}"
                )
            path = directory / name
            if before.get(path) == wanted[name]:
                continue
            _write_atomic(path, wanted[name], mode)
            changed.append(name)

        for path in before:
            if path.name not in wanted:
                # A delete that fails is a real failure. Swallowing it and
                # still reporting the file as changed told callers the box was
                # converged when the obsolete config was still being served.
                path.unlink()
                changed.append(path.name)
    except Exception:
        restore()
        raise

    try:
        yield sorted(changed)
    except Exception:
        restore()
        raise

    # Succeeded. Keep the undo available so a later tool's failure can put
    # these files back rather than leaving the host on a configuration the
    # database is about to roll back.
    if plan is not None and changed:

        def undo() -> Any:
            restore()
            return after_restore() if after_restore else None

        plan.on_unwind(tool or prefix, undo)


@contextmanager
def managed_file(
    path: Path,
    content: str,
    *,
    mode: int = 0o644,
    plan: "Plan | None" = None,
    tool: str = "",
) -> Iterator[bool]:
    """Own exactly one path, restoring it if the body raises.

    Deliberately not ``managed`` with a one-file map. That matches by prefix,
    which is right for a directory a tool owns outright and wrong here: it
    would treat ``shield.env.backup`` as an obsolete ``shield.env`` and delete
    a neighbour the tool does not own.
    """
    existed = path.exists()
    before = _read(path) if existed else None
    if existed and before is None:
        raise OSError(f"cannot read {path} to compare against desired content")
    if before == content:
        yield False
        return

    _write_atomic(path, content, mode)

    def restore() -> None:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, before, mode)

    try:
        yield True
    except Exception:
        restore()
        raise

    if plan is not None:
        plan.on_unwind(tool or path.name, restore)


# ---------------------------------------------------------------------------
# converge
# ---------------------------------------------------------------------------

Tool = Callable[[Plan], Any]


class ApplyFailed(Exception):
    """A tool raised. Names which one, and what had already changed."""

    def __init__(self, tool: str, error: Exception, *, already_changed: list[str]):
        super().__init__(f"{tool}: {error}")
        self.tool = tool
        self.error = error
        self.already_changed = already_changed

    @property
    def partial(self) -> bool:
        """Whether the box was left changed.

        A tool restores its own files before it raises, so the failing tool is
        never counted. Anything that finished before it is, because a container
        that has already been removed is not coming back on its own.
        """
        return bool(self.already_changed)

    def as_details(self) -> dict[str, Any]:
        return {
            "failed_tool": self.tool,
            "error": str(self.error),
            "outcome": "partial" if self.partial else "clean",
            "already_changed": list(self.already_changed),
        }


async def converge(tools: list[Tool], plan: Plan, *, progress: Any = None) -> list[str]:
    """Run every tool in order. Return the names of the ones that changed anything.

    Nothing here knows whether the user created, renamed or deleted something.
    Each tool renders from ``plan.desired`` and closes its own gap, which is why
    those are all the same code path.

    ``progress`` is the apply progress tracker when there is one. The executor
    drives it from each tool's declared phases, so a tool never hand-types a
    phase string and the two can no longer drift apart. Live mutation is read
    from ``plan.touched``, which a tool sets before its first change, so a tool
    that fails halfway is still counted as having touched the host.
    """
    changed: list[str] = []
    for tool in tools:
        name = getattr(tool, "tool_name", tool.__name__)
        tool_phases = getattr(tool, "phases", (name,))
        if progress is not None:
            progress.enter(tool_phases[0])
        try:
            result = tool(plan)
            if inspect.isawaitable(result):
                result = await result
            if result:
                changed.append(name)
        except Exception as exc:
            _record_mutation(progress, plan, name, tool_phases)
            # Deliberately does not unwind. Recovery is owned by whoever runs
            # this, because it has to stay available past the tool loop through
            # final verification and the database commit. Two owners meant the
            # second unwind found an empty stack and overwrote the first
            # result with "nothing was restored".
            raise _as_apply_failure(exc, name, tool_phases[0]) from exc
        _record_mutation(progress, plan, name, tool_phases)
        if progress is not None:
            for phase in tool_phases:
                progress.completed(phase)
    return changed


async def unwind_plan(plan: Plan) -> tuple[list[str], dict[str, str]]:
    """Put back what earlier tools changed, newest first.

    A save that fails rolls the database back, so leaving the host on the
    configuration the database no longer describes is the worst outcome: the
    two disagree and nothing says so. An unwind that itself fails is recorded
    rather than raised, so one bad restore cannot hide the others or replace
    the original cause.
    """
    unwound: list[str] = []
    errors: dict[str, str] = {}
    for tool, restore in reversed(plan.undo):
        try:
            result = restore()
            if inspect.isawaitable(result):
                await result
            unwound.append(tool)
        except Exception as exc:
            errors[tool] = str(exc)
    plan.undo.clear()
    return unwound, errors


async def unwind_recovery(recovery: list[Plan], prefix: str = "") -> dict[str, Any]:
    """Restore every plan in ``recovery``, and say what the host was left with.

    One helper for both places a change can be rejected after the tools have
    already run: after convergence but before the rows are durable, and after
    the commit itself failed. ``prefix`` only names which of the two it was, so
    a restore after a failed commit reads as ``commit_unwound_tools``.

    Reporting follows :func:`record_unwind_outcome`: a clean restore is not a
    partial failure and does not ask for manual review, because the host is
    back on the configuration the database still describes.
    """
    if not recovery:
        return {f"{prefix}unwind": "nothing to restore"}
    unwound: list[str] = []
    errors: dict[str, str] = {}
    touched: set[str] = set()
    for plan in recovery:
        touched |= set(plan.touched)
        plan_unwound, plan_errors = await unwind_plan(plan)
        unwound.extend(plan_unwound)
        errors.update(plan_errors)
    left = sorted(name for name in touched if name not in unwound)
    partial = bool(left or errors)
    details: dict[str, Any] = {
        f"{prefix}unwound_tools": unwound,
        f"{prefix}still_changed_tools": left,
        "failure_mode": "partial" if partial else "clean",
        "manual_review_required": partial,
    }
    if errors:
        details[f"{prefix}unwind_errors"] = errors
    return details


def record_unwind_outcome(
    failure: Any,
    unwound: list[str],
    errors: dict[str, str],
    touched: set[str] | None = None,
) -> None:
    """Say what was put back, and what the host was left holding.

    Derived from what each tool declared touching rather than from what it
    returned, so a tool that changed the host and then failed still counts.
    """
    left = sorted(name for name in (touched or set()) if name not in unwound)
    details = getattr(failure, "details", None)
    if not isinstance(details, dict):
        return
    details["unwound_tools"] = unwound
    details["still_changed_tools"] = left
    if errors:
        details["unwind_errors"] = errors
    partial = bool(left or errors)
    details["failure_mode"] = "partial" if partial else "clean"
    details["manual_review_required"] = partial


def _as_apply_failure(exc: Exception, tool: str, phase: str) -> Exception:
    """Keep a tool's own failure intact, and give a bare one a phase.

    A tool that raises ``ApplyFailed`` has already chosen the phase and the
    details it wants published, so re-wrapping it would replace a specific
    cause with the tool's name. Anything else gets the tool's first phase so
    the failure is still reported under a name clients recognise.
    """
    from app.services.apply_core import ApplyFailed as HostApplyFailed

    if isinstance(exc, HostApplyFailed):
        return exc
    # Keep the cause in the message. Reporting "tailscale failed" while the
    # real reason sat in a details field is how an operator ends up debugging
    # the wrong thing.
    return HostApplyFailed(
        f"{tool} failed: {exc}", phase=phase, details={"error": str(exc)}
    )


def _record_mutation(
    progress: Any, plan: Plan, name: str, tool_phases: tuple[str, ...]
) -> None:
    if progress is None or name not in plan.touched:
        return
    for phase in tool_phases:
        progress.live_mutation(phase)
