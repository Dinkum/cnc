from __future__ import annotations

from fastapi import HTTPException


def apply_failure_detail(details: dict[str, object]) -> dict[str, object]:
    root_cause = apply_failure_root_cause(details)
    message = "save failed; existing config is still active"
    if root_cause:
        message = f"{message}: {root_cause}"
    return {
        "message": message,
        "apply": details,
    }


def raise_apply_failure(details: dict[str, object]) -> None:
    status_code = 409 if str(details.get("phase") or "") == "lock" else 500
    raise HTTPException(status_code=status_code, detail=apply_failure_detail(details))


def apply_failure_root_cause(details: dict[str, object]) -> str:
    findings = details.get("findings")
    if isinstance(findings, list):
        for severity in ("blocking", "warning"):
            for item in findings:
                if not isinstance(item, dict):
                    continue
                if str(item.get("severity") or "").strip().lower() != severity:
                    continue
                message = str(item.get("message") or "").strip()
                if message:
                    return message
    error = str(details.get("error") or "").strip()
    if error and error.lower() != "command failed":
        return error
    return ""
