from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import ipaddress
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Iterable

from app.services.commands import run_command
from app.services.host_state import write_text_atomic


HTTP_PORTS = (80, 443)
UFW_STATUS_ACTIVE_PREFIX = "Status: active"
UFW_USER_RULES_PATH = Path("/etc/ufw/user.rules")
UFW_USER6_RULES_PATH = Path("/etc/ufw/user6.rules")
UFW_DEFAULTS_PATH = Path("/etc/default/ufw")
NGINX_LOOPBACK_ALLOWLIST = ("127.0.0.1", "::1")
NGINX_ALLOW_DIRECTIVE_RE = re.compile(r"^\s*allow\s+(?P<value>[^;]+);", re.MULTILINE)
NGINX_DENY_ALL_RE = re.compile(r"^\s*deny\s+all;", re.MULTILINE)


@dataclass(frozen=True)
class CloudflareIngressPolicy:
    cidrs: tuple[str, ...]
    ports: tuple[int, ...] = HTTP_PORTS

    @property
    def ipv4_cidrs(self) -> tuple[str, ...]:
        return tuple(cidr for cidr in self.cidrs if ":" not in cidr)

    @property
    def ipv6_cidrs(self) -> tuple[str, ...]:
        return tuple(cidr for cidr in self.cidrs if ":" in cidr)

    @property
    def expected_rule_count(self) -> int:
        return len(self.cidrs) * len(self.ports)

    @property
    def nginx_allowlist(self) -> tuple[str, ...]:
        return (*NGINX_LOOPBACK_ALLOWLIST, *self.cidrs)


def build_cloudflare_ingress_policy(cidrs: Iterable[str]) -> CloudflareIngressPolicy:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in cidrs:
        candidate = str(raw_value or "").strip()
        if not candidate:
            continue
        network = str(ipaddress.ip_network(candidate, strict=False))
        if network in seen:
            continue
        seen.add(network)
        normalized.append(network)
    if not normalized:
        raise ValueError("Cloudflare-only HTTP ingress requires at least one CIDR")
    return CloudflareIngressPolicy(cidrs=tuple(normalized))


def _split_ufw_rules_section(text: str) -> tuple[str, str, str]:
    before, separator, remainder = text.partition("### RULES ###\n")
    if not separator:
        raise RuntimeError("ufw rules file is missing the RULES section header")
    rules_body, separator_end, after = remainder.partition("### END RULES ###\n")
    if not separator_end:
        raise RuntimeError("ufw rules file is missing the RULES section footer")
    return before, rules_body, after


def _group_ufw_rule_lines(rules_body: str) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for raw_line in rules_body.splitlines():
        if not raw_line.strip():
            if current:
                groups.append(current)
                current = []
            continue
        current.append(raw_line)
    if current:
        groups.append(current)
    return groups


def _ufw_group_targets_http(group: list[str], *, ports: tuple[int, ...]) -> bool:
    discovered_ports: set[int] = set()
    for line in group:
        tuple_match = re.search(r"### tuple ### .* tcp (?P<port>\d+) ", line)
        if tuple_match is not None:
            discovered_ports.add(int(tuple_match.group("port")))
        dport_match = re.search(r"--dport (?P<port>\d+)", line)
        if dport_match is not None:
            discovered_ports.add(int(dport_match.group("port")))
    return bool(discovered_ports) and discovered_ports.issubset(set(ports))


def _render_cloudflare_rule_group(
    *, chain_prefix: str, any_net: str, cidr: str, port: int
) -> list[str]:
    return [
        f"### tuple ### allow tcp {port} {any_net} any {cidr} in",
        f"-A {chain_prefix}-user-input -p tcp --dport {port} -s {cidr} -j ACCEPT",
    ]


def _render_rewritten_ufw_http_rules(
    original: str,
    *,
    chain_prefix: str,
    any_net: str,
    policy: CloudflareIngressPolicy,
    cidrs: tuple[str, ...],
) -> str:
    before, rules_body, after = _split_ufw_rules_section(original)
    kept_groups = [
        group
        for group in _group_ufw_rule_lines(rules_body)
        if not _ufw_group_targets_http(group, ports=policy.ports)
    ]
    rendered_groups = list(kept_groups)
    for cidr in cidrs:
        for port in policy.ports:
            rendered_groups.append(
                _render_cloudflare_rule_group(
                    chain_prefix=chain_prefix, any_net=any_net, cidr=cidr, port=port
                )
            )
    rendered_lines = ["### RULES ###", ""]
    for group in rendered_groups:
        rendered_lines.extend(group)
        rendered_lines.append("")
    rendered_lines.append("### END RULES ###")
    return before + "\n".join(rendered_lines) + "\n" + after


