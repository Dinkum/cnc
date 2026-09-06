from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import secrets
import threading
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from app.services.thread_workers import BoundedThreadWorker
from app.config import Settings
from app.logger import get_logger, redact_sensitive_text
from app.services.file_locks import FileLock
from app.services.retry import is_retryable_http_exception


PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_RECEIPT_CANCEL_URL = (
    "https://api.pushover.net/1/receipts/{receipt}/cancel.json"
)
PUSHOVER_MESSAGE_LIMIT = 1024
PUSHOVER_TITLE_LIMIT = 100
PUSHOVER_EMERGENCY_RETRY_SEC = 300
PUSHOVER_EMERGENCY_EXPIRE_SEC = 3600
PUSHOVER_STATE_RETRY_TIMEOUT_SEC = 2.0
PUSHOVER_TRANSIENT_RETRY_SEC = 5
PUSHOVER_DEDUPE_WINDOW_SEC = 300
SENSITIVE_FACT_LABEL_RE = re.compile(
    r"(?:^|[ _-])(?:authorization|cookie|password|secret|token|access key|api key|csrf)(?:$|[ _-])",
    re.IGNORECASE,
)

logger = get_logger("notifications")
_notification_workers = BoundedThreadWorker(4)


@dataclass(frozen=True)
class PushoverResponse:
    accepted: bool
    request_id: str = ""
    receipt: str = ""


class PushoverDeliveryError(RuntimeError):
    retryable = False


class PushoverRejectedError(PushoverDeliveryError):
    pass


class PushoverTransientError(PushoverDeliveryError):
    retryable = True


def pushover_is_configured(settings: Settings) -> bool:
    return bool(
        str(settings.pushover_app_token).strip()
        and str(settings.pushover_user_key).strip()
    )


def mask_secret(value: str | None) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "off"
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return f"{'*' * (len(cleaned) - 4)}{cleaned[-4:]}"


def admin_dashboard_url(
    settings: Settings,
    *,
    path: str = "/",
    tab: str | None = None,
) -> str | None:
    host = _notification_admin_host(settings)
    if not host:
        return None
    normalized_path = path if path.startswith("/") else f"/{path}"
    query = urllib_parse.urlencode({"tab": tab}) if tab else ""
    return f"https://{host}{normalized_path}{'?' + query if query else ''}"


