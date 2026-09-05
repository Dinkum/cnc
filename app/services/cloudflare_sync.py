from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from urllib.request import Request, urlopen

from app.config import Settings
from app.logger import get_logger
from app.services import cloudflare_ingress
from app.services.host_state import (
    LockedStateSectionTransaction,
    host_state_lock_path,
)
from app.services.commands import CommandError, command_result_is_retryable, run_command
from app.services.managed_env import apply_managed_env_updates
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
)
from app.services.operations import HostMutationLockError, host_mutation_operation
from app.services.retry import retry_call


RULE_NUMBER_RE = re.compile(r"^\[\s*(?P<number>\d+)\]\s+(?P<rule>.+)$")
UFW_STATUS_ACTIVE_PREFIX = "Status: active"
SYNC_ENV_KEY = "NGINX_CLOUDFLARE_IPS"
SYNC_USER_AGENT = "cnc-cloudflare-sync/1.0"

logger = get_logger("cloudflare.sync")
HOST_STATE_SECTION = "cloudflare_sync"
CONTROL_PLANE_UPDATE_DEFERRED_REASON = "control_plane_update_running"


def _record_phase(
    payload: dict[str, Any], phase: str, *, status: str, **details: Any
) -> None:
    phases = payload.setdefault("phases", {})
    if not isinstance(phases, dict):
        phases = {}
        payload["phases"] = phases
    phases[phase] = {"status": status, **details}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cloudflare_sync_state_lock_path(path: Path) -> Path:
    return host_state_lock_path(path)


def _log_cloudflare_sync_state_written(path: Path, payload: dict[str, Any]) -> None:
    logger.info(
        "cloudflare.sync.state.written",
        path=str(path),
        key_count=len(payload),
    )


def _cloudflare_sync_state_transaction(
    settings: Settings,
) -> LockedStateSectionTransaction:
    return LockedStateSectionTransaction(
        settings.host_state_path,
        section=HOST_STATE_SECTION,
    )


def _parse_cloudflare_cidrs(raw_text: str) -> tuple[str, ...]:
    values = re.split(r"[\s,]+", raw_text.strip())
    return cloudflare_ingress.build_cloudflare_ingress_policy(values).cidrs


def _fetch_cloudflare_cidrs_once(
    settings: Settings,
    *,
    urlopen_func=urlopen,
) -> tuple[str, ...]:
    cidrs: list[str] = []
    for source_url in (settings.cloudflare_ips_v4_url, settings.cloudflare_ips_v6_url):
        request = Request(
            source_url,
            headers={
                "Accept": "text/plain",
                "User-Agent": SYNC_USER_AGENT,
            },
        )
        with urlopen_func(
            request, timeout=settings.cloudflare_sync_timeout_sec
        ) as response:
            body = response.read().decode("utf-8", errors="replace")
        cidrs.extend(_parse_cloudflare_cidrs(body))
    normalized: list[str] = []
    seen: set[str] = set()
    for cidr in cidrs:
        if cidr in seen:
            continue
        seen.add(cidr)
        normalized.append(cidr)
    return tuple(normalized)


def fetch_cloudflare_cidrs(
    settings: Settings,
    *,
    urlopen_func=urlopen,
    sleep_func=time.sleep,
) -> tuple[str, ...]:
    attempts = settings.cloudflare_sync_fetch_retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_cloudflare_cidrs_once(settings, urlopen_func=urlopen_func)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            backoff_sec = settings.cloudflare_sync_fetch_retry_backoff_sec * attempt
            logger.warning(
                "cloudflare.sync.fetch.retrying",
                attempt=attempt,
                max_attempts=attempts,
                backoff_sec=backoff_sec,
                error=str(exc),
            )
            if backoff_sec > 0:
                sleep_func(backoff_sec)
    assert last_error is not None
    raise RuntimeError(
        f"failed to fetch Cloudflare CIDRs after {attempts} attempts: {last_error}"
    ) from last_error


