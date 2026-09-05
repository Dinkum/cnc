import pytest

from app.config import Settings
from app.services import tailscale_admin
from app.services.commands import CommandResult


def test_parse_status_mappings_extracts_url_path_and_target() -> None:
    payload = """
Available within your tailnet:

https://cnc-node.tailnet.ts.net
|-- / proxy http://127.0.0.1:9090
|-- /app1 proxy http://127.0.0.1:12000
""".strip()

    mappings = tailscale_admin._parse_status_mappings(payload, mode="serve")

    assert len(mappings) == 2
    assert mappings[0].port == 443
    assert mappings[0].path == "/"
    assert mappings[0].target == "http://127.0.0.1:9090"
    assert mappings[1].path == "/app1"


def test_verify_tailscale_admin_exposure_allows_private_root_admin_mapping(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"],
            0,
            "https://cnc-node.tailnet.ts.net\n|-- / proxy http://127.0.0.1:9090\n|-- /app1 proxy http://127.0.0.1:12000\n",
            "",
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"], 0, "", ""
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )

    payload = tailscale_admin.verify_tailscale_admin_exposure(settings)

    assert payload["checked"] is True
    assert payload["serve_mappings"][0]["target"] == "http://127.0.0.1:9090"
    assert payload["funnel_mappings"] == []


def test_verify_tailscale_admin_exposure_allows_follower_admin_proxy_mapping(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"],
            0,
            "https://cnc-node.tailnet.ts.net\n|-- / proxy http://127.0.0.1:9091\n",
            "",
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"], 0, "", ""
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )

    payload = tailscale_admin.verify_tailscale_admin_exposure(settings)

    assert payload["checked"] is True
    assert payload["serve_mappings"][0]["target"] == "http://127.0.0.1:9091"


def test_verify_tailscale_admin_exposure_rejects_public_https_funnel(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"], 0, "", ""
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"],
            0,
            "https://cnc-node.tailnet.ts.net\n|-- / proxy http://127.0.0.1:9090\n",
            "",
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )

    with pytest.raises(
        tailscale_admin.TailscaleAdminExposureError,
        match="public route to a CNC admin target",
    ):
        tailscale_admin.verify_tailscale_admin_exposure(settings)


def test_verify_tailscale_admin_exposure_ignores_tailnet_only_funnel_status(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"],
            0,
            "https://cnc-node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:9090\n",
            "",
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"],
            0,
            "https://cnc-node.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:9090\n",
            "",
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )

    payload = tailscale_admin.verify_tailscale_admin_exposure(settings)

    assert payload["checked"] is True
    assert payload["funnel_mappings"] == []
    assert payload["funnel_tailnet_only"] is True


def test_verify_tailscale_admin_exposure_rejects_unapproved_private_admin_mapping(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"],
            0,
            "https://cnc-node.tailnet.ts.net\n|-- /admin proxy http://127.0.0.1:9090\n",
            "",
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"], 0, "", ""
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )

    with pytest.raises(
        tailscale_admin.TailscaleAdminExposureError,
        match="unapproved mapping /admin",
    ):
        tailscale_admin.verify_tailscale_admin_exposure(settings)


def test_verify_tailscale_admin_exposure_skips_when_tailnet_not_connected(
    monkeypatch,
) -> None:
    settings = Settings()

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 1, "", "not connected"),
    )

    payload = tailscale_admin.verify_tailscale_admin_exposure(settings)

    assert payload == {"checked": False, "reason": "tailscale_not_connected"}


def test_verify_tailscale_admin_exposure_retries_transient_status_failure(
    monkeypatch,
) -> None:
    settings = Settings(
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
    )
    serve_attempts = 0

    def fake_run(command, timeout_sec=30):
        nonlocal serve_attempts
        if command == ["tailscale", "ip", "-4"]:
            return CommandResult(command, 0, "100.64.0.1", "")
        if command == ["tailscale", "serve", "status"]:
            serve_attempts += 1
            if serve_attempts == 1:
                return CommandResult(command, 124, "", "timed out after 5s")
            return CommandResult(
                command,
                0,
                "https://cnc-node.tailnet.ts.net\n|-- / proxy http://127.0.0.1:9090\n",
                "",
            )
        if command == ["tailscale", "funnel", "status"]:
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(tailscale_admin, "run_command", fake_run)

    payload = tailscale_admin.verify_tailscale_admin_exposure(settings)

    assert payload["checked"] is True
    assert serve_attempts == 2


def test_current_tailscale_admin_hostnames_returns_admin_serve_hostname(
    monkeypatch,
) -> None:
    settings = Settings()
    responses = {
        ("tailscale", "ip", "-4"): CommandResult(
            ["tailscale", "ip", "-4"], 0, "100.64.0.1", ""
        ),
        ("tailscale", "serve", "status"): CommandResult(
            ["tailscale", "serve", "status"],
            0,
            "https://cnc-admin.example.ts.net\n|-- / proxy http://127.0.0.1:9090\n",
            "",
        ),
        ("tailscale", "funnel", "status"): CommandResult(
            ["tailscale", "funnel", "status"], 0, "", ""
        ),
    }

    monkeypatch.setattr(
        tailscale_admin,
        "run_command",
        lambda command, timeout_sec=30: responses[tuple(command)],
    )
    monkeypatch.setattr(tailscale_admin, "_admin_hostname_cache", (0.0, ()))

    hostnames = tailscale_admin.current_tailscale_admin_hostnames(settings)

    assert hostnames == ("cnc-admin.example.ts.net",)