def restore_cloudflare_http_firewall_files(
    *,
    user_rules_text: str,
    user6_rules_text: str,
    user_rules_path: Path | None = None,
    user6_rules_path: Path | None = None,
) -> None:
    resolved_user_rules_path = user_rules_path or UFW_USER_RULES_PATH
    resolved_user6_rules_path = user6_rules_path or UFW_USER6_RULES_PATH
    write_text_atomic(resolved_user_rules_path, user_rules_text, encoding="utf-8")
    write_text_atomic(resolved_user6_rules_path, user6_rules_text, encoding="utf-8")


def _is_read_only_filesystem_error(exc: Exception) -> bool:
    return isinstance(exc, OSError) and exc.errno == errno.EROFS


def rewrite_cloudflare_http_firewall_files(
    policy: CloudflareIngressPolicy,
    *,
    user_rules_path: Path | None = None,
    user6_rules_path: Path | None = None,
) -> dict[str, int | str]:
    resolved_user_rules_path = user_rules_path or UFW_USER_RULES_PATH
    resolved_user6_rules_path = user6_rules_path or UFW_USER6_RULES_PATH
    original_v4 = resolved_user_rules_path.read_text(encoding="utf-8")
    original_v6 = resolved_user6_rules_path.read_text(encoding="utf-8")
    rendered_v4 = _render_rewritten_ufw_http_rules(
        original_v4,
        chain_prefix="ufw",
        any_net="0.0.0.0/0",
        policy=policy,
        cidrs=policy.ipv4_cidrs,
    )
    rendered_v6 = _render_rewritten_ufw_http_rules(
        original_v6,
        chain_prefix="ufw6",
        any_net="::/0",
        policy=policy,
        cidrs=policy.ipv6_cidrs,
    )
    ipv4_changed = rendered_v4 != original_v4
    ipv6_changed = rendered_v6 != original_v6
    attempted_write = False
    completed_write = False
    try:
        if ipv4_changed:
            attempted_write = True
            write_text_atomic(resolved_user_rules_path, rendered_v4, encoding="utf-8")
            completed_write = True
        if ipv6_changed:
            attempted_write = True
            write_text_atomic(resolved_user6_rules_path, rendered_v6, encoding="utf-8")
            completed_write = True
    except Exception as exc:
        if completed_write or (
            attempted_write and not _is_read_only_filesystem_error(exc)
        ):
            restore_cloudflare_http_firewall_files(
                user_rules_text=original_v4,
                user6_rules_text=original_v6,
                user_rules_path=resolved_user_rules_path,
                user6_rules_path=resolved_user6_rules_path,
            )
        raise
    return {
        "mode": "file_rewrite",
        "changed": ipv4_changed or ipv6_changed,
        "ipv4_changed": ipv4_changed,
        "ipv6_changed": ipv6_changed,
        "ipv4_rule_count": len(policy.ipv4_cidrs) * len(policy.ports),
        "ipv6_rule_count": len(policy.ipv6_cidrs) * len(policy.ports),
    }


def render_cloudflare_nginx_acl(
    policy: CloudflareIngressPolicy, *, indent: str = "    "
) -> list[str]:
    lines = [f"{indent}allow {value};" for value in policy.nginx_allowlist]
    lines.append(f"{indent}deny all;")
    lines.append("")
    return lines


def _summarize_acl_values(values: tuple[str, ...], *, limit: int = 5) -> str:
    if not values:
        return "-"
    rendered = ", ".join(values[:limit])
    if len(values) > limit:
        rendered = f"{rendered}, +{len(values) - limit} more"
    return rendered


