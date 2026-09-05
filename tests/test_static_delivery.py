from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.static_delivery import build_static_asset_app


def _app_with_static(tmp_path: Path) -> FastAPI:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "app.css").write_text("a" * 4096, encoding="utf-8")
    vendor_dir = static_dir / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "chart-4.4.7.min.js").write_text("b" * 4096, encoding="utf-8")

    app = FastAPI()
    app.mount("/static", build_static_asset_app(static_dir), name="static")

    @app.get("/events")
    async def events():
        return "x" * 4096

    return app


def test_versioned_static_asset_is_compressed_and_immutable(tmp_path: Path) -> None:
    response = TestClient(_app_with_static(tmp_path)).get(
        "/static/app.css?v=release-1",
        headers={"accept-encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert response.headers["cache-control"] == ("public, max-age=31536000, immutable")
    assert response.text == "a" * 4096


def test_unversioned_static_asset_must_revalidate(tmp_path: Path) -> None:
    client = TestClient(_app_with_static(tmp_path))

    response = client.get("/static/app.css", headers={"accept-encoding": "identity"})
    revalidated = client.get(
        "/static/app.css",
        headers={"if-none-match": response.headers["etag"]},
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == (
        "public, max-age=0, must-revalidate"
    )


def test_versioned_vendor_filename_is_immutable(tmp_path: Path) -> None:
    response = TestClient(_app_with_static(tmp_path)).get(
        "/static/vendor/chart-4.4.7.min.js"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == ("public, max-age=31536000, immutable")


def test_static_compression_does_not_touch_non_static_routes(tmp_path: Path) -> None:
    response = TestClient(_app_with_static(tmp_path)).get(
        "/events",
        headers={"accept-encoding": "gzip"},
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "cache-control" not in response.headers


def test_static_byte_range_is_not_compressed(tmp_path: Path) -> None:
    response = TestClient(_app_with_static(tmp_path)).get(
        "/static/app.css?v=release-1",
        headers={"accept-encoding": "gzip", "range": "bytes=0-9"},
    )

    assert response.status_code == 206
    assert response.content == b"a" * 10
    assert response.headers["content-range"] == "bytes 0-9/4096"
    assert "content-encoding" not in response.headers


def test_missing_versioned_static_asset_is_not_cached(tmp_path: Path) -> None:
    response = TestClient(_app_with_static(tmp_path)).get(
        "/static/missing.css?v=release-1"
    )

    assert response.status_code == 404
    assert "immutable" not in response.headers.get("cache-control", "")


def test_admin_stylesheet_references_are_versioned() -> None:
    source_paths = [
        Path("app/main.py"),
        Path("app/ui/routes/pages.py"),
        Path("app/templates/access.html"),
        Path("app/templates/index.html"),
        Path("app/templates/output_detail.html"),
    ]

    for source_path in source_paths:
        source = source_path.read_text(encoding="utf-8")
        assert 'href="/static/css/app.css">' not in source
