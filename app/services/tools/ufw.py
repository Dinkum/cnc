"""ufw: restrict public HTTP to Cloudflare.

Converges as part of apply like every other tool. The hourly timer's only job
is to fetch Cloudflare's current CIDR list into settings; it no longer writes
the host itself, which is what made it a second convergence engine.

It keeps the part that is genuinely its own: it does not own its files
outright. It rewrites a section of ufw's ``user.rules`` around rules ufw itself
manages, so it renders the full file from the current one and then compares,
rather than rendering from scratch.
"""

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
#: Phase strings this tool owns. Published to clients in apply details and
#: matched by the progress card, so they keep their existing spelling.
phases = ("ufw",)


def applies(plan: Plan) -> bool:
    return bool(getattr(plan.settings, "nginx_cloudflare_only", False))


def ufw(plan: Plan) -> bool:
    if not applies(plan):
        return False

    # Read from desired state, the same place nginx reads them. The hourly
    # timer's only remaining job is to fetch the current list into settings.
    cidrs = tuple(plan.extra("cloudflare_cidrs") or plan.settings.cloudflare_ip_list)
    if not cidrs:
        # No CIDR list means the fetch has not run yet. Leaving the firewall
        # exactly as it is beats rewriting it to allow nobody, and there is no
        # reason to demand ufw be up when there is nothing to write.
        plan.details["ufw_skipped"] = True
        plan.details["ufw_skip_reason"] = "no Cloudflare CIDR list in desired state"
        return False

    v4 = Path(plan.extra("ufw_user_rules_path") or UFW_USER_RULES_PATH)
    v6 = Path(plan.extra("ufw_user6_rules_path") or UFW_USER6_RULES_PATH)
    if not v4.exists() or not v6.exists():
        # ufw is not managing rule files on this host. Recording that beats
        # failing every save, which is what raising here would do now that
        # this runs on the normal apply path rather than an hourly timer.
        plan.details["ufw_skipped"] = True
        plan.details["ufw_skip_reason"] = "ufw rule files are not present"
        return False

    before = (v4.read_text(encoding="utf-8"), v6.read_text(encoding="utf-8"))
    policy = build_cloudflare_ingress_policy(cidrs)
    if not _rules_files_are_ours_to_edit(before):
        # This tool rewrites a section inside ufw's own file rather than owning
        # the file outright, so it can only work on a file in the shape ufw
        # writes. Anything else is recorded and left alone: refusing to guess
        # is right, and failing every unrelated save over it is not.
        plan.details["ufw_blocked"] = True
        plan.details["ufw_blocked_reason"] = (
            "ufw rules files are not in the expected managed format"
        )
        return False

    # Compare before demanding anything of the host. A save that does not
    # change the firewall must not fail because the firewall is down: that
    # would turn every unrelated edit into a firewall outage report. When there
    # is real work, an inactive ufw is a genuine blocker and is raised.
    result = rewrite_cloudflare_http_firewall_files(
        policy, user_rules_path=v4, user6_rules_path=v6
    )
    if not _rewrote_anything(result):
        plan.details["ufw_unchanged"] = True
        return False

    try:
        _require_active(plan)
    except Exception:
        # Put the rendered bytes back; we are not going to activate them.
        v4.write_text(before[0], encoding="utf-8")
        v6.write_text(before[1], encoding="utf-8")
        raise

    plan.touching(tool_name)

    try:
        _run(plan, ["ufw", "reload"])
        # Reading the live iptables chain back is the point. Rendered files
        # that look right but do not actually block anything is the failure
        # this tool exists to prevent.
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
    """Whether both rule files carry the section markers the rewriter needs."""
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
    """Use the injected command runner so this talks to the same host as apply."""
    return getattr(plan.services.runtime_services, "run_command", run_command)


def _run(plan: Plan, command: list[str], *, check: bool = True) -> None:
    result = _runner(plan)(command, timeout_sec=plan.settings.command_timeout_apply_sec)
    if check and not result.ok:
        raise RuntimeError(result.stderr or result.stdout or " ".join(command))