async def send_pushover_notification_async(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: int = 0,
    url: str | None = None,
    url_title: str | None = None,
    monospace: bool = True,
    retry: int | None = None,
    expire: int | None = None,
    event: str = "",
    source: str = "",
    backend: str = "",
    run_id: int | None = None,
    dedupe_key: str = "",
    emergency_key: str = "",
) -> bool:
    log_context = {
        "title": title.strip(),
        "priority": priority,
    }
    normalized_event = str(event or "").strip()
    normalized_source = str(source or "").strip()
    normalized_backend = str(backend or "").strip()
    if normalized_event:
        log_context["event"] = normalized_event
    if normalized_source:
        log_context["source"] = normalized_source
    if normalized_backend:
        log_context["backend"] = normalized_backend
    if run_id is not None:
        log_context["run_id"] = run_id
    if not pushover_is_configured(settings):
        logger.info(
            "notification.pushover.skipped", reason="not_configured", **log_context
        )
        await _notification_workers.run(
            _record_pushover_delivery_state,
            settings,
            status="skipped",
            event=normalized_event,
            source=normalized_source,
            backend=normalized_backend,
            run_id=run_id,
            title=title,
            priority=priority,
            reason="not_configured",
        )
        return False
    safe_title = redact_sensitive_text(title.strip())
    safe_message = redact_sensitive_text(message.strip())
    safe_url = _redact_notification_url(url)
    safe_url_title = redact_sensitive_text(url_title or "") or None
    try:
        intent_id, already_delivered = await _notification_workers.run(
            _enqueue_pushover_notification,
            settings,
            title=safe_title,
            message=safe_message,
            priority=priority,
            url=safe_url,
            url_title=safe_url_title,
            monospace=monospace,
            retry=retry,
            expire=expire,
            event=normalized_event,
            source=normalized_source,
            backend=normalized_backend,
            run_id=run_id,
            dedupe_key=dedupe_key,
            emergency_key=emergency_key,
        )
    except Exception as exc:
        # Prefer direct delivery to a silent drop if the durable state is
        # unexpectedly unavailable; production normally takes the outbox path.
        logger.error(
            "notification.pushover.outbox_failed",
            **_notification_error_context(exc),
            **log_context,
        )
        intent_id = ""
        already_delivered = False

    if already_delivered:
        logger.info("notification.pushover.deduplicated", **log_context)
        return True

    logger.info(
        "notification.pushover.attempt",
        intent_id=intent_id,
        durable=bool(intent_id),
        **log_context,
    )
    try:
        if intent_id:
            response = await _notification_workers.run(
                _dispatch_pushover_intent, settings, intent_id
            )
        else:
            direct_result = await _notification_workers.run(
                _send_pushover_notification,
                settings,
                title=safe_title,
                message=safe_message,
                priority=priority,
                url=safe_url,
                url_title=safe_url_title,
                monospace=monospace,
                retry=retry,
                expire=expire,
                return_result=True,
            )
            response = (
                direct_result
                if isinstance(direct_result, PushoverResponse)
                else PushoverResponse(accepted=bool(direct_result))
            )
        logger.info(
            "notification.pushover.sent",
            intent_id=intent_id,
            provider_request_id=response.request_id,
            receipt=response.receipt,
            **log_context,
        )
        await _notification_workers.run(
            _record_pushover_delivery_state,
            settings,
            status="sent",
            event=normalized_event,
            source=normalized_source,
            backend=normalized_backend,
            run_id=run_id,
            title=title,
            priority=priority,
        )
        return response.accepted
    except Exception as exc:
        deferred = bool(intent_id) and _pushover_error_is_retryable(exc)
        log_method = logger.warning if deferred else logger.error
        log_method(
            "notification.pushover.deferred"
            if deferred
            else "notification.pushover.failed",
            **_notification_error_context(exc),
            **log_context,
        )
        await _notification_workers.run(
            _record_pushover_delivery_state,
            settings,
            status="queued" if deferred else "failed",
            event=normalized_event,
            source=normalized_source,
            backend=normalized_backend,
            run_id=run_id,
            title=title,
            priority=priority,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


def _pushover_dedupe_key(
    *,
    title: str,
    message: str,
    event: str,
    source: str,
    backend: str,
    run_id: int | None,
    explicit: str,
) -> str:
    source_value = explicit.strip() or json.dumps(
        [event, source, backend, run_id, title, message],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(source_value.encode("utf-8")).hexdigest()


def _redact_notification_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urllib_parse.urlsplit(raw)
    hostname = parsed.hostname or ""
    if hostname:
        host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    else:
        netloc = parsed.netloc
    query = urllib_parse.urlencode(
        [
            (
                key,
                "[redacted]"
                if SENSITIVE_FACT_LABEL_RE.search(key.replace("_", " "))
                else redact_sensitive_text(item),
            )
            for key, item in urllib_parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
        ]
    )
    return urllib_parse.urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            query,
            redact_sensitive_text(parsed.fragment),
        )
    )


def _enqueue_pushover_notification(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: int,
    url: str | None,
    url_title: str | None,
    monospace: bool,
    retry: int | None,
    expire: int | None,
    event: str,
    source: str,
    backend: str,
    run_id: int | None,
    dedupe_key: str,
    emergency_key: str,
) -> tuple[str, bool]:
    from app.services.notification_state import NotificationStateTransaction

    normalized_dedupe_key = _pushover_dedupe_key(
        title=title,
        message=message,
        event=event,
        source=source,
        backend=backend,
        run_id=run_id,
        explicit=dedupe_key,
    )
    now = datetime.now(UTC)
    with NotificationStateTransaction(
        settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
    ) as state:
        for intent_id, entry in state.get_pushover_outbox().items():
            if entry.get("dedupe_key") == normalized_dedupe_key:
                return intent_id, False
        delivered = state.get_pushover_deliveries().get(normalized_dedupe_key, {})
        delivered_at = _parse_timestamp(delivered.get("delivered_at"))
        if (
            delivered_at is not None
            and (now - delivered_at).total_seconds() < PUSHOVER_DEDUPE_WINDOW_SEC
        ):
            return "", True
        intent_id = f"push_{secrets.token_hex(8)}"
        state.set_pushover_outbox_entry(
            intent_id,
            {
                "id": intent_id,
                "created_at": now.isoformat(timespec="seconds"),
                "next_attempt_at": now.isoformat(timespec="seconds"),
                "attempts": 0,
                "title": title,
                "message": message,
                "priority": priority,
                "url": url or "",
                "url_title": url_title or "",
                "monospace": bool(monospace),
                "retry": retry,
                "expire": expire,
                "event": event,
                "source": source,
                "backend": backend,
                "run_id": run_id,
                "dedupe_key": normalized_dedupe_key,
                "emergency_key": emergency_key.strip(),
            },
        )
    return intent_id, False


