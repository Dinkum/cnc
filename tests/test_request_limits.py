from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.request_limits import RequestBodyLimitMiddleware


def _limited_app() -> FastAPI:
    app = FastAPI()

    @app.post("/limited")
    async def limited(request: Request):
        return {"size": len(await request.body())}

    @app.post("/unlimited")
    async def unlimited(request: Request):
        return {"size": len(await request.body())}

    app.add_middleware(RequestBodyLimitMiddleware, limits={"/limited": 8})
    return app


def test_body_limit_rejects_declared_oversize() -> None:
    response = TestClient(_limited_app()).post("/limited", content=b"123456789")

    assert response.status_code == 413
    assert response.text == "Request body too large"


def test_body_limit_does_not_apply_to_unlisted_authenticated_route() -> None:
    response = TestClient(_limited_app()).post("/unlimited", content=b"x" * 4096)

    assert response.status_code == 200
    assert response.json() == {"size": 4096}


async def test_body_limit_counts_stream_without_content_length() -> None:
    app = _limited_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/limited",
        "raw_path": b"/limited",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    incoming = iter(
        (
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        )
    )
    outgoing: list[dict] = []

    async def receive():
        return next(incoming)

    async def send(message):
        outgoing.append(message)

    await app(scope, receive, send)

    response_start = next(
        message for message in outgoing if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 413


async def test_body_limit_rejects_invalid_content_length() -> None:
    app = _limited_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/limited",
        "raw_path": b"/limited",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-length", b"invalid")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    outgoing: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        outgoing.append(message)

    await app(scope, receive, send)

    response_start = next(
        message for message in outgoing if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 400
