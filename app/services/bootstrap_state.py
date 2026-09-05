from __future__ import annotations

import fcntl
import json
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.app_containers import control_dir

BOOTSTRAP_EVENT_LIMIT = 50
BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_STATUSES = {"pending", "running", "succeeded", "failed"}
RECONCILE_PHASE_STATUSES = {"pending", "running", "succeeded", "failed"}
RECONCILE_PHASES = (
    "prepare",
    "create",
    "bootstrap",
    "publish",
    "verify",
    "steady_state",
)
VALID_BOOTSTRAP_STATUS_TRANSITIONS: dict[str | None, set[str]] = {
    None: BOOTSTRAP_STATUSES,
    "pending": {"pending", "running", "failed", "succeeded"},
    "running": {"running", "failed", "succeeded"},
    "failed": {"failed", "running", "succeeded"},
    "succeeded": {"succeeded", "running", "failed"},
}


@dataclass(frozen=True)
class BootstrapEvent:
    timestamp: str
    kind: str
    name: str
    status: str
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class BootstrapStateModel:
    schema_version: int
    backend: str
    status: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    container_name: str | None = None
    container_id: str | None = None
    command: str | None = None
    failure_phase: str | None = None
    last_log_excerpt: str | None = None
    dns_servers: list[str] = field(default_factory=list)
    reconcile_phase: str | None = None
    reconcile_phase_status: str | None = None
    reconcile_details: dict[str, Any] | None = None
    events: list[BootstrapEvent] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["events"] = [event.as_dict() for event in self.events]
        return payload


def bootstrap_state_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "bootstrap.json"


def bootstrap_lock_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "bootstrap.lock"


def failure_artifact_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "failure.json"


def bootstrap_state_lock_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "bootstrap-state.lock"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _normalize_event(entry: Any) -> BootstrapEvent | None:
    if not isinstance(entry, dict):
        return None
    timestamp = entry.get("timestamp")
    kind = entry.get("kind")
    name = entry.get("name")
    status = entry.get("status")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (timestamp, kind, name, status)
    ):
        return None
    details = entry.get("details")
    if details is not None and not isinstance(details, dict):
        details = None
    return BootstrapEvent(
        timestamp=str(timestamp),
        kind=str(kind),
        name=str(name),
        status=str(status),
        details=details,
    )


def _bootstrap_events(payload: dict[str, Any]) -> list[BootstrapEvent]:
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return []
    events = [_normalize_event(entry) for entry in raw_events]
    return [event for event in events if event is not None]


def append_bootstrap_event(
    payload: dict[str, Any],
    *,
    kind: str,
    name: str,
    status: str,
    details: dict[str, Any] | None = None,
    limit: int = BOOTSTRAP_EVENT_LIMIT,
) -> dict[str, Any]:
    event = BootstrapEvent(
        timestamp=_utc_now(),
        kind=kind,
        name=name,
        status=status,
        details=details,
    )
    enriched = dict(payload)
    events = _bootstrap_events(enriched)
    events.append(event)
    enriched["events"] = [item.as_dict() for item in events[-max(1, int(limit)) :]]
    return enriched


def _normalize_bootstrap_state(
    payload: dict[str, Any] | None,
    *,
    backend_name: str,
    now: str | None = None,
) -> BootstrapStateModel | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    current_time = now or _utc_now()
    raw_status = payload.get("status")
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in BOOTSTRAP_STATUSES
        else "pending"
    )
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        schema_version = BOOTSTRAP_SCHEMA_VERSION

    reconcile_phase = payload.get("reconcile_phase")
    if not isinstance(reconcile_phase, str) or reconcile_phase not in RECONCILE_PHASES:
        reconcile_phase = None
    reconcile_phase_status = payload.get("reconcile_phase_status")
    if (
        not isinstance(reconcile_phase_status, str)
        or reconcile_phase_status not in RECONCILE_PHASE_STATUSES
    ):
        reconcile_phase_status = None
    reconcile_details = payload.get("reconcile_details")
    if reconcile_details is not None and not isinstance(reconcile_details, dict):
        reconcile_details = None

    started_at = (
        payload.get("started_at")
        if isinstance(payload.get("started_at"), str)
        else None
    )
    finished_at = (
        payload.get("finished_at")
        if isinstance(payload.get("finished_at"), str)
        else None
    )
    updated_at = (
        payload.get("updated_at")
        if isinstance(payload.get("updated_at"), str)
        else current_time
    )
    if status == "running" and not started_at:
        started_at = updated_at
    if status in {"failed", "succeeded"} and not finished_at:
        finished_at = updated_at

    dns_servers = payload.get("dns_servers")
    if not isinstance(dns_servers, list):
        dns_servers = []
    dns_servers = [entry for entry in dns_servers if isinstance(entry, str)]

    return BootstrapStateModel(
        schema_version=schema_version,
        backend=backend_name,
        status=status,
        updated_at=updated_at,
        started_at=started_at,
        finished_at=finished_at,
        container_name=payload.get("container_name")
        if isinstance(payload.get("container_name"), str)
        else None,
        container_id=payload.get("container_id")
        if isinstance(payload.get("container_id"), str)
        else None,
        command=payload.get("command")
        if isinstance(payload.get("command"), str)
        else None,
        failure_phase=payload.get("failure_phase")
        if isinstance(payload.get("failure_phase"), str)
        else None,
        last_log_excerpt=payload.get("last_log_excerpt")
        if isinstance(payload.get("last_log_excerpt"), str)
        else None,
        dns_servers=dns_servers,
        reconcile_phase=reconcile_phase,
        reconcile_phase_status=reconcile_phase_status,
        reconcile_details=reconcile_details,
        events=_bootstrap_events(payload)[-BOOTSTRAP_EVENT_LIMIT:],
    )


