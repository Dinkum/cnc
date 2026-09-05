from .support import (
    ACCESS_COOKIE_NAME,
    Backend,
    HTMLResponse,
    Path,
    Settings,
    SimpleNamespace,
    _PostedRequest,
    _cookie_values,
    _make_session,
    _request,
    _set_cookie_headers,
    access_cookie_is_valid,
    access_key_is_configured,
    hash_access_key,
    select,
    set_access_cookie,
    ui_hardening,
    ui_pages,
    ui_reads,
    ui_settings,
    ui_shared,
    verify_access_key,
)


def test_access_cookie_is_secure_for_forwarded_https_admin() -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))
    response = HTMLResponse("ok")
    request = _PostedRequest(
        path="/access",
        headers=[
            (b"host", b"admin.tailnet.example"),
            (b"x-forwarded-proto", b"https"),
        ],
    )

    set_access_cookie(response, settings, request=request)

    assert "Secure" in response.headers["set-cookie"]


def test_flash_cookies_are_secure_for_forwarded_https_admin() -> None:
    response = HTMLResponse("ok")
    request = _PostedRequest(
        path="/",
        headers=[
            (b"host", b"admin.tailnet.example"),
            (b"x-forwarded-proto", b"https"),
        ],
    )

    ui_shared._set_flash_cookies(response, request, flash_success="Saved")

    assert "Secure" in response.headers["set-cookie"]


async def test_dashboard_reads_flash_cookies_and_tab_from_request(monkeypatch) -> None:
    captured: dict[str, object] = {}
    settings = Settings()

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"active_tab": "home", "flash_error": None, "flash_success": None}

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_pages, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _request(
        path="/?tab=inputs",
        headers=[(b"cookie", b"cnc_flash_success=Saved; cnc_flash_error=Broken")],
    )
    response = await ui_pages.dashboard(request, settings=settings, session=object())

    assert response.status_code == 200
    assert captured["context"]["active_tab"] == "inputs"
    assert captured["context"]["flash_success"] == "Saved"
    assert str(captured["context"]["flash_error"]).startswith("Broken")
    assert " - Error CNC-08015-" in str(captured["context"]["flash_error"])
    set_cookie = _set_cookie_headers(response)
    assert "cnc_flash_success=" in set_cookie
    assert "cnc_flash_error=" in set_cookie


async def test_download_support_debug_bundle_returns_zip(monkeypatch) -> None:
    async def fake_build_support_debug_bundle(_session, _settings):
        return SimpleNamespace(filename="cnc-support-test.zip", content=b"zip-bytes")

    monkeypatch.setattr(
        ui_reads,
        "build_support_debug_bundle",
        fake_build_support_debug_bundle,
    )
    monkeypatch.setattr(ui_reads, "flush_logging_pipeline", lambda: None)

    response = await ui_reads.download_support_debug_bundle(
        settings=Settings(),
        session=object(),
    )

    assert response.status_code == 200
    assert response.media_type == "application/zip"
    assert response.body == b"zip-bytes"
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="cnc-support-test.zip"'
    )


async def test_provision_backend_ssh_key_generates_key_and_reconciles(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        apply_lock_path=tmp_path / "apply.lock",
    )
    reconciled: list[list[tuple[str, str | None]]] = []

    def fake_ensure_keypair(backend: Backend) -> bool:
        backend.ssh_public_key = "ssh-ed25519 AAAAOUTPUT cnc-web"
        backend.ssh_private_key = "PRIVATE KEY\n"
        return True

    async def fake_reconcile(
        backends: list[Backend], _settings: Settings
    ) -> dict[str, object]:
        reconciled.append(
            [(backend.name, backend.ssh_public_key) for backend in backends]
        )
        return {"ssh_backends": [backend.name for backend in backends]}

    monkeypatch.setattr(ui_hardening, "ensure_backend_ssh_keypair", fake_ensure_keypair)
    monkeypatch.setattr(ui_hardening, "reconcile_backend_ssh_access", fake_reconcile)
    monkeypatch.setattr(ui_hardening, "enforce_csrf", lambda *_args, **_kwargs: None)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            enabled=True,
            volumes_json="[]",
        )
        session.add(backend)
        await session.commit()

        response = await ui_hardening.provision_backend_ssh_key(
            backend.id,
            _PostedRequest(path=f"/api/backends/{backend.id}/ssh-key"),
            csrf_token="token",
            settings=settings,
            session=session,
        )
        stored = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one()

    assert response.status_code == 200
    assert response.body == b"PRIVATE KEY\n"
    assert 'filename="cnc-web-id_ed25519"' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert stored.ssh_public_key == "ssh-ed25519 AAAAOUTPUT cnc-web"
    assert reconciled == [[("web", "ssh-ed25519 AAAAOUTPUT cnc-web")]]


async def test_download_backend_ssh_key_is_read_only(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")

    assert not hasattr(ui_reads, "ensure_backend_ssh_keypair")
    assert not hasattr(ui_reads, "reconcile_backend_ssh_access")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            enabled=True,
            volumes_json="[]",
            ssh_public_key="ssh-ed25519 AAAAOUTPUT cnc-web",
            ssh_private_key="PRIVATE KEY\n",
        )
        session.add(backend)
        await session.commit()

        response = await ui_reads.download_backend_ssh_key(
            backend.id,
            settings=Settings(),
            session=session,
        )

    assert response.status_code == 200
    assert response.body == b"PRIVATE KEY\n"


