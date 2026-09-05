import errno

import pytest

from app.config import Settings
from app.services import self_audit
from app.services.app_network_isolation import APP_ISOLATION_CHAIN
from app.services.commands import CommandResult


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(app_quadlet_dir=tmp_path / "quadlets", **overrides)


def _self_audit_command_runner(responses: dict[tuple[str, ...], CommandResult]):
    def run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        key = tuple(command)
        if key in responses:
            return responses[key]
        if key == ("iptables", "-S", APP_ISOLATION_CHAIN):
            return CommandResult(command, 0, f"-N {APP_ISOLATION_CHAIN}\n", "")
        if key == ("iptables", "-S", "FORWARD"):
            return CommandResult(
                command, 0, f"-A FORWARD -j {APP_ISOLATION_CHAIN}\n", ""
            )
        return CommandResult(command, 1, "", "unexpected command")

    return run


def test_run_control_plane_self_audit_succeeds_with_expected_runtime(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    (nginx_dir / "cnc-host-web.conf").write_text(
        "server 127.0.0.1:12000;", encoding="utf-8"
    )
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"], 0, "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n", ""
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    payload = self_audit.run_control_plane_self_audit(settings)

    assert payload["admin_loopback_bind"]["listeners"] == ["127.0.0.1:9090"]
    assert payload["firewall_restrictions"]["cidr_count"] == 2
    assert payload["sshd_global_policy"]["permitrootlogin"] == "prohibit-password"


def test_run_control_plane_self_audit_accepts_without_password_root_login_alias(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"], 0, "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n", ""
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin without-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    payload = self_audit.run_control_plane_self_audit(settings)

    assert payload["sshd_global_policy"]["permitrootlogin"] == "without-password"


def test_run_control_plane_self_audit_startup_mode_uses_effective_admin_bind(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        admin_host="127.0.0.1",
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )

    responses = {
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 2400:cb00::/32 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    payload = self_audit.run_control_plane_self_audit(settings, startup_mode=True)

    assert payload["admin_loopback_bind"]["configured_host"] == "127.0.0.1"
    assert payload["admin_loopback_bind"]["effective_host"] == "127.0.0.1"
    assert payload["admin_loopback_bind"]["mode"] == "startup_config"


def test_run_control_plane_self_audit_startup_mode_rejects_remote_admin_bind(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        admin_host="0.0.0.0",
        admin_unsafe_allow_remote=True,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )

    responses = {
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"], 0, "", ""
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    with pytest.raises(self_audit.ControlPlaneSelfAuditError) as exc_info:
        self_audit.run_control_plane_self_audit(settings, startup_mode=True)

    assert exc_info.value.findings[0]["check"] == "admin_loopback_bind"
    assert "not loopback-bound" in str(exc_info.value.findings[0]["message"])


def test_run_control_plane_self_audit_blocks_when_quadlet_dir_is_read_only(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"],
            0,
            "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n",
            "",
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            0,
            "",
            "",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    def fail_write_path(_settings):
        raise self_audit.AppQuadletWriteError(
            "app Quadlet directory is not writable",
            details={
                "quadlet_dir": str(settings.app_quadlet_dir),
                "error": "Read-only file system",
                "errno": errno.EROFS,
                "operator_hint": "restart cnc-admin",
            },
        )

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(self_audit, "verify_app_quadlet_dir_writable", fail_write_path)
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    with pytest.raises(self_audit.ControlPlaneSelfAuditError) as exc_info:
        self_audit.run_control_plane_self_audit(settings)

    details = exc_info.value.as_details()
    assert details["blocks_apply"] is True
    assert details["blocking_count"] == 1
    finding = exc_info.value.findings[0]
    assert finding["check"] == "admin_runtime_write_paths"
    assert "cannot write app Quadlet directory" in str(finding["message"])
    assert finding["details"]["errno"] == errno.EROFS


def test_run_control_plane_self_audit_reports_multiple_findings(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    (nginx_dir / "cnc-host-admin.conf").write_text(
        "proxy_pass http://127.0.0.1:9090;", encoding="utf-8"
    )
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"], 0, "LISTEN 0 4096 0.0.0.0:9090 0.0.0.0:*\n", ""
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"], 0, "", ""
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication yes",
                    "kbdinteractiveauthentication yes",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin yes",
                    "maxauthtries 6",
                    "logingracetime 120",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: (_ for _ in ()).throw(
            RuntimeError("tailscale funnel exposes HTTPS port 443")
        ),
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    with pytest.raises(self_audit.ControlPlaneSelfAuditError) as exc_info:
        self_audit.run_control_plane_self_audit(settings)

    messages = [str(item["message"]) for item in exc_info.value.findings]
    severities = [str(item["severity"]) for item in exc_info.value.findings]
    assert any("not loopback-bound" in message for message in messages)
    assert any("references admin port 9090" in message for message in messages)
    assert any(
        "tailscale funnel exposes HTTPS port 443" in message for message in messages
    )
    assert any("broad accept rule" in message for message in messages)
    assert any("sshd global policy is weaker" in message for message in messages)
    assert "blocking" in severities
    assert "warning" in severities
    assert exc_info.value.as_details()["blocking_count"] == 3
    assert exc_info.value.as_details()["warning_count"] == 2


def test_run_control_plane_self_audit_tolerates_missing_ipv6_chain_when_ufw_ipv6_disabled(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"], 0, "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n", ""
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            1,
            "",
            "ip6tables: No chain/target/match by that name.\n",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit.cloudflare_ingress, "_ufw_ipv6_enabled", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    payload = self_audit.run_control_plane_self_audit(settings)

    assert payload["firewall_restrictions"]["checked"] is True
    assert payload["firewall_restrictions"]["ipv6_enabled"] is False


def test_run_control_plane_self_audit_tolerates_missing_ipv6_chain_when_ufw_reports_no_chain(
    monkeypatch, tmp_path
) -> None:
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir()
    settings = _settings(
        tmp_path,
        nginx_generated_dir=nginx_dir,
        nginx_cloudflare_only=True,
        nginx_cloudflare_ips="173.245.48.0/20,2400:cb00::/32",
    )

    responses = {
        ("ss", "-ltnH"): CommandResult(
            ["ss", "-ltnH"], 0, "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n", ""
        ),
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            1,
            "",
            "ip6tables: No chain/target/match by that name.\n",
        ),
        ("sshd", "-T"): CommandResult(
            ["sshd", "-T"],
            0,
            "\n".join(
                [
                    "passwordauthentication no",
                    "kbdinteractiveauthentication no",
                    "permitemptypasswords no",
                    "pubkeyauthentication yes",
                    "permitrootlogin prohibit-password",
                    "maxauthtries 3",
                    "logingracetime 20",
                ]
            ),
            "",
        ),
    }

    monkeypatch.setattr(
        self_audit,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        self_audit.cloudflare_ingress, "_ufw_ipv6_enabled", lambda **_kwargs: True
    )
    monkeypatch.setattr(
        self_audit, "run_command", _self_audit_command_runner(responses)
    )

    payload = self_audit.run_control_plane_self_audit(settings)

    assert payload["firewall_restrictions"]["checked"] is True
    assert payload["firewall_restrictions"]["ipv6_enabled"] is True
    assert payload["firewall_restrictions"]["ipv6_chain_present"] is False
