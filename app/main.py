import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import html
from http import HTTPStatus
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import uvicorn

from app.access import build_access_required_response
from app.config import Settings, get_settings
from app.database import dispose_engine, init_db, verify_db_connection
from app.ui.diagnostics import cancel_runtime_diagnostics
from app.http_security import SecurityHeadersMiddleware
from app.logger import (
    bind_log_context,
    configure_logging,
    current_app_id,
    flush_logging_pipeline_async,
    get_logger,
    logging_pipeline_status,
    resolve_log_version,
)
from app.request_limits import (
    ADMIN_UNAUTHENTICATED_BODY_LIMITS,
    RequestBodyLimitMiddleware,
)
from app.routes import (
    agent,
    apply,
    backends,
    inputs,
    internal,
    join,
    status,
    ui,
    update,
)
from app.security import CNCTrustedHostMiddleware
from app.static_delivery import build_static_asset_app, stylesheet_asset_version
from app.services.error_reporting import CNCError, ErrorCode, cnc_error_from_payload
from app.services.app_hardening import fail_interrupted_hardening_runs
from app.services.backend_backup_service import (
    fail_interrupted_backend_backups,
    drain_backup_descriptions,
)
from app.services.backend_ssh_audit import (
    BACKEND_SSH_INTERNAL_HEADER_PATH,
    BackendSshAuditListener,
    create_backend_ssh_internal_header,
    start_backend_ssh_audit_listener,
)
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    run_pushover_outbox_dispatcher,
    send_pushover_notification_async,
)
from app.services.operations import (
    fail_interrupted_operations,
    try_acquire_host_mutation_lock,
    validate_host_mutation_lock_path,
)
from app.services.runtime_assets import inspect_managed_systemd_assets
from app.services.self_audit import (
    ControlPlaneSelfAuditError,
    handle_startup_self_audit_notification,
    run_control_plane_self_audit,
)
from app.services.startup_state import (
    initialize_startup_state,
    set_startup_phase_state,
    snapshot_startup_state,
)
from app.services.status_service import (
    cancel_status_cache_prefill,
    schedule_status_cache_prefill,
)
from app.services.systemd_watchdog import (
    run_systemd_watchdog,
    systemd_watchdog_controller_from_env,
)


BOOTSTRAP_TRUSTED_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _bootstrap_env_text(name: str, default: str) -> str:
    raw_value = str(os.getenv(name, "")).strip()
    return raw_value or default


def _bootstrap_env_int(name: str, default: int) -> int:
    raw_value = str(os.getenv(name, "")).strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _bootstrap_env_bool(name: str, default: bool) -> bool:
    raw_value = str(os.getenv(name, "")).strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    return default


def _bootstrap_env_path(name: str, default: Path) -> Path:
    raw_value = str(os.getenv(name, "")).strip()
    return Path(raw_value) if raw_value else default


