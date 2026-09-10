"""Project runtime diagnostics and cached metrics into output health views."""

from __future__ import annotations

from app.models.entities import Backend
from app.services.renderers import container_name
from app.services.shield_runtime import SHIELD_CONTAINER_SERVICE


def _humanize_diagnostic_code(value: str | None) -> str:
    normalized = str(value or "").strip().replace("_", " ")
    return normalized or "unknown"


def _configured_health_endpoint_label(
    *,
    healthcheck_mode: str,
    healthcheck_path: str,
    healthcheck_host_header: str,
) -> str:
    mode = str(healthcheck_mode or "http").strip().lower()
    if mode == "auto":
        mode = "http"
    path = str(healthcheck_path or "-").strip() or "-"
    host = str(healthcheck_host_header or "-").strip() or "-"
    if mode == "http":
        if host != "-":
            return f"http {path} @ {host}"
        return f"http {path}"
    if mode == "tcp":
        return "tcp internal app port"
    if mode == "none":
        return "none"
    if host != "-":
        return f"http {path} @ {host}"
    return f"http {path}"


def runtime_health_view(
    *,
    backend: Backend,
    healthcheck_mode: str,
    healthcheck_path: str,
    healthcheck_host_header: str,
    runtime_diagnostics: dict[str, object] | None,
) -> dict[str, object]:
    health_target = _configured_health_endpoint_label(
        healthcheck_mode=healthcheck_mode,
        healthcheck_path=healthcheck_path,
        healthcheck_host_header=healthcheck_host_header,
    )
    if not backend.enabled:
        return {
            "label": "NOT ENABLED",
            "tone": "inactive",
            "summary": "NOT ENABLED",
            "detail": "output is disabled",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "- / -",
            "health_target": health_target,
        }
    if backend.kind == "shield":
        diagnosis = str((runtime_diagnostics or {}).get("diagnosis") or "")
        if diagnosis != "healthy":
            return {
                "label": "unknown" if not diagnosis else "UNHEALTHY",
                "tone": "queued" if not diagnosis else "error",
                "summary": "unknown" if not diagnosis else "UNHEALTHY",
                "detail": "shield runtime health not loaded"
                if not diagnosis
                else "shield health endpoint unreachable",
                "alert": None,
                "sandbox_status": "-",
                "guest_status": "-",
                "app_handoff_status": "-",
                "runtime_state": "shield runtime",
                "health_target": health_target,
            }
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "shield access gate runtime",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "shield runtime",
            "health_target": health_target,
        }
    if backend.kind != "app":
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "static output",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "-",
            "health_target": health_target,
        }
    if runtime_diagnostics is None:
        return {
            "label": "unknown",
            "tone": "queued",
            "summary": "unknown",
            "detail": "live runtime diagnosis not loaded",
            "alert": None,
            "sandbox_status": "-",
            "guest_status": "-",
            "app_handoff_status": "-",
            "runtime_state": "- / -",
            "health_target": health_target,
        }

    diagnostics = runtime_diagnostics
    diagnosis = str(diagnostics.get("diagnosis") or "").strip().lower()
    sandbox_status = str(diagnostics.get("sandbox_status") or "-").strip() or "-"
    guest_status = str(diagnostics.get("guest_status") or "-").strip() or "-"
    app_handoff_status = (
        str(diagnostics.get("app_handoff_status") or "-").strip() or "-"
    )
    container_state = diagnostics.get("container_state")
    runtime_state = f"guest {guest_status} · handoff {app_handoff_status}"
    if isinstance(container_state, dict):
        active_state = str(container_state.get("ActiveState") or "-").strip() or "-"
        sub_state = str(container_state.get("SubState") or "-").strip() or "-"
        runtime_state = (
            f"guest {guest_status} · handoff {app_handoff_status} · "
            f"service {active_state} / {sub_state}"
        )
    primary_error = ""
    for key in (
        "app_handoff_error",
        "loopback_error",
        "private_error",
        "inspect_error",
    ):
        value = str(diagnostics.get(key) or "").strip()
        if value:
            primary_error = value.splitlines()[0].strip()
            break

    if diagnosis == "healthy":
        return {
            "label": "HEALTHY",
            "tone": "success",
            "summary": "HEALTHY",
            "detail": "configured health endpoint reachable",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    if diagnosis == "app_unmonitored":
        return {
            "label": "UNMONITORED",
            "tone": "warn",
            "summary": "UNMONITORED",
            "detail": "app health check is disabled",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    if diagnosis in {
        "backend_observation_deferred",
        "backend_observation_unavailable",
    }:
        return {
            "label": "UNKNOWN",
            "tone": "warn",
            "summary": "UNKNOWN",
            "detail": "guest observation is temporarily unavailable",
            "alert": None,
            "sandbox_status": sandbox_status,
            "guest_status": guest_status,
            "app_handoff_status": app_handoff_status,
            "runtime_state": runtime_state,
            "health_target": health_target,
        }

    summary = "unhealthy"
    detail = "runtime state unknown"
    title = "Output unhealthy"
    if diagnosis == "app_service_down":
        summary = "unhealthy (configured health endpoint)"
        detail = "configured health endpoint unreachable"
    elif diagnosis == "sandbox_missing":
        summary = "down"
        detail = "guest missing"
        title = "Guest missing"
    elif diagnosis == "guest_init_broken":
        summary = "guest init broken"
        detail = "guest shell is up but systemd is not ready"
        title = "Guest init broken"
    elif diagnosis == "sandbox_publish_broken":
        summary = "publish broken"
        detail = "guest is up but CNC publish handoff is broken"
        title = "Publish handoff broken"
    elif diagnosis:
        summary = _humanize_diagnostic_code(diagnosis)
        detail = _humanize_diagnostic_code(diagnosis)

    alert_rows: list[tuple[str, str]] = [("status", summary)]
    if health_target != "none":
        alert_rows.append(("health check", health_target))
    if primary_error:
        alert_rows.append(("error", primary_error))
    issue_list = diagnostics.get("issues")
    if isinstance(issue_list, list):
        issue_label = ", ".join(
            _humanize_diagnostic_code(item) for item in issue_list if item
        )
        if issue_label:
            alert_rows.append(("signals", issue_label))

    return {
        "label": "UNHEALTHY",
        "tone": "error",
        "summary": "UNHEALTHY",
        "detail": detail,
        "alert": {
            "title": title,
            "summary": detail,
            "rows": alert_rows,
            "tone": "error",
            "pill": "unhealthy",
        },
        "sandbox_status": sandbox_status,
        "guest_status": guest_status,
        "app_handoff_status": app_handoff_status,
        "runtime_state": runtime_state,
        "health_target": health_target,
    }


def _backend_runtime_service_name(
    backend: Backend | str, *, kind: str | None = None
) -> str:
    backend_name = backend.name if isinstance(backend, Backend) else str(backend or "")
    backend_kind = (backend.kind if isinstance(backend, Backend) else kind) or ""
    if str(backend_kind).strip().lower() == "shield":
        return SHIELD_CONTAINER_SERVICE
    return container_name(backend_name)


def runtime_diagnostics_from_status_payload(
    payload: dict[str, object] | None,
    backend_name: str,
    *,
    kind: str = "app",
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    expected_service = _backend_runtime_service_name(backend_name, kind=kind)
    for service in services:
        if not isinstance(service, dict):
            continue
        if str(service.get("service") or "").strip() != expected_service:
            continue
        data = service.get("data")
        if not isinstance(data, dict):
            return None
        issues_raw = str(data.get("RuntimeIssues") or "").strip()
        issues = [
            item.strip()
            for item in issues_raw.split(",")
            if item.strip() and item.strip() != "-"
        ]
        diagnostics: dict[str, object] = {
            "backend": backend_name,
            "diagnosis": str(data.get("RuntimeDiagnosis") or "").strip().lower(),
            "sandbox_status": str(data.get("SandboxStatus") or "-").strip() or "-",
            "guest_status": str(data.get("GuestStatus") or "-").strip() or "-",
            "app_handoff_status": str(data.get("AppHandoffStatus") or "-").strip()
            or "-",
            "private_ip": str(data.get("PrivateAddress") or "").strip(),
            "private_reachable": str(data.get("PrivateReachable") or "").strip().lower()
            == "yes",
            "loopback_reachable": str(data.get("ProxyReachable") or "").strip().lower()
            == "yes",
            "issues": issues,
            "container_state": {
                "ActiveState": str(data.get("ActiveState") or "").strip(),
                "SubState": str(data.get("SubState") or "").strip(),
            },
        }
        error_text = str(service.get("error") or "").strip()
        if error_text:
            diagnostics["app_handoff_error"] = error_text
        return diagnostics
    return None


def service_metrics_from_status_payload(
    payload: dict[str, object] | None,
    backend: Backend,
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    expected_service = _backend_runtime_service_name(backend)
    for service in services:
        if not isinstance(service, dict):
            continue
        if str(service.get("service") or "").strip() != expected_service:
            continue
        metrics = service.get("metrics")
        if isinstance(metrics, dict):
            return metrics
        return None
    return None
