from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from importlib import metadata
import os
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logger import get_logger
from app.models.entities import UpdateCheck, UpdateRun
from app.schemas.apply import ApplyResponse
from app.services.commands import (
    CommandError,
    command_result_is_retryable,
    run_command_checked_async,
)
from app.services.control_events import emit_control_event
from app.services.error_reporting import CNCError, ErrorCode
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    send_pushover_notification_async,
)
from app.services.operations import HostMutationLockError, host_mutation_operation
from app.services.retry import is_retryable_http_exception, retry_call

logger = get_logger("update")
PENDING_UPDATE_STATUSES = {"queued", "running"}
UPDATE_LAUNCH_TIMEOUT_SEC = 15
UPDATE_SYSTEMD_KEYS = [
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
]
UPDATE_LOG_EXCERPT_MIN_CHARS = 16000
UPDATE_NOTIFICATION_LOG_TAIL_CHARS = 700
UPDATE_LOG_SUCCESS_MARKERS = (
    "[updater] update complete",
    "status=success",
)
UPDATE_LOG_FAILURE_MARKERS = (
    "[updater][error]",
    "[updater] update failed",
    "status=error",
)
UPDATE_LOG_MIGRATION_BOUNDARY_MARKER = "migration boundary crossed"


def _track_update_notification_task(
    task: asyncio.Task[bool], *, event: str, run_id: int
) -> None:
    try:
        notified = task.result()
    except Exception as exc:
        logger.warning(
            "update.notification.background_failed",
            event=event,
            run_id=run_id,
            error=str(exc),
        )
        return
    logger.info(
        "update.notification.background_completed",
        event=event,
        run_id=run_id,
        notified=notified,
    )


async def _send_update_notification(
    settings: Settings,
    *,
    mode: str,
    title: str,
    message: str,
    event: str,
    run_id: int,
) -> bool:
    kwargs = {
        "title": title,
        "message": message,
        "priority": 0,
        "event": event,
        "source": "update",
        "run_id": run_id,
        "url": admin_dashboard_url(settings, tab="home"),
        "url_title": "Open CNC",
    }
    if mode == "background":
        task = asyncio.create_task(send_pushover_notification_async(settings, **kwargs))
        task.add_done_callback(
            lambda completed: _track_update_notification_task(
                completed, event=event, run_id=run_id
            )
        )
        return False
    return await send_pushover_notification_async(settings, **kwargs)


class UpdateRejectedError(CNCError):
    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(
            ErrorCode.UPDATE_FAILED,
            message,
            details=details,
            status_code=409,
            severity="warning",
        )


def _clip_output(
    content: str, max_chars: int, *, tail: bool = False
) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    if tail:
        clipped = content[-max_chars:]
        if "\n" in clipped:
            clipped = clipped.split("\n", 1)[1]
        return f"...[truncated]\n{clipped}", True
    clipped = content[:max_chars]
    return f"{clipped}\n...[truncated]", True


def _effective_update_log_excerpt_chars(settings: Settings) -> int:
    return max(UPDATE_LOG_EXCERPT_MIN_CHARS, int(settings.update_output_max_chars))


