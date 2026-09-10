"""Real form navigation regression.

Run with CNC_PLAYWRIGHT_MODULE pointing to an installed Playwright JS entrypoint.
Set CNC_BROWSER_ENGINES=chromium,webkit to cover both installed browser engines.
The browser supplies Origin itself; HTTP test clients cannot model this behavior.
"""

import os
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.access import access_request_is_authenticated, hash_access_key
from app.config import Settings
from app.dependencies import settings_dependency
from app.http_security import SecurityHeadersMiddleware
from app.ui.routes.pages import router


@pytest.mark.skipif(
    not os.environ.get("CNC_PLAYWRIGHT_MODULE"),
    reason="Set CNC_PLAYWRIGHT_MODULE to run real-browser access regression",
)
def test_browser_native_access_form(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        access_key_hash=hash_access_key("browser-test-access-key"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
    )
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.dependency_overrides[settings_dependency] = lambda: settings
    app.include_router(router)

    @app.get("/browser-success")
    def success(request: Request):
        assert access_request_is_authenticated(request, settings)
        return {"authenticated": True}

    @app.get("/browser-attacker", response_class=HTMLResponse)
    def attacker(request: Request):
        port = request.url.port
        return (
            f'<form method="post" action="http://127.0.0.1:{port}/access">'
            '<input name="access_key" value="browser-test-access-key">'
            "<button>Submit</button></form>"
        )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            result = subprocess.run(
                ["node", "--input-type=module", "-", str(port)],
                input=_BROWSER_TEST,
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


_BROWSER_TEST = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const playwright = await import(pathToFileURL(process.env.CNC_PLAYWRIGHT_MODULE));
const port = process.argv[2];
const base = `http://127.0.0.1:${port}`;
for (const engine of (process.env.CNC_BROWSER_ENGINES || 'chromium').split(',')) {
    const browser = await playwright[engine].launch({headless: true});
    try {
        const page = await browser.newPage();
        page.setDefaultTimeout(10000);
        page.setDefaultNavigationTimeout(10000);
        await page.goto(`${base}/access?next=/browser-success`);
        await page.locator('[name=access_key]').fill('browser-test-access-key');
        const posted = page.waitForRequest(r => r.method() === 'POST');
        await Promise.all([
            page.waitForURL(`${base}/browser-success`),
            page.locator('form button[type=submit]').click(),
        ]);
        assert.equal((await posted).headers().origin, base);
        assert.match(await page.locator('body').innerText(), /"authenticated":true/);

        // A different hostname is a different origin, even on this same server.
        await page.context().clearCookies();
        await page.goto(`http://localhost:${port}/browser-attacker`);
        const rejected = page.waitForResponse(r => r.request().method() === 'POST');
        await page.locator('button').click();
        assert.equal((await rejected).status(), 403);
        assert.equal((await page.context().cookies()).length, 0);
        console.log(`${engine}: native login succeeds; cross-origin form rejected`);
    } finally {
        await browser.close();
    }
}
"""
