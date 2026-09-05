from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.host_state import (
    LockedStateSectionTransaction,
    read_host_state_unlocked,
)


logger = get_logger("notifications.state")
HOST_STATE_SECTION = "notification_state"
PUSHOVER_DELIVERY_EVENT_KEY = "pushover_delivery"


def _read_notification_state_unlocked(path: Path) -> dict[str, Any]:
    payload = read_host_state_unlocked(path)
    section = payload.get(HOST_STATE_SECTION)
    if section is None:
        return {"events": {}}
    if not isinstance(section, dict):
        logger.warning("notification.state.invalid_payload", path=str(path))
        return {"events": {}}
    if not isinstance(section.get("events"), dict):
        logger.warning("notification.state.invalid_events", path=str(path))
        return {"events": {}}
    return dict(section)


def _log_notification_state_written(path: Path, payload: dict[str, Any]) -> None:
    logger.info(
        "notification.state.written",
        path=str(path),
        event_count=len(payload.get("events") or {})
        if isinstance(payload.get("events"), dict)
        else 0,
    )


class NotificationStateTransaction(
    AbstractContextManager["NotificationStateTransaction"]
):
    def __init__(
        self,
        settings: Settings,
        *,
        blocking: bool = True,
        lock_timeout_sec: float | None = None,
    ) -> None:
        self._path = settings.host_state_path
        self._state = LockedStateSectionTransaction(
            self._path,
            section=HOST_STATE_SECTION,
            blocking=blocking,
            lock_timeout_sec=lock_timeout_sec,
        )
        self._payload: dict[str, Any] | None = None

    def __enter__(self) -> "NotificationStateTransaction":
        self._state.__enter__()
        self._payload = self._state.value
        if self._ensure_shape():
            self._state.mark_dirty()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None and self._payload is not None and self._state.dirty:
            _log_notification_state_written(self._path, self._payload)
        self._state.__exit__(exc_type, exc, tb)

    def get_event(self, event_key: str) -> dict[str, Any]:
        events = self._events()
        entry = events.get(event_key)
        return dict(entry) if isinstance(entry, dict) else {}

    def set_event(self, event_key: str, entry: dict[str, Any]) -> None:
        self._events()[event_key] = dict(entry)
        self._state.mark_dirty()
        log_context = {
            "event_key": event_key,
            "status": str(entry.get("status") or "").strip(),
        }
        signature = str(entry.get("signature") or "").strip()
        if signature:
            log_context["signature"] = signature
        logger.info("notification.state.updated", **log_context)

    def get_pushover_outbox(self) -> dict[str, dict[str, Any]]:
        return {
            str(key): dict(value)
            for key, value in self._mapping("pushover_outbox").items()
            if isinstance(value, dict)
        }

    def set_pushover_outbox_entry(self, intent_id: str, entry: dict[str, Any]) -> None:
        self._mapping("pushover_outbox")[intent_id] = dict(entry)
        self._state.mark_dirty()

    def remove_pushover_outbox_entry(self, intent_id: str) -> None:
        outbox = self._mapping("pushover_outbox")
        if intent_id in outbox:
            del outbox[intent_id]
            self._state.mark_dirty()

    def get_pushover_receipt(self, emergency_key: str) -> dict[str, Any]:
        entry = self._mapping("pushover_receipts").get(emergency_key)
        return dict(entry) if isinstance(entry, dict) else {}

    def set_pushover_receipt(self, emergency_key: str, entry: dict[str, Any]) -> None:
        self._mapping("pushover_receipts")[emergency_key] = dict(entry)
        self._state.mark_dirty()

    def remove_pushover_receipt(self, emergency_key: str) -> None:
        receipts = self._mapping("pushover_receipts")
        if emergency_key in receipts:
            del receipts[emergency_key]
            self._state.mark_dirty()

    def get_pushover_deliveries(self) -> dict[str, dict[str, Any]]:
        return {
            str(key): dict(value)
            for key, value in self._mapping("pushover_deliveries").items()
            if isinstance(value, dict)
        }

    def set_pushover_delivery(self, dedupe_key: str, entry: dict[str, Any]) -> None:
        deliveries = self._mapping("pushover_deliveries")
        deliveries[dedupe_key] = dict(entry)
        if len(deliveries) > 128:
            oldest_keys = sorted(
                deliveries,
                key=lambda key: str(
                    deliveries.get(key, {}).get("delivered_at")
                    if isinstance(deliveries.get(key), dict)
                    else ""
                ),
            )[: len(deliveries) - 128]
            for key in oldest_keys:
                deliveries.pop(key, None)
        self._state.mark_dirty()

    def replace(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload)
        self._state.replace(self._payload)

    def _events(self) -> dict[str, Any]:
        assert self._payload is not None
        events = self._payload.get("events")
        if not isinstance(events, dict):
            events = {}
            self._payload["events"] = events
            self._state.mark_dirty()
        return events

    def _mapping(self, key: str) -> dict[str, Any]:
        assert self._payload is not None
        mapping = self._payload.get(key)
        if not isinstance(mapping, dict):
            mapping = {}
            self._payload[key] = mapping
            self._state.mark_dirty()
        return mapping

    def _ensure_shape(self) -> bool:
        assert self._payload is not None
        dirty = False
        if not isinstance(self._payload.get("events"), dict):
            logger.warning("notification.state.invalid_events", path=str(self._path))
            self._payload["events"] = {}
            dirty = True
        for key in (
            "pushover_outbox",
            "pushover_receipts",
            "pushover_deliveries",
        ):
            if not isinstance(self._payload.get(key), dict):
                self._payload[key] = {}
                dirty = True
        return dirty


def read_notification_state(settings: Settings) -> dict[str, Any]:
    with NotificationStateTransaction(settings) as transaction:
        return dict(transaction._payload or {"events": {}})


def write_notification_state(settings: Settings, payload: dict[str, Any]) -> None:
    with NotificationStateTransaction(settings) as transaction:
        transaction.replace(payload)


def get_notification_event(settings: Settings, event_key: str) -> dict[str, Any]:
    with NotificationStateTransaction(settings) as transaction:
        return transaction.get_event(event_key)


def update_notification_event(
    settings: Settings, event_key: str, entry: dict[str, Any]
) -> None:
    with NotificationStateTransaction(settings) as transaction:
        transaction.set_event(event_key, entry)


def get_pushover_delivery_summary(settings: Settings) -> dict[str, object]:
    try:
        delivery = get_notification_event(settings, PUSHOVER_DELIVERY_EVENT_KEY)
    except Exception:
        return {
            "delivery_status": "unknown",
            "delivery_label": "unknown",
            "delivery_at": "",
            "delivery_error": "",
            "delivery_event": "",
        }
    status = str(delivery.get("status") or "").strip() or "none"
    observed_at = str(delivery.get("last_observed_at") or "").strip()
    event = str(delivery.get("last_event") or "").strip()
    error = str(delivery.get("last_error") or delivery.get("last_reason") or "").strip()
    label = status
    if status == "sent" and event:
        label = f"sent ({event})"
    elif status == "failed" and event:
        label = f"failed ({event})"
    elif status == "skipped" and event:
        label = f"skipped ({event})"
    return {
        "delivery_status": status,
        "delivery_label": label,
        "delivery_at": observed_at,
        "delivery_error": error,
        "delivery_event": event,
    }