def _notification_log_tail(content: object) -> str:
    cleaned = str(content or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= UPDATE_NOTIFICATION_LOG_TAIL_CHARS:
        return cleaned
    clipped = cleaned[-UPDATE_NOTIFICATION_LOG_TAIL_CHARS:]
    if "\n" in clipped:
        clipped = clipped.split("\n", 1)[1]
    return f"...[truncated]\n{clipped}"


def _update_log_terminal_status(content: object) -> str | None:
    cleaned = str(content or "").lower()
    if not cleaned:
        return None
    success_pos = max(
        cleaned.rfind(marker.lower()) for marker in UPDATE_LOG_SUCCESS_MARKERS
    )
    failure_pos = max(
        cleaned.rfind(marker.lower()) for marker in UPDATE_LOG_FAILURE_MARKERS
    )
    if success_pos > failure_pos:
        return "success"
    if failure_pos > success_pos:
        return "error"
    return None


def _systemd_state_summary(unit_state: dict[str, str]) -> str:
    parts = []
    for key in ("ActiveState", "SubState", "Result", "ExecMainStatus"):
        value = str(unit_state.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _systemd_failure_reason(
    unit_state: dict[str, str], *, log_terminal_status: str | None = None
) -> str:
    load_state = unit_state.get("LoadState", "")
    active_state = unit_state.get("ActiveState", "")
    sub_state = unit_state.get("SubState", "")
    result_state = unit_state.get("Result", "")
    exec_main_status = unit_state.get("ExecMainStatus", "")

    if log_terminal_status == "error":
        return "updater log ended with failure"
    if load_state in {"not-found", "error", "bad-setting"}:
        if load_state == "not-found":
            return "transient systemd unit is no longer available"
        return f"systemd unit is {load_state}"
    if result_state and result_state != "success":
        return f"systemd result: {result_state}"
    if exec_main_status and exec_main_status != "0":
        return f"process exited with status {exec_main_status}"
    if active_state == "failed":
        if sub_state:
            return f"systemd state: {active_state}/{sub_state}"
        return "systemd unit failed"
    if sub_state:
        return f"systemd substate: {sub_state}"
    return "unit failed"


def _build_update_failure_notification_message(
    summary: str,
    *,
    facts: list[tuple[str, object]],
    log_excerpt: object = "",
    action: str,
) -> str:
    message = build_operator_message(
        summary,
        status="failure",
        facts=facts,
        action=action,
    )
    log_tail = _notification_log_tail(log_excerpt)
    if not log_tail:
        return message
    return f"{message}\nlog tail:\n{log_tail}"


def _write_update_log(
    log_path: str,
    *,
    script: str,
    status: str,
    stdout: str,
    stderr: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    body = (
        f"timestamp={now}\n"
        f"status={status}\n"
        f"script={script}\n"
        "----- stdout -----\n"
        f"{stdout}\n"
        "----- stderr -----\n"
        f"{stderr}\n"
    )
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(log_path, 0o600)


def _parse_systemctl_show(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _parse_utc_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _serialize_update_run(run: UpdateRun) -> ApplyResponse:
    try:
        details = json.loads(run.details_json or "{}")
    except json.JSONDecodeError:
        details = {"raw": run.details_json}
    return ApplyResponse(
        status=run.status,
        message=run.message,
        details=details,
        created_at=run.created_at,
    )


def current_app_version() -> str:
    try:
        return metadata.version("cnc")
    except metadata.PackageNotFoundError:
        version_path = Path(__file__).resolve().parents[2] / "version.json"
        try:
            payload = json.loads(version_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "dev"
        version = payload.get("version")
        return (
            str(version).strip()
            if isinstance(version, str) and version.strip()
            else "dev"
        )


def _normalize_github_repo(raw: str) -> str | None:
    cleaned = raw.strip()
    prefixes = (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    )
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    cleaned = cleaned.strip().strip("/")
    if cleaned.count("/") != 1:
        return None
    owner, repo = cleaned.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _parse_version_tuple(raw: str) -> tuple[int, ...] | None:
    cleaned = raw.strip().removeprefix("v")
    if not cleaned:
        return None
    parts = cleaned.split(".")
    parsed: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        parsed.append(int(part))
    return tuple(parsed)


def _compare_versions(left: str, right: str) -> int | None:
    left_parts = _parse_version_tuple(left)
    right_parts = _parse_version_tuple(right)
    if left_parts is None or right_parts is None:
        return None
    width = max(len(left_parts), len(right_parts))
    normalized_left = left_parts + (0,) * (width - len(left_parts))
    normalized_right = right_parts + (0,) * (width - len(right_parts))
    if normalized_left < normalized_right:
        return -1
    if normalized_left > normalized_right:
        return 1
    return 0


def _fetch_remote_version(
    repo: str,
    ref: str,
    token: str,
    timeout_sec: int,
    *,
    retries: int = 0,
    backoff_sec: float = 0.0,
) -> str:
    encoded_ref = urllib_parse.quote(ref, safe="")
    url = f"https://api.github.com/repos/{repo}/contents/version.json?ref={encoded_ref}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "cnc-update-check",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"

    def log_retry(
        exc: Exception, attempt: int, max_attempts: int, delay_sec: float
    ) -> None:
        logger.warning(
            "update.version_check.retrying",
            repo=repo,
            ref=ref,
            attempt=attempt,
            max_attempts=max_attempts,
            backoff_sec=delay_sec,
            error=str(exc),
        )

    def fetch_once() -> str:
        request = urllib_request.Request(url, headers=headers)
        with urllib_request.urlopen(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload.get("content")
        encoding = str(payload.get("encoding") or "")
        if not isinstance(content, str) or encoding.lower() != "base64":
            raise ValueError(
                "GitHub contents response did not include base64 version.json content"
            )
        decoded = base64.b64decode(content)
        version_payload = json.loads(decoded.decode("utf-8"))
        version = version_payload.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("remote version.json did not include a version string")
        return version.strip()

    return retry_call(
        fetch_once,
        attempts=retries + 1,
        backoff_sec=backoff_sec,
        should_retry=is_retryable_http_exception,
        on_retry=log_retry,
    )


def _version_rejection_message(update_version: dict[str, Any]) -> str:
    current_version = str(update_version.get("current_version") or "unknown")
    available_version = str(update_version.get("available_version") or "").strip()
    error = str(update_version.get("error") or "").strip()
    if error:
        return f"update availability check failed: {error}"
    if available_version and available_version == current_version:
        return f"already on latest version ({current_version})"
    if available_version:
        return f"remote version {available_version} is not newer than current version {current_version}"
    return "no newer version is available"


async def get_update_version_info(
    settings: Settings,
    *,
    current_version: str | None = None,
) -> dict[str, Any]:
    resolved_current_version = current_version or current_app_version()
    normalized_repo = _normalize_github_repo(settings.github_repo)
    payload: dict[str, Any] = {
        "current_version": resolved_current_version,
        "available_version": None,
        "has_update": False,
        "repo": normalized_repo or settings.github_repo,
        "ref": settings.github_ref,
        "error": None,
    }
    if not normalized_repo:
        payload["error"] = f"invalid github repo: {settings.github_repo}"
        return payload

    try:
        available_version = await asyncio.to_thread(
            _fetch_remote_version,
            normalized_repo,
            settings.github_ref,
            settings.github_readonly_pat,
            settings.command_timeout_status_sec,
            retries=settings.external_http_retry_attempts,
            backoff_sec=settings.external_http_retry_backoff_sec,
        )
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        binascii.Error,
        urllib_error.HTTPError,
        urllib_error.URLError,
    ) as exc:
        payload["error"] = str(exc)
        return payload

    payload["available_version"] = available_version
    comparison = _compare_versions(resolved_current_version, available_version)
    if comparison is None:
        payload["error"] = (
            "cannot compare current version "
            f"{resolved_current_version!r} with remote version {available_version!r}"
        )
        return payload
    payload["has_update"] = comparison < 0
    return payload


def _base_update_version_payload(
    settings: Settings, current_version: str
) -> dict[str, Any]:
    normalized_repo = _normalize_github_repo(settings.github_repo)
    payload: dict[str, Any] = {
        "current_version": current_version,
        "available_version": None,
        "has_update": False,
        "repo": normalized_repo or settings.github_repo,
        "ref": settings.github_ref,
        "error": None,
        "status": "unchecked",
        "checked_at": None,
    }
    if not normalized_repo:
        payload["status"] = "error"
        payload["error"] = f"invalid github repo: {settings.github_repo}"
    return payload


def _payload_from_update_check(
    check: UpdateCheck, settings: Settings, current_version: str
) -> dict[str, Any]:
    payload = _base_update_version_payload(settings, current_version)
    payload.update(
        {
            "available_version": check.available_version,
            "repo": check.repo,
            "ref": check.ref,
            "error": check.error,
            "status": check.status,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        }
    )
    if check.error:
        payload["has_update"] = False
        return payload
    available_version = str(check.available_version or "").strip()
    if not available_version:
        payload["has_update"] = False
        return payload
    comparison = _compare_versions(current_version, available_version)
    if comparison is None:
        payload["has_update"] = False
        payload["error"] = (
            "cannot compare current version "
            f"{current_version!r} with remote version {available_version!r}"
        )
        payload["status"] = "error"
        return payload
    payload["has_update"] = comparison < 0
    return payload


async def get_cached_update_version_info(
    session: AsyncSession,
    settings: Settings,
    *,
    current_version: str | None = None,
) -> dict[str, Any]:
    resolved_current_version = current_version or current_app_version()
    payload = _base_update_version_payload(settings, resolved_current_version)
    if payload.get("error"):
        return payload

    latest = (
        await session.execute(
            select(UpdateCheck)
            .order_by(UpdateCheck.checked_at.desc(), UpdateCheck.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return payload

    expected_repo = payload["repo"]
    if latest.repo != expected_repo or latest.ref != settings.github_ref:
        return payload
    return _payload_from_update_check(latest, settings, resolved_current_version)


async def refresh_update_version_info(
    session: AsyncSession,
    settings: Settings,
    *,
    current_version: str | None = None,
) -> dict[str, Any]:
    resolved_current_version = current_version or current_app_version()
    payload = await get_update_version_info(
        settings, current_version=resolved_current_version
    )
    check = UpdateCheck(
        status="error" if payload.get("error") else "success",
        current_version=resolved_current_version,
        available_version=payload.get("available_version")
        if isinstance(payload.get("available_version"), str)
        else None,
        has_update=bool(payload.get("has_update")),
        repo=str(payload.get("repo") or ""),
        ref=str(payload.get("ref") or settings.github_ref),
        error=str(payload.get("error")) if payload.get("error") else None,
        details_json=json.dumps({"source": "github"}),
        checked_at=datetime.now(UTC),
    )
    session.add(check)
    await session.commit()
    await session.refresh(check)
    try:
        from app.services.status_service import invalidate_status_cache

        invalidate_status_cache(settings, prefill=True)
    except Exception:
        logger.warning("update.version_check.cache_invalidate_failed")
    return _payload_from_update_check(check, settings, resolved_current_version)


def _update_unit_name(run_id: int) -> str:
    return f"cnc-update-run-{run_id}.service"


def _read_update_log_excerpt(log_path: Path, max_chars: int) -> tuple[str, bool]:
    if not log_path.exists():
        return "", False
    max_chars = max(1, int(max_chars))
    read_bytes = (max_chars * 4) + 256
    with log_path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - read_bytes)
        handle.seek(start)
        content = handle.read().decode("utf-8", errors="replace")
    if start > 0 and "\n" in content:
        content = content.split("\n", 1)[1]
    return _clip_output(content, max_chars, tail=True)


def _unit_completed_successfully(unit_state: dict[str, str]) -> bool:
    load_state = unit_state.get("LoadState", "")
    active_state = unit_state.get("ActiveState", "")
    sub_state = unit_state.get("SubState", "")
    result_state = unit_state.get("Result", "")
    exec_main_status = unit_state.get("ExecMainStatus", "")

    if load_state in {"not-found", "error", "bad-setting"}:
        return False
    if active_state not in {"inactive", "active"}:
        return False
    if sub_state not in {"dead", "exited"}:
        return False
    if result_state not in {"", "success"}:
        return False
    if exec_main_status not in {"", "0"}:
        return False
    return True


def _build_launch_command(
    unit_name: str,
    script_path: Path,
    log_path: Path,
    *,
    runtime_timeout_sec: int,
) -> list[str]:
    runtime_timeout = max(1, int(runtime_timeout_sec))
    return [
        "systemd-run",
        "--no-block",
        "--unit",
        unit_name,
        "--description",
        f"cnc update run {unit_name}",
        "--property",
        "Type=oneshot",
        "--property",
        f"RuntimeMaxSec={runtime_timeout}s",
        "--property",
        f"TimeoutStartSec={runtime_timeout}s",
        "--property",
        f"StandardOutput=append:{log_path}",
        "--property",
        f"StandardError=append:{log_path}",
        str(script_path),
    ]


def _update_launch_timeout(settings: Settings) -> int:
    return min(settings.command_timeout_update_sec, UPDATE_LAUNCH_TIMEOUT_SEC)


async def _create_update_run(
    session: AsyncSession,
    *,
    status: str,
    message: str,
    details: dict[str, Any],
    commit: bool = True,
) -> UpdateRun:
    run = UpdateRun(
        status=status,
        message=message,
        details_json=json.dumps(details),
    )
    session.add(run)
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(run)
    return run


async def _update_run_record(
    session: AsyncSession,
    run: UpdateRun,
    *,
    status: str,
    message: str,
    details: dict[str, Any],
) -> UpdateRun:
    run.status = status
    run.message = message
    run.details_json = json.dumps(details)
    await session.commit()
    await session.refresh(run)
    return run


async def reconcile_update_run(
    session: AsyncSession,
    settings: Settings,
    run: UpdateRun | None,
    *,
    notification_mode: str = "inline",
) -> UpdateRun | None:
    if run is None or run.status not in PENDING_UPDATE_STATUSES:
        return run

    try:
        details = json.loads(run.details_json or "{}")
    except json.JSONDecodeError:
        details = {"raw": run.details_json}
    original_details = dict(details)

    unit_name = details.get("unit")
    log_path_raw = details.get("log_path")
    if not isinstance(unit_name, str) or not unit_name:
        details["unit_lookup_error"] = "pending update run has no systemd unit"
        return await _update_run_record(
            session,
            run,
            status="error",
            message="update failed",
            details=details,
        )

    log_excerpt = ""
    log_truncated = False
    if isinstance(log_path_raw, str) and log_path_raw:
        try:
            log_excerpt, log_truncated = _read_update_log_excerpt(
                Path(log_path_raw),
                _effective_update_log_excerpt_chars(settings),
            )
        except OSError as exc:
            details["log_read_error"] = str(exc)
            logger.warning(
                "update.run.log_read_failed",
                update_run_id=run.id,
                log_path=log_path_raw,
                error=str(exc),
            )
        details["log_excerpt"] = log_excerpt
        details["log_truncated"] = log_truncated
    log_terminal_status = _update_log_terminal_status(log_excerpt)
    if log_terminal_status:
        details["log_terminal_status"] = log_terminal_status
    else:
        details.pop("log_terminal_status", None)
    if UPDATE_LOG_MIGRATION_BOUNDARY_MARKER in log_excerpt.lower():
        details["migration_boundary_crossed"] = True
        details["rollback_after_migration"] = "disabled"
    else:
        details.pop("migration_boundary_crossed", None)
        details.setdefault("rollback_after_migration", "disabled")

    try:
        result = await run_command_checked_async(
            [
                "systemctl",
                "show",
                unit_name,
                "--property",
                ",".join(UPDATE_SYSTEMD_KEYS),
            ],
            timeout_sec=settings.command_timeout_status_sec,
        )
    except CommandError as exc:
        try:
            previous_failure_count = int(details.get("unit_lookup_failure_count") or 0)
        except (TypeError, ValueError):
            previous_failure_count = 0
        lookup_error = exc.result.stderr or exc.result.stdout or str(exc)
        now = datetime.now(UTC)
        retryable = command_result_is_retryable(exc.result)
        last_failed_at = _parse_utc_timestamp(details.get("unit_lookup_last_failed_at"))
        within_backoff = (
            retryable
            and previous_failure_count > 0
            and last_failed_at is not None
            and now - last_failed_at
            < timedelta(seconds=settings.transient_command_retry_backoff_sec)
        )
        failure_count = (
            previous_failure_count if within_backoff else previous_failure_count + 1
        )
        details["unit_lookup_error"] = lookup_error
        details["unit_lookup_failure_count"] = failure_count
        if not within_backoff:
            details["unit_lookup_last_failed_at"] = now.isoformat()
        details["unit_lookup_last_observed_at"] = now.isoformat()
        details["unit_lookup_backoff_active"] = within_backoff
        retrying = (
            retryable and failure_count <= settings.transient_command_retry_attempts
        )
        details["unit_lookup_retrying"] = retrying
        logger.warning(
            "update.run.unit_lookup_failed",
            update_run_id=run.id,
            unit=unit_name,
            retryable=retryable,
            retrying=retrying,
            failure_count=failure_count,
            backoff_active=within_backoff,
            retry_attempts=settings.transient_command_retry_attempts,
            error=lookup_error,
        )
        if retrying:
            return await _update_run_record(
                session,
                run,
                status="running",
                message="update status lookup failed; retrying",
                details=details,
            )
        updated_run = await _update_run_record(
            session,
            run,
            status="error",
            message="update failed",
            details=details,
        )
        await emit_control_event(
            settings,
            kind="update_failed",
            source="update",
            summary="Host update status lookup failed",
            severity="error",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{updated_run.id}"},
                {"label": "status", "value": "failed"},
                {"label": "reason", "value": lookup_error},
            ],
            details={
                "run_id": updated_run.id,
                "status": "error",
                "unit": unit_name,
                "unit_lookup_error": lookup_error,
                "log_path": details.get("log_path"),
            },
            notify=False,
        )
        if not original_details.get("pushover_notified_at"):
            notified = await _send_update_notification(
                settings,
                mode=notification_mode,
                title="CNC update failed",
                message=_build_update_failure_notification_message(
                    "Background update status lookup failed repeatedly.",
                    facts=[
                        ("run_id", updated_run.id),
                        ("phase", "update"),
                        ("unit", unit_name),
                        ("log_path", details.get("log_path") or ""),
                        ("error", lookup_error),
                    ],
                    log_excerpt=details.get("log_excerpt") or "",
                    action="cnc-admin logs admin --errors",
                ),
                event="update_failed",
                run_id=updated_run.id,
            )
            if notified:
                details["pushover_notified_at"] = datetime.now(UTC).isoformat()
                updated_run = await _update_run_record(
                    session,
                    updated_run,
                    status="error",
                    message="update failed",
                    details=details,
                )
        return updated_run

    unit_state = _parse_systemctl_show(result.stdout)
    details["unit_state"] = unit_state
    for transient_key in (
        "unit_lookup_error",
        "unit_lookup_failure_count",
        "unit_lookup_last_failed_at",
        "unit_lookup_last_observed_at",
        "unit_lookup_backoff_active",
        "unit_lookup_retrying",
    ):
        details.pop(transient_key, None)
    load_state = unit_state.get("LoadState", "")
    active_state = unit_state.get("ActiveState", "")
    sub_state = unit_state.get("SubState", "")
    result_state = unit_state.get("Result", "")

    next_status = run.status
    next_message = run.message
    failure_reason = ""
    unit_state_summary = ""
    if load_state in {"not-found", "error", "bad-setting"}:
        if load_state == "not-found" and log_terminal_status == "success":
            next_status = "success"
            next_message = "update completed"
            details["unit_gc_note"] = (
                "systemd transient unit was removed after the updater log completed"
            )
            details.pop("unit_lookup_error", None)
        else:
            next_status = "error"
            next_message = "update failed"
            details["unit_lookup_error"] = f"systemd unit {unit_name} is {load_state}"
            details.pop("unit_gc_note", None)
    elif active_state in {"activating", "reloading", "deactivating"} or sub_state in {
        "start-pre",
        "start",
        "running",
        "stop-sigterm",
        "stop-post",
    }:
        next_status = "running"
        next_message = "update running"
    elif _unit_completed_successfully(unit_state):
        next_status = "success"
        next_message = "update completed"
    elif active_state == "failed" or (result_state and result_state != "success"):
        next_status = "error"
        next_message = "update failed"

    if (
        next_status == run.status
        and next_message == run.message
        and details == original_details
    ):
        return run

    updated_run = await _update_run_record(
        session,
        run,
        status=next_status,
        message=next_message,
        details=details,
    )
    if next_status == "success":
        await emit_control_event(
            settings,
            kind="update_completed",
            source="update",
            summary="Host update completed",
            severity="success",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{updated_run.id}"},
                {"label": "status", "value": "success"},
            ],
            details={
                "run_id": updated_run.id,
                "status": "success",
                "log_path": details.get("log_path"),
            },
            notify=False,
        )
    elif next_status == "error":
        failure_reason = _systemd_failure_reason(
            unit_state, log_terminal_status=log_terminal_status
        )
        unit_state_summary = _systemd_state_summary(unit_state)
        await emit_control_event(
            settings,
            kind="update_failed",
            source="update",
            summary="Host update failed",
            severity="error",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{updated_run.id}"},
                {"label": "status", "value": "failed"},
                {"label": "reason", "value": failure_reason},
            ],
            details={
                "run_id": updated_run.id,
                "status": "error",
                "unit_state": unit_state,
                "log_path": details.get("log_path"),
                "reason": failure_reason,
            },
            notify=False,
        )
    if next_status == "error" and not original_details.get("pushover_notified_at"):
        notified = await _send_update_notification(
            settings,
            mode=notification_mode,
            title="CNC update failed",
            message=_build_update_failure_notification_message(
                "Background update run entered an error state.",
                facts=[
                    ("run_id", updated_run.id),
                    ("phase", "update"),
                    ("log_path", details.get("log_path") or ""),
                    ("error", failure_reason),
                    ("unit_state", unit_state_summary),
                ],
                log_excerpt=details.get("log_excerpt") or "",
                action="cnc-admin logs admin --errors",
            ),
            event="update_failed",
            run_id=updated_run.id,
        )
        if notified:
            details["pushover_notified_at"] = datetime.now(UTC).isoformat()
            updated_run = await _update_run_record(
                session,
                updated_run,
                status=next_status,
                message=next_message,
                details=details,
            )
    if next_status == "success" and not original_details.get(
        "pushover_success_notified_at"
    ):
        notified = await _send_update_notification(
            settings,
            mode=notification_mode,
            title="CNC update completed",
            message=build_operator_message(
                "Background update run completed.",
                status="success",
                facts=[
                    ("run_id", updated_run.id),
                    ("log_path", details.get("log_path") or ""),
                ],
                action="cnc-admin logs admin",
            ),
            event="update_completed",
            run_id=updated_run.id,
        )
        if notified:
            details["pushover_success_notified_at"] = datetime.now(UTC).isoformat()
            updated_run = await _update_run_record(
                session,
                updated_run,
                status=next_status,
                message=next_message,
                details=details,
            )
    return updated_run


async def run_update(session: AsyncSession, settings: Settings) -> ApplyResponse:
    script_path = settings.updater_script_path
    settings.update_log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    log_path = settings.update_log_dir / f"update-{stamp}.log"
    queued_run: UpdateRun | None = None
    logger.info(
        "update.run.requested",
        script=str(script_path),
        log_path=str(log_path),
        timeout_sec=settings.command_timeout_update_sec,
    )
    if not script_path.exists():
        details = {
            "error": f"updater script not found: {script_path}",
            "script": str(script_path),
            "log_path": str(log_path),
        }
        _write_update_log(
            str(log_path),
            script=str(script_path),
            status="error",
            stdout="",
            stderr=details["error"],
        )
        run = await _create_update_run(
            session,
            status="error",
            message="update failed",
            details=details,
        )
        await emit_control_event(
            settings,
            kind="update_failed",
            source="update",
            summary="Host update failed",
            severity="error",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{run.id}"},
                {"label": "status", "value": "failed"},
                {"label": "reason", "value": "missing updater script"},
            ],
            details={"run_id": run.id, **details},
            notify=False,
        )
        await send_pushover_notification_async(
            settings,
            title="CNC update failed",
            message=build_operator_message(
                "Update could not start because the updater script is missing.",
                status="failure",
                facts=[
                    ("run_id", run.id),
                    ("script", details.get("script") or ""),
                    ("error", details.get("error") or ""),
                ],
                action="cnc-admin logs admin --errors",
            ),
            priority=0,
            event="update_failed",
            source="update",
            run_id=run.id,
            url=admin_dashboard_url(settings, tab="home"),
            url_title="Open CNC",
        )
        return _serialize_update_run(run)

    try:
        async with host_mutation_operation(
            settings,
            kind="update_control_plane",
            phase="launch",
            details={"script": str(script_path), "log_path": str(log_path)},
        ) as operation:
            existing = (
                (
                    await session.execute(
                        select(UpdateRun)
                        .where(UpdateRun.status.in_(tuple(PENDING_UPDATE_STATUSES)))
                        .order_by(UpdateRun.id.desc())
                    )
                )
                .scalars()
                .first()
            )
            existing = await reconcile_update_run(session, settings, existing)
            if existing is not None and existing.status in PENDING_UPDATE_STATUSES:
                logger.info(
                    "update.run.already_pending",
                    update_run_id=existing.id,
                    status=existing.status,
                )
                await operation.complete(
                    "success",
                    phase="already_pending",
                    details={"update_run_id": existing.id, "status": existing.status},
                )
                return _serialize_update_run(existing)

            update_version = await refresh_update_version_info(session, settings)
            if not update_version.get("has_update"):
                raise UpdateRejectedError(
                    _version_rejection_message(update_version),
                    details={"update_version": update_version},
                )

            log_path.touch(exist_ok=True)
            os.chmod(log_path, 0o600)
            queued_run = await _create_update_run(
                session,
                status="queued",
                message="update queued",
                details={
                    "script": str(script_path),
                    "log_path": str(log_path),
                    "rollback_after_migration": "disabled",
                },
                commit=False,
            )
            unit_name = _update_unit_name(queued_run.id)
            launch_command = _build_launch_command(
                unit_name,
                script_path,
                log_path,
                runtime_timeout_sec=settings.command_timeout_update_sec,
            )
            details = {
                "script": str(script_path),
                "log_path": str(log_path),
                "unit": unit_name,
                "launch_command": launch_command,
                "rollback_after_migration": "disabled",
            }
            queued_run = await _update_run_record(
                session,
                queued_run,
                status="queued",
                message="update queued",
                details=details,
            )
            await operation.update(
                details={"update_run_id": queued_run.id, "unit": unit_name}
            )
            await run_command_checked_async(
                launch_command,
                timeout_sec=_update_launch_timeout(settings),
            )
            await operation.complete(
                "success",
                phase="queued",
                details={"update_run_id": queued_run.id, "unit": unit_name},
            )
        logger.info(
            "update.run.queued",
            update_run_id=queued_run.id,
            unit=unit_name,
            log_path=str(log_path),
        )
        await emit_control_event(
            settings,
            kind="update_queued",
            source="update",
            summary="Host update queued",
            severity="info",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{queued_run.id}"},
                {"label": "status", "value": "queued"},
                {"label": "unit", "value": unit_name},
            ],
            details={"run_id": queued_run.id, **details},
        )
        from app.services.status_service import invalidate_status_cache

        invalidate_status_cache(settings, prefill=True)
        return _serialize_update_run(queued_run)
    except HostMutationLockError as exc:
        logger.info("update.run.lock_blocked", script=str(script_path), error=str(exc))
        details = {
            "script": str(script_path),
            "log_path": str(log_path),
            "error": str(exc),
            "phase": "lock",
        }
        if queued_run is not None:
            run = await _update_run_record(
                session,
                queued_run,
                status="error",
                message="update blocked",
                details=details,
            )
        else:
            run = await _create_update_run(
                session,
                status="error",
                message="update blocked",
                details=details,
            )
        return _serialize_update_run(run)
    except CommandError as exc:
        logger.warning(
            "update.run.launch_failed",
            script=str(script_path),
            command=exc.result.command,
            stderr=(exc.result.stderr[:300] if exc.result.stderr else ""),
        )
        _write_update_log(
            str(log_path),
            script=str(script_path),
            status="error",
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
        stdout_view, stdout_truncated = _clip_output(
            exc.result.stdout, settings.update_output_max_chars
        )
        stderr_view, stderr_truncated = _clip_output(
            exc.result.stderr, settings.update_output_max_chars
        )
        details = {
            "script": str(script_path),
            "log_path": str(log_path),
            "stdout": stdout_view,
            "stderr": stderr_view,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        if queued_run is not None:
            run = await _update_run_record(
                session,
                queued_run,
                status="error",
                message="update failed",
                details=details,
            )
        else:
            run = await _create_update_run(
                session,
                status="error",
                message="update failed",
                details=details,
            )
        await emit_control_event(
            settings,
            kind="update_failed",
            source="update",
            summary="Host update failed",
            severity="error",
            scope="host",
            affects_all=True,
            subevents=[
                {"label": "run", "value": f"#{run.id}"},
                {"label": "status", "value": "failed"},
                {"label": "reason", "value": "launch failed"},
            ],
            details={"run_id": run.id, **details},
            notify=False,
        )
        await send_pushover_notification_async(
            settings,
            title="CNC update failed",
            message=build_operator_message(
                "Update could not launch the systemd-run job.",
                status="failure",
                facts=[
                    ("run_id", run.id),
                    ("script", details.get("script") or ""),
                    ("error", details.get("stderr") or details.get("stdout") or ""),
                ],
                action="cnc-admin logs admin --errors",
            ),
            priority=0,
            event="update_failed",
            source="update",
            run_id=run.id,
            url=admin_dashboard_url(settings, tab="home"),
            url_title="Open CNC",
        )
        return _serialize_update_run(run)
