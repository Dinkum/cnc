
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
    outputs: frozenset[str] | None = None
    reason: str = "full reconcile"

    @property
    def is_full(self) -> bool:
        return self.outputs is None

    def covers(self, output: str) -> bool:
        return self.is_full or output in (self.outputs or frozenset())

    def limit(self, names: set[str]) -> set[str]:
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
    desired: "DesiredState"
    settings: "Settings"
    services: Any = None
    scope: Scope = field(default_factory=Scope.everything)
    extras: Mapping[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)
    undo: list[tuple[str, Callable[[], Any]]] = field(default_factory=list)

    def touching(self, tool: str) -> None:
        self.touched.add(tool)

    def on_unwind(self, tool: str, restore: Callable[[], Any]) -> None:
        self.undo.append((tool, restore))

    def extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    async def report(self, phase: str, substate: str) -> None:
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
    directory.mkdir(parents=True, exist_ok=True)


def _owned(directory: Path, prefix: str) -> dict[Path, str]:
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
    _ensure_dir(directory)
    before = _owned(directory, prefix)
    changed: list[str] = []

    def restore() -> None:
        for path in list(directory.iterdir()) if directory.is_dir() else []:
            if path.name.startswith(prefix) and path not in before and path.is_file():
                path.unlink(missing_ok=True)
        for path, content in before.items():
            _write_atomic(path, content, mode)

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
                path.unlink()
                changed.append(path.name)
    except BaseException:
        restore()
        raise

    try:
        yield sorted(changed)
    except BaseException:
        restore()
        raise

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
    except BaseException:
        restore()
        raise

    if plan is not None:
        plan.on_unwind(tool or path.name, restore)



Tool = Callable[[Plan], Any]


class ApplyFailed(Exception):

    def __init__(self, tool: str, error: Exception, *, already_changed: list[str]):
        super().__init__(f"{tool}: {error}")
        self.tool = tool
        self.error = error
        self.already_changed = already_changed

    @property
    def partial(self) -> bool:
        return bool(self.already_changed)

    def as_details(self) -> dict[str, Any]:
        return {
            "failed_tool": self.tool,
            "error": str(self.error),
            "outcome": "partial" if self.partial else "clean",
            "already_changed": list(self.already_changed),
        }


async def converge(tools: list[Tool], plan: Plan, *, progress: Any = None) -> list[str]:
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
            raise _as_apply_failure(exc, name, tool_phases[0]) from exc
        _record_mutation(progress, plan, name, tool_phases)
        if progress is not None:
            for phase in tool_phases:
                progress.completed(phase)
    return changed


async def unwind_plan(plan: Plan) -> tuple[list[str], dict[str, str]]:
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
    from app.services.apply_core import ApplyFailed as HostApplyFailed

    if isinstance(exc, HostApplyFailed):
        return exc
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
