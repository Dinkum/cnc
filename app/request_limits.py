from __future__ import annotations

from collections.abc import Mapping

from starlette.responses import PlainTextResponse


ADMIN_UNAUTHENTICATED_BODY_LIMITS = {
    "/access": 4 * 1024,
    "/join/register": 64 * 1024,
    "/join/confirm": 64 * 1024,
}
SHIELD_BODY_LIMITS = {
    "/submit": 2 * 1024,
    "/shield/submit": 2 * 1024,
}


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce small limits on selected request paths without buffering bodies."""

    def __init__(self, app, *, limits: Mapping[str, int]) -> None:
        self.app = app
        self.limits = {
            str(path): int(limit)
            for path, limit in limits.items()
            if str(path).startswith("/") and int(limit) > 0
        }

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limits.get(str(scope.get("path") or ""))
        if limit is None:
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value.strip()
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            if len(content_lengths) != 1 or not content_lengths[0].isdigit():
                await self._reject(scope, receive, send, invalid=True)
                return
            try:
                declared_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._reject(scope, receive, send, invalid=True)
                return
            if declared_length > limit:
                await self._reject(scope, receive, send)
                return

        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:  # pragma: no cover - selected handlers read first
                raise
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, *, invalid: bool = False) -> None:
        response = PlainTextResponse(
            "Invalid content length" if invalid else "Request body too large",
            status_code=400 if invalid else 413,
        )
        await response(scope, receive, send)