def _bootstrap_host_candidate(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower().rstrip(".")
    if not normalized:
        return None
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return normalized
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        pass
    if BOOTSTRAP_TRUSTED_HOST_RE.fullmatch(normalized):
        return normalized
    return None


def _bootstrap_uvicorn_host(settings: Settings) -> str:
    candidate = _bootstrap_host_candidate(settings.admin_host)
    if not candidate:
        return "127.0.0.1"
    if settings.admin_unsafe_allow_remote:
        return candidate
    if candidate in {"localhost", "127.0.0.1", "::1"}:
        return candidate
    try:
        if ipaddress.ip_address(candidate).is_loopback:
            return candidate
    except ValueError:
        pass
    return "127.0.0.1"


def _bootstrap_trusted_hosts(settings: Settings) -> tuple[str, ...]:
    hosts = {"127.0.0.1", "localhost", "::1"}
    candidates = [settings.admin_host]
    candidates.extend(settings.admin_allowed_hosts.replace("\n", ",").split(","))
    for raw_value in candidates:
        normalized = _bootstrap_host_candidate(raw_value)
        if normalized:
            hosts.add(normalized)
    return tuple(sorted(hosts))


def _bootstrap_settings() -> Settings:
    defaults = Settings.model_construct()
    return defaults.model_copy(
        update={
            "log_app_id": _bootstrap_env_text("LOG_APP_ID", defaults.log_app_id),
            "log_env": _bootstrap_env_text("LOG_ENV", defaults.log_env),
            "log_version": _bootstrap_env_text("LOG_VERSION", defaults.log_version),
            "admin_host": _bootstrap_env_text("ADMIN_HOST", defaults.admin_host),
            "admin_port": _bootstrap_env_int("ADMIN_PORT", defaults.admin_port),
            "admin_unsafe_allow_remote": _bootstrap_env_bool(
                "ADMIN_UNSAFE_ALLOW_REMOTE",
                defaults.admin_unsafe_allow_remote,
            ),
            "admin_allowed_hosts": _bootstrap_env_text(
                "ADMIN_ALLOWED_HOSTS", defaults.admin_allowed_hosts
            ),
            "static_files_dir": _bootstrap_env_path(
                "STATIC_FILES_DIR", defaults.static_files_dir
            ),
        }
    )


boot_settings = _bootstrap_settings()
logger = get_logger("main")
STARTUP_INTERRUPTED_CLEANUP_RETRY_DELAY_SEC = 5.0
STARTUP_INTERRUPTED_CLEANUP_RETRY_ATTEMPTS = 720


def _runtime_settings():
    return get_settings()


def _safe_header_token(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._:/@+-]", "_", str(value or "").strip())[:96]


def _configure_bootstrap_logging(settings: Settings) -> None:
    configure_logging(
        None,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        compress_rotated=settings.log_compress_rotated,
        app_id=settings.log_app_id,
        env=settings.log_env,
        version=resolve_log_version(settings.log_version),
    )


def _configure_runtime_logging(settings: Settings) -> None:
    configure_logging(
        str(settings.log_path),
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        compress_rotated=settings.log_compress_rotated,
        app_id=settings.log_app_id,
        env=settings.log_env,
        version=resolve_log_version(settings.log_version),
    )


def _load_runtime_settings_for_startup(app: FastAPI) -> Settings:
    started_at = time.monotonic()
    _configure_bootstrap_logging(boot_settings)
    try:
        settings = _runtime_settings()
    except Exception as exc:
        logger.error(
            "config.invalid",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        set_startup_phase_state(
            app,
            "config",
            {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
        raise RuntimeError(f"invalid CNC configuration: {exc}") from exc

    try:
        _configure_runtime_logging(settings)
    except Exception as exc:
        logger.error(
            "logging.configure.failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        set_startup_phase_state(
            app,
            "config",
            {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "stage": "logging",
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
        raise RuntimeError(f"invalid CNC logging configuration: {exc}") from exc

    logger.info(
        "logging.configured",
        log_path=str(settings.log_path),
        logging=logging_pipeline_status(),
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    set_startup_phase_state(
        app,
        "config",
        {
            "status": "ok",
            "error": "",
            "log_path": str(settings.log_path),
            "logging": logging_pipeline_status(),
            "duration_sec": _phase_duration_seconds(started_at),
        },
    )
    return settings


def _phase_duration_seconds(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    initialize_startup_state(_app)
    watchdog = systemd_watchdog_controller_from_env()
    watchdog_task: asyncio.Task[None] | None = None
    notification_dispatch_task: asyncio.Task[None] | None = None
    ssh_audit_listener: BackendSshAuditListener | None = None
    if watchdog:
        watchdog.notify_status(status_message="cnc-admin starting")
    try:
        settings = _load_runtime_settings_for_startup(_app)
        try:
            _app.state.backend_ssh_internal_token = create_backend_ssh_internal_header()
        except OSError as exc:
            _app.state.backend_ssh_internal_token = ""
            logger.warning(
                "backend.ssh.internal_auth.unavailable",
                path=str(BACKEND_SSH_INTERNAL_HEADER_PATH),
                error=str(exc),
            )
        ssh_audit_listener = start_backend_ssh_audit_listener()
        await _run_startup_database_phase(_app, settings)
        await _run_startup_runtime_asset_observation(_app, settings)
        await _run_startup_self_audit(_app, settings)
        if watchdog:
            ready_sent = watchdog.notify_ready(status_message="cnc-admin ready")
            if not ready_sent:
                raise RuntimeError("systemd watchdog ready notification failed")
            watchdog_task = asyncio.create_task(
                run_systemd_watchdog(watchdog, status_message="cnc-admin healthy")
            )
        schedule_status_cache_prefill(settings, force_refresh=True)
        notification_dispatch_task = asyncio.create_task(
            run_pushover_outbox_dispatcher(settings)
        )
        logger.info("app.startup", host=settings.admin_host, port=settings.admin_port)
        yield
    finally:
        cleanup_task = getattr(_app.state, "cnc_startup_interrupted_cleanup_task", None)
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await cleanup_task
        if watchdog_task is not None:
            watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog_task
        if notification_dispatch_task is not None:
            notification_dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await notification_dispatch_task
        await cancel_status_cache_prefill()
        await cancel_runtime_diagnostics()
        if ssh_audit_listener is not None:
            ssh_audit_listener.close()
        await drain_backup_descriptions()
        await dispose_engine()
        if watchdog:
            watchdog.notify_stopping(status_message="cnc-admin stopping")


async def _run_startup_database_phase(app: FastAPI, settings) -> None:
    started_at = time.monotonic()
    current_stage = "verify_db_connection"
    cleanup_deferred_reason = ""

    try:
        await asyncio.wait_for(
            verify_db_connection(),
            timeout=settings.startup_db_timeout_sec,
        )
        current_stage = "init_db"
        await init_db()
        current_stage = "validate_host_mutation_lock"
        validate_host_mutation_lock_path(settings)
        current_stage = "fail_interrupted_host_mutations"
        cleanup_result = await _run_interrupted_cleanup_if_unlocked(settings)
        if cleanup_result != "completed":
            cleanup_deferred_reason = cleanup_result
            logger.warning(
                "startup.interrupted_cleanup_deferred",
                reason=cleanup_result,
            )
            _schedule_interrupted_cleanup_retry(app, settings)
            current_stage = "fail_interrupted_host_mutations_deferred"
        phase_state = {
            "status": "warning" if cleanup_deferred_reason else "ok",
            "error": (
                f"interrupted cleanup deferred: {cleanup_deferred_reason}"
                if cleanup_deferred_reason
                else ""
            ),
            "stage": current_stage,
            "duration_sec": _phase_duration_seconds(started_at),
        }
        if cleanup_deferred_reason:
            phase_state["cleanup_deferred_reason"] = cleanup_deferred_reason
        set_startup_phase_state(
            app,
            "db",
            phase_state,
        )
    except TimeoutError:
        logger.error(
            "startup.db.timed_out",
            stage=current_stage,
            timeout_sec=settings.startup_db_timeout_sec,
        )
        set_startup_phase_state(
            app,
            "db",
            {
                "status": "timeout",
                "error": f"startup database phase timed out during {current_stage}",
                "stage": current_stage,
                "timeout_sec": settings.startup_db_timeout_sec,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
        raise RuntimeError(
            f"startup database phase timed out during {current_stage}"
        ) from None
    except Exception as exc:
        logger.error("startup.db.failed", stage=current_stage, error=str(exc))
        set_startup_phase_state(
            app,
            "db",
            {
                "status": "failed",
                "error": str(exc),
                "stage": current_stage,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
        raise RuntimeError(
            f"startup database phase failed during {current_stage}: {exc}"
        ) from exc


def _schedule_interrupted_cleanup_retry(app: FastAPI, settings) -> None:
    existing = getattr(app.state, "cnc_startup_interrupted_cleanup_task", None)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return
    app.state.cnc_startup_interrupted_cleanup_task = asyncio.create_task(
        _retry_interrupted_cleanup_when_unlocked(settings, app),
        name="cnc-startup-interrupted-cleanup",
    )


async def _retry_interrupted_cleanup_when_unlocked(settings, app: FastAPI) -> None:
    for attempt in range(1, STARTUP_INTERRUPTED_CLEANUP_RETRY_ATTEMPTS + 1):
        await asyncio.sleep(STARTUP_INTERRUPTED_CLEANUP_RETRY_DELAY_SEC)
        cleanup_result = await _run_interrupted_cleanup_if_unlocked(settings)
        if cleanup_result != "completed":
            logger.info(
                "startup.interrupted_cleanup_waiting",
                attempt=attempt,
                attempts=STARTUP_INTERRUPTED_CLEANUP_RETRY_ATTEMPTS,
                reason=cleanup_result,
            )
            continue
        state = snapshot_startup_state(app)["db"]
        if state.get("status") == "warning" and state.get("cleanup_deferred_reason"):
            state.update(status="ok", error="", stage="fail_interrupted_host_mutations")
            state.pop("cleanup_deferred_reason", None)
            set_startup_phase_state(app, "db", state)
        logger.info("startup.interrupted_cleanup_completed", attempt=attempt)
        return
    logger.warning(
        "startup.interrupted_cleanup_abandoned",
        attempts=STARTUP_INTERRUPTED_CLEANUP_RETRY_ATTEMPTS,
    )


async def _run_interrupted_cleanup_if_unlocked(settings) -> str:
    lock = try_acquire_host_mutation_lock(settings)
    if lock is None:
        return "host_mutation_lock_busy"
    cleanup_task = asyncio.create_task(
        _run_interrupted_cleanup_steps(settings),
        name="cnc-interrupted-cleanup-steps",
    )
    try:
        await cleanup_task
        return "completed"
    except asyncio.CancelledError:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        raise
    except Exception as exc:
        logger.warning("startup.interrupted_cleanup_failed", error=str(exc))
        return "cleanup_failed"
    finally:
        lock.__exit__(None, None, None)


async def _run_interrupted_cleanup_steps(settings) -> None:
    await fail_interrupted_operations(settings)
    await fail_interrupted_hardening_runs(settings)
    await fail_interrupted_backend_backups(settings)


async def _run_startup_runtime_asset_observation(app: FastAPI, settings) -> None:
    started_at = time.monotonic()
    try:
        assets_result = await asyncio.wait_for(
            asyncio.to_thread(inspect_managed_systemd_assets, settings),
            timeout=settings.startup_observe_timeout_sec,
        )
        timer_drift = [
            name
            for name, state in assets_result["timer_states"].items()
            if not state["enabled"] or not state["active"]
        ]
        changed_files = list(assets_result.get("changed_files") or [])
        if (
            assets_result["changed_units"]
            or changed_files
            or assets_result["timer_reconcile_needed"]
        ):
            logger.warning(
                "runtime.assets.drift_detected",
                changed_units=assets_result["changed_units"],
                changed_files=changed_files,
                timer_drift=timer_drift,
            )
            status = "warning"
        else:
            status = "ok"
        set_startup_phase_state(
            app,
            "runtime_assets",
            {
                "status": status,
                "error": "",
                "changed_units": list(assets_result["changed_units"]),
                "changed_files": changed_files,
                "timer_drift": timer_drift,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
    except TimeoutError:
        logger.warning(
            "runtime.assets.inspect_timed_out",
            timeout_sec=settings.startup_observe_timeout_sec,
        )
        set_startup_phase_state(
            app,
            "runtime_assets",
            {
                "status": "timeout",
                "error": "managed runtime asset inspection timed out during startup",
                "timeout_sec": settings.startup_observe_timeout_sec,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
    except Exception as exc:
        logger.warning("runtime.assets.inspect_failed", error=str(exc))
        set_startup_phase_state(
            app,
            "runtime_assets",
            {
                "status": "failed",
                "error": str(exc),
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )


async def _run_startup_self_audit(app: FastAPI, settings) -> None:
    started_at = time.monotonic()
    try:
        audit_result = await asyncio.wait_for(
            asyncio.to_thread(_run_control_plane_self_audit_for_startup, settings),
            timeout=settings.startup_observe_timeout_sec,
        )
        logger.info("control_plane.self_audit.succeeded", details=audit_result)
        await handle_startup_self_audit_notification(settings, audit_result)
        set_startup_phase_state(
            app,
            "self_audit",
            {
                "status": "ok",
                "error": "",
                "details": audit_result,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
    except TimeoutError:
        logger.warning(
            "control_plane.self_audit.timed_out",
            timeout_sec=settings.startup_observe_timeout_sec,
        )
        set_startup_phase_state(
            app,
            "self_audit",
            {
                "status": "timeout",
                "error": "control-plane self-audit timed out during startup",
                "timeout_sec": settings.startup_observe_timeout_sec,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
    except ControlPlaneSelfAuditError as exc:
        logger.warning("control_plane.self_audit.failed", details=exc.as_details())
        await handle_startup_self_audit_notification(settings, exc)
        set_startup_phase_state(
            app,
            "self_audit",
            {
                "status": "failed",
                "error": str(exc),
                "blocks_apply": exc.blocks_apply,
                "findings": exc.findings,
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )
    except Exception as exc:
        logger.warning("control_plane.self_audit.error", error=str(exc))
        set_startup_phase_state(
            app,
            "self_audit",
            {
                "status": "error",
                "error": str(exc),
                "duration_sec": _phase_duration_seconds(started_at),
            },
        )


def _run_control_plane_self_audit_for_startup(settings):
    return run_control_plane_self_audit(settings, startup_mode=True)


async def _send_internal_error_notification(
    settings: Settings,
    *,
    request_id: str,
    error_code: str,
    error_name: str,
    error_inst: str,
    method: str,
    path: str,
    error_type: str,
    error: str,
) -> None:
    try:
        await send_pushover_notification_async(
            settings,
            title="CNC internal error",
            message=build_operator_message(
                f"{method} {path} raised {error_type}.",
                status="failure",
                facts=[
                    ("app_id", current_app_id()),
                    ("error_code", error_code),
                    ("error_name", error_name),
                    ("error_inst", error_inst),
                    ("request_id", request_id),
                    ("error", error),
                ],
                action="cnc-admin logs admin --errors",
            ),
            priority=0,
            event="internal_error",
            source="main",
            url=admin_dashboard_url(settings, tab="home"),
            url_title="Open CNC",
        )
    except Exception as notify_exc:
        logger.warning(
            "http.request.unhandled_notification_failed",
            request_id=request_id,
            error_code=error_code,
            error_name=error_name,
            error_inst=error_inst,
            error=str(notify_exc),
            error_type=type(notify_exc).__name__,
        )


app = FastAPI(
    title="cnc admin",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    limits=ADMIN_UNAUTHENTICATED_BODY_LIMITS,
)
app.add_middleware(
    CNCTrustedHostMiddleware,
    allowed_hosts=list(_bootstrap_trusted_hosts(boot_settings)),
    settings_getter=get_settings,
)
app.mount(
    "/static",
    build_static_asset_app(boot_settings.static_files_dir, check_dir=False),
    name="static",
)

app.include_router(backends.router)
app.include_router(inputs.router)
app.include_router(apply.router)
app.include_router(agent.router)
app.include_router(internal.router)
app.include_router(join.router)
app.include_router(status.router)
app.include_router(status.probe_router)
app.include_router(update.router)
app.include_router(ui.router)


def _request_prefers_html(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept or not accept


def _request_id_for_request(request: Request) -> str:
    state_request_id = getattr(getattr(request, "state", None), "request_id", None)
    if isinstance(state_request_id, str) and state_request_id.strip():
        return state_request_id.strip()
    header_value = str(request.headers.get("x-request-id") or "").strip()
    if REQUEST_ID_RE.fullmatch(header_value):
        return header_value
    return f"req_{secrets.token_urlsafe(6)}"


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "-"


def _request_kind(request: Request) -> str:
    requested_with = request.headers.get("x-requested-with", "").strip().lower()
    accept = request.headers.get("accept", "").strip().lower()
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if requested_with:
        return f"async:{_safe_header_token(requested_with)}"
    if "application/json" in accept:
        return "json"
    if "text/html" in accept or not accept:
        return "html"
    if content_type:
        return f"content:{_safe_header_token(content_type)}"
    return "other"


def _route_template(request: Request) -> str:
    route = request.scope.get("route") if hasattr(request, "scope") else None
    path = getattr(route, "path", "")
    return str(path or "")


def _safe_location_path(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    return str(parsed.path or "")[:160]


def _request_log_context(request: Request) -> dict[str, object]:
    return {
        "method": request.method,
        "path": request.url.path,
        "route": _route_template(request),
        "client": _client_host(request),
        "request_kind": _request_kind(request),
    }


def _response_log_context(response) -> dict[str, object]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    context: dict[str, object] = {
        "status_code": status_code,
        "status_family": f"{status_code // 100}xx" if status_code else "unknown",
    }
    content_type = (
        str(response.headers.get("content-type", "") or "").split(";", 1)[0].strip()
    )
    if content_type:
        context["content_type"] = content_type
    location_path = _safe_location_path(response.headers.get("location"))
    if location_path:
        context["location_path"] = location_path
    return context


def _error_debug_text(
    *,
    app_id: str,
    request_id: str,
    error_code: str,
    error_name: str,
    error_inst: str,
    request: Request,
    status_code: int,
    message: str,
) -> str:
    lines = [
        f"app_id: {app_id}",
        f"error_code: {error_code}",
        f"error_name: {error_name}",
        f"error_inst: {error_inst}",
        f"request_id: {request_id}",
        f"status: {status_code}",
        f"method: {request.method}",
        f"path: {request.url.path}",
        f"client: {_client_host(request)}",
        f"message: {message}",
    ]
    return "\n".join(lines)


def _render_error_page(
    *,
    status_code: int,
    title: str,
    summary: str,
    debug_text: str,
    app_id: str,
    request_id: str,
    error_code: str,
    error_name: str,
    error_inst: str,
    log_path: str,
) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary)
    safe_debug = html.escape(debug_text)
    safe_asset_version = html.escape(
        stylesheet_asset_version(boot_settings.static_files_dir), quote=True
    )
    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <link rel="stylesheet" href="/static/css/app.css?v={safe_asset_version}">
</head>
<body class="error-page">
  <main class="shell error-shell">
    <section class="panel error-panel" style="display:block">
      <article class="card error-card">
        <div class="error-hero">
          <div class="error-copy">
            <p class="eyebrow">error</p>
            <h1>{safe_title}</h1>
            <p class="subtle">{safe_summary}</p>
          </div>
          <div class="error-badge">
            <span class="metric-label">status</span>
            <strong>{status_code}</strong>
          </div>
        </div>
        <div class="detail-card error-detail">
          <div class="detail-grid error-metrics">
            <div><span class="label">status</span><span class="value">{status_code}</span></div>
            <div><span class="label">app id</span><span class="value mono">{html.escape(app_id)}</span></div>
            <div><span class="label">code</span><span class="value mono">{html.escape(error_code)}</span></div>
            <div><span class="label">name</span><span class="value mono">{html.escape(error_name)}</span></div>
            <div><span class="label">inst</span><span class="value mono">{html.escape(error_inst)}</span></div>
            <div><span class="label">req</span><span class="value mono">{html.escape(request_id)}</span></div>
            <div><span class="label">log file</span><span class="value mono">{html.escape(log_path)}</span></div>
          </div>
          <div class="code-block error-debug">
            <div class="code-head code-head-actions">
              <span>debug text</span>
              <button type="button" id="copy-debug" class="copy-button">Copy debug text</button>
            </div>
            <textarea id="debug-text" class="mono error-debug-field" readonly>{safe_debug}</textarea>
          </div>
        </div>
      </article>
    </section>
  </main>
  <script>
    (() => {{
      const button = document.getElementById("copy-debug");
      const field = document.getElementById("debug-text");
      if (!button || !field) return;
      button.addEventListener("click", async () => {{
        try {{
          await navigator.clipboard.writeText(field.value);
          button.textContent = "Copied";
        }} catch (_error) {{
          field.focus();
          field.select();
          button.textContent = "Select";
        }}
      }});
    }})();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=content, status_code=status_code)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = _request_id_for_request(request)
    request.state.request_id = request_id
    started = time.monotonic()
    with bind_log_context(request_id=request_id):
        logger.info(
            "http.request.started",
            **_request_log_context(request),
        )
        access_response = build_access_required_response(request, get_settings())
        if access_response is not None:
            access_response.headers["X-Request-ID"] = request_id
            log_context = {
                **_request_log_context(request),
                **_response_log_context(access_response),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "access_gate": "blocked",
            }
            logger.warning(
                "http.request.completed",
                **log_context,
            )
            await flush_logging_pipeline_async()
            return access_response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log_context = {
            **_request_log_context(request),
            **_response_log_context(response),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        log_method = (
            logger.warning
            if int(getattr(response, "status_code", 0) or 0) >= 400
            else logger.info
        )
        log_method(
            "http.request.completed",
            **log_context,
        )
        if response.status_code >= 400 or request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            await flush_logging_pipeline_async()
        return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    request_id = _request_id_for_request(request)
    request.state.request_id = request_id
    error = cnc_error_from_payload(
        exc.detail,
        default_error_code=ErrorCode.HTTP_REQUEST_REJECTED,
        status_code=exc.status_code,
    )
    error_fields = error.log_context()
    error_inst = error.error_inst
    request.state.error_inst = error_inst
    response_headers = exc.headers or {}
    logger.warning(
        "http.request.rejected",
        request_id=request_id,
        **error_fields,
        **_request_log_context(request),
        status_code=exc.status_code,
        status_family=f"{int(exc.status_code) // 100}xx",
        detail=str(error),
        allow=str(response_headers.get("allow") or response_headers.get("Allow") or ""),
    )
    await flush_logging_pipeline_async()
    if not _request_prefers_html(request):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "error_code": error.code,
                "error_name": error.name,
                "error_inst": error_inst,
                "request_id": request_id,
            },
            headers=exc.headers,
        )
    phrase = (
        HTTPStatus(exc.status_code).phrase
        if exc.status_code in HTTPStatus._value2member_map_
        else "Request error"
    )
    debug_text = _error_debug_text(
        app_id=current_app_id(),
        request_id=request_id,
        error_code=error.code,
        error_name=error.name,
        error_inst=error_inst,
        request=request,
        status_code=exc.status_code,
        message=str(exc.detail),
    )
    return _render_error_page(
        status_code=exc.status_code,
        title=f"{exc.status_code} {phrase}",
        summary=str(exc.detail),
        debug_text=debug_text,
        app_id=current_app_id(),
        request_id=request_id,
        error_code=error.code,
        error_name=error.name,
        error_inst=error_inst,
        log_path=str(_runtime_settings().log_path),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    settings = _runtime_settings()
    request_id = _request_id_for_request(request)
    request.state.request_id = request_id
    error = CNCError(
        ErrorCode.INTERNAL_UNHANDLED_EXCEPTION,
        cause=exc,
    )
    error_fields = error.log_context()
    error_inst = error.error_inst
    request.state.error_inst = error_inst
    logger.exception(
        "http.request.unhandled",
        request_id=request_id,
        **error_fields,
        method=request.method,
        path=request.url.path,
    )
    asyncio.create_task(
        _send_internal_error_notification(
            settings,
            request_id=request_id,
            error_code=error.code,
            error_name=error.name,
            error_inst=error_inst,
            method=request.method,
            path=request.url.path,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    )
    if not _request_prefers_html(request):
        return JSONResponse(
            status_code=500,
            content={
                "detail": "internal server error",
                "error_code": error.code,
                "error_name": error.name,
                "error_inst": error_inst,
                "request_id": request_id,
            },
        )
    debug_text = _error_debug_text(
        app_id=current_app_id(),
        request_id=request_id,
        error_code=error.code,
        error_name=error.name,
        error_inst=error_inst,
        request=request,
        status_code=500,
        message="internal server error",
    )
    return _render_error_page(
        status_code=500,
        title="500 Internal Server Error",
        summary="The admin UI hit an unexpected error. Use the codes below to find it in the log.",
        debug_text=debug_text,
        app_id=current_app_id(),
        request_id=request_id,
        error_code=error.code,
        error_name=error.name,
        error_inst=error_inst,
        log_path=str(settings.log_path),
    )


app.add_middleware(SecurityHeadersMiddleware)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=_bootstrap_uvicorn_host(boot_settings),
        port=boot_settings.admin_port,
        reload=False,
    )
