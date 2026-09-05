from __future__ import annotations

from app.access import access_key_is_configured, sanitize_next_path
from app.config import Settings
from app.static_delivery import stylesheet_asset_version
from app.ui.settings import _access_key_settings_summary


def _access_page_context(
    settings: Settings,
    *,
    next_path: str,
    flash_error: str | None = None,
    flash_success: str | None = None,
) -> dict[str, object]:
    configured = access_key_is_configured(settings)
    return {
        "mode": "unlock" if configured else "setup",
        "next_path": sanitize_next_path(next_path),
        "flash_error": flash_error,
        "flash_success": flash_success,
        "access_key_settings": _access_key_settings_summary(settings),
        "asset_version": stylesheet_asset_version(settings.static_files_dir),
    }
