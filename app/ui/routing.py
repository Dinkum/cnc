"""Canonical input/output route projections, attachment options, and shield presentation."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.models.entities import Backend, Input
from app.services.status_service import dashboard_overview_counts
from app.services.tailscale_urls import tailscale_service_url
from app.services.validators import ensure_input_kind
from app.ui.view_models import (
    _change_state,
    _format_timestamp,
    _timestamp_data_value,
)


def get_input_value(item: Input) -> str:
    return str(item.hostname or "")


def get_input_kind(item: Input) -> str:
    return ensure_input_kind(str(item.kind or "domain"))


def input_kind_label(kind: str) -> str:
    if kind == "domain":
        return "domain"
    if kind == "shield":
        return "shield"
    if kind == "tailnet_service":
        return "tailscale subdomain"
    return "tailnet path"


def _tailnet_service_route_label(input_value: str, settings: Settings) -> str:
    return (
        tailscale_service_url(input_value, settings)
        or f"tailscale subdomain {input_value}"
    )


def build_backend_input_maps(
    inputs: list[Input],
) -> tuple[dict[int, list[int]], dict[int, list[str]]]:
    backend_input_ids: dict[int, list[int]] = {}
    backend_input_labels: dict[int, list[str]] = {}
    for item in inputs:
        label = f"{input_kind_label(get_input_kind(item))}: {get_input_value(item)}"
        for backend in sorted(item.backends, key=lambda backend: backend.id):
            backend_input_ids.setdefault(backend.id, []).append(item.id)
            backend_input_labels.setdefault(backend.id, []).append(label)
    return backend_input_ids, backend_input_labels


def build_input_attach_options(
    inputs: list[Input],
    *,
    current_backend_id: int | None = None,
) -> list[dict[str, object]]:
    options: list[dict[str, object]] = []
    for item in inputs:
        input_kind = get_input_kind(item)
        input_value = get_input_value(item)
        attached_to_current = current_backend_id is not None and any(
            backend.id == current_backend_id for backend in item.backends
        )
        enabled_backends = [
            backend
            for backend in item.backends
            if backend.enabled and backend.id != current_backend_id
        ]
        unavailable_reason = ""
        if (
            input_kind in {"shield", "tailnet_path", "tailnet_service"}
            and enabled_backends
        ):
            names = ", ".join(backend.name for backend in enabled_backends[:2])
            if len(enabled_backends) > 2:
                names += f" +{len(enabled_backends) - 2}"
            unavailable_reason = f"already attached to {names}"
        elif input_kind == "domain":
            static_backends = [
                backend for backend in enabled_backends if backend.kind == "static"
            ]
            if static_backends:
                names = ", ".join(backend.name for backend in static_backends[:2])
                if len(static_backends) > 2:
                    names += f" +{len(static_backends) - 2}"
                unavailable_reason = f"static route already attached to {names}"
        options.append(
            {
                "id": item.id,
                "value": input_value,
                "kind": input_kind,
                "kind_label": input_kind_label(input_kind),
                "attachable": not unavailable_reason,
                "attached": attached_to_current,
                "unavailable_reason": unavailable_reason,
            }
        )
    return options


def build_attached_input_attach_options(
    inputs: list[Input],
) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "value": get_input_value(item),
            "kind": get_input_kind(item),
            "kind_label": input_kind_label(get_input_kind(item)),
            "attachable": True,
            "attached": True,
            "unavailable_reason": "",
        }
        for item in sorted(inputs, key=lambda item: item.id)
    ]


def build_shield_status(
    backends: list[Backend],
    inputs: list[Input],
    settings: Settings,
    service_map: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    shield_backend = next(
        (backend for backend in backends if backend.kind == "shield"), None
    )
    shield_inputs = [
        item
        for item in inputs
        if get_input_kind(item) == "shield"
        and any(backend.kind == "shield" for backend in item.backends)
    ]
    enabled_shield_inputs = [
        item
        for item in shield_inputs
        if item.enabled
        and any(
            backend.kind == "shield" and backend.enabled for backend in item.backends
        )
    ]
    server_enabled = bool(settings.shield_enabled)
    shield_service = (service_map or {}).get("cnc-shield.service") or {}
    shield_service_data = (
        shield_service.get("data")
        if isinstance(shield_service.get("data"), dict)
        else {}
    )
    shield_service_active = (
        bool(shield_service.get("ok"))
        and str(shield_service_data.get("ActiveState") or "").strip().lower()
        == "active"
    )
    output_state = (
        "healthy"
        if shield_backend is not None
        and shield_backend.enabled
        and shield_service_active
        else ("missing" if shield_backend is None else "unhealthy")
    )
    config_ready = server_enabled and bool(enabled_shield_inputs)
    ready = config_ready and output_state == "healthy"
    return {
        "server_enabled": server_enabled,
        "backend_exists": shield_backend is not None,
        "backend_id": shield_backend.id if shield_backend else None,
        "backend_enabled": bool(shield_backend.enabled) if shield_backend else False,
        "output_state": output_state,
        "output_healthy": output_state == "healthy",
        "input_exists": bool(shield_inputs),
        "input_values": [get_input_value(item) for item in shield_inputs],
        "config_ready": config_ready,
        "ready": ready,
    }


_SINGLE_OUTPUT_INPUT_KINDS = {"shield", "tailnet_path", "tailnet_service"}

_INPUT_MULTI_OUTPUT_TARGETS = {
    "shield": "shield inputs can mount only one output",
    "tailnet_path": "tailnet paths can mount only one output",
    "tailnet_service": "tailnet services can mount only one output",
}


def _route_label(input_kind: str, input_value: str, settings: Settings) -> str:
    if input_kind == "tailnet_path":
        return f"tailnet {input_value}"
    if input_kind == "tailnet_service":
        return _tailnet_service_route_label(input_value, settings)
    return input_value


def _backend_route_target(
    backend: Backend,
    *,
    input_kind: str,
    input_value: str,
    settings: Settings,
) -> str:
    if backend.kind == "app":
        destination = (
            f"http://127.0.0.1:{backend.port}"
            if backend.port
            else "port assigned on save"
        )
        return (
            f"{_route_label(input_kind, input_value, settings)} -> {destination}"
            if input_kind in {"tailnet_path", "tailnet_service"}
            else destination
        )
    if backend.kind == "shield":
        return f"{input_value} -> Shield access gate"
    target = backend.static_root or "-"
    return (
        f"{_route_label(input_kind, input_value, settings)} -> {target}"
        if input_kind in {"tailnet_path", "tailnet_service"}
        else target
    )


def _route_row_state(
    *,
    input_enabled: bool,
    backend: Backend,
    input_kind: str,
    enabled_backend_count: int,
    enabled_kind_count: int,
    enabled_static_backend_count: int,
) -> str:
    if not input_enabled or not backend.enabled:
        return "inactive"
    if (
        enabled_kind_count > 1
        or (input_kind in _SINGLE_OUTPUT_INPUT_KINDS and enabled_backend_count > 1)
        or (backend.kind == "static" and enabled_static_backend_count > 1)
    ):
        return "error"
    return "active"


def _input_route_view(
    item: Input,
    *,
    input_kind: str,
    input_value: str,
    settings: Settings,
    include_routing_rows: bool,
) -> tuple[list[Backend], list[str], str, str, str, str, list[dict[str, str | bool]]]:
    attached_backends = sorted(item.backends, key=lambda backend: backend.id)
    enabled_backends = [backend for backend in attached_backends if backend.enabled]
    attached_names = [backend.name for backend in attached_backends]

    backend_summary = "none"
    backend_kind = "-"
    route_state = "inactive"
    target = "no outputs attached"
    enabled_kind_count = 0
    enabled_static_backend_count = 0
    summary_conflict = False

    if attached_backends:
        attached_kinds = {backend.kind for backend in attached_backends}
        enabled_kind_count = len({backend.kind for backend in enabled_backends})
        enabled_static_backend_count = sum(
            1 for backend in enabled_backends if backend.kind == "static"
        )
        backend_summary = ", ".join(attached_names[:3]) + (
            f" +{len(attached_names) - 3}" if len(attached_names) > 3 else ""
        )
        backend_kind = (
            attached_backends[0].kind if len(attached_kinds) == 1 else "mixed"
        )
        summary_conflict = (
            len(attached_kinds) > 1
            or (input_kind in _SINGLE_OUTPUT_INPUT_KINDS and len(enabled_backends) > 1)
            or (backend_kind == "static" and len(enabled_backends) > 1)
        )
        route_state = (
            "inactive"
            if not enabled_backends or not item.enabled
            else ("error" if summary_conflict else "active")
        )
        if len(attached_kinds) > 1:
            target = "mixed output kinds are not routable"
        elif input_kind in _INPUT_MULTI_OUTPUT_TARGETS and len(enabled_backends) > 1:
            target = _INPUT_MULTI_OUTPUT_TARGETS[input_kind]
        elif backend_kind == "app":
            target = (
                "all attached outputs are disabled"
                if not enabled_backends
                else (
                    _backend_route_target(
                        enabled_backends[0],
                        input_kind=input_kind,
                        input_value=input_value,
                        settings=settings,
                    )
                    if input_kind in {"tailnet_path", "tailnet_service"}
                    else ", ".join(
                        _backend_route_target(
                            backend,
                            input_kind=input_kind,
                            input_value=input_value,
                            settings=settings,
                        )
                        for backend in enabled_backends
                    )
                )
            )
        else:
            target = _backend_route_target(
                enabled_backends[0] if enabled_backends else attached_backends[0],
                input_kind=input_kind,
                input_value=input_value,
                settings=settings,
            )

    routing_rows: list[dict[str, str | bool]] = []
    if include_routing_rows and not attached_backends:
        routing_rows.append(
            {
                "input_value": input_value,
                "input_kind": input_kind,
                "input_enabled": item.enabled,
                "backend_name": "none",
                "backend_enabled": False,
                "kind": "-",
                "target": target,
                "route_state": route_state,
            }
        )
    elif include_routing_rows:
        routing_rows.extend(
            {
                "input_value": input_value,
                "input_kind": input_kind,
                "input_enabled": item.enabled,
                "backend_name": backend.name,
                "backend_enabled": backend.enabled,
                "kind": backend.kind,
                "target": _backend_route_target(
                    backend,
                    input_kind=input_kind,
                    input_value=input_value,
                    settings=settings,
                ),
                "route_state": _route_row_state(
                    input_enabled=item.enabled,
                    backend=backend,
                    input_kind=input_kind,
                    enabled_backend_count=len(enabled_backends),
                    enabled_kind_count=enabled_kind_count,
                    enabled_static_backend_count=enabled_static_backend_count,
                ),
            }
            for backend in attached_backends
        )

    return (
        attached_backends,
        attached_names,
        backend_summary,
        backend_kind,
        route_state,
        target,
        routing_rows,
    )


def build_input_details(
    *,
    inputs: list[Input],
    last_applied_at: datetime | None,
    settings: Settings,
    include_routing_rows: bool = True,
) -> tuple[dict[int, dict[str, object]], list[dict[str, str | bool]], int]:
    input_details: dict[int, dict[str, object]] = {}
    routing_rows: list[dict[str, str | bool]] = []
    pending_inputs = 0

    for item in inputs:
        change_state = _change_state(
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_applied_at=last_applied_at,
        )
        if change_state is not None:
            pending_inputs += 1
        input_kind = get_input_kind(item)
        input_value = get_input_value(item)
        route = _input_route_view(
            item,
            input_kind=input_kind,
            input_value=input_value,
            settings=settings,
            include_routing_rows=include_routing_rows,
        )
        (
            attached_backends,
            attached_names,
            backend_summary,
            backend_kind,
            route_state,
            target,
            route_rows,
        ) = route
        routing_rows.extend(route_rows)

        input_details[item.id] = {
            "input_id": item.id,
            "kind": input_kind,
            "kind_label": input_kind_label(input_kind),
            "value": input_value,
            "enabled": item.enabled,
            "created_at": _format_timestamp(item.created_at) or "-",
            "created_at_raw": _timestamp_data_value(item.created_at),
            "updated_at": _format_timestamp(item.updated_at) or "-",
            "updated_at_raw": _timestamp_data_value(item.updated_at),
            "backend_name": backend_summary,
            "backend_kind": backend_kind,
            "target": target,
            "route_state": route_state,
            "input_state": "enabled" if item.enabled else "disabled",
            "shield_enabled": bool(getattr(item, "shield_enabled", False)),
            "shield_code_configured": bool(
                str(getattr(item, "shield_code_hash", "") or "").strip()
            ),
            "shield_access_code": str(getattr(item, "shield_access_code", "") or ""),
            "backend_ids": item.backend_ids,
            "backend_names": attached_names,
            "backend_tags": [
                {"id": backend.id, "name": backend.name}
                for backend in attached_backends
            ],
            "change_state": change_state,
        }

    return input_details, routing_rows, pending_inputs


def pending_input_count(inputs: list[Input], last_applied_at: datetime | None) -> int:
    return sum(
        1
        for item in inputs
        if _change_state(
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_applied_at=last_applied_at,
        )
        is not None
    )


def routing_counts(inputs: list[Input]) -> tuple[int, int]:
    counts = dashboard_overview_counts([], inputs)
    return counts["routes_total"], counts["routes_active"]
