from .support import (
    Backend,
    Input,
    Path,
    Settings,
    _make_session,
    _request,
    socket,
    ui_pages,
    ui_shared,
)


def test_backend_operator_commands_use_server_ip_when_request_is_ip() -> None:
    backend = Backend(id=7, name="web", kind="app")
    request = _request(path="/outputs/1", headers=[(b"host", b"203.0.113.10")])

    commands = ui_shared._backend_operator_commands(backend, request)

    assert len(commands) == 3
    assert commands[0]["label"] == "ssh shell"
    assert commands[0]["command"] == "ssh web@203.0.113.10"
    assert commands[1]["label"] == "ssh exec"
    assert commands[1]["command"] == "ssh web@203.0.113.10 'python3 --version'"
    assert commands[2]["label"] == "llm handoff"
    assert "This app is hosted in a service called CNC." in commands[2]["command"]
    assert "ssh web@203.0.113.10 llm-help" in commands[2]["command"]
    assert "ssh web@203.0.113.10" in commands[2]["command"]
    assert "ssh root@" not in commands[2]["command"]


def test_backend_operator_commands_hide_disabled_app_access() -> None:
    backend = Backend(id=7, name="draft", kind="app", enabled=False)
    request = _request(path="/outputs/7", headers=[(b"host", b"203.0.113.10")])

    commands = ui_shared._backend_operator_commands(backend, request)

    assert commands == []


def test_backend_operator_commands_fall_back_to_placeholder_when_host_resolution_fails(
    monkeypatch,
) -> None:
    backend = Backend(id=7, name="web", kind="app")
    request = _request(
        path="/outputs/1", headers=[(b"host", b"cnc-node.example.ts.net")]
    )

    def fake_getaddrinfo(_host: str, _port, proto: int):
        assert proto == socket.IPPROTO_TCP
        raise socket.gaierror("unresolvable")

    monkeypatch.setattr(ui_shared.socket, "getaddrinfo", fake_getaddrinfo)

    commands = ui_shared._backend_operator_commands(backend, request)

    assert len(commands) == 3
    assert commands[0]["command"] == "ssh web@SERVER_IP"
    assert "ssh web@SERVER_IP llm-help" in commands[2]["command"]
    assert "ssh web@SERVER_IP" in commands[2]["command"]
    assert "ssh root@" not in commands[2]["command"]
    assert "CNC" in commands[2]["command"]


def test_backend_operator_commands_resolve_host_to_ip(monkeypatch) -> None:
    backend = Backend(id=7, name="web", kind="app")
    request = _request(
        path="/outputs/1",
        headers=[(b"host", b"admin.example.com")],
    )

    def fake_getaddrinfo(host: str, _port, proto: int):
        assert host == "admin.example.com"
        assert proto == socket.IPPROTO_TCP
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("fd7a:115c:a1e0::1234", 0, 0, 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", 0),
            ),
        ]

    monkeypatch.setattr(ui_shared.socket, "getaddrinfo", fake_getaddrinfo)

    commands = ui_shared._backend_operator_commands(backend, request)

    assert len(commands) == 3
    assert commands[0]["command"] == "ssh web@203.0.113.10"
    assert commands[1]["command"] == "ssh web@203.0.113.10 'python3 --version'"
    assert "ssh web@203.0.113.10 llm-help" in commands[2]["command"]
    assert "ssh web@203.0.113.10" in commands[2]["command"]
    assert "ssh root@" not in commands[2]["command"]


