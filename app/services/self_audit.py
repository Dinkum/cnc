from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from app.config import Settings
from app.logger import get_logger
from app.services import cloudflare_ingress
from app.services.app_network_isolation import audit_app_network_isolation_guard
from app.services.app_quadlet import (
    AppQuadletWriteError,
    verify_app_quadlet_dir_writable,
)
from app.services.commands import run_command
from app.services.runtime_services import AppRuntimeServices
from app.services.notification_state import NotificationStateTransaction
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
    summarize_list,
)
from app.services.tailscale_admin import (
    TailscaleAdminExposureError,
    verify_tailscale_admin_exposure,
)


SS_LISTEN_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+")
SECONDS_RE = re.compile(r"^(?P<number>\d+)")
ADMIN_REFERENCE_PATTERNS = (
    re.compile(r"\b127\.0\.0\.1:9090\b"),
    re.compile(r"\blocalhost:9090\b"),
    re.compile(r"\[::1\]:9090\b"),
)
SSHD_BASELINE = {
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "permitemptypasswords": "no",
    "pubkeyauthentication": "yes",
}
SSHD_PERMIT_ROOTLOGIN_ALLOWED = {"no", "prohibit-password", "without-password"}
SSHD_MAX_AUTH_TRIES_MAX = 10
SSHD_LOGIN_GRACE_TIME_MAX_SEC = 20
SELF_AUDIT_CHECK_SEVERITY = {
    "admin_loopback_bind": "blocking",
    "admin_runtime_write_paths": "blocking",
    "nginx_admin_reference": "blocking",
    "tailscale_admin_exposure": "blocking",
    "firewall_restrictions": "warning",
    "app_network_isolation": "warning",
    "sshd_global_policy": "warning",
}
STARTUP_SELF_AUDIT_EVENT_KEY = "startup_self_audit"
logger = get_logger("self_audit")


class ControlPlaneSelfAuditError(RuntimeError):
    def __init__(self, findings: list[dict[str, object]]) -> None:
        self.findings = findings
        message = "; ".join(
            str(item.get("message") or "control plane self-audit failed")
            for item in findings
        )
        super().__init__(message)

    def as_details(self) -> dict[str, object]:
        severity_counts = {"blocking": 0, "warning": 0}
        for item in self.findings:
            severity = str(item.get("severity") or "").strip().lower()
            if severity in severity_counts:
                severity_counts[severity] += 1
        return {
            "findings": self.findings,
            "count": len(self.findings),
            "blocking_count": severity_counts["blocking"],
            "warning_count": severity_counts["warning"],
            "blocks_apply": self.blocks_apply,
        }

    @property
    def blocking_findings(self) -> list[dict[str, object]]:
        return [
            item
            for item in self.findings
            if str(item.get("severity") or "").strip().lower() == "blocking"
        ]

    @property
    def warning_findings(self) -> list[dict[str, object]]:
        return [
            item
            for item in self.findings
            if str(item.get("severity") or "").strip().lower() == "warning"
        ]

    @property
    def blocks_apply(self) -> bool:
        return bool(self.blocking_findings)


@dataclass(frozen=True)
class _AdminListener:
    address: str
    port: int


def _parse_local_endpoint(value: str) -> _AdminListener | None:
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith("[") and "]:" in normalized:
        host, _, port = normalized[1:].partition("]:")
    else:
        host, _, port = normalized.rpartition(":")
    if not host or not port.isdigit():
        return None
    return _AdminListener(address=host, port=int(port))


def _is_loopback_host(value: str) -> bool:
    normalized = value.strip().strip("[]")
    if normalized in {"127.0.0.1", "::1", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _audit_admin_loopback_bind(settings: Settings) -> dict[str, object]:
    result = run_command(
        ["ss", "-ltnH"], timeout_sec=settings.command_timeout_status_sec
    )
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout or "ss -ltnH failed")

    listeners: list[str] = []
    for raw_line in result.stdout.splitlines():
        match = SS_LISTEN_RE.match(raw_line.strip())
        if match is None:
            continue
        parsed = _parse_local_endpoint(match.group("local"))
        if parsed is None or parsed.port != settings.admin_port:
            continue
        listeners.append(f"{parsed.address}:{parsed.port}")
        if not _is_loopback_host(parsed.address):
            raise RuntimeError(
                f"admin listener is not loopback-bound: {parsed.address}:{parsed.port}"
            )

    if not listeners:
        raise RuntimeError(
            f"admin listener on port {settings.admin_port} was not found in ss -ltn output"
        )

    return {"listeners": listeners}