def _pushover_dispatch_lock_path(settings: Settings):
    return settings.host_state_path.with_name(
        f"{settings.host_state_path.name}.pushover-dispatch.lock"
    )


def _dispatch_pushover_intent(settings: Settings, intent_id: str) -> PushoverResponse:
    from app.services.notification_state import NotificationStateTransaction

    with FileLock(
        settings.host_state_path,
        lock_path=_pushover_dispatch_lock_path(settings),
        timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC,
    ):
        with NotificationStateTransaction(
            settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
        ) as state:
            intent = state.get_pushover_outbox().get(intent_id)
        if not isinstance(intent, dict):
            return PushoverResponse(accepted=True)
        next_attempt_at = _parse_timestamp(intent.get("next_attempt_at"))
        if next_attempt_at is not None and next_attempt_at > datetime.now(UTC):
            raise PushoverTransientError("pushover notification is queued for retry")
        try:
            result = _send_pushover_notification(
                settings,
                title=str(intent.get("title") or ""),
                message=str(intent.get("message") or ""),
                priority=int(intent.get("priority") or 0),
                url=str(intent.get("url") or "") or None,
                url_title=str(intent.get("url_title") or "") or None,
                monospace=bool(intent.get("monospace", True)),
                retry=(
                    int(intent["retry"])
                    if isinstance(intent.get("retry"), int)
                    else None
                ),
                expire=(
                    int(intent["expire"])
                    if isinstance(intent.get("expire"), int)
                    else None
                ),
                return_result=True,
            )
            response = (
                result
                if isinstance(result, PushoverResponse)
                else PushoverResponse(accepted=bool(result))
            )
        except Exception as exc:
            with NotificationStateTransaction(
                settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
            ) as state:
                current = state.get_pushover_outbox().get(intent_id)
                if isinstance(current, dict):
                    if _pushover_error_is_retryable(exc):
                        current["attempts"] = int(current.get("attempts") or 0) + 1
                        current["next_attempt_at"] = (
                            datetime.now(UTC)
                            + timedelta(seconds=PUSHOVER_TRANSIENT_RETRY_SEC)
                        ).isoformat(timespec="seconds")
                        current["last_error"] = _single_line(
                            redact_sensitive_text(exc), 240
                        )
                        state.set_pushover_outbox_entry(intent_id, current)
                    else:
                        state.remove_pushover_outbox_entry(intent_id)
            raise

        with NotificationStateTransaction(
            settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
        ) as state:
            state.remove_pushover_outbox_entry(intent_id)
            normalized_dedupe_key = str(intent.get("dedupe_key") or "")
            if normalized_dedupe_key:
                state.set_pushover_delivery(
                    normalized_dedupe_key,
                    {
                        "intent_id": intent_id,
                        "delivered_at": _utc_timestamp(),
                        "provider_request_id": response.request_id,
                    },
                )
            normalized_emergency_key = str(intent.get("emergency_key") or "").strip()
            if normalized_emergency_key and response.receipt:
                state.set_pushover_receipt(
                    normalized_emergency_key,
                    {
                        "receipt": response.receipt,
                        "intent_id": intent_id,
                        "created_at": _utc_timestamp(),
                    },
                )
        return response


def _pending_pushover_intents(settings: Settings) -> list[dict[str, Any]]:
    from app.services.notification_state import NotificationStateTransaction

    with NotificationStateTransaction(
        settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
    ) as state:
        return sorted(
            state.get_pushover_outbox().values(),
            key=lambda entry: str(entry.get("created_at") or ""),
        )


