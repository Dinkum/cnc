"""HTTP responses, flash cookies, template rendering, and bounded upload copying."""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Backend
from app.security import secure_cookie_required
from app.services.operation_runtime import (
    create_backend_progress_steps,
    input_progress_pipelines,
    operation_progress_pipelines,
    output_save_progress_steps,
)
from app.ui.dashboard.context import dashboard_context
from app.ui.errors import (
    ensure_context_flash_error_code as _ensure_context_flash_error_code,
)
from app.ui.errors import ensure_operator_coded_error as _ensure_operator_coded_error
from app.ui.operator_help import (
    backend_operator_commands_for_page,
    backend_ssh_destination,
    output_llm_help_url,
)
from app.ui.read_models import dashboard_tab
from app.ui.view_models import (
    _format_bytes,
    _save_apply_feedback,
    app_version,
    stylesheet_version,
)

templates = Jinja2Templates(directory="app/templates")

FLASH_SUCCESS_COOKIE = "cnc_flash_success"

FLASH_ERROR_COOKIE = "cnc_flash_error"

FLASH_COOKIE_VALUE_LIMIT = 380

UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024


def copy_upload_file_with_limit(
    source: BinaryIO, destination: Path, *, max_bytes: int
) -> int:
    copied = 0
    with destination.open("wb") as handle:
        while True:
            chunk = source.read(UPLOAD_COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise ValueError(
                    f"backup upload exceeds {_format_bytes(max_bytes)} limit"
                )
            handle.write(chunk)
    return copied


def render_dashboard_template(
    request: Request,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    context.setdefault("app_version", app_version())
    context.setdefault("asset_version", app_version())
    context.setdefault("create_output_progress_steps", create_backend_progress_steps())
    context.setdefault("input_progress_pipelines", input_progress_pipelines())
    context.setdefault("operation_progress_pipelines", operation_progress_pipelines())
    context.setdefault("output_save_progress_steps", output_save_progress_steps())
    return templates.TemplateResponse(
        request, "index.html", context, status_code=status_code
    )


def render_output_template(
    request: Request,
    settings: Settings,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    context["request"] = request
    backend = context.get("selected_backend")
    if isinstance(backend, Backend) and str(backend.kind or "").lower() == "app":
        context["ssh_host"] = backend_ssh_destination(
            settings, request, resolve_dns=False
        )
        context["operator_commands"] = backend_operator_commands_for_page(
            backend,
            settings=settings,
            host=backend_ssh_destination(settings, request, resolve_dns=False),
            llm_help_url=output_llm_help_url(request, backend.id),
        )
    else:
        context["operator_commands"] = []
    context.setdefault("app_version", app_version())
    context.setdefault("asset_version", stylesheet_version(settings))
    context.setdefault("operation_progress_pipelines", operation_progress_pipelines())
    return templates.TemplateResponse(
        request, "output_detail.html", context, status_code=status_code
    )


def render_access_template(
    request: Request,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    _ensure_context_flash_error_code(context)
    return templates.TemplateResponse(
        request, "access.html", context, status_code=status_code
    )


def _sanitize_flash_cookie_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char for char in str(value)
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > FLASH_COOKIE_VALUE_LIMIT:
        return f"{cleaned[: FLASH_COOKIE_VALUE_LIMIT - 3].rstrip()}..."
    return cleaned


def request_prefers_json(request: Request) -> bool:
    accept = str(request.headers.get("accept") or "").lower()
    requested_with = str(request.headers.get("x-requested-with") or "").strip().lower()
    return "application/json" in accept or requested_with == "fetch"


def form_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def ui_request_mode(request: Request) -> str:
    return "async" if request_prefers_json(request) else "page"


def json_save_feedback(
    response, *, success_message: str, failure_prefix: str, **extra: object
) -> JSONResponse:
    success_flash, error_flash = _save_apply_feedback(
        response,
        success_message=success_message,
        failure_prefix=failure_prefix,
    )
    payload = {
        "flash_success": success_flash,
        "flash_error": error_flash,
        "apply_status": response.status,
        "run_id": response.run_id,
        **extra,
    }
    return JSONResponse(payload, status_code=200)


def dashboard_tab_url(
    request: Request, active_tab: str, *, defer_status: bool = False
) -> str:
    try:
        base = str(request.url_for("dashboard"))
    except (AttributeError, RuntimeError):
        base = "/"
    params = {"tab": dashboard_tab(active_tab, "home")}
    if defer_status:
        params["defer_status"] = "1"
    return f"{base}?{urlencode(params)}"


def _set_flash_cookies(
    response: Response,
    request: Request,
    *,
    flash_success: str | None = None,
    flash_error: str | None = None,
) -> None:
    secure_cookie = secure_cookie_required(request)
    response.delete_cookie(FLASH_SUCCESS_COOKIE, path="/", secure=secure_cookie)
    response.delete_cookie(FLASH_ERROR_COOKIE, path="/", secure=secure_cookie)
    safe_success = _sanitize_flash_cookie_value(flash_success)
    safe_error = _sanitize_flash_cookie_value(
        _ensure_operator_coded_error(flash_error) if flash_error else None
    )
    if safe_success:
        response.set_cookie(
            FLASH_SUCCESS_COOKIE,
            safe_success,
            max_age=60,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )
    if safe_error:
        response.set_cookie(
            FLASH_ERROR_COOKIE,
            safe_error,
            max_age=60,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )


def dashboard_redirect(
    request: Request,
    *,
    active_tab: str,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    target = dashboard_tab_url(request, active_tab)
    response = RedirectResponse(url=target, status_code=status_code)
    _set_flash_cookies(
        response, request, flash_success=flash_success, flash_error=flash_error
    )
    return response


async def dashboard_refresh_response(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    *,
    active_tab: str,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    resolved_tab = dashboard_tab(active_tab, "home")
    context = await dashboard_context(
        session, settings, prefer_cached_status=True, active_tab=resolved_tab
    )
    context["request"] = request
    context["active_tab"] = resolved_tab
    context["flash_success"] = flash_success
    context["flash_error"] = flash_error
    return render_dashboard_template(request, context, status_code=status_code)


def output_redirect(
    request: Request,
    *,
    backend_id: int,
    flash_success: str | None = None,
    flash_error: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    target = f"/outputs/{backend_id}"
    response = RedirectResponse(url=target, status_code=status_code)
    _set_flash_cookies(
        response, request, flash_success=flash_success, flash_error=flash_error
    )
    return response