def _effective_admin_bind_host(settings: Settings) -> str:
    configured = str(settings.admin_host or "").strip()
    if not configured:
        return "127.0.0.1"
    if _is_loopback_host(configured):
        return configured
    if settings.admin_unsafe_allow_remote:
        return configured
    return "127.0.0.1"


def _audit_admin_loopback_startup_config(settings: Settings) -> dict[str, object]:
    effective_host = _effective_admin_bind_host(settings)
    if not _is_loopback_host(effective_host):
        raise RuntimeError(
            f"admin startup bind is not loopback-bound: {effective_host}:{settings.admin_port}"
        )
    return {
        "configured_host": str(settings.admin_host or "").strip() or "127.0.0.1",
        "effective_host": effective_host,
        "port": settings.admin_port,
        "mode": "startup_config",
    }


def _audit_nginx_admin_reference(settings: Settings) -> dict[str, object]:
    offending_files: list[str] = []
    generated_dir = settings.nginx_generated_dir
    if not generated_dir.exists():
        return {"checked_files": 0, "offending_files": []}

    checked_files = 0
    for path in sorted(generated_dir.glob("*.conf")):
        checked_files += 1
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(content) for pattern in ADMIN_REFERENCE_PATTERNS):
            offending_files.append(str(path))

    if offending_files:
        raise RuntimeError(
            f"generated nginx config references admin port 9090: {', '.join(offending_files)}"
        )

    return {"checked_files": checked_files, "offending_files": []}


def _audit_admin_runtime_write_paths(settings: Settings) -> dict[str, object]:
    return verify_app_quadlet_dir_writable(settings)


def _audit_firewall_cloudflare_restrictions(settings: Settings) -> dict[str, object]:
    if not settings.nginx_cloudflare_only:
        return {"checked": False, "reason": "nginx_cloudflare_only_disabled"}
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        settings.cloudflare_ip_list
    )
    return cloudflare_ingress.audit_live_cloudflare_http_firewall(
        policy,
        run_command_func=run_command,
        timeout_sec=settings.command_timeout_status_sec,
    )


def _parse_sshd_effective_config(settings: Settings) -> dict[str, str]:
    result = run_command(
        ["sshd", "-T"], timeout_sec=settings.command_timeout_status_sec
    )
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout or "sshd -T failed")
    payload: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or " " not in line:
            continue
        key, value = line.split(None, 1)
        payload[key] = value.strip()
    return payload


def _parse_duration_seconds(value: str) -> int | None:
    match = SECONDS_RE.match(value.strip())
    if match is None:
        return None
    try:
        return int(match.group("number"))
    except ValueError:
        return None


def _audit_sshd_global_policy(settings: Settings) -> dict[str, object]:
    effective = _parse_sshd_effective_config(settings)
    failures: list[str] = []

    for key, expected in SSHD_BASELINE.items():
        actual = str(effective.get(key, "")).strip().lower()
        if actual != expected:
            failures.append(f"{key}={actual or '-'} (expected {expected})")

    permit_root_login = str(effective.get("permitrootlogin", "")).strip().lower()
    if permit_root_login not in SSHD_PERMIT_ROOTLOGIN_ALLOWED:
        failures.append(
            f"permitrootlogin={permit_root_login or '-'} (expected one of {sorted(SSHD_PERMIT_ROOTLOGIN_ALLOWED)})"
        )

    max_auth_tries_raw = str(effective.get("maxauthtries", "")).strip()
    try:
        max_auth_tries = int(max_auth_tries_raw)
    except ValueError:
        max_auth_tries = None
    if max_auth_tries is None or max_auth_tries > SSHD_MAX_AUTH_TRIES_MAX:
        failures.append(
            f"maxauthtries={max_auth_tries_raw or '-'} (expected <= {SSHD_MAX_AUTH_TRIES_MAX})"
        )

    login_grace_time_raw = str(effective.get("logingracetime", "")).strip()
    login_grace_time = _parse_duration_seconds(login_grace_time_raw)
    if login_grace_time is None or login_grace_time > SSHD_LOGIN_GRACE_TIME_MAX_SEC:
        failures.append(
            f"logingracetime={login_grace_time_raw or '-'} (expected <= {SSHD_LOGIN_GRACE_TIME_MAX_SEC}s)"
        )

    if failures:
        raise RuntimeError(
            "sshd global policy is weaker than baseline: " + ", ".join(failures)
        )

    return {
        "passwordauthentication": effective.get("passwordauthentication"),
        "kbdinteractiveauthentication": effective.get("kbdinteractiveauthentication"),
        "permitrootlogin": effective.get("permitrootlogin"),
        "maxauthtries": effective.get("maxauthtries"),
        "logingracetime": effective.get("logingracetime"),
    }