def _run_command_checked(
    command: list[str],
    *,
    timeout_sec: int,
    retries: int = 0,
    backoff_sec: float = 0.0,
) -> str:
    attempts = retries + 1

    def should_retry(exc: Exception) -> bool:
        return isinstance(exc, CommandError) and command_result_is_retryable(exc.result)

    def log_retry(
        exc: Exception, attempt: int, max_attempts: int, delay_sec: float
    ) -> None:
        logger.warning(
            "cloudflare.sync.command.retrying",
            command=" ".join(command),
            attempt=attempt,
            max_attempts=max_attempts,
            backoff_sec=delay_sec,
            error=str(exc),
        )

    def run_checked_once() -> str:
        result = run_command(command, timeout_sec=timeout_sec)
        if not result.ok:
            raise CommandError(result)
        return result.stdout

    try:
        return retry_call(
            run_checked_once,
            attempts=attempts,
            backoff_sec=backoff_sec,
            should_retry=should_retry,
            on_retry=log_retry,
        )
    except CommandError as exc:
        raise RuntimeError(
            exc.result.stderr
            or exc.result.stdout
            or f"command failed: {' '.join(command)}"
        ) from exc


def _ufw_status_lines(settings: Settings) -> list[str]:
    status_text = _run_command_checked(
        ["ufw", "status", "numbered"], timeout_sec=settings.command_timeout_status_sec
    )
    lines = [line.rstrip() for line in status_text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith(UFW_STATUS_ACTIVE_PREFIX):
        raise RuntimeError(
            "ufw must be active before Cloudflare CIDR sync can reconcile HTTP/S rules"
        )
    return lines


def _http_rule_numbers(status_lines: list[str]) -> list[int]:
    numbers: list[int] = []
    for line in status_lines:
        match = RULE_NUMBER_RE.match(line.strip())
        if match is None:
            continue
        rule_text = match.group("rule")
        if any(f"{port}/tcp" in rule_text for port in cloudflare_ingress.HTTP_PORTS):
            numbers.append(int(match.group("number")))
    return sorted(numbers, reverse=True)


def _rewrite_cloudflare_http_firewall_files(
    settings: Settings, *, cidrs: tuple[str, ...]
) -> dict[str, Any]:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(cidrs)
    return cloudflare_ingress.rewrite_cloudflare_http_firewall_files(policy)


def _last_known_good_cidrs(settings: Settings) -> tuple[str, ...]:
    if settings.cloudflare_ip_list:
        return settings.cloudflare_ip_list

    state_payload = read_cloudflare_sync_state(settings)
    raw_cached = state_payload.get("last_successful_cidrs")
    if not isinstance(raw_cached, list):
        return ()
    try:
        return cloudflare_ingress.build_cloudflare_ingress_policy(raw_cached).cidrs
    except ValueError:
        logger.warning(
            "cloudflare.sync.cached_cidrs.invalid", path=str(settings.host_state_path)
        )
        return ()


async def _load_desired_state_for_sync(settings: Settings):
    from app.database import SessionLocal
    from app.services.apply_state import build_desired_state

    async with SessionLocal() as session:
        return await build_desired_state(session, settings)


def _desired_cluster_ingress_cidrs(desired: Any) -> tuple[str, ...]:
    cidrs: list[str] = []
    for node in getattr(desired, "cluster_nodes", ()) or ():
        if not isinstance(node, dict):
            continue
        tailnet_ip = str(node.get("tailnet_ip") or "").strip()
        if tailnet_ip:
            cidrs.append(tailnet_ip)
    return tuple(cidrs)


def _validate_generated_nginx_preview(
    nginx_files: dict[str, str],
    *,
    cidrs: tuple[str, ...],
    extra_ingress_cidrs: tuple[str, ...] = (),
) -> dict[str, Any]:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        (*cidrs, *extra_ingress_cidrs)
    )
    for filename, content in nginx_files.items():
        try:
            cloudflare_ingress.audit_rendered_cloudflare_nginx_acl(policy, content)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}: {filename}") from exc
    return {
        "checked": True,
        "file_count": len(nginx_files),
        "cidr_count": len(cidrs),
        "extra_ingress_cidr_count": len(extra_ingress_cidrs),
    }


def _validate_firewall_preview(*, cidrs: tuple[str, ...]) -> dict[str, Any]:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(cidrs)
    with tempfile.TemporaryDirectory(prefix="cnc-cloudflare-preview.") as temp_dir:
        preview_root = Path(temp_dir)
        preview_v4 = preview_root / "user.rules"
        preview_v6 = preview_root / "user6.rules"
        shutil.copy2(cloudflare_ingress.UFW_USER_RULES_PATH, preview_v4)
        shutil.copy2(cloudflare_ingress.UFW_USER6_RULES_PATH, preview_v6)
        rewrite_details = cloudflare_ingress.rewrite_cloudflare_http_firewall_files(
            policy,
            user_rules_path=preview_v4,
            user6_rules_path=preview_v6,
        )
        audit_details = cloudflare_ingress.audit_rendered_cloudflare_http_firewall(
            policy,
            user_rules_path=preview_v4,
            user6_rules_path=preview_v6,
        )
    return {
        **rewrite_details,
        **audit_details,
    }