async def dispatch_pending_pushover_notifications_async(
    settings: Settings, *, limit: int = 5
) -> int:
    if not pushover_is_configured(settings):
        return 0
    try:
        pending = await _notification_workers.run(_pending_pushover_intents, settings)
    except Exception as exc:
        logger.error(
            "notification.pushover.outbox_read_failed",
            **_notification_error_context(exc),
        )
        return 0

    delivered = 0
    now = datetime.now(UTC)
    attempted = 0
    for intent in pending:
        if attempted >= max(0, limit):
            break
        next_attempt_at = _parse_timestamp(intent.get("next_attempt_at"))
        if next_attempt_at is not None and next_attempt_at > now:
            continue
        intent_id = str(intent.get("id") or "")
        if not intent_id:
            continue
        attempted += 1
        try:
            await _notification_workers.run(
                _dispatch_pushover_intent, settings, intent_id
            )
            delivered += 1
        except Exception as exc:
            logger.warning(
                "notification.pushover.pending_failed",
                intent_id=intent_id,
                **_notification_error_context(exc),
            )
    return delivered


async def run_pushover_outbox_dispatcher(settings: Settings) -> None:
    while True:
        await dispatch_pending_pushover_notifications_async(settings)
        await asyncio.sleep(PUSHOVER_TRANSIENT_RETRY_SEC)


def _send_pushover_notification(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: int,
    url: str | None,
    url_title: str | None,
    monospace: bool = True,
    retry: int | None = None,
    expire: int | None = None,
    return_result: bool = False,
) -> bool | PushoverResponse:
    result = _send_pushover_notification_once(
        settings,
        title=title,
        message=message,
        priority=priority,
        url=url,
        url_title=url_title,
        monospace=monospace,
        retry=retry,
        expire=expire,
    )
    return result if return_result else result.accepted