def read_bootstrap_state(
    settings: Settings, backend_name: str
) -> dict[str, Any] | None:
    path = bootstrap_state_path(settings, backend_name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    normalized = _normalize_bootstrap_state(
        payload if isinstance(payload, dict) else None, backend_name=backend_name
    )
    return normalized.as_dict() if normalized is not None else None


class BootstrapStateLock(AbstractContextManager["BootstrapStateLock"]):
    def __init__(self, settings: Settings, backend_name: str) -> None:
        self._path = bootstrap_state_lock_path(settings, backend_name)
        self._handle = None

    def __enter__(self) -> "BootstrapStateLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _assert_valid_transition(current_status: str | None, next_status: str) -> None:
    allowed = VALID_BOOTSTRAP_STATUS_TRANSITIONS.get(current_status, BOOTSTRAP_STATUSES)
    if next_status not in allowed:
        raise ValueError(
            f"invalid bootstrap state transition: {current_status or 'none'} -> {next_status}"
        )


def write_bootstrap_state(
    settings: Settings, backend_name: str, payload: dict[str, Any]
) -> None:
    directory = control_dir(settings, backend_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = bootstrap_state_path(settings, backend_name)
    tmp_path = path.with_suffix(".json.tmp")
    with BootstrapStateLock(settings, backend_name):
        now = _utc_now()
        current_state = _normalize_bootstrap_state(
            read_bootstrap_state(settings, backend_name),
            backend_name=backend_name,
            now=now,
        )
        current_payload = current_state.as_dict() if current_state is not None else {}
        enriched = dict(current_payload)
        enriched.update(payload)
        enriched["backend"] = backend_name
        enriched["schema_version"] = BOOTSTRAP_SCHEMA_VERSION
        enriched["updated_at"] = now
        next_status = enriched.get("status")
        if not isinstance(next_status, str) or next_status not in BOOTSTRAP_STATUSES:
            next_status = "pending"
            enriched["status"] = next_status
        _assert_valid_transition(
            current_state.status if current_state is not None else None, next_status
        )
        if "events" not in payload and current_state is not None:
            enriched["events"] = [event.as_dict() for event in current_state.events]
        normalized = _normalize_bootstrap_state(
            enriched, backend_name=backend_name, now=now
        )
        if normalized is None:
            raise ValueError(f"unable to normalize bootstrap state for {backend_name}")
        tmp_path.write_text(
            json.dumps(normalized.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp_path.replace(path)


def read_failure_artifact(
    settings: Settings, backend_name: str
) -> dict[str, Any] | None:
    path = failure_artifact_path(settings, backend_name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_failure_artifact(
    settings: Settings, backend_name: str, payload: dict[str, Any]
) -> None:
    directory = control_dir(settings, backend_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = failure_artifact_path(settings, backend_name)
    tmp_path = path.with_suffix(".json.tmp")
    enriched = dict(payload)
    enriched["backend"] = backend_name
    enriched["updated_at"] = _utc_now()
    tmp_path.write_text(
        json.dumps(enriched, indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp_path.replace(path)


def clear_failure_artifact(settings: Settings, backend_name: str) -> None:
    try:
        failure_artifact_path(settings, backend_name).unlink()
    except FileNotFoundError:
        return


class BackendBootstrapLock(AbstractContextManager["BackendBootstrapLock"]):
    def __init__(self, settings: Settings, backend_name: str) -> None:
        self._path = bootstrap_lock_path(settings, backend_name)
        self._handle = None

    def __enter__(self) -> "BackendBootstrapLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def bootstrap_lock_is_held(settings: Settings, backend_name: str) -> bool:
    try:
        with BackendBootstrapLock(settings, backend_name):
            return False
    except BlockingIOError:
        return True


def bootstrap_state_age_seconds(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    # Staleness is lack of progress, not total operation age. Writers refresh
    # updated_at on each meaningful bootstrap state transition or heartbeat.
    raw_started_at = payload.get("updated_at") or payload.get("started_at")
    if not isinstance(raw_started_at, str) or not raw_started_at.strip():
        return None
    try:
        started_at = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    elapsed = datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)
    return max(0, int(elapsed.total_seconds()))
