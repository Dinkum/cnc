from __future__ import annotations

from typing import Sequence

from app.config import Settings
from app.models.entities import Backend, Input
from app.services.commands import run_command
from app.services.renderers import SHIELD_BACKEND_NAME
from app.services.shield_runtime import SHIELD_CONTAINER_SERVICE


def _safe_input(item: Input) -> str:
    return str(item.hostname or "")


def _safe_input_kind(item: Input) -> str:
    return str(item.kind or "domain").strip().lower() or "domain"


def _is_shield_backend(backend: Backend) -> bool:
    return (
        str(backend.kind or "").strip().lower() == SHIELD_BACKEND_NAME
        and str(backend.name or "").strip().lower() == SHIELD_BACKEND_NAME
    )


def _shield_backend_exists(backends: Sequence[Backend]) -> Backend | None:
    for backend in backends:
        if _is_shield_backend(backend):
            return backend
    return None


def collect_shield_status(
    *,
    backends: Sequence[Backend],
    inputs: Sequence[Input],
    settings: Settings,
) -> dict[str, object]:
    shield_backend = _shield_backend_exists(backends)
    shield_inputs = [
        item
        for item in inputs
        if _safe_input_kind(item) == "shield"
        and any(_is_shield_backend(backend) for backend in item.backends)
    ]
    enabled_shield_inputs = [
        item
        for item in shield_inputs
        if item.enabled
        and any(
            _is_shield_backend(backend) and backend.enabled for backend in item.backends
        )
    ]

    service_result = run_command(
        ["systemctl", "is-active", SHIELD_CONTAINER_SERVICE],
        timeout_sec=settings.command_timeout_status_sec,
    )
    shield_service_active = (
        service_result.ok and str(service_result.stdout).strip().lower() == "active"
    )
    health_result = (
        run_command(
            [
                "curl",
                "-fsS",
                "--max-time",
                "2",
                f"http://127.0.0.1:{settings.shield_port}/health",
            ],
            timeout_sec=min(settings.command_timeout_status_sec, 3),
        )
        if shield_service_active
        else None
    )
    shield_health_ok = bool(health_result and health_result.ok)

    output_state = (
        "healthy"
        if shield_backend is not None
        and shield_backend.enabled
        and shield_service_active
        and shield_health_ok
        else ("missing" if shield_backend is None else "unhealthy")
    )

    server_enabled = bool(settings.shield_enabled)
    input_exists = bool(shield_inputs)
    config_ready = server_enabled and bool(enabled_shield_inputs)
    ready = config_ready and output_state == "healthy"

    return {
        "server_enabled": server_enabled,
        "backend_exists": shield_backend is not None,
        "backend_id": shield_backend.id if shield_backend else None,
        "backend_enabled": bool(shield_backend.enabled) if shield_backend else False,
        "output_state": output_state,
        "output_healthy": output_state == "healthy",
        "service_active": shield_service_active,
        "health_checked": health_result is not None,
        "health_error": ""
        if health_result is None or health_result.ok
        else (health_result.stderr or health_result.stdout),
        "input_exists": input_exists,
        "input_values": [_safe_input(item) for item in shield_inputs],
        "config_ready": config_ready,
        "ready": ready,
    }
