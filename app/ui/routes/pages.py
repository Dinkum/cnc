from __future__ import annotations

import html

from app.access import (
    ACCESS_ROUTE_PATH,
    access_key_is_configured,
    access_origin_guard,
    access_redirect_target,
    access_request_is_authenticated,
    hash_access_key,
    normalize_access_key,
    sanitize_next_path,
    set_access_cookie,
    verify_access_key_async,
)
from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.models.entities import Backend
from app.services.error_reporting import ErrorCode
from app.services.llm_help import (
    build_backend_llm_help_payload,
    build_host_llm_help_payload,
    render_backend_llm_help_text,
    render_host_llm_help_text,
)
from app.services.managed_env import apply_managed_env_updates
from app.ui.errors import operator_coded_error as _operator_coded_error
from app.ui.access import _access_page_context
from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.ui.routes.shared import (
    FLASH_ERROR_COOKIE,
    FLASH_SUCCESS_COOKIE,
    _backend_ssh_destination,
    _dashboard_context,
    _dashboard_tab,
    _host_llm_help_context,
    _host_llm_help_url,
    _output_llm_help_url,
    _output_page_context,
    _render_access_template,
    _render_dashboard_template,
    _render_output_template,
    _stylesheet_asset_version,
    _ssh_destination_for_request,
    logger,
)


router = APIRouter(tags=["ui"])


@router.get(ACCESS_ROUTE_PATH, response_class=HTMLResponse)
async def access_page(
    request: Request, settings: Settings = Depends(settings_dependency)
) -> HTMLResponse:
    next_path = sanitize_next_path(request.query_params.get("next"))
    if access_key_is_configured(settings) and access_request_is_authenticated(
        request, settings
    ):
        return RedirectResponse(url=next_path, status_code=303)
    context = _access_page_context(settings, next_path=next_path)
    context["request"] = request
    return _render_access_template(request, context)


@router.post(ACCESS_ROUTE_PATH)
async def access_submit(
    request: Request,
    access_key: str = Form(""),
    access_key_confirm: str = Form(""),
    next_path: str = Form("/"),
    settings: Settings = Depends(settings_dependency),
) -> HTMLResponse:
    access_origin_guard(request, settings)
    next_target = sanitize_next_path(next_path or access_redirect_target(request))
    client_host = request.client.host if request.client else None
    if access_key_is_configured(settings):
        valid, wait_seconds = await verify_access_key_async(
            settings, access_key, attempt_key=client_host
        )
        if wait_seconds > 0:
            context = _access_page_context(
                settings,
                next_path=next_target,
                flash_error=f"Try again in {wait_seconds}s.",
            )
            context["request"] = request
            return _render_access_template(request, context, status_code=429)
        if not valid:
            context = _access_page_context(
                settings,
                next_path=next_target,
                flash_error="Access key did not match.",
            )
            context["request"] = request
            return _render_access_template(request, context, status_code=401)
        response = RedirectResponse(url=next_target, status_code=303)
        set_access_cookie(response, settings, request=request)
        return response

    try:
        normalized_key = normalize_access_key(access_key)
    except ValueError as exc:
        context = _access_page_context(
            settings, next_path=next_target, flash_error=str(exc)
        )
        context["request"] = request
        return _render_access_template(request, context, status_code=400)
    if normalized_key != str(access_key_confirm or "").strip():
        context = _access_page_context(
            settings,
            next_path=next_target,
            flash_error=_operator_coded_error(
                "Access key confirmation must match.",
                ErrorCode.VALIDATION_FAILED,
            ),
        )
        context["request"] = request
        return _render_access_template(request, context, status_code=400)
    next_settings = apply_managed_env_updates(
        settings, {"ACCESS_KEY_HASH": hash_access_key(normalized_key)}
    )
    response = RedirectResponse(url=next_target, status_code=303)
    set_access_cookie(response, next_settings, request=request)
    return response