def _audit_app_network_isolation_guard(settings: Settings) -> dict[str, object]:
    return audit_app_network_isolation_guard(
        settings,
        services=AppRuntimeServices(run_command=run_command),
    )


def run_control_plane_self_audit(
    settings: Settings, *, startup_mode: bool = False
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    details: dict[str, object] = {}

    checks = (
        (
            "admin_loopback_bind",
            _audit_admin_loopback_startup_config
            if startup_mode
            else _audit_admin_loopback_bind,
        ),
        ("admin_runtime_write_paths", _audit_admin_runtime_write_paths),
        ("nginx_admin_reference", _audit_nginx_admin_reference),
        ("tailscale_admin_exposure", verify_tailscale_admin_exposure),
        ("firewall_restrictions", _audit_firewall_cloudflare_restrictions),
        ("app_network_isolation", _audit_app_network_isolation_guard),
        ("sshd_global_policy", _audit_sshd_global_policy),
    )

    for name, fn in checks:
        try:
            details[name] = fn(settings)
        except TailscaleAdminExposureError as exc:
            findings.append(_self_audit_finding(name, exc))
        except Exception as exc:
            findings.append(_self_audit_finding(name, exc))

    if findings:
        raise ControlPlaneSelfAuditError(findings)

    return details


def _self_audit_finding(name: str, exc: Exception) -> dict[str, object]:
    message = str(exc)
    details = getattr(exc, "details", None)
    if isinstance(exc, AppQuadletWriteError) and isinstance(details, dict):
        quadlet_dir = str(details.get("quadlet_dir") or "").strip()
        error = str(details.get("error") or message).strip()
        if quadlet_dir and error:
            message = f"active apply process cannot write app Quadlet directory {quadlet_dir}: {error}"
    finding: dict[str, object] = {
        "check": name,
        "message": message,
        "severity": str(SELF_AUDIT_CHECK_SEVERITY.get(name, "warning")),
    }
    if isinstance(details, dict):
        finding["details"] = details
    return finding


def _notification_state_transaction(settings: Settings):
    return NotificationStateTransaction(settings)


def _notification_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _notification_marker(value: object) -> str:
    return str(value or "").strip()


def _read_startup_self_audit_event(settings: Settings) -> dict[str, str]:
    with _notification_state_transaction(settings) as notification_state:
        return _startup_self_audit_event_snapshot(
            notification_state.get_event(STARTUP_SELF_AUDIT_EVENT_KEY)
        )


def _write_startup_self_audit_event(settings: Settings, entry: dict[str, str]) -> None:
    with _notification_state_transaction(settings) as notification_state:
        notification_state.set_event(STARTUP_SELF_AUDIT_EVENT_KEY, entry)


def _startup_self_audit_signature(exc: ControlPlaneSelfAuditError) -> str:
    findings = exc.findings if isinstance(exc.findings, list) else []
    parts = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        check = str(item.get("check") or "").strip()
        message = str(item.get("message") or "").strip()
        severity = str(item.get("severity") or "").strip()
        parts.append(f"{severity}:{check}:{message}")
    return "|".join(sorted(part for part in parts if part))


def _startup_self_audit_failure_title(exc: ControlPlaneSelfAuditError) -> str:
    return (
        "CNC host safeguard violation"
        if exc.blocks_apply
        else "CNC host hardening drift detected"
    )


def _startup_self_audit_failure_message(exc: ControlPlaneSelfAuditError) -> str:
    findings = exc.blocking_findings if exc.blocks_apply else exc.warning_findings
    if not findings:
        findings = exc.findings if isinstance(exc.findings, list) else []
    top_finding = ""
    if findings and isinstance(findings[0], dict):
        top_finding = str(findings[0].get("message") or "").strip()
    return build_operator_message(
        (
            "Critical host guardrail drift detected during CNC startup. CNC stayed online."
            if exc.blocks_apply
            else "Host hardening drift detected during CNC startup. CNC stayed online."
        ),
        status="failure" if exc.blocks_apply else "warning",
        facts=[
            ("top_finding", top_finding),
            ("finding_count", len(findings)),
        ],
        action="cnc-admin logs admin --errors",
    )


def _startup_self_audit_recovered_message(previous_signature: str) -> str:
    findings = [item.strip() for item in previous_signature.split("|") if item.strip()]
    return build_operator_message(
        "Startup host drift cleared.",
        status="success",
        facts=[
            ("cleared_findings", summarize_list(findings, limit=2) if findings else ""),
        ],
        action="cnc-admin logs admin --errors",
    )


def _startup_self_audit_event_snapshot(
    raw_event: Mapping[str, object] | None,
) -> dict[str, str]:
    event = raw_event if isinstance(raw_event, Mapping) else {}
    return {
        "status": _notification_marker(event.get("status")),
        "signature": _notification_marker(event.get("signature")),
        "pending_recovery_signature": _notification_marker(
            event.get("pending_recovery_signature")
        ),
        "last_failure_notified_signature": _notification_marker(
            event.get("last_failure_notified_signature")
        ),
        "last_failure_notified_at": _notification_marker(
            event.get("last_failure_notified_at")
        ),
        "last_recovery_notified_signature": _notification_marker(
            event.get("last_recovery_notified_signature")
        ),
        "last_recovery_notified_at": _notification_marker(
            event.get("last_recovery_notified_at")
        ),
    }


def _ok_startup_self_audit_event(
    previous: dict[str, str],
    *,
    recovery_signature: str,
    recovery_notified: bool,
) -> dict[str, str]:
    return {
        "status": "ok",
        "signature": "",
        "pending_recovery_signature": "" if recovery_notified else recovery_signature,
        "last_failure_notified_signature": previous["last_failure_notified_signature"],
        "last_failure_notified_at": previous["last_failure_notified_at"],
        "last_recovery_notified_signature": (
            recovery_signature
            if recovery_notified
            else previous["last_recovery_notified_signature"]
        ),
        "last_recovery_notified_at": (
            _notification_timestamp()
            if recovery_notified
            else previous["last_recovery_notified_at"]
        ),
    }


def _failed_startup_self_audit_event(
    previous: dict[str, str],
    *,
    signature: str,
    failure_notified: bool,
) -> dict[str, str]:
    return {
        "status": "failed",
        "signature": signature,
        "pending_recovery_signature": "",
        "last_failure_notified_signature": (
            signature
            if failure_notified
            else previous["last_failure_notified_signature"]
        ),
        "last_failure_notified_at": (
            _notification_timestamp()
            if failure_notified
            else previous["last_failure_notified_at"]
        ),
        "last_recovery_notified_signature": previous[
            "last_recovery_notified_signature"
        ],
        "last_recovery_notified_at": previous["last_recovery_notified_at"],
    }


async def handle_startup_self_audit_notification(
    settings: Settings,
    outcome: dict[str, object] | ControlPlaneSelfAuditError,
) -> None:
    previous = _read_startup_self_audit_event(settings)

    if isinstance(outcome, ControlPlaneSelfAuditError):
        signature = _startup_self_audit_signature(outcome)
        should_notify = (
            previous["status"] != "failed"
            or previous["signature"] != signature
            or previous["last_failure_notified_signature"] != signature
        )
        notified = False
        if should_notify:
            # Notification delivery owns the same host-state lock for its durable
            # outbox, so it must run between the short state transactions.
            notified = await send_pushover_notification_async(
                settings,
                title=_startup_self_audit_failure_title(outcome),
                message=_startup_self_audit_failure_message(outcome),
                priority=0,
                event="host_drift_detected",
                source="main",
                url=admin_dashboard_url(settings, tab="home"),
                url_title="Open CNC",
            )
            logger.info(
                "control_plane.self_audit.notification_observed",
                signature=signature,
                notified=notified,
            )
        else:
            logger.info(
                "control_plane.self_audit.notification_suppressed",
                signature=signature,
                reason="duplicate",
            )
        _write_startup_self_audit_event(
            settings,
            _failed_startup_self_audit_event(
                previous,
                signature=signature,
                failure_notified=notified,
            ),
        )
        return

    recovery_signature = previous["pending_recovery_signature"] or (
        previous["signature"] if previous["status"] == "failed" else ""
    )
    should_notify_recovery = (
        bool(recovery_signature)
        and previous["last_failure_notified_signature"] == recovery_signature
        and previous["last_recovery_notified_signature"] != recovery_signature
    )
    notified = False
    if should_notify_recovery:
        notified = await send_pushover_notification_async(
            settings,
            title="CNC host drift cleared",
            message=_startup_self_audit_recovered_message(recovery_signature),
            priority=0,
            event="host_drift_cleared",
            source="main",
            url=admin_dashboard_url(settings, tab="home"),
            url_title="Open CNC",
        )
        logger.info(
            "control_plane.self_audit.recovery_observed",
            previous_signature=recovery_signature,
            notified=notified,
        )
    else:
        logger.info("control_plane.self_audit.recovery_not_needed")
    _write_startup_self_audit_event(
        settings,
        _ok_startup_self_audit_event(
            previous,
            recovery_signature=recovery_signature,
            recovery_notified=notified,
        ),
    )