def _send_pushover_notification_once(
    settings: Settings,
    *,
    title: str,
    message: str,
    priority: int,
    url: str | None,
    url_title: str | None,
    monospace: bool = True,
    retry: int | None = None,
    expire: int | None = None,
) -> PushoverResponse:
    payload = {
        "token": str(settings.pushover_app_token).strip(),
        "user": str(settings.pushover_user_key).strip(),
        "title": _clip_value(
            redact_sensitive_text(title.strip()), PUSHOVER_TITLE_LIMIT
        ),
        "message": _clip_value(
            redact_sensitive_text(message.strip()), PUSHOVER_MESSAGE_LIMIT
        ),
        "priority": str(priority),
    }
    if url:
        payload["url"] = url
    if url_title:
        payload["url_title"] = url_title
    if monospace:
        payload["monospace"] = "1"
    if priority == 2:
        payload["retry"] = str(retry or PUSHOVER_EMERGENCY_RETRY_SEC)
        payload["expire"] = str(expire or PUSHOVER_EMERGENCY_EXPIRE_SEC)
    data = urllib_parse.urlencode(payload).encode("utf-8")
    request = urllib_request.Request(
        PUSHOVER_API_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(
            request, timeout=settings.command_timeout_status_sec
        ) as response:
            body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        if 500 <= int(exc.code) <= 599:
            raise PushoverTransientError(
                f"pushover temporarily unavailable (HTTP {exc.code})"
            ) from exc
        raise PushoverRejectedError(
            f"pushover rejected message (HTTP {exc.code})"
        ) from exc
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise PushoverTransientError("pushover request failed transiently") from exc
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PushoverRejectedError("pushover returned an invalid response") from exc
    if int(decoded.get("status") or 0) != 1:
        raise PushoverRejectedError("pushover rejected message")
    return PushoverResponse(
        accepted=True,
        request_id=str(decoded.get("request") or ""),
        receipt=str(decoded.get("receipt") or ""),
    )


async def cancel_pushover_emergency_async(
    settings: Settings, *, emergency_key: str
) -> bool:
    normalized_key = emergency_key.strip()
    if not normalized_key or not pushover_is_configured(settings):
        return False
    try:
        cancelled = await _notification_workers.run(
            _cancel_pushover_emergency, settings, normalized_key
        )
    except Exception as exc:
        logger.warning(
            "notification.pushover.emergency_cancel_failed",
            emergency_key=normalized_key,
            **_notification_error_context(exc),
        )
        return False
    if cancelled:
        logger.info(
            "notification.pushover.emergency_cancelled",
            emergency_key=normalized_key,
        )
    return cancelled


def _cancel_pushover_emergency(settings: Settings, emergency_key: str) -> bool:
    from app.services.notification_state import NotificationStateTransaction

    with FileLock(
        settings.host_state_path,
        lock_path=_pushover_dispatch_lock_path(settings),
        timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC,
    ):
        with NotificationStateTransaction(
            settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
        ) as state:
            pending_removed = False
            for intent_id, intent in state.get_pushover_outbox().items():
                if str(intent.get("emergency_key") or "") == emergency_key:
                    state.remove_pushover_outbox_entry(intent_id)
                    pending_removed = True
            receipt_entry = state.get_pushover_receipt(emergency_key)
        receipt = str(receipt_entry.get("receipt") or "").strip()
        if not receipt:
            return pending_removed
        data = urllib_parse.urlencode(
            {"token": str(settings.pushover_app_token).strip()}
        ).encode("utf-8")
        request = urllib_request.Request(
            PUSHOVER_RECEIPT_CANCEL_URL.format(
                receipt=urllib_parse.quote(receipt, safe="")
            ),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                request, timeout=settings.command_timeout_status_sec
            ) as response:
                body = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            if 500 <= int(exc.code) <= 599:
                raise PushoverTransientError(
                    f"pushover receipt cancellation unavailable (HTTP {exc.code})"
                ) from exc
            with NotificationStateTransaction(
                settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
            ) as state:
                state.remove_pushover_receipt(emergency_key)
            raise PushoverRejectedError(
                f"pushover rejected receipt cancellation (HTTP {exc.code})"
            ) from exc
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise PushoverTransientError(
                "pushover receipt cancellation failed transiently"
            ) from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PushoverRejectedError(
                "pushover returned an invalid cancellation response"
            ) from exc
        if int(decoded.get("status") or 0) != 1:
            raise PushoverRejectedError("pushover rejected receipt cancellation")
        with NotificationStateTransaction(
            settings, lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC
        ) as state:
            state.remove_pushover_receipt(emergency_key)
        return True


def build_failure_message(
    *,
    summary: str,
    details: dict[str, Any] | None = None,
) -> str:
    details = details if isinstance(details, dict) else {}
    phase = str(details.get("phase") or details.get("failed_phase") or "").strip()
    error = str(details.get("error") or details.get("stderr") or "").strip()
    parts = [summary.strip()]
    if phase:
        parts.append(f"phase: {phase}")
    if error:
        parts.append(f"error: {_single_line(error, 240)}")
    return "\n".join(part for part in parts if part)


def build_operator_message(
    summary: str,
    *,
    status: str = "",
    facts: list[tuple[str, object]] | None = None,
    action: str | None = None,
) -> str:
    parts = [
        f"status: {_render_status(status or _infer_status(summary))}",
        f"summary: {redact_sensitive_text(summary.strip())}",
    ]
    for label, value in facts or []:
        rendered_label = _render_fact_label(label)
        rendered = (
            "[redacted]"
            if SENSITIVE_FACT_LABEL_RE.search(rendered_label)
            else _render_fact_value(value)
        )
        if not rendered:
            continue
        parts.append(f"{rendered_label}: {rendered}")
    if action:
        parts.append(f"next: {_single_line(action, 240)}")
    return "\n".join(part for part in parts if part)


def summarize_list(values: list[str], *, limit: int = 3) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return ", ".join(normalized)
    shown = ", ".join(normalized[:limit])
    return f"{shown} (+{len(normalized) - limit} more)"


def _clip_value(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = " ...[truncated]"
    return value[: max(0, limit - len(suffix))] + suffix


def _single_line(value: str, limit: int) -> str:
    return _clip_value(" ".join(redact_sensitive_text(value).split()), limit)


def _render_fact_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return _single_line(summarize_list(items, limit=4), 240)
    return _single_line(str(value).strip(), 240)


def _render_fact_label(label: object) -> str:
    normalized = str(label or "").strip().replace("_", " ")
    return normalized or "detail"


def _render_status(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "ok": "success",
        "good": "success",
        "healthy": "success",
        "passed": "success",
        "done": "success",
        "failed": "failure",
        "fail": "failure",
        "bad": "failure",
        "error": "failure",
        "critical": "failure",
        "warn": "warning",
        "degraded": "warning",
    }
    return aliases.get(normalized, normalized or "info")


def _infer_status(summary: str) -> str:
    normalized = str(summary or "").lower()
    if any(
        term in normalized
        for term in ("failed", "down", "error", "could not", "missing")
    ):
        return "failure"
    if any(
        term in normalized
        for term in (
            "cleared",
            "recovered",
            "completed",
            "finished",
            "created",
            "saved",
        )
    ):
        return "success"
    if any(
        term in normalized
        for term in ("warning", "partially", "drift", "changed", "resized", "disabled")
    ):
        return "warning"
    return "info"


def _notification_admin_host(settings: Settings) -> str:
    candidates = []
    candidates.extend(
        str(settings.admin_allowed_hosts or "").replace("\n", ",").split(",")
    )
    candidates.append(str(settings.admin_host or ""))
    for candidate in candidates:
        host = str(candidate or "").strip().lower().rstrip(".")
        if not host or host in {"localhost", "127.0.0.1", "::1", "*"}:
            continue
        if host.startswith("http://") or host.startswith("https://"):
            parsed = urllib_parse.urlparse(host)
            host = parsed.netloc or parsed.path
        if not host:
            continue
        return host
    return ""


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record_pushover_delivery_state(
    settings: Settings,
    *,
    status: str,
    event: str,
    source: str,
    backend: str,
    run_id: int | None,
    title: str,
    priority: int,
    reason: str = "",
    error: str = "",
    error_type: str = "",
) -> None:
    payload = {
        "status": status,
        "event": event,
        "source": source,
        "backend": backend,
        "run_id": run_id,
        "title": title,
        "priority": priority,
        "reason": reason,
        "error": error,
        "error_type": error_type,
    }
    try:
        _write_pushover_delivery_state(settings, blocking=False, **payload)
    except BlockingIOError:
        logger.info(
            "notification.pushover.state_retrying",
            reason="state_lock_busy",
            event=event,
        )
        _retry_pushover_delivery_state(settings, payload)
    except Exception as exc:
        logger.warning(
            "notification.pushover.state_failed", error=str(exc), event=event
        )


def _write_pushover_delivery_state(
    settings: Settings,
    *,
    blocking: bool,
    status: str,
    event: str,
    source: str,
    backend: str,
    run_id: int | None,
    title: str,
    priority: int,
    reason: str = "",
    error: str = "",
    error_type: str = "",
    lock_timeout_sec: float | None = None,
) -> None:
    from app.services.notification_state import (
        PUSHOVER_DELIVERY_EVENT_KEY,
        NotificationStateTransaction,
    )

    now = _utc_timestamp()
    with NotificationStateTransaction(
        settings,
        blocking=blocking,
        lock_timeout_sec=lock_timeout_sec,
    ) as state:
        previous = state.get_event(PUSHOVER_DELIVERY_EVENT_KEY)
        entry = dict(previous)
        entry.update(
            {
                "status": status,
                "last_observed_at": now,
                "last_event": event,
                "last_source": source,
                "last_backend": backend,
                "last_run_id": run_id,
                "last_title": title.strip(),
                "last_priority": priority,
                "last_reason": reason,
                "last_error": _single_line(error, 240),
                "last_error_type": error_type,
            }
        )
        if status == "sent":
            entry["last_sent_at"] = now
        elif status == "failed":
            entry["last_failed_at"] = now
        elif status == "queued":
            entry["last_queued_at"] = now
        elif status == "skipped":
            entry["last_skipped_at"] = now
        state.set_event(PUSHOVER_DELIVERY_EVENT_KEY, entry)


def _retry_pushover_delivery_state(
    settings: Settings, payload: dict[str, object]
) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _write_pushover_delivery_state_with_timeout(settings, payload)
        return

    thread = threading.Thread(
        target=_write_pushover_delivery_state_with_timeout,
        args=(settings, payload),
        name="cnc-pushover-state-retry",
        daemon=False,
    )
    thread.start()


def _write_pushover_delivery_state_with_timeout(
    settings: Settings, payload: dict[str, object]
) -> None:
    try:
        _write_pushover_delivery_state(
            settings,
            blocking=True,
            lock_timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC,
            **payload,
        )
    except BlockingIOError:
        logger.warning(
            "notification.pushover.state_retry_timeout",
            event=payload.get("event") or "",
            timeout_sec=PUSHOVER_STATE_RETRY_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.warning(
            "notification.pushover.state_retry_failed",
            error=str(exc),
            event=payload.get("event") or "",
        )


def _notification_error_context(exc: Exception) -> dict[str, object]:
    context: dict[str, object] = {
        "error": redact_sensitive_text(exc),
        "error_type": type(exc).__name__,
        "retryable": _pushover_error_is_retryable(exc),
    }
    if isinstance(exc, urllib_error.HTTPError):
        context["http_status"] = exc.code
        if exc.reason:
            context["reason"] = str(exc.reason)
    elif isinstance(exc, urllib_error.URLError):
        context["reason"] = str(exc.reason)
    return context


def _pushover_error_is_retryable(exc: Exception) -> bool:
    if isinstance(exc, PushoverDeliveryError):
        return exc.retryable
    if isinstance(exc, urllib_error.HTTPError):
        return 500 <= int(exc.code) <= 599
    return is_retryable_http_exception(exc) and not isinstance(
        exc, urllib_error.HTTPError
    )


async def send_notification_test_suite(
    settings: Settings,
    *,
    backend_name: str,
) -> dict[str, Any]:
    logger.info("notification.test.started", backend=backend_name)
    notifications: list[dict[str, Any]] = []
    scenarios = [
        (
            "host_drift_detected",
            "CNC host drift detected",
            build_operator_message(
                "Host drift detected during CNC startup. CNC stayed online.",
                status="warning",
                facts=[
                    ("top_finding", "tailscale funnel exposes admin port 9090"),
                    ("finding_count", 1),
                ],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "host_drift_cleared",
            "CNC host drift cleared",
            build_operator_message(
                "Startup host drift cleared.",
                status="success",
                facts=[("cleared_findings", "tailscale admin exposure")],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "apply_failed",
            "CNC apply failed",
            build_operator_message(
                "Apply did not finish cleanly.",
                status="failure",
                facts=[
                    ("phase", "app_bootstrap"),
                    ("error", "bootstrap lock timed out for backend web"),
                ],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "apply_completed_warning",
            "CNC apply completed with warnings",
            build_operator_message(
                "Apply completed, but one or more output health checks need review.",
                status="warning",
                facts=[
                    ("run_id", 14),
                    ("outputs", 3),
                    ("healthcheck_failures", f"{backend_name}: loopback probe failed"),
                ],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "update_completed",
            "CNC update completed",
            build_operator_message(
                "Update finished successfully.",
                status="success",
                facts=[("result", "services reconciled and healthy")],
                action="cnc-admin logs admin --lines 100",
            ),
            0,
        ),
        (
            "update_failed",
            "CNC update failed",
            build_operator_message(
                "Update entered an error state.",
                status="failure",
                facts=[
                    ("run_id", 12),
                    ("error", "systemd update unit failed"),
                ],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "auto_size_resized",
            "CNC auto-size resized outputs",
            build_operator_message(
                "Auto-size changed output resource sizes.",
                status="success",
                facts=[
                    ("outputs", 1),
                    (
                        "changes",
                        f"{backend_name}: small->medium (sustained_pressure, cpu p95 92%, memory p95 88%)",
                    ),
                    ("apply_run", "#9"),
                ],
                action="cnc-admin logs admin",
            ),
            0,
        ),
        (
            "cloudflare_sync_failed",
            "CNC Cloudflare sync failed",
            build_operator_message(
                "Cloudflare IP sync failed before ingress rules were converged.",
                status="failure",
                facts=[
                    ("phase", "firewall_reload"),
                    ("rollback", "ok"),
                    ("error", "ufw reload timed out"),
                ],
                action="cnc-admin cloudflare sync --json",
            ),
            0,
        ),
        (
            "backend_auto_fix_failed",
            f"CNC backend auto-fix failed: {backend_name}",
            build_operator_message(
                "Backend auto-fix completed but did not recover the output.",
                status="failure",
                facts=[
                    ("backend", backend_name),
                    ("diagnosis", "loopback_unreachable"),
                    ("issues", "container_unhealthy, loopback_unreachable"),
                    ("error", "service restart did not restore loopback reachability"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "backup_failed",
            f"CNC backup failed: {backend_name}",
            build_operator_message(
                "Backend backup failed before a restorable bundle was produced.",
                status="failure",
                facts=[
                    ("backend", backend_name),
                    ("backup_id", 42),
                    ("scope", "mounted_data"),
                    ("error", "disk full while writing backup bundle"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "backup_import_failed",
            f"CNC backup import failed: {backend_name}",
            build_operator_message(
                "Backup bundle import failed before it was added to history.",
                status="failure",
                facts=[
                    ("backend", backend_name),
                    ("backup_id", 43),
                    ("bundle", f"{backend_name}-backup.tar"),
                    ("error", "bundle manifest verification failed"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "restore_completed",
            f"CNC restore completed: {backend_name}",
            build_operator_message(
                "Backend restore completed.",
                status="success",
                facts=[
                    ("backend", backend_name),
                    ("restored_paths", 3),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "backend_restored",
            f"CNC backend restored: {backend_name}",
            build_operator_message(
                "Backup bundle restored as a new backend.",
                status="success",
                facts=[
                    ("backend", backend_name),
                    ("backup_id", 44),
                    ("restored_paths", 3),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "restore_failed",
            f"CNC restore failed: {backend_name}",
            build_operator_message(
                "Backend restore failed before the output was returned to a verified state.",
                status="failure",
                facts=[
                    ("backend", backend_name),
                    ("backup_id", 44),
                    ("error", "backup bundle verification failed"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "clone_failed",
            f"CNC clone failed: {backend_name}-copy",
            build_operator_message(
                "Output clone failed before the new backend was ready.",
                status="failure",
                facts=[
                    ("source_backend", backend_name),
                    ("clone_backend", f"{backend_name}-copy"),
                    ("error", "target port is already allocated"),
                ],
                action=f"cnc-admin app doctor {backend_name}",
            ),
            0,
        ),
        (
            "internal_error",
            "CNC internal error",
            build_operator_message(
                "GET /status raised RuntimeError.",
                status="failure",
                facts=[
                    ("error_code", "CNC-09001"),
                    ("error_name", "INTERNAL_UNHANDLED_EXCEPTION"),
                    ("error_inst", "7K2Q9M4D"),
                    ("request_id", "req-test"),
                ],
                action="cnc-admin logs admin --errors",
            ),
            0,
        ),
        (
            "settings_changed",
            "CNC security setting changed",
            build_operator_message(
                "Admin access key was saved.",
                status="warning",
                facts=[("setting", "access_key"), ("state", "enabled")],
                action="cnc-admin logs admin --lines 100",
            ),
            0,
        ),
        (
            "shield_settings_changed",
            "CNC security setting changed",
            build_operator_message(
                "Shield global setting changed.",
                status="warning",
                facts=[("setting", "shield"), ("state", "enabled")],
                action="cnc-admin logs admin --lines 100",
            ),
            0,
        ),
        (
            "pushover_settings_changed",
            "CNC notification setting changed",
            build_operator_message(
                "Pushover notification credentials were saved.",
                status="warning",
                facts=[
                    ("app_token_changed", "yes"),
                    ("user_key_changed", "yes"),
                ],
                action="cnc-admin logs admin --lines 100",
            ),
            0,
        ),
    ]

    for kind, title, message, priority in scenarios:
        sent = await send_pushover_notification_async(
            settings,
            title=title,
            message=message,
            priority=priority,
            event=kind,
            source="notifications.test",
            backend=backend_name,
        )
        logger.info(
            "notification.test.scenario",
            backend=backend_name,
            kind=kind,
            title=title,
            sent=sent,
        )
        notifications.append({"kind": kind, "title": title, "sent": sent})

    logger.info(
        "notification.test.completed",
        backend=backend_name,
        notification_count=len(notifications),
    )
    return {
        "backend": backend_name,
        "notifications_configured": pushover_is_configured(settings),
        "notifications": notifications,
        "ok": all(bool(item["sent"]) for item in notifications),
    }
