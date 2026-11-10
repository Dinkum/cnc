
from __future__ import annotations

from pathlib import Path

from app.services.cloudflare_ingress import (
    RULES_SECTION_FOOTER,
    RULES_SECTION_HEADER,
    UFW_STATUS_ACTIVE_PREFIX,
    UFW_USER6_RULES_PATH,
    UFW_USER_RULES_PATH,
    audit_live_cloudflare_http_firewall,
    build_cloudflare_ingress_policy,
    rewrite_cloudflare_http_firewall_files,
)
from app.services.commands import run_command
from app.services.tools.core import Plan

tool_name = "ufw"
phases = ("ufw",)


def applies(plan: Plan) -> bool:
    return bool(getattr(plan.settings, "nginx_cloudflare_only", False))


def ufw(plan: Plan) -> bool:
    if not applies(plan):
        return False

    cidrs = tuple(plan.extra("cloudflare_cidrs") or plan.settings.cloudflare_ip_list)
    if not cidrs:
        plan.details["ufw_skipped"] = True
        plan.details["ufw_skip_reason"] = "no Cloudflare CIDR list in desired state"
        return False

    v4 = Path(plan.extra("ufw_user_rules_path") or UFW_USER_RULES_PATH)
    v6 = Path(plan.extra("ufw_user6_rules_path") or UFW_USER6_RULES_PATH)
    if not v4.exists() or not v6.exists():
        plan.details["ufw_skipped"] = True
        plan.details["ufw_skip_reason"] = "ufw rule files are not present"
        return False

    before = (v4.read_text(encoding="utf-8"), v6.read_text(encoding="utf-8"))
    policy = build_cloudflare_ingress_policy(cidrs)
    if not _rules_files_are_ours_to_edit(before):
        plan.details["ufw_blocked"] = True
        plan.details["ufw_blocked_reason"] = (
            "ufw rules files are not in the expected managed format"
        )
        return False

    result = rewrite_cloudflare_http_firewall_files(
        policy, user_rules_path=v4, user6_rules_path=v6
    )
    if not _rewrote_anything(result):
        plan.details["ufw_unchanged"] = True
        return False

    try:
        _require_active(plan)
    except Exception:
        v4.write_text(before[0], encoding="utf-8")
        v6.write_text(before[1], encoding="utf-8")
        raise

    plan.touching(tool_name)

    try:
        _run(plan, ["ufw", "reload"])
        audit_live_cloudflare_http_firewall(
            policy,
            run_command_func=_runner(plan),
            timeout_sec=plan.settings.command_timeout_apply_sec,
        )
    except Exception:
        v4.write_text(before[0], encoding="utf-8")
        v6.write_text(before[1], encoding="utf-8")
        _run(plan, ["ufw", "reload"], check=False)
        raise
    return True


def _rules_files_are_ours_to_edit(contents: tuple[str, str]) -> bool:
    return all(
        RULES_SECTION_HEADER in text and RULES_SECTION_FOOTER in text
        for text in contents
    )


def _rewrote_anything(result: dict) -> bool:
    return any(
        int(value or 0)
        for key, value in result.items()
        if key.endswith("_rules_written") or key.endswith("_rules_removed")
    ) or bool(result.get("changed"))


def _require_active(plan: Plan) -> None:
    result = _runner(plan)(
        ["ufw", "status"], timeout_sec=plan.settings.command_timeout_status_sec
    )
    if not result.ok or UFW_STATUS_ACTIVE_PREFIX not in result.stdout:
        raise RuntimeError("ufw is not active while nginx_cloudflare_only is enabled")


def _runner(plan: Plan):
    return getattr(plan.services.runtime_services, "run_command", run_command)


def _run(plan: Plan, command: list[str], *, check: bool = True) -> None:
    result = _runner(plan)(command, timeout_sec=plan.settings.command_timeout_apply_sec)
    if check and not result.ok:
        raise RuntimeError(result.stderr or result.stdout or " ".join(command))
