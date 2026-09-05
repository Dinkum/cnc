from pathlib import Path

import pytest

from app.services import cloudflare_ingress
from app.services.commands import CommandResult


def test_rewrite_cloudflare_http_firewall_files_replaces_http_rules_preserving_other_access(
    tmp_path: Path,
) -> None:
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n\n"
        "### tuple ### allow tcp 80 0.0.0.0/0 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp --dport 80 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 443 ::/0 any ::/0 in\n"
        "-A ufw6-user-input -p tcp --dport 443 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )

    payload = cloudflare_ingress.rewrite_cloudflare_http_firewall_files(
        cloudflare_ingress.build_cloudflare_ingress_policy(
            ("173.245.48.0/20", "2400:cb00::/32")
        ),
        user_rules_path=user_rules,
        user6_rules_path=user6_rules,
    )

    assert payload["ipv4_rule_count"] == 2
    assert payload["ipv6_rule_count"] == 2
    updated_v4 = user_rules.read_text(encoding="utf-8")
    updated_v6 = user6_rules.read_text(encoding="utf-8")
    assert "-A ufw-user-input -p tcp --dport 22 -j ACCEPT" in updated_v4
    assert "-A ufw-user-input -p tcp --dport 80 -j ACCEPT" not in updated_v4
    assert "### tuple ### allow tcp 80 0.0.0.0/0 any 173.245.48.0/20 in" in updated_v4
    assert (
        "-A ufw-user-input -p tcp --dport 80 -s 173.245.48.0/20 -j ACCEPT" in updated_v4
    )
    assert "-A ufw6-user-input -p tcp --dport 443 -j ACCEPT" not in updated_v6
    assert "### tuple ### allow tcp 443 ::/0 any 2400:cb00::/32 in" in updated_v6
    assert (
        "-A ufw6-user-input -p tcp --dport 443 -s 2400:cb00::/32 -j ACCEPT"
        in updated_v6
    )


def test_rewrite_cloudflare_http_firewall_files_writes_atomically(
    monkeypatch, tmp_path: Path
) -> None:
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    writes: list[Path] = []
    original_write = cloudflare_ingress.write_text_atomic

    def wrapped(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        writes.append(path)
        original_write(path, content, encoding=encoding)

    monkeypatch.setattr(cloudflare_ingress, "write_text_atomic", wrapped)

    cloudflare_ingress.rewrite_cloudflare_http_firewall_files(
        cloudflare_ingress.build_cloudflare_ingress_policy(
            ("173.245.48.0/20", "2400:cb00::/32")
        ),
        user_rules_path=user_rules,
        user6_rules_path=user6_rules,
    )

    assert writes == [user_rules, user6_rules]


def test_render_cloudflare_nginx_acl_uses_policy_allowlist() -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        ("173.245.48.0/20", "2400:cb00::/32")
    )

    lines = cloudflare_ingress.render_cloudflare_nginx_acl(policy)

    assert lines == [
        "    allow 127.0.0.1;",
        "    allow ::1;",
        "    allow 173.245.48.0/20;",
        "    allow 2400:cb00::/32;",
        "    deny all;",
        "",
    ]


def test_audit_rendered_cloudflare_nginx_acl_requires_expected_allowlist() -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        ("173.245.48.0/20", "2400:cb00::/32")
    )
    content = "\n".join(
        [
            "server {",
            "    listen 80;",
            "    allow 127.0.0.1;",
            "    allow ::1;",
            "    allow 173.245.48.0/20;",
            "    allow 2400:cb00::/32;",
            "    deny all;",
            "}",
            "",
        ]
    )

    payload = cloudflare_ingress.audit_rendered_cloudflare_nginx_acl(policy, content)

    assert payload["checked"] is True
    assert payload["allow_count"] == 4
    assert payload["deny_all_count"] == 1


def test_audit_rendered_cloudflare_nginx_acl_reports_mismatch_details() -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        ("173.245.48.0/20", "100.64.0.19")
    )
    content = "\n".join(
        [
            "server {",
            "    listen 80;",
            "    allow 127.0.0.1;",
            "    allow ::1;",
            "    allow 173.245.48.0/20;",
            "    allow 198.51.100.4;",
            "    deny all;",
            "}",
            "",
        ]
    )

    with pytest.raises(RuntimeError) as exc_info:
        cloudflare_ingress.audit_rendered_cloudflare_nginx_acl(policy, content)

    error = str(exc_info.value)
    assert "expected_count=4 rendered_count=4" in error
    assert "missing=100.64.0.19/32" in error
    assert "extra=198.51.100.4" in error


def test_audit_live_cloudflare_http_firewall_reports_expected_rule_shape(
    monkeypatch,
) -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(
        ("173.245.48.0/20", "2400:cb00::/32")
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
    }

    monkeypatch.setattr(cloudflare_ingress, "_ufw_ipv6_enabled", lambda **_kwargs: True)

    payload = cloudflare_ingress.audit_live_cloudflare_http_firewall(
        policy,
        run_command_func=lambda command, timeout_sec=30: responses[tuple(command)],
        timeout_sec=5,
    )

    assert payload["checked"] is True
    assert payload["cidr_count"] == 2
    assert payload["expected_rule_count"] == 4
    assert payload["live_rule_count"] == 4


def test_audit_live_cloudflare_http_firewall_rejects_zero_cidr_accept(
    monkeypatch,
) -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(("173.245.48.0/20",))
    responses = {
        ("ufw", "status"): CommandResult(["ufw", "status"], 0, "Status: active\n", ""),
        ("iptables", "-S", "ufw-user-input"): CommandResult(
            ["iptables", "-S", "ufw-user-input"],
            0,
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n"
            "-A ufw-user-input -p tcp -s 0.0.0.0/0 --dport 443 -j ACCEPT\n",
            "",
        ),
        ("ip6tables", "-S", "ufw-user-input"): CommandResult(
            ["ip6tables", "-S", "ufw-user-input"],
            0,
            "",
            "",
        ),
    }

    monkeypatch.setattr(cloudflare_ingress, "_ufw_ipv6_enabled", lambda **_kwargs: True)

    with pytest.raises(RuntimeError, match="broad accept"):
        cloudflare_ingress.audit_live_cloudflare_http_firewall(
            policy,
            run_command_func=lambda command, timeout_sec=30: responses[tuple(command)],
            timeout_sec=5,
        )


def test_audit_rendered_cloudflare_http_firewall_requires_tuple_source_shape(
    tmp_path: Path,
) -> None:
    policy = cloudflare_ingress.build_cloudflare_ingress_policy(("173.245.48.0/20",))
    user_rules = tmp_path / "user.rules"
    user6_rules = tmp_path / "user6.rules"
    user_rules.write_text(
        "*filter\n"
        ":ufw-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### tuple ### allow tcp 80 173.245.48.0/20 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 80 -j ACCEPT\n\n"
        "### tuple ### allow tcp 443 173.245.48.0/20 any 0.0.0.0/0 in\n"
        "-A ufw-user-input -p tcp -s 173.245.48.0/20 --dport 443 -j ACCEPT\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )
    user6_rules.write_text(
        "*filter\n"
        ":ufw6-user-input - [0:0]\n"
        "### RULES ###\n\n"
        "### END RULES ###\nCOMMIT\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing UFW tuple"):
        cloudflare_ingress.audit_rendered_cloudflare_http_firewall(
            policy,
            user_rules_path=user_rules,
            user6_rules_path=user6_rules,
        )