def test_backend_operator_commands_for_page_use_advertised_host_without_dns(
    monkeypatch,
) -> None:
    backend = Backend(id=7, name="web", kind="app")
    request = _request(
        path="/outputs/1",
        headers=[(b"host", b"admin.example.com")],
    )
    settings = Settings(ssh_advertise_host="203.0.113.10")

    def fail_getaddrinfo(*_args, **_kwargs):
        raise AssertionError(
            "page operator commands should not resolve DNS during render"
        )

    monkeypatch.setattr(ui_shared.socket, "getaddrinfo", fail_getaddrinfo)

    commands = ui_shared._backend_operator_commands_for_page(
        backend,
        settings=settings,
        host=ui_shared._backend_ssh_destination(settings, request, resolve_dns=False),
        llm_help_url=ui_shared._output_llm_help_url(request, backend.id),
    )

    assert len(commands) == 3
    assert commands[0]["command"] == "ssh web@203.0.113.10"
    assert "ssh web@203.0.113.10 llm-help" in commands[2]["command"]
    assert "ssh web@203.0.113.10" in commands[2]["command"]
    assert "ssh root@" not in commands[2]["command"]


def test_backend_operator_commands_for_page_prefer_configured_tailnet_host(
    monkeypatch,
) -> None:
    backend = Backend(id=7, name="web", kind="app")
    request = _request(path="/outputs/1", headers=[(b"host", b"203.0.113.10")])
    settings = Settings(
        admin_allowed_hosts="admin.example.ts.net",
        ssh_advertise_host="203.0.113.10",
    )

    def fail_getaddrinfo(*_args, **_kwargs):
        raise AssertionError(
            "page operator commands should not resolve DNS during render"
        )

    monkeypatch.setattr(ui_shared.socket, "getaddrinfo", fail_getaddrinfo)

    commands = ui_shared._backend_operator_commands_for_page(
        backend,
        settings=settings,
        host=ui_shared._backend_ssh_destination(settings, request, resolve_dns=False),
        llm_help_url=ui_shared._output_llm_help_url(request, backend.id),
    )

    assert len(commands) == 3
    assert commands[0]["command"] == "ssh web@admin.example.ts.net"
    assert "ssh web@admin.example.ts.net llm-help" in commands[2]["command"]
    assert "ssh web@203.0.113.10" not in commands[2]["command"]


def test_backend_ssh_destination_uses_tailnet_host_for_backend_access() -> None:
    request = _request(
        path="/outputs/1",
        headers=[(b"host", b"cnc-admin.example.ts.net")],
    )

    host = ui_shared._backend_ssh_destination(Settings(), request, resolve_dns=False)

    assert host == "cnc-admin.example.ts.net"