async def test_download_backend_ssh_key_does_not_mint_missing_key(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")

    assert not hasattr(ui_reads, "ensure_backend_ssh_keypair")
    assert not hasattr(ui_reads, "reconcile_backend_ssh_access")

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            enabled=True,
            volumes_json="[]",
        )
        session.add(backend)
        await session.commit()

        response = await ui_reads.download_backend_ssh_key(
            backend.id,
            settings=Settings(),
            session=session,
        )
        await session.refresh(backend)

        assert response.status_code == 409
        assert backend.ssh_private_key is None


async def test_access_page_uses_access_template(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("ACCESS_KEY_HASH", raising=False)
    settings = Settings()

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _request(path="/access?next=%2Foutputs%2F7")
    response = await ui_pages.access_page(request, settings=settings)

    assert response.status_code == 200
    assert captured["name"] == "access.html"
    assert captured["context"]["mode"] == "setup"
    assert captured["context"]["next_path"] == "/outputs/7"


async def test_access_submit_unlock_sets_signed_cookie(monkeypatch) -> None:
    settings = Settings(access_key_hash=hash_access_key("correct horse battery staple"))

    request = _PostedRequest(path="/access")
    response = await ui_pages.access_submit(
        request,
        access_key="correct horse battery staple",
        access_key_confirm="",
        next_path="/outputs/7",
        settings=settings,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/outputs/7"
    cookies = _cookie_values(response)
    assert ACCESS_COOKIE_NAME in cookies
    assert access_cookie_is_valid(settings, cookies[ACCESS_COOKIE_NAME]) is True


async def test_access_submit_setup_writes_hash_and_sets_cookie(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ACCESS_KEY_HASH", raising=False)
    settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    request = _PostedRequest(path="/access")
    response = await ui_pages.access_submit(
        request,
        access_key="correct horse battery staple",
        access_key_confirm="correct horse battery staple",
        next_path="/",
        settings=settings,
    )

    next_settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    assert response.status_code == 303
    assert verify_access_key(next_settings, "correct horse battery staple") is True
    cookies = _cookie_values(response)
    assert access_cookie_is_valid(next_settings, cookies[ACCESS_COOKIE_NAME]) is True


async def test_update_access_key_settings_form_sets_cookie(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    async def fake_dashboard_context(
        _session, next_settings, **_kwargs
    ) -> dict[str, object]:
        return {
            "flash_error": None,
            "flash_success": None,
            "active_tab": "home",
            "access_key_settings": ui_shared._access_key_settings_summary(
                next_settings
            ),
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    request = _PostedRequest(path="/ui/settings/access-key")
    response = await ui_settings.update_access_key_settings_form(
        request,
        csrf_token="token",
        access_key="correct horse battery staple",
        access_key_confirm="correct horse battery staple",
        action="save",
        settings=settings,
        session=object(),
    )

    next_settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    assert response.status_code == 200
    assert captured["context"]["flash_success"] == "Access key saved."
    cookies = _cookie_values(response)
    assert access_cookie_is_valid(next_settings, cookies[ACCESS_COOKIE_NAME]) is True


async def test_update_access_key_settings_form_reopens_modal_on_validation_error(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    settings = Settings(managed_env_file_path=tmp_path / "cnc.env")

    async def fake_dashboard_context(
        _session, _settings, **_kwargs
    ) -> dict[str, object]:
        return {"flash_error": None, "flash_success": None, "active_tab": "home"}

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    response = await ui_settings.update_access_key_settings_form(
        _PostedRequest(path="/ui/settings/access-key"),
        csrf_token="token",
        access_key="correct horse battery staple",
        access_key_confirm="different horse battery staple",
        action="save",
        settings=settings,
        session=object(),
    )

    assert response.status_code == 400
    assert captured["context"]["active_tab"] == "settings"
    assert captured["context"]["access_key_modal_open"] is True
    assert str(captured["context"]["flash_error"]).startswith(
        "Access key confirmation must match."
    )
    assert " - Error CNC-01001-" in str(captured["context"]["flash_error"])


async def test_update_access_key_settings_form_disables_existing_key(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    env_path = tmp_path / "cnc.env"
    settings = Settings(
        managed_env_file_path=env_path,
        access_key_hash=hash_access_key("correct horse battery staple"),
    )

    async def fake_dashboard_context(
        _session, next_settings, **_kwargs
    ) -> dict[str, object]:
        return {
            "flash_error": None,
            "flash_success": None,
            "active_tab": "home",
            "access_key_settings": ui_shared._access_key_settings_summary(
                next_settings
            ),
        }

    def fake_template_response(request, name, context, status_code=200):
        captured["request"] = request
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return HTMLResponse("ok", status_code=status_code)

    monkeypatch.setattr(ui_settings, "enforce_csrf", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_shared, "_dashboard_context", fake_dashboard_context)
    monkeypatch.setattr(ui_shared.templates, "TemplateResponse", fake_template_response)

    response = await ui_settings.update_access_key_settings_form(
        _PostedRequest(path="/ui/settings/access-key"),
        csrf_token="token",
        access_key="",
        access_key_confirm="",
        action="disable",
        settings=settings,
        session=object(),
    )

    next_settings = Settings(managed_env_file_path=env_path)

    assert response.status_code == 200
    assert captured["context"]["flash_success"] == "Access key disabled."
    assert access_key_is_configured(next_settings) is False


def test_dashboard_redirect_sanitizes_flash_cookie_control_chars() -> None:
    response = ui_shared._dashboard_redirect(
        _request(path="/"),
        active_tab="outputs",
        flash_error="line 1\nstdout:\n\nstderr:\nuseradd: cannot lock /etc/passwd;\ttry again later.",
    )

    flash_error = _cookie_values(response)["cnc_flash_error"]
    assert flash_error.startswith(
        "line 1 stdout: stderr: useradd: cannot lock /etc/passwd; try again later."
    )
    assert " - Error CNC-08015-" in flash_error
