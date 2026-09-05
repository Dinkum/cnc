from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import html
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from app.http_security import SecurityHeadersMiddleware
from app.request_limits import RequestBodyLimitMiddleware, SHIELD_BODY_LIMITS
from app.services.renderers import SHIELD_PUBLIC_PATH_PREFIX
from app.services.shield_service import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SEC,
    ShieldStore,
)


@dataclass(frozen=True)
class ShieldAppConfig:
    db_path: Path
    secret_key: str
    access_code_hash: str
    config_path: Path | None = None
    session_ttl_sec: int = SESSION_TTL_SEC


def config_from_env() -> ShieldAppConfig:
    return ShieldAppConfig(
        db_path=Path(os.environ.get("SHIELD_DB_PATH", "/var/lib/cnc/shield/shield.db")),
        secret_key=os.environ.get("SHIELD_SECRET_KEY", ""),
        access_code_hash=os.environ.get("SHIELD_ACCESS_CODE_HASH", ""),
        config_path=Path(os.environ["SHIELD_CONFIG_PATH"])
        if os.environ.get("SHIELD_CONFIG_PATH")
        else None,
        session_ttl_sec=int(
            os.environ.get("SHIELD_SESSION_TTL_SEC", str(SESSION_TTL_SEC))
        ),
    )


def create_app(config: ShieldAppConfig | None = None) -> Starlette:
    active_config = config or config_from_env()
    store = ShieldStore(active_config.db_path)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await asyncio.to_thread(store.init)
        yield

    async def access(request: Request) -> HTMLResponse:
        next_target = _safe_next(request.query_params.get("next"))
        return HTMLResponse(_render_access_page(next_target=next_target, failed=False))

    async def submit(request: Request) -> Response:
        form = await _safe_form(request)
        next_target = _safe_next(str(form.get("next") or "/"))
        access_code = str(form.get("access_code") or "")
        identity_key = _identity_key(request)
        output_key = _output_key(request)
        expected_code_hash = _access_code_hash_for_output(active_config, output_key)
        result = await asyncio.to_thread(
            store.authenticate_code,
            secret_key=active_config.secret_key,
            expected_code_hash=expected_code_hash,
            submitted_code=access_code,
            identity_key=identity_key,
            output_key=output_key,
            session_ttl_sec=active_config.session_ttl_sec,
        )
        if not result.ok or not result.session_token:
            return HTMLResponse(
                _render_access_page(next_target=next_target, failed=True),
                status_code=200,
            )
        response = RedirectResponse(next_target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            result.session_token,
            max_age=active_config.session_ttl_sec,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    async def check(request: Request) -> Response:
        output_key = _output_key(request)
        if await asyncio.to_thread(
            store.session_is_valid,
            secret_key=active_config.secret_key,
            session_token=request.cookies.get(SESSION_COOKIE_NAME),
            output_key=output_key,
        ):
            return Response(status_code=204)
        return PlainTextResponse("unauthorized", status_code=401)

    async def health(_request: Request) -> Response:
        await asyncio.to_thread(store.init)
        return PlainTextResponse("ok")

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", access, methods=["GET"]),
            Route("/access", access, methods=["GET"]),
            Route(f"{SHIELD_PUBLIC_PATH_PREFIX}/access", access, methods=["GET"]),
            Route("/submit", submit, methods=["POST"]),
            Route(f"{SHIELD_PUBLIC_PATH_PREFIX}/submit", submit, methods=["POST"]),
            Route("/check", check, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ],
    )
    app.add_middleware(RequestBodyLimitMiddleware, limits=SHIELD_BODY_LIMITS)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


async def _safe_form(request: Request) -> FormData:
    content_length = request.headers.get("content-length")
    try:
        content_length_value = int(content_length or "0")
    except ValueError:
        content_length_value = 0
    if content_length is not None and content_length_value > 2048:
        return FormData()
    return await request.form(max_fields=4, max_files=0)


def _identity_key(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return f"ip:{cf_ip}"
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded_for:
        return f"ip:{forwarded_for}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


def _output_key(request: Request) -> str:
    value = request.headers.get("x-shield-output", "").strip()
    return value[:128] or "global"


def _access_code_hash_for_output(config: ShieldAppConfig, output_key: str) -> str:
    if not config.config_path:
        return config.access_code_hash
    try:
        payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config.access_code_hash
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        return config.access_code_hash
    output = outputs.get(output_key)
    if not isinstance(output, dict):
        return config.access_code_hash
    code_hash = output.get("code_hash")
    return (
        code_hash
        if isinstance(code_hash, str) and code_hash
        else config.access_code_hash
    )


def _safe_next(value: str | None) -> str:
    candidate = str(value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\r" in candidate or "\n" in candidate:
        return "/"
    return candidate[:1024] or "/"


def _render_access_page(*, next_target: str, failed: bool) -> str:
    escaped_next = html.escape(next_target, quote=True)
    failure = '<p class="error">Try again.</p>' if failed else ""
    submit_path = f"{SHIELD_PUBLIC_PATH_PREFIX}/submit"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Access</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #060c14;
      --panel: rgba(17, 25, 38, 0.92);
      --line: rgba(159, 191, 232, 0.18);
      --text: #f4f8ff;
      --muted: #8c9ab1;
      --accent: #87d9ff;
      --accent-strong: #5ac8fa;
      --danger: #ff9ca8;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; margin: 0; }}
    body {{
      display: grid;
      place-items: center;
      padding: 24px;
      background:
        radial-gradient(circle at 50% 0%, rgba(76, 136, 169, 0.20), transparent 34rem),
        var(--bg);
      color: var(--text);
      font: 16px/1.4 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(100%, 360px);
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
    }}
    label {{
      display: block;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    input {{
      width: 100%;
      height: 46px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 13px;
      background: rgba(4, 9, 17, 0.72);
      color: var(--text);
      font: inherit;
      outline: none;
    }}
    input:focus {{
      border-color: rgba(135, 217, 255, 0.74);
      box-shadow: 0 0 0 3px rgba(135, 217, 255, 0.14);
    }}
    button {{
      width: 100%;
      height: 46px;
      margin-top: 14px;
      border: 0;
      border-radius: 6px;
      background: linear-gradient(180deg, var(--accent), var(--accent-strong));
      color: #03101a;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
    }}
    .error {{
      margin: 12px 0 0;
      color: var(--danger);
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <main>
    <form method="post" action="{submit_path}">
      <label for="access-code">Access Code:</label>
      <input id="access-code" name="access_code" autocomplete="one-time-code" autofocus>
      <input type="hidden" name="next" value="{escaped_next}">
      <button type="submit">Submit</button>
      {failure}
    </form>
  </main>
</body>
</html>
"""


app = create_app()