def audit_rendered_cloudflare_nginx_acl(
    policy: CloudflareIngressPolicy, content: str
) -> dict[str, object]:
    allow_values = [
        match.group("value").strip()
        for match in NGINX_ALLOW_DIRECTIVE_RE.finditer(content)
    ]
    if tuple(allow_values) != policy.nginx_allowlist:
        rendered_allowlist = tuple(allow_values)
        expected_set = set(policy.nginx_allowlist)
        rendered_set = set(rendered_allowlist)
        missing = tuple(
            value for value in policy.nginx_allowlist if value not in rendered_set
        )
        extra = tuple(
            value for value in rendered_allowlist if value not in expected_set
        )
        raise RuntimeError(
            "rendered nginx Cloudflare ACL does not match the expected allowlist "
            f"(expected_count={len(policy.nginx_allowlist)} "
            f"rendered_count={len(rendered_allowlist)} "
            f"missing={_summarize_acl_values(missing)} "
            f"extra={_summarize_acl_values(extra)})"
        )
    if len(NGINX_DENY_ALL_RE.findall(content)) != 1:
        raise RuntimeError(
            "rendered nginx Cloudflare ACL must contain exactly one deny-all guard"
        )
    return {
        "checked": True,
        "allow_count": len(allow_values),
        "deny_all_count": 1,
    }


def audit_rendered_cloudflare_http_firewall(
    policy: CloudflareIngressPolicy,
    *,
    user_rules_path: Path | None = None,
    user6_rules_path: Path | None = None,
) -> dict[str, object]:
    resolved_user_rules_path = user_rules_path or UFW_USER_RULES_PATH
    resolved_user6_rules_path = user6_rules_path or UFW_USER6_RULES_PATH
    rules_v4 = resolved_user_rules_path.read_text(encoding="utf-8")
    rules_v6 = resolved_user6_rules_path.read_text(encoding="utf-8")

    for port in policy.ports:
        if _iptables_has_broad_accept_for_port(
            rules_v4, port=port
        ) or _iptables_has_broad_accept_for_port(rules_v6, port=port):
            raise RuntimeError(
                f"rendered firewall exposes {port}/tcp with a broad accept rule"
            )

    for cidr in policy.cidrs:
        rules = rules_v6 if ":" in cidr else rules_v4
        any_net = "::/0" if ":" in cidr else "0.0.0.0/0"
        for port in policy.ports:
            if not _ufw_tuple_accepts_port_for_source(
                rules, port=port, source=cidr, any_net=any_net
            ):
                raise RuntimeError(
                    f"rendered firewall missing UFW tuple for Cloudflare source rule "
                    f"{cidr} on port {port}"
                )
            if not _iptables_accepts_port_for_source(rules, port=port, source=cidr):
                raise RuntimeError(
                    f"rendered firewall missing Cloudflare source rule for {cidr} on port {port}"
                )

    return {
        "checked": True,
        "cidr_count": len(policy.cidrs),
        "ports": list(policy.ports),
        "expected_rule_count": policy.expected_rule_count,
        "rendered_rule_count": policy.expected_rule_count,
        "ipv4_rule_count": len(policy.ipv4_cidrs) * len(policy.ports),
        "ipv6_rule_count": len(policy.ipv6_cidrs) * len(policy.ports),
    }


def _iptables_accepts_port_for_source(rules: str, *, port: int, source: str) -> bool:
    port_token = f"--dport {port}"
    source_token = f"-s {source}"
    return any(
        port_token in line and source_token in line and "-j ACCEPT" in line
        for line in rules.splitlines()
    )


def _ufw_tuple_accepts_port_for_source(
    rules: str, *, port: int, source: str, any_net: str
) -> bool:
    expected = f"### tuple ### allow tcp {port} {any_net} any {source} in"
    return any(line.strip() == expected for line in rules.splitlines())


def _iptables_accept_source_is_broad(source: str | None) -> bool:
    if source is None:
        return True
    try:
        network = ipaddress.ip_network(source, strict=False)
    except ValueError:
        return False
    return network.prefixlen == 0


def _iptables_accept_source(line: str) -> str | None:
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for index, token in enumerate(tokens):
        if token == "-s" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _iptables_has_broad_accept_for_port(rules: str, *, port: int) -> bool:
    port_token = f"--dport {port}"
    return any(
        port_token in line
        and "-j ACCEPT" in line
        and _iptables_accept_source_is_broad(_iptables_accept_source(line))
        for line in rules.splitlines()
    )


def _iptables_chain_missing(output: str) -> bool:
    normalized = output.strip().lower()
    return "no chain/target/match by that name" in normalized


