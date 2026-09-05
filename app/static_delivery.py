from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import parse_qs

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from app.services.update_service import current_app_version


STATIC_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
STATIC_REVALIDATE_CACHE_CONTROL = "public, max-age=0, must-revalidate"
_VERSIONED_VENDOR_PATH_RE = re.compile(
    r"^/(?:static/)?vendor/[a-z0-9._-]+-\d+\.\d+\.\d+(?:[._-][a-z0-9]+)*\.[a-z0-9]+$"
)


def stylesheet_asset_version(static_files_dir: Path) -> str:
    version = current_app_version()
    try:
        css_path = Path(static_files_dir) / "css" / "app.css"
        return f"{version}-{int(css_path.stat().st_mtime)}"
    except OSError:
        return version


class StaticAssetCacheMiddleware:
    """Cache only explicitly versioned static URLs without risking stale assets."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cache_control = (
            STATIC_IMMUTABLE_CACHE_CONTROL
            if _request_is_versioned_asset(scope)
            else STATIC_REVALIDATE_CACHE_CONTROL
        )

        async def send_with_cache_control(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                response_cache_control = (
                    cache_control
                    if int(message.get("status") or 0) in {200, 206, 304}
                    else "no-store"
                )
                headers.setdefault("Cache-Control", response_cache_control)
            await send(message)

        await self.app(scope, receive, send_with_cache_control)


class StaticCompressionMiddleware:
    """Compress ordinary static responses while preserving byte-range semantics."""

    def __init__(self, app) -> None:
        self.app = app
        self.compressed_app = GZipMiddleware(
            app,
            minimum_size=1024,
            compresslevel=6,
        )

    async def __call__(self, scope, receive, send) -> None:
        has_range = scope["type"] == "http" and any(
            name.lower() == b"range" for name, _value in scope.get("headers", ())
        )
        target = self.app if has_range else self.compressed_app
        await target(scope, receive, send)


def build_static_asset_app(
    directory: Path,
    *,
    check_dir: bool = True,
):
    static_files = StaticFiles(directory=str(directory), check_dir=check_dir)
    compressed = StaticCompressionMiddleware(static_files)
    return StaticAssetCacheMiddleware(compressed)


def _request_is_versioned_asset(scope) -> bool:
    path = str(scope.get("path") or "").lower()
    if _VERSIONED_VENDOR_PATH_RE.fullmatch(path):
        return True
    raw_query = scope.get("query_string", b"")
    try:
        query = (
            raw_query.decode("ascii")
            if isinstance(raw_query, bytes)
            else str(raw_query)
        )
    except UnicodeDecodeError:
        return False
    versions = parse_qs(query, keep_blank_values=True).get("v", ())
    return any(str(version).strip() for version in versions)
