"""Present output event history without losing diagnostic detail."""

from __future__ import annotations

import json

from app.models.entities import ControlEvent
from app.ui.view_models import (
    _format_bytes,
    _timestamp_data_value,
)


def _event_tone(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "success":
        return "success"
    if normalized == "warn":
        return "warn"
    if normalized == "error":
        return "error"
    return ""


_EVENT_VISIBLE_KEY_EXCLUDE = {
    "affects_all",
    "backend",
    "backend_name",
    "category",
    "kind",
    "message",
    "scope",
    "source",
    "status",
    "summary",
}


def _event_details(event: ControlEvent) -> dict[str, object]:
    try:
        details = json.loads(event.details_json or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    return details


def _event_visible_pairs(
    event: ControlEvent, details: dict[str, object], *, limit: int = 5
) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_pair(label: object, value: object) -> None:
        if len(pairs) >= limit:
            return
        normalized_label = str(label or "").strip()
        normalized_key = normalized_label.lower().replace("-", "_").replace(" ", "_")
        if (
            not normalized_label
            or normalized_key in _EVENT_VISIBLE_KEY_EXCLUDE
            or normalized_key in seen
        ):
            return
        if normalized_key == "path" or normalized_key.endswith("_path"):
            return
        if normalized_key.endswith("_id") and normalized_key[:-3] in seen:
            return
        if isinstance(value, (dict, list, tuple, set)):
            return
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return
        if normalized_key.endswith("size_bytes"):
            try:
                normalized_value = _format_bytes(int(normalized_value))
            except ValueError:
                pass
        seen.add(normalized_key)
        pairs.append(
            {"label": normalized_label.replace("_", " "), "value": normalized_value}
        )

    for subevent in event.subevents:
        add_pair(subevent.get("label"), subevent.get("value"))
    for label, value in details.items():
        add_pair(label, value)
    return pairs


def _event_detail_dump(event: ControlEvent, details: dict[str, object]) -> str:
    payload = {
        "kind": event.kind,
        "severity": event.severity,
        "scope": event.scope,
        "source": event.source,
        "backend": event.backend_name,
        "affects_all": bool(event.affects_all),
        "related_backends": event.related_backends,
        "summary": event.summary,
        "key_values": event.subevents,
        "details": details,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def event_rows(events: list[ControlEvent]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for event in events:
        details = _event_details(event)
        rows.append(
            {
                "summary": event.summary,
                "created_at": event.created_at.strftime("%b %d %H:%M")
                if event.created_at
                else "-",
                "created_at_raw": _timestamp_data_value(event.created_at),
                "tone": _event_tone(event.severity),
                "severity": event.severity or "info",
                "pairs": _event_visible_pairs(event, details),
                "details": _event_detail_dump(event, details),
            }
        )
    return rows