async def test_host_llm_help_context_uses_cold_cache_without_live_status(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")

    async def fail_collect_status(*_args, **_kwargs):
        raise AssertionError("host LLM help must not trigger live host status")

    monkeypatch.setattr(ui_shared, "collect_status", fail_collect_status)
    monkeypatch.setattr(ui_shared, "peek_cached_status", lambda: None)

    async with maker() as session:
        session.add(
            Backend(
                name="docs",
                kind="static",
                static_root="/srv/docs",
                enabled=True,
            )
        )
        await session.commit()
        context = await ui_shared._host_llm_help_context(session, settings)

    backends = context["backends"]
    output_details = context["output_details"]
    assert [backend.name for backend in backends] == ["docs"]
    assert output_details[backends[0].id]["target"] == "/srv/docs"


async def test_host_llm_help_renders_plain_text(monkeypatch) -> None:
    backend = Backend(
        id=7,
        name="web",
        kind="app",
        enabled=True,
        port=12001,
        internal_port=8337,
        workdir="/srv/web",
        base_image="docker.io/library/ubuntu:24.04",
        install_command="apt-get update",
        update_command="git pull --ff-only && .venv/bin/pip install -r requirements.txt",
        start_command="python3 app.py",
        healthcheck_mode="tcp",
        healthcheck_path=None,
        env_json='{"PORT":"8337"}',
        volumes_json='["/srv/web-data:/data"]',
    )
    backend.inputs = [Input(kind="domain", hostname="web.example.com", enabled=True)]

    async def fake_host_llm_help_context(_session, _settings):
        return {
            "backends": [backend],
            "output_details": {
                7: {
                    "status_value": "healthy",
                    "service_state": "active / running",
                    "target": "http://127.0.0.1:12001",
                    "exposure_value": "127.0.0.1:12001:8337/tcp",
                }
            },
        }

    monkeypatch.setattr(ui_pages, "_host_llm_help_context", fake_host_llm_help_context)

    response = await ui_pages.host_llm_help(
        _request(path="/llm.txt", headers=[(b"host", b"203.0.113.10")]),
        settings=Settings(),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "# CNC Host Context" in body
    assert "## What CNC Is" in body
    assert "runtime_manager: podman" in body
    assert (
        "Do not use docker commands on this host; CNC app runtimes are managed with Podman."
        in body
    )
    assert (
        "Do not use podman exec, podman restart, or podman rm directly unless CNC guidance specifically requires it."
        in body
    )
    assert "## Workflow" in body
    assert "## Backend Inventory" in body
    assert (
        "Backend SSH is ordinary OpenSSH restricted to tailnet source addresses, not the Tailscale-managed SSH feature. Public-IP SSH is for host/root recovery, not backend users."
        in body
    )
    assert (
        "If public traffic is failing, verify both the edge route and the backend doctor output."
        in body
    )
    assert "admin_bind:" not in body
    assert "updater_script_path:" not in body
    assert "podman ps -a" not in body
    assert "cnc-admin host apply" in body
    assert "ssh <backend>@203.0.113.10" in body
    assert "`web`: status=healthy;" in body
    assert "http://203.0.113.10/llm.txt" not in body


async def test_host_llm_help_uses_advertised_host_for_backend_aliases(
    monkeypatch,
) -> None:
    backend = Backend(id=7, name="web", kind="app", enabled=True)

    async def fake_host_llm_help_context(_session, _settings):
        return {
            "backends": [backend],
            "output_details": {
                7: {
                    "status_value": "healthy",
                    "service_state": "active / running",
                    "target": "http://127.0.0.1:12001",
                    "exposure_value": "127.0.0.1:12001:8337/tcp",
                }
            },
        }

    monkeypatch.setattr(ui_pages, "_host_llm_help_context", fake_host_llm_help_context)

    response = await ui_pages.host_llm_help(
        _request(
            path="/llm.txt",
            headers=[(b"host", b"admin.example.com")],
        ),
        settings=Settings(ssh_advertise_host="203.0.113.10"),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "ssh <backend>@203.0.113.10" in body
    assert "ssh <backend>@100.64.0.10" not in body


async def test_host_llm_help_prefers_tailnet_host_for_backend_aliases(
    monkeypatch,
) -> None:
    backend = Backend(id=7, name="web", kind="app", enabled=True)

    async def fake_host_llm_help_context(_session, _settings):
        return {
            "backends": [backend],
            "output_details": {
                7: {
                    "status_value": "healthy",
                    "service_state": "active / running",
                    "target": "http://127.0.0.1:12001",
                    "exposure_value": "127.0.0.1:12001:8337/tcp",
                }
            },
        }

    monkeypatch.setattr(ui_pages, "_host_llm_help_context", fake_host_llm_help_context)

    response = await ui_pages.host_llm_help(
        _request(path="/llm.txt", headers=[(b"host", b"203.0.113.10")]),
        settings=Settings(
            admin_allowed_hosts="admin.example.ts.net",
            ssh_advertise_host="203.0.113.10",
        ),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "ssh <backend>@admin.example.ts.net" in body
    assert "ssh root@203.0.113.10 llm-help" in body
    assert "ssh <backend>@203.0.113.10" not in body


async def test_output_llm_help_renders_plain_text(monkeypatch) -> None:
    backend = Backend(
        id=7,
        name="web",
        kind="app",
        enabled=True,
        port=12001,
        internal_port=8337,
        workdir="/srv/web",
        base_image="docker.io/library/ubuntu:24.04",
        install_command="apt-get update",
        update_command="git pull --ff-only && .venv/bin/pip install -r requirements.txt",
        start_command="python3 app.py",
        healthcheck_mode="tcp",
        healthcheck_path=None,
        env_json='{"PORT":"8337"}',
        volumes_json='["/srv/web-data:/data"]',
    )
    backend.inputs = [Input(kind="domain", hostname="web.example.com", enabled=True)]

    async def fake_output_page_context(_session, _settings, backend_id: int):
        assert backend_id == 7
        return {
            "selected_backend": backend,
            "selected_output_detail": {
                "status_value": "healthy",
                "service_state": "active / running",
                "target": "http://127.0.0.1:12001",
                "exposure_value": "127.0.0.1:12001:8337/tcp",
            },
        }

    monkeypatch.setattr(ui_pages, "_output_page_context", fake_output_page_context)

    response = await ui_pages.output_llm_help(
        7,
        _request(path="/outputs/7/llm.txt", headers=[(b"host", b"203.0.113.10")]),
        settings=Settings(),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "# CNC Backend Context: web" in body
    assert "## What CNC Is" in body
    assert "## LLM Rules" in body
    assert "backend: web" in body
    assert "## Sandbox Boundary" in body
    assert "## Verify Next" in body
    assert "debug_toolbelt: v3" in body
    assert (
        "debug_tools: bash, curl, dig, ip, jq, nslookup, ping, ps, rg, sqlite3, ss, sudo, top, wget"
        in body
    )
    assert "mounts: /srv/web-data:/data" in body
    assert "## Commands" in body
    assert "## Escalate To Host" in body
    assert "inputs: domain: web.example.com" in body
    assert "http://203.0.113.10/outputs/7/llm.txt" not in body
    assert "ssh web@203.0.113.10 llm-help" in body
    assert "# refresh this backend brief" in body
    assert "# primary app shell inside the backend container" in body
    assert "ssh web@203.0.113.10" in body
    assert "cnc-admin shell web" not in body
    assert "cnc-admin logs app web --lines 200" not in body
    assert "cnc-admin apply" not in body
    assert (
        "DO NOT treat CNC metadata, apply, or fix as the source of truth for app env, startup, or process roles inside the sandbox."
        in body
    )
    assert (
        "IF backend-local health passes and public traffic still fails, classify the incident as CNC/host-side until proven otherwise."
        in body
    )
    assert "## App Onboarding Inside The Guest" in body
    assert (
        "Create the app's own systemd unit under `/etc/systemd/system/<app>.service` inside the guest."
        in body
    )
    assert (
        "Do not create CNC-specific launcher hooks, wrapper scripts, or CNC-owned service metadata for app startup."
        in body
    )
    assert "# sandbox-local health with CNC's configured contract" in body
    assert (
        "python3 -c \"import socket; socket.create_connection(('127.0.0.1', 8337), 2).close()\""
        in body
    )
    assert "# host-side CNC diagnosis for the published sandbox" in body
    assert "# public route check when the backend has an enabled domain" in body
    assert "forced-command path into the backend sandbox" in body
    assert "Any `sudo` only affects the sandbox." in body
    assert (
        "`sudo` may be available for compatibility with VPS-style app runbooks" in body
    )
    assert (
        "Escalate to host-level control only when the next required action is not possible through the container path."
        in body
    )
    assert (
        "If the next action needs host/root access, stop backend-shell work and ask the user to run the root escalation command; include the exact command, why it is needed, and what output to return."
        in body
    )
    assert (
        "Before interacting at the host level, run `ssh root@203.0.113.10 llm-help` to refresh host context and supported CNC actions."
        in body
    )
    assert "ssh root@203.0.113.10 llm-help" in body
    assert "ssh root@203.0.113.10 'llm-help web'" in body
    assert "ssh root@203.0.113.10 'cnc-admin app doctor web'" in body
    assert (
        "Stop backend-shell recovery if the app answers on `127.0.0.1:8337` but public routing still fails"
        in body
    )
    assert (
        "switch to the host-level commands above rather than trying to smuggle host operations through the backend shell."
        in body
    )
    assert (
        "Do not use podman exec, podman restart, or podman rm directly unless CNC guidance specifically requires it."
        in body
    )
    assert "scp or sftp" in body
    assert "<backend>" not in body


async def test_output_llm_help_uses_advertised_host_for_backend_access(
    monkeypatch,
) -> None:
    backend = Backend(
        id=7,
        name="web",
        kind="app",
        enabled=True,
        port=12001,
        internal_port=8337,
        workdir="/srv/web",
        base_image="docker.io/library/ubuntu:24.04",
        install_command="apt-get update",
        update_command="git pull --ff-only && .venv/bin/pip install -r requirements.txt",
        start_command="python3 app.py",
        healthcheck_mode="tcp",
        healthcheck_path=None,
        env_json='{"PORT":"8337"}',
        volumes_json='["/srv/web-data:/data"]',
    )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        assert backend_id == 7
        return {
            "selected_backend": backend,
            "selected_output_detail": {
                "status_value": "healthy",
                "service_state": "active / running",
                "target": "http://127.0.0.1:12001",
                "exposure_value": "127.0.0.1:12001:8337/tcp",
            },
        }

    monkeypatch.setattr(ui_pages, "_output_page_context", fake_output_page_context)

    response = await ui_pages.output_llm_help(
        7,
        _request(
            path="/outputs/7/llm.txt",
            headers=[(b"host", b"admin.example.com")],
        ),
        settings=Settings(ssh_advertise_host="203.0.113.10"),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "ssh web@203.0.113.10 llm-help" in body
    assert "ssh web@203.0.113.10" in body
    assert (
        "Backend SSH is ordinary OpenSSH restricted to tailnet source addresses, not the Tailscale-managed SSH feature. Public-IP SSH is for host/root recovery, not backend users."
        in body
    )
    assert "ssh web@100.64.0.10" not in body
    assert "ssh web@cnc-admin.example.ts.net" not in body


async def test_output_llm_help_prefers_tailnet_host_for_backend_access(
    monkeypatch,
) -> None:
    backend = Backend(
        id=7,
        name="web",
        kind="app",
        enabled=True,
        port=12001,
        internal_port=8337,
        workdir="/srv/web",
        base_image="docker.io/library/ubuntu:24.04",
        install_command="apt-get update",
        update_command="git pull --ff-only && .venv/bin/pip install -r requirements.txt",
        start_command="python3 app.py",
        healthcheck_mode="tcp",
        healthcheck_path=None,
        env_json='{"PORT":"8337"}',
        volumes_json='["/srv/web-data:/data"]',
    )

    async def fake_output_page_context(_session, _settings, backend_id: int):
        assert backend_id == 7
        return {
            "selected_backend": backend,
            "selected_output_detail": {
                "status_value": "healthy",
                "service_state": "active / running",
                "target": "http://127.0.0.1:12001",
                "exposure_value": "127.0.0.1:12001:8337/tcp",
            },
        }

    monkeypatch.setattr(ui_pages, "_output_page_context", fake_output_page_context)

    response = await ui_pages.output_llm_help(
        7,
        _request(path="/outputs/7/llm.txt", headers=[(b"host", b"203.0.113.10")]),
        settings=Settings(
            admin_allowed_hosts="admin.example.ts.net",
            ssh_advertise_host="203.0.113.10",
        ),
        session=object(),
    )

    body = response.body.decode()
    assert response.status_code == 200
    assert "ssh web@admin.example.ts.net llm-help" in body
    assert "ssh web@admin.example.ts.net" in body
    assert "ssh root@203.0.113.10 llm-help" in body
    assert "ssh web@203.0.113.10" not in body
