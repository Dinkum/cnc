from __future__ import annotations

import argparse

from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cloudflare = subparsers.add_parser("cloudflare")
    cloudflare_subparsers = cloudflare.add_subparsers(
        dest="cloudflare_command", required=True
    )
    cloudflare_sync = cloudflare_subparsers.add_parser("sync")
    cloudflare_sync.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        cloudflare_sync,
        handler=_run_cloudflare_sync_command,
        formatter=format_cloudflare_sync_text,
        needs_settings=True,
    )


def format_cloudflare_sync_text(payload: dict[str, object]) -> str:
    firewall = (
        payload.get("firewall") if isinstance(payload.get("firewall"), dict) else {}
    )
    apply_payload = (
        payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    )
    rollback = (
        payload.get("rollback") if isinstance(payload.get("rollback"), dict) else {}
    )
    preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
    lines = [
        f"checked_at: {payload.get('checked_at') or '-'}",
        f"ok: {'yes' if payload.get('ok') else 'no'}",
        f"cloudflare_only: {'yes' if payload.get('cloudflare_only') else 'no'}",
        f"changed: {'yes' if payload.get('changed') else 'no'}",
        f"cidr_count: {payload.get('fetched_cidr_count') or payload.get('previous_cidr_count') or 0}",
        f"preview_checked: {'yes' if preview.get('checked') else 'no'}",
        f"managed_env_updated: {'yes' if payload.get('managed_env_updated') else 'no'}",
        f"firewall_reconciled: {'yes' if firewall and not firewall.get('skipped') else 'no'}",
        f"firewall_rule_count: {firewall.get('live_rule_count') if firewall else '-'}",
        f"apply_status: {apply_payload.get('status') or '-'}",
        f"rollback_ok: {'yes' if rollback.get('ok') else ('no' if rollback else '-')}",
    ]
    status = str(payload.get("status") or "").strip()
    if status:
        lines.insert(1, f"status: {status}")
    if payload.get("deferred"):
        lines.append("deferred: yes")
        deferred_reason = str(payload.get("deferred_reason") or "").strip()
        if deferred_reason:
            lines.append(f"deferred_reason: {deferred_reason}")
        blocker = payload.get("blocker")
        if isinstance(blocker, dict):
            blocker_kind = str(blocker.get("kind") or "host_mutation")
            blocker_id = blocker.get("id")
            blocker_status = str(blocker.get("status") or "unknown")
            blocker_phase = str(blocker.get("phase") or "unknown")
            blocker_label = (
                f"{blocker_kind} #{blocker_id}"
                if blocker_id is not None
                else blocker_kind
            )
            lines.append(
                f"blocker: {blocker_label} ({blocker_status}, {blocker_phase})"
            )
    if rollback:
        lines.append(
            f"rollback_firewall_restored: {'yes' if rollback.get('firewall_restored') else 'no'}"
        )
        lines.append(
            f"rollback_apply_restored: {'yes' if rollback.get('apply_restored') else 'no'}"
        )
    error = str(payload.get("error") or "").strip()
    if error:
        lines.append(f"error: {error}")
    failed_phase = str(payload.get("failed_phase") or "").strip()
    if failed_phase:
        lines.append(f"failed_phase: {failed_phase}")
    preview_reason = str(preview.get("reason") or "").strip()
    if preview_reason:
        lines.append(f"preview_reason: {preview_reason}")
    return "\n".join(lines)


async def cloudflare_sync_async(settings: Settings) -> CommandResult:
    from app.services.cloudflare_sync import run_cloudflare_sync

    try:
        payload = await run_cloudflare_sync(settings)
    except Exception as exc:
        return 1, None, str(exc)
    if payload.get("status") == "deferred":
        return 0, payload, None
    apply_payload = (
        payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    )
    if apply_payload and apply_payload.get("status") not in {None, "success"}:
        return 2, payload, None
    return (0 if payload.get("ok") else 2), payload, None


async def _run_cloudflare_sync_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await cloudflare_sync_async(settings)
