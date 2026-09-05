from __future__ import annotations

from app.access import access_key_is_configured, access_key_needs_rehash
from app.config import Settings
from app.services.notification_state import get_pushover_delivery_summary
from app.services.notifications import (
    mask_secret,
    pushover_is_configured,
)
from app.services.tailscale_urls import tailscale_service_url


def _access_key_settings_summary(settings: Settings) -> dict[str, object]:
    configured = access_key_is_configured(settings)
    return {
        "configured": configured,
        "status_label": "enabled" if configured else "off",
        "ttl_label": (
            f"{settings.access_session_ttl_sec // 86400}d"
            if settings.access_session_ttl_sec >= 86400
            else f"{settings.access_session_ttl_sec // 3600}h"
        ),
        "needs_rehash": access_key_needs_rehash(settings),
    }


def _notification_settings_summary(settings: Settings) -> dict[str, object]:
    configured = pushover_is_configured(settings)
    app_token = str(settings.pushover_app_token or "").strip()
    user_key = str(settings.pushover_user_key or "").strip()
    app_token_masked = mask_secret(app_token)
    user_key_masked = mask_secret(user_key)
    delivery = _notification_delivery_summary(settings)
    return {
        "configured": configured,
        "status_label": "enabled" if configured else "off",
        "app_token_masked": app_token_masked,
        "user_key_masked": user_key_masked,
        "app_token_display": app_token_masked if app_token else "",
        "user_key_display": user_key_masked if user_key else "",
        **delivery,
    }


def _notification_delivery_summary(settings: Settings) -> dict[str, object]:
    return get_pushover_delivery_summary(settings)


def _netdata_settings_summary(settings: Settings) -> dict[str, object]:
    url = tailscale_service_url("netdata", settings)
    return {
        "enabled": bool(settings.netdata_enabled),
        "status_label": "enabled" if settings.netdata_enabled else "off",
        "url": url,
        "port": settings.netdata_port,
    }


def _resolve_masked_secret_submission(
    submitted_value: str, current_value: str
) -> tuple[str, bool]:
    submitted = str(submitted_value or "").strip()
    current = str(current_value or "").strip()
    if not submitted:
        return "", bool(current)
    if current and submitted == mask_secret(current):
        return current, False
    return submitted, submitted != current