@router.get("/", response_class=HTMLResponse, name="dashboard")
async def dashboard(
    request: Request,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    active_tab = _dashboard_tab(request.query_params.get("tab"), "home")
    if active_tab == "routing" and not settings.beta_routing:
        active_tab = "home"
    context = await _dashboard_context(
        session,
        settings,
        prefer_cached_status=True,
        active_tab=active_tab,
        defer_status=request.query_params.get("defer_status") == "1",
    )
    flash_error = request.cookies.get(FLASH_ERROR_COOKIE)
    flash_success = request.cookies.get(FLASH_SUCCESS_COOKIE)
    if flash_error:
        context["flash_error"] = flash_error
    if flash_success:
        context["flash_success"] = flash_success
    context["request"] = request
    context["active_tab"] = active_tab
    response = _render_dashboard_template(request, context)
    if hasattr(response, "delete_cookie"):
        response.delete_cookie(FLASH_SUCCESS_COOKIE, path="/")
        response.delete_cookie(FLASH_ERROR_COOKIE, path="/")
    return response


@router.get("/llm.txt", response_class=PlainTextResponse, name="host_llm_help")
async def host_llm_help(
    request: Request,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> PlainTextResponse:
    context = await _host_llm_help_context(session, settings)
    backends = context.get("backends", [])
    output_details = context.get("output_details", {})
    host = _ssh_destination_for_request(request)
    backend_ssh_host = _backend_ssh_destination(settings, request, resolve_dns=True)
    backend_payloads: list[dict[str, object]] = []
    for backend in backends:
        if not isinstance(backend, Backend):
            continue
        detail = output_details.get(backend.id, {})
        backend_payloads.append(
            build_backend_llm_help_payload(
                backend,
                host=host,
                backend_ssh_host=backend_ssh_host,
                llm_help_url=_output_llm_help_url(request, backend.id),
                status_value=str(detail.get("status_value") or ""),
                service_state=str(detail.get("service_state") or ""),
                target=str(detail.get("target") or ""),
                exposure_value=str(detail.get("exposure_value") or ""),
            )
        )
    payload = build_host_llm_help_payload(
        [backend for backend in backends if isinstance(backend, Backend)],
        settings=settings,
        host=host,
        backend_ssh_host=backend_ssh_host,
        host_llm_help_url=_host_llm_help_url(request),
        backend_payloads=backend_payloads,
    )
    return PlainTextResponse(render_host_llm_help_text(payload))


@router.get("/outputs/{backend_id}", response_class=HTMLResponse)
async def output_detail(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    try:
        context = await _output_page_context(
            session, settings, backend_id, prefer_cached_runtime=True
        )
    except LookupError:
        logger.warning(
            "ui.output.detail.missing",
            backend_id=backend_id,
            path=str(request.url.path),
        )
        asset_version = html.escape(_stylesheet_asset_version(settings), quote=True)
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Output not found</title>
  <link rel="stylesheet" href="/static/css/app.css?v={asset_version}">
</head>
<body class="error-page">
  <main class="shell error-shell">
    <section class="panel error-panel" style="display:block">
      <article class="card error-card">
        <div class="error-hero">
          <div class="error-copy">
            <p class="eyebrow">output</p>
            <h1>Output not found</h1>
            <p class="subtle">This output no longer exists.</p>
            <p><a class="back-crumb" href="/?tab=outputs">&#x2190; outputs</a></p>
          </div>
        </div>
      </article>
    </section>
  </main>
</body>
</html>
""",
            status_code=404,
        )
    context["request"] = request
    return _render_output_template(request, settings, context)


@router.get(
    "/outputs/{backend_id}/llm.txt",
    response_class=PlainTextResponse,
    name="output_llm_help",
)
async def output_llm_help(
    backend_id: int,
    request: Request,
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> PlainTextResponse:
    try:
        context = await _output_page_context(session, settings, backend_id)
    except LookupError:
        return PlainTextResponse("backend not found", status_code=404)

    backend = context["selected_backend"]
    detail = context.get("selected_output_detail", {})
    assert isinstance(backend, Backend)
    payload = build_backend_llm_help_payload(
        backend,
        host=_ssh_destination_for_request(request),
        backend_ssh_host=_backend_ssh_destination(settings, request, resolve_dns=True),
        llm_help_url=_output_llm_help_url(request, backend_id),
        status_value=str(detail.get("status_value") or ""),
        service_state=str(detail.get("service_state") or ""),
        target=str(detail.get("target") or ""),
        exposure_value=str(detail.get("exposure_value") or ""),
    )
    return PlainTextResponse(render_backend_llm_help_text(payload))