def _ufw_ipv6_enabled(*, defaults_path: Path = UFW_DEFAULTS_PATH) -> bool:
    try:
        content = defaults_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip().upper() != "IPV6":
            continue
        normalized = value.strip().strip('"').strip("'").lower()
        return normalized in {"yes", "true", "1"}
    return True


def audit_live_cloudflare_http_firewall(
    policy: CloudflareIngressPolicy,
    *,
    run_command_func=run_command,
    timeout_sec: int,
    ufw_defaults_path: Path | None = None,
) -> dict[str, object]:
    ufw_result = run_command_func(["ufw", "status"], timeout_sec=timeout_sec)
    if not ufw_result.ok or UFW_STATUS_ACTIVE_PREFIX not in ufw_result.stdout:
        raise RuntimeError("ufw is not active while nginx_cloudflare_only is enabled")

    iptables_v4 = run_command_func(
        ["iptables", "-S", "ufw-user-input"], timeout_sec=timeout_sec
    )
    iptables_v6 = run_command_func(
        ["ip6tables", "-S", "ufw-user-input"], timeout_sec=timeout_sec
    )
    ipv6_enabled = _ufw_ipv6_enabled(
        defaults_path=ufw_defaults_path or UFW_DEFAULTS_PATH
    )
    ipv6_chain_missing = (not iptables_v6.ok) and _iptables_chain_missing(
        f"{iptables_v6.stderr or ''}\n{iptables_v6.stdout or ''}"
    )
    if not iptables_v4.ok:
        raise RuntimeError(
            iptables_v4.stderr
            or iptables_v4.stdout
            or "iptables -S ufw-user-input failed"
        )
    if not iptables_v6.ok and ipv6_enabled and not ipv6_chain_missing:
        raise RuntimeError(
            iptables_v6.stderr
            or iptables_v6.stdout
            or "ip6tables -S ufw-user-input failed"
        )
    rules_v6 = iptables_v6.stdout if iptables_v6.ok else ""

    for port in policy.ports:
        if _iptables_has_broad_accept_for_port(
            iptables_v4.stdout, port=port
        ) or _iptables_has_broad_accept_for_port(rules_v6, port=port):
            raise RuntimeError(f"firewall exposes {port}/tcp with a broad accept rule")

    for cidr in policy.cidrs:
        if ":" in cidr and (not ipv6_enabled or ipv6_chain_missing):
            continue
        rules = rules_v6 if ":" in cidr else iptables_v4.stdout
        for port in policy.ports:
            if not _iptables_accepts_port_for_source(rules, port=port, source=cidr):
                raise RuntimeError(
                    f"firewall missing Cloudflare source rule for {cidr} on port {port}"
                )

    return {
        "checked": True,
        "cidr_count": len(policy.cidrs),
        "ports": list(policy.ports),
        "expected_rule_count": policy.expected_rule_count,
        "live_rule_count": policy.expected_rule_count,
        "ipv6_enabled": ipv6_enabled,
        "ipv6_chain_present": not ipv6_chain_missing,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.services.cloudflare_ingress")
    subparsers = parser.add_subparsers(dest="command", required=True)

    rewrite = subparsers.add_parser("rewrite-ufw")
    rewrite.add_argument("--cidr", action="append", default=[])
    rewrite.add_argument("--user-rules-path", default=str(UFW_USER_RULES_PATH))
    rewrite.add_argument("--user6-rules-path", default=str(UFW_USER6_RULES_PATH))

    verify = subparsers.add_parser("verify-live")
    verify.add_argument("--cidr", action="append", default=[])
    verify.add_argument("--timeout-sec", type=int, default=5)
    verify.add_argument("--ufw-defaults-path", default=str(UFW_DEFAULTS_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    policy = build_cloudflare_ingress_policy(args.cidr)
    try:
        if args.command == "rewrite-ufw":
            payload = rewrite_cloudflare_http_firewall_files(
                policy,
                user_rules_path=Path(args.user_rules_path),
                user6_rules_path=Path(args.user6_rules_path),
            )
        else:
            payload = audit_live_cloudflare_http_firewall(
                policy,
                run_command_func=run_command,
                timeout_sec=args.timeout_sec,
                ufw_defaults_path=Path(args.ufw_defaults_path),
            )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