async def _preview_cloudflare_sync(
    settings: Settings, *, cidrs: tuple[str, ...]
) -> dict[str, Any]:
    if not settings.nginx_cloudflare_only:
        return {
            "checked": False,
            "reason": "nginx_cloudflare_only_disabled",
            "cidrs": list(cidrs),
            "nginx": {"checked": False, "reason": "nginx_cloudflare_only_disabled"},
            "firewall": {"checked": False, "reason": "nginx_cloudflare_only_disabled"},
            "nginx_files": [],
        }

    desired = await _load_desired_state_for_sync(settings)
    extra_ingress_cidrs = _desired_cluster_ingress_cidrs(desired)
    nginx_preview = _validate_generated_nginx_preview(
        desired.nginx_files,
        cidrs=cidrs,
        extra_ingress_cidrs=extra_ingress_cidrs,
    )
    firewall_preview = _validate_firewall_preview(cidrs=cidrs)
    return {
        "checked": True,
        "cidrs": list(cidrs),
        "nginx": nginx_preview,
        "firewall": firewall_preview,
        "nginx_files": sorted(desired.nginx_files.keys()),
    }


def reconcile_cloudflare_http_firewall(
    settings: Settings, *, cidrs: tuple[str, ...]
) -> dict[str, Any]:
    if not settings.nginx_cloudflare_only:
        return {
            "skipped": True,
            "reason": "nginx_cloudflare_only_disabled",
            "deleted_rule_numbers": [],
            "added_rule_count": 0,
        }

    if not cidrs:
        raise RuntimeError(
            "Cloudflare CIDR sync requires at least one CIDR when nginx_cloudflare_only is enabled"
        )

    policy = cloudflare_ingress.build_cloudflare_ingress_policy(cidrs)
    before_lines = _ufw_status_lines(settings)
    deleted_rule_numbers = _http_rule_numbers(before_lines)
    original_v4 = cloudflare_ingress.UFW_USER_RULES_PATH.read_text(encoding="utf-8")
    original_v6 = cloudflare_ingress.UFW_USER6_RULES_PATH.read_text(encoding="utf-8")
    rewrite_completed = False
    files_changed = False
    live_reload_attempted = False
    try:
        rewrite_details = _rewrite_cloudflare_http_firewall_files(settings, cidrs=cidrs)
        rewrite_completed = True
        files_changed = bool(rewrite_details.get("changed"))
        rendered_audit_details = (
            cloudflare_ingress.audit_rendered_cloudflare_http_firewall(
                policy,
                user_rules_path=cloudflare_ingress.UFW_USER_RULES_PATH,
                user6_rules_path=cloudflare_ingress.UFW_USER6_RULES_PATH,
            )
        )
        if files_changed:
            live_reload_attempted = True
            _run_command_checked(
                ["ufw", "reload"],
                timeout_sec=settings.command_timeout_apply_sec,
                retries=settings.transient_command_retry_attempts,
                backoff_sec=settings.transient_command_retry_backoff_sec,
            )
            audit_details = cloudflare_ingress.audit_live_cloudflare_http_firewall(
                policy,
                run_command_func=run_command,
                timeout_sec=settings.command_timeout_status_sec,
            )
        else:
            try:
                audit_details = cloudflare_ingress.audit_live_cloudflare_http_firewall(
                    policy,
                    run_command_func=run_command,
                    timeout_sec=settings.command_timeout_status_sec,
                )
            except Exception as audit_exc:
                logger.warning(
                    "cloudflare.sync.firewall_live_audit_failed_reloading",
                    error=str(audit_exc),
                )
                live_reload_attempted = True
                _run_command_checked(
                    ["ufw", "reload"],
                    timeout_sec=settings.command_timeout_apply_sec,
                    retries=settings.transient_command_retry_attempts,
                    backoff_sec=settings.transient_command_retry_backoff_sec,
                )
                audit_details = cloudflare_ingress.audit_live_cloudflare_http_firewall(
                    policy,
                    run_command_func=run_command,
                    timeout_sec=settings.command_timeout_status_sec,
                )
    except Exception as exc:
        rollback_errors: list[str] = []
        if rewrite_completed and files_changed:
            try:
                cloudflare_ingress.restore_cloudflare_http_firewall_files(
                    user_rules_text=original_v4,
                    user6_rules_text=original_v6,
                )
            except Exception as rollback_exc:
                rollback_errors.append(f"file_restore_failed: {rollback_exc}")
        if live_reload_attempted and files_changed:
            try:
                _run_command_checked(
                    ["ufw", "reload"],
                    timeout_sec=settings.command_timeout_apply_sec,
                    retries=settings.transient_command_retry_attempts,
                    backoff_sec=settings.transient_command_retry_backoff_sec,
                )
            except Exception as rollback_exc:
                rollback_errors.append(f"ufw_reload_failed: {rollback_exc}")
        if rollback_errors:
            logger.warning(
                "cloudflare.sync.firewall_rollback_failed",
                error=str(exc),
                rollback_errors=rollback_errors,
            )
            raise RuntimeError(
                f"{exc}; rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise

    return {
        "skipped": False,
        "deleted_rule_numbers": deleted_rule_numbers,
        "added_rule_count": len(cidrs) * len(cloudflare_ingress.HTTP_PORTS),
        **rewrite_details,
        **rendered_audit_details,
        **audit_details,
        "reload_attempted": live_reload_attempted,
    }


def write_cloudflare_sync_state(settings: Settings, payload: dict[str, Any]) -> None:
    with _cloudflare_sync_state_transaction(settings) as transaction:
        transaction.replace(payload)
        _log_cloudflare_sync_state_written(settings.host_state_path, payload)


def read_cloudflare_sync_state(settings: Settings) -> dict[str, Any]:
    with _cloudflare_sync_state_transaction(settings) as transaction:
        return dict(transaction.value)


def _settings_with_cloudflare_cidrs(
    settings: Settings, *, cidrs: tuple[str, ...]
) -> Settings:
    return settings.model_copy(update={"nginx_cloudflare_ips": ",".join(cidrs)})


async def _run_apply_for_sync(settings: Settings) -> tuple[dict[str, Any], bool]:
    from app.database import SessionLocal
    from app.services.apply_service import run_apply

    async with SessionLocal() as session:
        response = await run_apply(session, settings)
    return json.loads(response.model_dump_json()), response.status == "success"


async def _notify_cloudflare_sync_failure(
    settings: Settings,
    *,
    phase: str,
    error: str,
    rollback: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    details = payload if isinstance(payload, dict) else {}
    firewall = (
        details.get("firewall") if isinstance(details.get("firewall"), dict) else {}
    )
    rollback_status = "not needed"
    if rollback:
        rollback_status = "ok" if rollback.get("ok") else "failed"
    diagnosis = _classify_cloudflare_sync_failure(phase=phase, error=error)
    facts: list[tuple[str, object]] = [
        ("phase", phase),
        ("diagnosis", diagnosis),
        ("changed", "yes" if details.get("changed") else "no"),
        (
            "cidrs",
            f"{details.get('fetched_cidr_count') or 0} fetched, "
            f"{details.get('previous_cidr_count') or 0} previous",
        ),
        ("firewall", _summarize_firewall_failure_state(firewall)),
        ("rollback", rollback_status),
        ("error", error),
    ]
    return await send_pushover_notification_async(
        settings,
        title="CNC Cloudflare sync failed",
        message=build_operator_message(
            "Cloudflare IP sync failed.",
            status="failure",
            facts=facts,
            action="cnc-admin cloudflare sync --json",
        ),
        priority=0,
        event="cloudflare_sync_failed",
        source="cloudflare.sync",
        url=admin_dashboard_url(settings, tab="settings"),
        url_title="Open settings",
    )


def _classify_cloudflare_sync_failure(*, phase: str, error: str) -> str:
    normalized = error.lower()
    if (
        phase == "firewall"
        and "read-only file system" in normalized
        and "/etc/ufw" in error
    ):
        return "ufw_rules_read_only"
    if phase == "preview":
        return "preview_validation_failed"
    if phase == "fetch":
        return "cloudflare_cidr_fetch_failed"
    if phase == "apply":
        return "apply_failed"
    if phase == "persist_env":
        return "managed_env_update_failed"
    return ""


def _summarize_firewall_failure_state(firewall: dict[str, Any]) -> str:
    if not firewall:
        return "not reached"
    if firewall.get("skipped"):
        return f"skipped ({firewall.get('reason') or 'no reason'})"
    if firewall.get("changed") is False:
        return "audit only; files already matched"
    if firewall.get("changed") is True:
        return "files changed"
    return "started"


async def _rollback_cloudflare_sync(
    previous_settings: Settings,
    *,
    restore_firewall: bool,
    restore_apply: bool,
) -> dict[str, Any]:
    rollback: dict[str, Any] = {
        "ok": True,
        "restore_firewall": restore_firewall,
        "restore_apply": restore_apply,
    }

    if restore_firewall:
        try:
            rollback["firewall"] = reconcile_cloudflare_http_firewall(
                previous_settings,
                cidrs=previous_settings.cloudflare_ip_list,
            )
            rollback["firewall_restored"] = True
        except Exception as exc:
            rollback["ok"] = False
            rollback["firewall_restored"] = False
            rollback["firewall_error"] = str(exc)

    if restore_apply:
        try:
            apply_payload, apply_ok = await _run_apply_for_sync(previous_settings)
            rollback["apply"] = apply_payload
            rollback["apply_restored"] = apply_ok
            rollback["ok"] = rollback["ok"] and apply_ok
        except Exception as exc:
            rollback["ok"] = False
            rollback["apply_restored"] = False
            rollback["apply_error"] = str(exc)

    return rollback


async def run_cloudflare_sync(settings: Settings) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checked_at": _utc_now_iso(),
        "cloudflare_only": settings.nginx_cloudflare_only,
        "source_urls": [settings.cloudflare_ips_v4_url, settings.cloudflare_ips_v6_url],
        "previous_cidr_count": len(settings.cloudflare_ip_list),
        "changed": False,
        "phases": {},
    }
    current_phase = "fetch"
    logger.info(
        "cloudflare.sync.started",
        cloudflare_only=settings.nginx_cloudflare_only,
        previous_cidr_count=len(settings.cloudflare_ip_list),
    )

    try:
        try:
            fetched_cidrs = fetch_cloudflare_cidrs(settings)
            payload["fetch_source"] = "remote"
            payload["used_cached_cidrs"] = False
        except Exception as exc:
            cached_cidrs = _last_known_good_cidrs(settings)
            if not cached_cidrs:
                raise
            fetched_cidrs = cached_cidrs
            payload["fetch_source"] = "last_known_good"
            payload["used_cached_cidrs"] = True
            payload["fetch_warning"] = str(exc)
            logger.warning(
                "cloudflare.sync.fetch.failed_using_last_known_good",
                error=str(exc),
                cidr_count=len(fetched_cidrs),
            )
        payload["fetched_cidr_count"] = len(fetched_cidrs)
        payload["fetched_cidrs"] = list(fetched_cidrs)
        _record_phase(
            payload,
            "fetch",
            status="ok" if not payload.get("used_cached_cidrs") else "fallback",
            cidr_count=len(fetched_cidrs),
            source=payload.get("fetch_source"),
            error=payload.get("fetch_warning"),
        )

        preview_settings = _settings_with_cloudflare_cidrs(
            settings, cidrs=fetched_cidrs
        )
        current_phase = "preview"
        payload["preview"] = await _preview_cloudflare_sync(
            preview_settings, cidrs=fetched_cidrs
        )
        _record_phase(
            payload,
            "preview",
            status="ok",
            checked=bool(payload["preview"].get("checked")),
            reason=payload["preview"].get("reason"),
        )

        if not settings.nginx_cloudflare_only:
            payload["firewall"] = {
                "skipped": True,
                "reason": "nginx_cloudflare_only_disabled",
                "deleted_rule_numbers": [],
                "added_rule_count": 0,
            }
            _record_phase(
                payload,
                "firewall",
                status="skipped",
                reason="nginx_cloudflare_only_disabled",
            )
            _record_phase(
                payload,
                "apply",
                status="skipped",
                reason="nginx_cloudflare_only_disabled",
            )
            if fetched_cidrs != settings.cloudflare_ip_list:
                payload["changed"] = True
                current_phase = "persist_env"
                settings = apply_managed_env_updates(
                    settings, {SYNC_ENV_KEY: ",".join(fetched_cidrs)}
                )
                payload["managed_env_updated"] = True
                payload["updated_at"] = _utc_now_iso()
                _record_phase(
                    payload, "persist_env", status="ok", cidr_count=len(fetched_cidrs)
                )
            else:
                payload["managed_env_updated"] = False
                _record_phase(
                    payload, "persist_env", status="skipped", reason="cidrs_unchanged"
                )
            payload["ok"] = True
            write_cloudflare_sync_state(settings, payload)
            logger.info(
                "cloudflare.sync.completed",
                changed=payload["changed"],
                cidr_count=payload["fetched_cidr_count"],
            )
            return payload

        # Context entry can defer before yielding, so the gate owns its own phase.
        current_phase = "mutation_gate"
        if fetched_cidrs != settings.cloudflare_ip_list:
            payload["changed"] = True
            next_settings = preview_settings
            async with host_mutation_operation(
                settings,
                kind="cloudflare_sync",
                phase="firewall",
                details={"fetched_cidr_count": len(fetched_cidrs)},
                defer_on_update_blocker=True,
            ) as operation:
                current_phase = "firewall"
                payload["firewall"] = reconcile_cloudflare_http_firewall(
                    next_settings, cidrs=fetched_cidrs
                )
                _record_phase(
                    payload,
                    "firewall",
                    status="ok",
                    skipped=bool(payload["firewall"].get("skipped")),
                    rule_count=payload["firewall"].get("live_rule_count"),
                )

                current_phase = "apply"
                apply_payload, apply_ok = await _run_apply_for_sync(next_settings)
                payload["apply"] = apply_payload
                _record_phase(
                    payload,
                    "apply",
                    status="ok" if apply_ok else "failed",
                    apply_status=apply_payload.get("status"),
                )
                if not apply_ok:
                    payload["managed_env_updated"] = False
                    payload["failed_phase"] = current_phase
                    current_phase = "rollback"
                    payload["rollback"] = await _rollback_cloudflare_sync(
                        settings,
                        restore_firewall=True,
                        restore_apply=False,
                    )
                    _record_phase(
                        payload,
                        "rollback",
                        status="ok" if payload["rollback"].get("ok") else "failed",
                    )
                    payload["ok"] = False
                    await operation.complete(
                        "failed",
                        phase="apply",
                        error=str(apply_payload.get("message") or "apply failed"),
                        details=payload,
                    )
                    payload[
                        "notification_sent"
                    ] = await _notify_cloudflare_sync_failure(
                        settings,
                        phase="apply",
                        error=str(apply_payload.get("message") or "apply failed"),
                        rollback=payload["rollback"],
                        payload=payload,
                    )
                    write_cloudflare_sync_state(settings, payload)
                    logger.warning(
                        "cloudflare.sync.failed",
                        apply=apply_payload,
                        rollback=payload["rollback"],
                    )
                    return payload
                try:
                    current_phase = "persist_env"
                    settings = apply_managed_env_updates(
                        settings, {SYNC_ENV_KEY: ",".join(fetched_cidrs)}
                    )
                except Exception as exc:
                    payload["managed_env_updated"] = False
                    payload["error"] = str(exc)
                    payload["failed_phase"] = current_phase
                    _record_phase(
                        payload, "persist_env", status="failed", error=str(exc)
                    )
                    current_phase = "rollback"
                    payload["rollback"] = await _rollback_cloudflare_sync(
                        settings,
                        restore_firewall=True,
                        restore_apply=True,
                    )
                    _record_phase(
                        payload,
                        "rollback",
                        status="ok" if payload["rollback"].get("ok") else "failed",
                    )
                    payload["ok"] = False
                    await operation.complete(
                        "failed", phase="persist_env", error=str(exc), details=payload
                    )
                    payload[
                        "notification_sent"
                    ] = await _notify_cloudflare_sync_failure(
                        settings,
                        phase="persist_env",
                        error=str(exc),
                        rollback=payload["rollback"],
                        payload=payload,
                    )
                    write_cloudflare_sync_state(settings, payload)
                    logger.warning(
                        "cloudflare.sync.failed",
                        error=str(exc),
                        rollback=payload["rollback"],
                    )
                    return payload
                payload["managed_env_updated"] = True
                payload["updated_at"] = _utc_now_iso()
                _record_phase(
                    payload, "persist_env", status="ok", cidr_count=len(fetched_cidrs)
                )
                await operation.complete("success", phase="completed", details=payload)
        else:
            payload["managed_env_updated"] = False
            async with host_mutation_operation(
                settings,
                kind="cloudflare_sync",
                phase="firewall",
                details={"fetched_cidr_count": len(fetched_cidrs), "changed": False},
                defer_on_update_blocker=True,
            ) as operation:
                current_phase = "firewall"
                payload["firewall"] = reconcile_cloudflare_http_firewall(
                    settings, cidrs=fetched_cidrs
                )
                _record_phase(
                    payload,
                    "firewall",
                    status="ok",
                    skipped=bool(payload["firewall"].get("skipped")),
                    rule_count=payload["firewall"].get("live_rule_count"),
                )
                await operation.complete("success", phase="completed", details=payload)
            _record_phase(payload, "apply", status="skipped", reason="cidrs_unchanged")
            _record_phase(
                payload, "persist_env", status="skipped", reason="cidrs_unchanged"
            )

        payload["ok"] = True
        if not payload.get("used_cached_cidrs"):
            payload["last_successful_cidrs"] = list(fetched_cidrs)
            payload["last_successful_fetch_at"] = _utc_now_iso()
        write_cloudflare_sync_state(settings, payload)
        logger.info(
            "cloudflare.sync.completed",
            changed=payload["changed"],
            cidr_count=payload["fetched_cidr_count"],
        )
        return payload
    except Exception as exc:
        if (
            isinstance(exc, HostMutationLockError)
            and exc.blocker is not None
            and exc.blocker.kind == "update_control_plane"
        ):
            blocker = exc.blocker
            payload["ok"] = False
            payload["status"] = "deferred"
            payload["deferred"] = True
            payload["deferred_reason"] = CONTROL_PLANE_UPDATE_DEFERRED_REASON
            payload["deferred_phase"] = current_phase
            payload["managed_env_updated"] = False
            payload["notification_sent"] = False
            payload["blocker"] = {
                "id": blocker.id,
                "kind": blocker.kind,
                "status": blocker.status,
                "phase": blocker.phase,
            }
            payload["firewall"] = {
                "skipped": True,
                "reason": CONTROL_PLANE_UPDATE_DEFERRED_REASON,
                "deleted_rule_numbers": [],
                "added_rule_count": 0,
            }
            _record_phase(
                payload,
                "mutation_gate",
                status="deferred",
                reason=CONTROL_PLANE_UPDATE_DEFERRED_REASON,
                blocker_id=blocker.id,
                blocker_status=blocker.status,
                blocker_phase=blocker.phase,
            )
            _record_phase(
                payload,
                "firewall",
                status="deferred",
                reason=CONTROL_PLANE_UPDATE_DEFERRED_REASON,
            )
            if payload["changed"]:
                _record_phase(
                    payload,
                    "apply",
                    status="deferred",
                    reason=CONTROL_PLANE_UPDATE_DEFERRED_REASON,
                )
                _record_phase(
                    payload,
                    "persist_env",
                    status="deferred",
                    reason=CONTROL_PLANE_UPDATE_DEFERRED_REASON,
                )
            else:
                _record_phase(
                    payload, "apply", status="skipped", reason="cidrs_unchanged"
                )
                _record_phase(
                    payload,
                    "persist_env",
                    status="skipped",
                    reason="cidrs_unchanged",
                )
            write_cloudflare_sync_state(settings, payload)
            logger.info(
                "cloudflare.sync.deferred",
                reason=CONTROL_PLANE_UPDATE_DEFERRED_REASON,
                blocker_id=blocker.id,
                blocker_status=blocker.status,
                blocker_phase=blocker.phase,
                cidr_count=payload.get("fetched_cidr_count"),
                changed=payload["changed"],
            )
            return payload
        payload["ok"] = False
        payload["error"] = str(exc)
        payload["error_type"] = type(exc).__name__
        payload["failed_phase"] = current_phase
        payload["failure"] = {
            "phase": current_phase,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        _record_phase(payload, current_phase, status="failed", error=str(exc))
        payload["notification_sent"] = await _notify_cloudflare_sync_failure(
            settings,
            phase=current_phase,
            error=str(exc),
            payload=payload,
        )
        write_cloudflare_sync_state(settings, payload)
        logger.exception(
            "cloudflare.sync.failed",
            phase=current_phase,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return payload
