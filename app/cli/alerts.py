from __future__ import annotations

import argparse

from app.cli.common import CommandResult, bind_command
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backend_alerts = subparsers.add_parser("backend-alerts")
    backend_alerts_subparsers = backend_alerts.add_subparsers(
        dest="backend_alerts_command", required=True
    )
    backend_alerts_check = backend_alerts_subparsers.add_parser("check")
    backend_alerts_check.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        backend_alerts_check,
        handler=_run_backend_alerts_check_command,
        formatter=format_backend_alerts_text,
        needs_settings=True,
    )
    backend_alerts_test = backend_alerts_subparsers.add_parser("test")
    backend_alerts_test.add_argument("--backend", dest="backend", required=True)
    backend_alerts_test.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        backend_alerts_test,
        handler=_run_backend_alerts_test_command,
        formatter=format_backend_alerts_text,
        needs_settings=True,
    )

    notifications = subparsers.add_parser("notifications")
    notifications_subparsers = notifications.add_subparsers(
        dest="notifications_command", required=True
    )
    notifications_test = notifications_subparsers.add_parser("test")
    notifications_test.add_argument("--backend", dest="backend", required=True)
    notifications_test.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        notifications_test,
        handler=_run_notifications_test_command,
        formatter=format_notifications_test_text,
        needs_settings=True,
    )


def format_backend_alerts_text(payload: dict[str, object]) -> str:
    notifications = (
        payload.get("notifications")
        if isinstance(payload.get("notifications"), list)
        else []
    )
    backends = (
        payload.get("backends") if isinstance(payload.get("backends"), list) else []
    )
    lines = [
        f"checked_at: {payload.get('checked_at') or '-'}",
        f"backend_count: {payload.get('backend_count') or 0}",
        f"healthy_count: {payload.get('healthy_count') or 0}",
        f"unhealthy_count: {payload.get('unhealthy_count') or 0}",
        f"auto_fix_attempt_count: {payload.get('auto_fix_attempt_count') or 0}",
        f"auto_fix_success_count: {payload.get('auto_fix_success_count') or 0}",
        f"auto_fix_failure_count: {payload.get('auto_fix_failure_count') or 0}",
        f"notifications_configured: {'yes' if payload.get('notifications_configured') else 'no'}",
    ]
    if notifications:
        lines.append("notifications:")
        for item in notifications:
            if not isinstance(item, dict):
                continue
            lines.append(
                "  "
                + f"{item.get('backend') or '-'}: {item.get('kind') or '-'} -> "
                + ("sent" if item.get("sent") else "failed")
            )
    else:
        lines.append("notifications: none")
    if backends:
        lines.append("backends:")
        for item in backends:
            if not isinstance(item, dict):
                continue
            issues = item.get("issues") if isinstance(item.get("issues"), list) else []
            issue_text = ", ".join(str(value) for value in issues) if issues else "none"
            auto_fix = (
                item.get("auto_fix") if isinstance(item.get("auto_fix"), dict) else {}
            )
            auto_fix_summary = "auto_fix=none"
            if auto_fix:
                auto_fix_summary = (
                    "auto_fix="
                    + ("attempted" if auto_fix.get("attempted") else "skipped")
                    + f"/{auto_fix.get('outcome') or '-'}"
                    + f"/{auto_fix.get('reason') or '-'}"
                )
            lines.append(
                "  "
                + f"{item.get('backend') or '-'}: {'up' if item.get('ok') else 'down'} | issues={issue_text} | {auto_fix_summary}"
            )
    return "\n".join(lines)


def format_notifications_test_text(payload: dict[str, object]) -> str:
    notifications = (
        payload.get("notifications")
        if isinstance(payload.get("notifications"), list)
        else []
    )
    lines = [
        f"backend: {payload.get('backend') or '-'}",
        f"notifications_configured: {'yes' if payload.get('notifications_configured') else 'no'}",
        f"notification_count: {len(notifications)}",
    ]
    if notifications:
        lines.append("notifications:")
        for item in notifications:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("kind") or "-")
            lines.append(f"  {title}: {'sent' if item.get('sent') else 'failed'}")
    else:
        lines.append("notifications: none")
    return "\n".join(lines)


async def backend_alerts_check_async(settings: Settings) -> CommandResult:
    from app.services.backend_alerts import run_backend_alerts_check

    try:
        payload = await run_backend_alerts_check(settings)
    except Exception as exc:
        return 1, None, str(exc)
    return 0, payload, None


async def backend_alerts_test_async(
    backend_name: str,
    settings: Settings,
) -> CommandResult:
    from app.services.backend_alerts import send_backend_alert_test_notifications

    try:
        payload = await send_backend_alert_test_notifications(
            settings, backend_name=backend_name
        )
    except Exception as exc:
        return 1, None, str(exc)
    return (0 if payload.get("ok") else 2), payload, None


async def notifications_test_async(
    backend_name: str,
    settings: Settings,
) -> CommandResult:
    from app.services.backend_alerts import send_backend_alert_test_notifications
    from app.services.notifications import send_notification_test_suite

    try:
        operator_payload = await send_notification_test_suite(
            settings, backend_name=backend_name
        )
        backend_payload = await send_backend_alert_test_notifications(
            settings, backend_name=backend_name
        )
    except Exception as exc:
        return 1, None, str(exc)
    notifications: list[object] = []
    notifications.extend(
        operator_payload.get("notifications")
        if isinstance(operator_payload.get("notifications"), list)
        else []
    )
    notifications.extend(
        backend_payload.get("notifications")
        if isinstance(backend_payload.get("notifications"), list)
        else []
    )
    payload = {
        "backend": backend_name,
        "notifications_configured": bool(
            operator_payload.get("notifications_configured")
            and backend_payload.get("notifications_configured")
        ),
        "notifications": notifications,
        "ok": bool(operator_payload.get("ok")) and bool(backend_payload.get("ok")),
    }
    return (0 if payload.get("ok") else 2), payload, None


async def _run_backend_alerts_check_command(
    args: argparse.Namespace,
    settings: Settings | None,
) -> CommandResult:
    assert settings is not None
    return await backend_alerts_check_async(settings)


async def _run_backend_alerts_test_command(
    args: argparse.Namespace,
    settings: Settings | None,
) -> CommandResult:
    assert settings is not None
    return await backend_alerts_test_async(args.backend, settings)


async def _run_notifications_test_command(
    args: argparse.Namespace,
    settings: Settings | None,
) -> CommandResult:
    assert settings is not None
    return await notifications_test_async(args.backend, settings)
