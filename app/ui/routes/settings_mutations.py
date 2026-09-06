from __future__ import annotations

import json

from app.access import (
    clear_access_cookie,
    hash_access_key,
    normalize_access_key,
    set_access_cookie,
)
from app.config import Settings
from app.dependencies import (
    db_session_dependency,
    settings_dependency,
)
from app.security import enforce_csrf
from app.services.apply_service import run_apply
from app.services.cluster_nodes import (
    create_join_command,
    remove_node,
)
from app.services.error_reporting import ErrorCode
from app.services.managed_env import apply_managed_env_updates
from app.services.notifications import (
    admin_dashboard_url,
    build_operator_message,
    pushover_is_configured,
    send_pushover_notification_async,
)
from app.services.update_service import (
    UpdateRejectedError,
    run_update,
)
from app.ui.errors import (
    operator_coded_error as _operator_coded_error,
    operator_error_message as _operator_error_message,
)
from app.ui.forms import _form_string_or_default
from app.ui.settings import _resolve_masked_secret_submission
from app.ui.routes.shared import (
    _cached_dashboard_context,
    _dashboard_redirect,
    _host_mutation_preflight_message,
    _render_dashboard_template,
    logger,
)
from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(tags=["ui"])


@router.post("/ui/update")
async def update_form(
    request: Request,
    csrf_token: str = Form(...),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    logger.info("ui.update.requested")
    try:
        response = await run_update(session, settings)
    except UpdateRejectedError as exc:
        context = await _cached_dashboard_context(
            session, settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        context["flash_error"] = _operator_error_message(str(exc), exc.log_context())
        return _render_dashboard_template(request, context, status_code=409)
    logger.info("ui.update.completed", status=response.status)
    context = await _cached_dashboard_context(session, settings, active_tab="settings")
    context["request"] = request
    context["active_tab"] = "settings"
    if response.status in {"queued", "running"}:
        context["flash_success"] = "Update running in background."
    elif response.status == "success":
        context["flash_success"] = "Update completed."
    else:
        context["flash_error"] = f"Update failed: {json.dumps(response.details)}"
    return _render_dashboard_template(request, context, status_code=200)


@router.post("/ui/settings/access-key")
async def update_access_key_settings_form(
    request: Request,
    csrf_token: str = Form(...),
    access_key: str = Form(""),
    access_key_confirm: str = Form(""),
    action: str = Form("save"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    normalized_action = (
        _form_string_or_default(action, "save").strip().lower() or "save"
    )
    try:
        if normalized_action in {"clear", "disable"}:
            next_settings = apply_managed_env_updates(
                settings, {"ACCESS_KEY_HASH": None}
            )
            await send_pushover_notification_async(
                next_settings,
                title="CNC security setting changed",
                message=build_operator_message(
                    "Admin access key was disabled.",
                    status="warning",
                    facts=[("setting", "access_key"), ("state", "disabled")],
                    action="cnc-admin logs admin --lines 100",
                ),
                priority=0,
                event="access_key_changed",
                source="ui.settings",
                url=admin_dashboard_url(next_settings, tab="settings"),
                url_title="Open settings",
            )
            context = await _cached_dashboard_context(
                session, next_settings, active_tab="settings"
            )
            context["request"] = request
            context["active_tab"] = "settings"
            context["flash_success"] = "Access key disabled."
            response = _render_dashboard_template(request, context, status_code=200)
            clear_access_cookie(response, request=request)
            return response

        try:
            normalized_key = normalize_access_key(access_key)
        except ValueError as exc:
            context = await _cached_dashboard_context(
                session, settings, active_tab="settings"
            )
            context["request"] = request
            context["active_tab"] = "settings"
            context["access_key_modal_open"] = True
            context["flash_error"] = str(exc)
            return _render_dashboard_template(request, context, status_code=400)
        if normalized_key != str(access_key_confirm or "").strip():
            context = await _cached_dashboard_context(
                session, settings, active_tab="settings"
            )
            context["request"] = request
            context["active_tab"] = "settings"
            context["access_key_modal_open"] = True
            context["flash_error"] = _operator_coded_error(
                "Access key confirmation must match.",
                ErrorCode.VALIDATION_FAILED,
            )
            return _render_dashboard_template(request, context, status_code=400)

        next_settings = apply_managed_env_updates(
            settings, {"ACCESS_KEY_HASH": hash_access_key(normalized_key)}
        )
        await send_pushover_notification_async(
            next_settings,
            title="CNC security setting changed",
            message=build_operator_message(
                "Admin access key was saved.",
                status="warning",
                facts=[("setting", "access_key"), ("state", "enabled")],
                action="cnc-admin logs admin --lines 100",
            ),
            priority=0,
            event="access_key_changed",
            source="ui.settings",
            url=admin_dashboard_url(next_settings, tab="settings"),
            url_title="Open settings",
        )
        context = await _cached_dashboard_context(
            session, next_settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        context["flash_success"] = "Access key saved."
        response = _render_dashboard_template(request, context, status_code=200)
        set_access_cookie(response, next_settings, request=request)
        return response
    except Exception as exc:
        logger.warning(
            "ui.settings.access_key.failed", error=str(exc), action=normalized_action
        )
        context = await _cached_dashboard_context(
            session, settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        if normalized_action not in {"clear", "disable"}:
            context["access_key_modal_open"] = True
        context["flash_error"] = f"failed to save access key: {exc}"
        return _render_dashboard_template(request, context, status_code=400)


@router.post("/ui/settings/notifications")
async def update_notification_settings_form(
    request: Request,
    csrf_token: str = Form(...),
    pushover_app_token: str = Form(""),
    pushover_user_key: str = Form(""),
    action: str = Form("save"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    normalized_action = (
        _form_string_or_default(action, "save").strip().lower() or "save"
    )
    if normalized_action == "save_test":
        normalized_action = "test"
    current_app_token = str(settings.pushover_app_token or "").strip()
    current_user_key = str(settings.pushover_user_key or "").strip()
    next_settings = settings

    try:
        if normalized_action == "clear":
            next_settings = apply_managed_env_updates(
                settings,
                {
                    "PUSHOVER_APP_TOKEN": None,
                    "PUSHOVER_USER_KEY": None,
                },
            )
            await send_pushover_notification_async(
                settings,
                title="CNC notification setting changed",
                message=build_operator_message(
                    "Pushover notifications were cleared.",
                    status="warning",
                    facts=[("setting", "pushover"), ("state", "disabled")],
                    action="cnc-admin logs admin --lines 100",
                ),
                priority=0,
                event="pushover_settings_changed",
                source="ui.settings",
                url=admin_dashboard_url(settings, tab="settings"),
                url_title="Open settings",
            )
            context = await _cached_dashboard_context(
                session, next_settings, active_tab="settings"
            )
            context["request"] = request
            context["active_tab"] = "settings"
            context["flash_success"] = "Pushover notifications cleared."
            return _render_dashboard_template(request, context, status_code=200)

        updates: dict[str, str | None] = {}
        effective_app_token, app_token_changed = _resolve_masked_secret_submission(
            pushover_app_token,
            current_app_token,
        )
        effective_user_key, user_key_changed = _resolve_masked_secret_submission(
            pushover_user_key,
            current_user_key,
        )
        if app_token_changed:
            updates["PUSHOVER_APP_TOKEN"] = effective_app_token
        if user_key_changed:
            updates["PUSHOVER_USER_KEY"] = effective_user_key

        if normalized_action == "test":
            next_settings = settings.model_copy(
                update={
                    "pushover_app_token": effective_app_token,
                    "pushover_user_key": effective_user_key,
                }
            )
        elif updates:
            next_settings = apply_managed_env_updates(settings, updates)
        elif effective_app_token or effective_user_key:
            next_settings = settings.model_copy(
                update={
                    "pushover_app_token": effective_app_token,
                    "pushover_user_key": effective_user_key,
                }
            )
        else:
            context = await _cached_dashboard_context(
                session, settings, active_tab="settings"
            )
            context["request"] = request
            context["active_tab"] = "settings"
            context["flash_error"] = (
                "Enter a Pushover app token or user key, or use Clear."
            )
            return _render_dashboard_template(request, context, status_code=400)

        context = await _cached_dashboard_context(
            session, next_settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"

        if normalized_action == "test":
            if not effective_app_token or not effective_user_key:
                context["flash_error"] = (
                    "Pushover test requires both the app token and the user key."
                )
                return _render_dashboard_template(request, context, status_code=400)
            sent = await send_pushover_notification_async(
                next_settings,
                title="CNC test alert",
                message=build_operator_message(
                    "Pushover notifications are configured and the CNC test alert was accepted.",
                    status="success",
                    facts=[("source", "settings")],
                ),
                priority=-1,
                event="settings_test_alert",
                source="ui.settings",
                url=admin_dashboard_url(next_settings, tab="settings"),
                url_title="Open settings",
            )
            if sent:
                context["flash_success"] = "Pushover test alert sent."
                return _render_dashboard_template(request, context, status_code=200)
            context["flash_error"] = "Pushover test alert could not be delivered."
            return _render_dashboard_template(request, context, status_code=502)

        if updates:
            await send_pushover_notification_async(
                next_settings,
                title="CNC notification setting changed",
                message=build_operator_message(
                    "Pushover notification credentials were saved.",
                    status="warning",
                    facts=[
                        ("app_token_changed", "yes" if app_token_changed else "no"),
                        ("user_key_changed", "yes" if user_key_changed else "no"),
                    ],
                    action="cnc-admin logs admin --lines 100",
                ),
                priority=0,
                event="pushover_settings_changed",
                source="ui.settings",
                url=admin_dashboard_url(next_settings, tab="settings"),
                url_title="Open settings",
            )

        if pushover_is_configured(next_settings):
            context["flash_success"] = "Pushover settings saved."
        else:
            context["flash_success"] = (
                "Pushover settings saved. Add both values to enable delivery."
            )
        return _render_dashboard_template(request, context, status_code=200)
    except Exception as exc:
        logger.warning(
            "ui.settings.notifications.failed", error=str(exc), action=normalized_action
        )
        context = await _cached_dashboard_context(
            session, settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        context["flash_error"] = f"failed to save Pushover settings: {exc}"
        return _render_dashboard_template(request, context, status_code=400)


@router.post("/ui/settings/beta")
async def update_beta_settings_form(
    request: Request,
    csrf_token: str = Form(...),
    beta_routing: bool = Form(False),
    beta_hardening: bool = Form(False),
    shield_enabled: bool = Form(False),
    netdata_enabled: bool = Form(False),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    beta_routing_enabled = beta_routing if isinstance(beta_routing, bool) else False
    shield_server_enabled = (
        shield_enabled if isinstance(shield_enabled, bool) else False
    )
    netdata_server_enabled = (
        netdata_enabled if isinstance(netdata_enabled, bool) else False
    )
    try:
        if bool(settings.netdata_enabled) != netdata_server_enabled:
            blocked_message = await _host_mutation_preflight_message(
                settings, action="Netdata setting save"
            )
            if blocked_message is not None:
                context = await _cached_dashboard_context(
                    session, settings, active_tab="settings"
                )
                context["request"] = request
                context["active_tab"] = "settings"
                context["flash_error"] = blocked_message
                return _render_dashboard_template(request, context, status_code=409)
        next_settings = apply_managed_env_updates(
            settings,
            {
                "BETA_ROUTING": "true" if beta_routing_enabled else "false",
                "BETA_HARDENING": "true" if beta_hardening is True else "false",
                "SHIELD_ENABLED": "true" if shield_server_enabled else "false",
                "NETDATA_ENABLED": "true" if netdata_server_enabled else "false",
            },
        )
        if bool(settings.shield_enabled) != shield_server_enabled:
            await send_pushover_notification_async(
                next_settings,
                title="CNC security setting changed",
                message=build_operator_message(
                    "Shield global setting changed.",
                    status="warning",
                    facts=[
                        ("setting", "shield"),
                        (
                            "state",
                            "enabled" if shield_server_enabled else "disabled",
                        ),
                    ],
                    action="cnc-admin logs admin --lines 100",
                ),
                priority=0,
                event="shield_settings_changed",
                source="ui.settings",
                url=admin_dashboard_url(next_settings, tab="settings"),
                url_title="Open settings",
            )
        flash_success = "Settings saved."
        apply_flash_error = None
        if bool(settings.netdata_enabled) != netdata_server_enabled:
            apply_response = await run_apply(
                session,
                next_settings,
                operation_kind="ui.settings.netdata",
                actor="ui",
            )
            if apply_response.status != "success":
                flash_success = None
                apply_error = str(
                    apply_response.details.get("error")
                    or apply_response.message
                    or "host apply failed"
                ).strip()
                apply_flash_error = (
                    f"Netdata setting could not be applied: {apply_error}"
                )
        return _dashboard_redirect(
            request,
            active_tab="settings",
            flash_success=flash_success,
            flash_error=apply_flash_error,
        )
    except Exception as exc:
        logger.warning(
            "ui.settings.beta.failed",
            error=str(exc),
            beta_routing=beta_routing_enabled,
            netdata_enabled=netdata_server_enabled,
        )
        context = await _cached_dashboard_context(
            session, settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        context["flash_error"] = f"failed to save beta settings: {exc}"
        return _render_dashboard_template(request, context, status_code=400)


@router.post("/ui/settings/nodes")
async def update_node_settings_form(
    request: Request,
    csrf_token: str = Form(...),
    multi_node_enabled: bool = Form(False),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> HTMLResponse:
    enforce_csrf(request, settings, csrf_token)
    try:
        apply_managed_env_updates(
            settings,
            {"MULTI_NODE_ENABLED": "true" if multi_node_enabled else "false"},
        )
        return _dashboard_redirect(
            request,
            active_tab="settings",
            flash_success="Node settings saved.",
        )
    except Exception as exc:
        logger.warning(
            "ui.settings.nodes.failed",
            error=str(exc),
            multi_node_enabled=multi_node_enabled,
        )
        context = await _cached_dashboard_context(
            session, settings, active_tab="settings"
        )
        context["request"] = request
        context["active_tab"] = "settings"
        context["flash_error"] = f"failed to save node settings: {exc}"
        return _render_dashboard_template(request, context, status_code=400)


@router.post("/ui/settings/nodes/join-command")
async def create_node_join_command(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, x_csrf_token)
    if not settings.multi_node_enabled:
        return JSONResponse(
            status_code=400, content={"detail": "multi node mode is disabled"}
        )
    try:
        join = await create_join_command(
            session,
            settings,
            request_base_url=str(request.base_url).rstrip("/"),
        )
    except Exception as exc:
        await session.rollback()
        logger.warning("ui.settings.nodes.join_command.failed", error=str(exc))
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(
        {
            "command": join.command,
            "expires_at": join.expires_at.isoformat(),
            "ttl_sec": settings.cluster_join_token_ttl_sec,
            "base_url": join.base_url,
        }
    )


@router.post("/ui/settings/nodes/{node_uid}/remove")
async def remove_cluster_node(
    node_uid: str,
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    settings: Settings = Depends(settings_dependency),
    session: AsyncSession = Depends(db_session_dependency),
) -> JSONResponse:
    enforce_csrf(request, settings, x_csrf_token)
    if not settings.multi_node_enabled:
        return JSONResponse(
            status_code=400, content={"detail": "multi node mode is disabled"}
        )
    try:
        node = await remove_node(
            session,
            settings,
            node_uid=node_uid,
            request_host=request.headers.get("host"),
        )
    except Exception as exc:
        await session.rollback()
        logger.warning(
            "ui.settings.nodes.remove.failed",
            node_uid=node_uid,
            error=str(exc),
        )
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse({"node_uid": node.node_uid, "state": node.state})
