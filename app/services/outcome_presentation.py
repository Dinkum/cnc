"""User-facing mutation outcomes derived from durable recovery facts.

Operation and apply records keep their precise internal states, including
``partial``.  UI cards, history, text output, and notifications use this module
to answer the narrower operator question: was the requested save retained?
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


OUTCOME_FACT_KEYS = (
    "commit_outcome",
    "failure_mode",
    "manual_review_required",
    "still_changed_tools",
    "commit_still_changed_tools",
    "unwind_errors",
    "commit_unwind_errors",
    "deferred",
)


@dataclass(frozen=True)
class MutationOutcomePresentation:
    """The terminal label and consequence shown outside diagnostic payloads."""

    status_label: str
    tone: str
    recovery_detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def mutation_outcome_facts(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy only the durable facts needed to reproduce presentation later."""

    facts = details or {}
    return {key: facts[key] for key in OUTCOME_FACT_KEYS if key in facts}


def _changed_tools(details: Mapping[str, Any]) -> list[str]:
    tools: list[str] = []
    for key in ("still_changed_tools", "commit_still_changed_tools"):
        values = details.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            name = str(value).strip()
            if name and name not in tools:
                tools.append(name)
    return tools


def _explicit_recovery_failure(details: Mapping[str, Any]) -> bool:
    return bool(
        _changed_tools(details)
        or details.get("unwind_errors")
        or details.get("commit_unwind_errors")
    )


def mutation_recovery_detail(details: Mapping[str, Any] | None) -> str:
    """Describe what a failed or interrupted mutation means for the operator."""

    facts = details or {}
    commit_outcome = str(facts.get("commit_outcome") or "").strip().lower()
    changed_tools = _changed_tools(facts)
    tool_detail = ", ".join(changed_tools)

    if commit_outcome == "persisted":
        if tool_detail:
            return (
                "The requested configuration was saved, but host recovery did not "
                f"finish for: {tool_detail}. Review host state before retrying."
            )
        if facts.get("unwind_errors") or facts.get("commit_unwind_errors"):
            return (
                "The requested configuration was saved, but CNC could not finish "
                "host recovery. Review host state before retrying."
            )
        return (
            "The requested configuration was saved and the host change completed. "
            "CNC will verify it again on the next convergence."
        )
    if commit_outcome == "unknown":
        return (
            "CNC could not confirm whether the requested configuration was saved. "
            "Compare the saved configuration with host state before retrying."
        )
    if tool_detail:
        return (
            f"CNC could not restore host changes for: {tool_detail}. "
            "Review host state before retrying."
        )
    if facts.get("deferred"):
        return (
            "No changes were attempted because a control-plane update was in "
            "progress. Retry after the update finishes."
        )
    if facts.get("manual_review_required") or str(
        facts.get("failure_mode") or ""
    ).strip().lower() == "partial":
        return (
            "CNC could not confirm that recovery restored the host. "
            "Review host state before retrying."
        )
    if commit_outcome == "rolled_back" or str(
        facts.get("failure_mode") or ""
    ).strip().lower() == "clean":
        return "The previous configuration remains saved and active."
    return "CNC did not confirm the requested configuration as saved."


def present_mutation_outcome(
    status: object,
    details: Mapping[str, Any] | None = None,
) -> MutationOutcomePresentation:
    """Map internal outcome state to the small vocabulary users act on."""

    normalized = str(status or "").strip().lower()
    facts = details or {}
    if normalized == "success":
        return MutationOutcomePresentation("saved", "success")
    if normalized in {"queued", "pending"}:
        return MutationOutcomePresentation("queued", "queued")
    if normalized == "running":
        return MutationOutcomePresentation("running", "queued")
    if normalized in {"error", "failed", "partial", "cancelled"}:
        recovery_detail = mutation_recovery_detail(facts)
        if (
            str(facts.get("commit_outcome") or "").strip().lower() == "persisted"
            and not _explicit_recovery_failure(facts)
        ):
            return MutationOutcomePresentation(
                "saved", "success", recovery_detail=recovery_detail
            )
        return MutationOutcomePresentation(
            "failed", "error", recovery_detail=recovery_detail
        )
    return MutationOutcomePresentation(normalized or "unknown", "inactive")
