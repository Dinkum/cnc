from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
import ipaddress
import json
from pathlib import Path
import shlex
import tempfile

from app.config import Settings, get_settings
from app.services.cloudflare_ingress import build_cloudflare_ingress_policy
from app.services.commands import CommandError
from app.services.file_locks import FileLock
from app.services.host_state import write_json_atomic
from app.services.nginx_contract import (
    BRIDGE_NAME,
    CONTAINER_IP,
    CONTAINER_IP_V6,
    GATEWAY,
    GATEWAY_V6,
    HTTP_PORT,
    HTTPS_PORT,
)
from app.services.runtime_services import AppRuntimeServices, default_app_runtime_services


INPUT_CHAIN = "CNC_NGINX_INPUT"
FORWARD_CHAIN = "CNC_NGINX_FORWARD"
ALLOW_CHAIN = "CNC_NGINX_ALLOW"
TABLE_CHAINS = {
    "mangle": {"INPUT": INPUT_CHAIN, "FORWARD": FORWARD_CHAIN},
    "filter": {"FORWARD": ALLOW_CHAIN},
}


@dataclass(frozen=True)
class NginxFirewallPolicy:
    backend_ports: tuple[int, ...]
    cloudflare_only: bool
    cloudflare_cidrs: tuple[str, ...]


DENY_ALL = NginxFirewallPolicy((), True, ())


def nginx_firewall_policy_path(settings: Settings) -> Path:
    return settings.app_control_dir / "nginx-firewall.json"


def _policy(
    backend_ports: Iterable[int], cloudflare_only: bool, cloudflare_cidrs: Iterable[str]
) -> NginxFirewallPolicy:
    ports = tuple(backend_ports)
    if any(type(port) is not int or not 0 < port < 65536 for port in ports):
        raise ValueError("invalid NGINX upstream port")
    if type(cloudflare_only) is not bool:
        raise ValueError("invalid NGINX Cloudflare ingress mode")
    cidrs = tuple(cloudflare_cidrs)
    normalized = build_cloudflare_ingress_policy(cidrs).cidrs if cidrs else ()
    return NginxFirewallPolicy(tuple(sorted(set(ports))), cloudflare_only, normalized)


def read_nginx_firewall_policy(settings: Settings) -> NginxFirewallPolicy | None:
    path = nginx_firewall_policy_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"backend_ports", "cloudflare_only", "cloudflare_cidrs"}
        or not isinstance(raw["backend_ports"], list)
        or not isinstance(raw["cloudflare_cidrs"], list)
    ):
        raise ValueError("invalid saved NGINX firewall policy")
    policy = _policy(**raw)
    if asdict(policy) != {
        "backend_ports": tuple(raw["backend_ports"]),
        "cloudflare_only": raw["cloudflare_only"],
        "cloudflare_cidrs": tuple(raw["cloudflare_cidrs"]),
    }:
        raise ValueError("invalid saved NGINX firewall policy")
    return policy


def _public_connections(
    policy: NginxFirewallPolicy, *, ipv6: bool
) -> Iterator[tuple[str, int, int]]:
    sources = (
        tuple(cidr for cidr in policy.cloudflare_cidrs if (":" in cidr) == ipv6)
        if policy.cloudflare_only
        else ("::/0" if ipv6 else "0.0.0.0/0",)
    )
    for source in sources:
        for host_port, container_port in ((80, HTTP_PORT), (443, HTTPS_PORT)):
            yield source, host_port, container_port


def render_nginx_firewall_rules(
    policy: NginxFirewallPolicy, *, ipv6: bool, table: str = "mangle"
) -> dict[str, tuple[str, ...]]:
    address = CONTAINER_IP_V6 if ipv6 else CONTAINER_IP
    gateway = GATEWAY_V6 if ipv6 else GATEWAY
    icmp = "ipv6-icmp" if ipv6 else "icmp"
    incoming = f"-o {BRIDGE_NAME} -d {address}"
    outgoing = f"-i {BRIDGE_NAME} -s {address}"
    conntrack = "-m conntrack --ctstate DNAT"
    related = f"{incoming} -p {icmp} -m conntrack --ctstate RELATED"
    direct = [
        (
            f"{incoming} -s {source} -p tcp -m tcp --dport {container_port} "
            f"{conntrack} --ctorigdstport {host_port} --ctdir ORIGINAL",
            f"{outgoing} -d {source} -p tcp -m tcp --sport {container_port} "
            f"{conntrack} --ctorigdstport {host_port} --ctdir REPLY",
        )
        for source, host_port, container_port in _public_connections(policy, ipv6=ipv6)
    ]
    if table == "filter":
        accepted = [incoming_match for incoming_match, _ in direct]
        if accepted:
            accepted.append(related)
        return {ALLOW_CHAIN: tuple(f"-A {ALLOW_CHAIN} {rule} -j ACCEPT" for rule in accepted)}

    input_rules = [
        f"{outgoing} -m conntrack --ctstate ESTABLISHED --ctdir REPLY -j RETURN",
    ]
    if ipv6:
        input_rules.extend(
            f"-i {BRIDGE_NAME} -p {icmp} -m icmp6 --icmpv6-type {kind} -j RETURN"
            for kind in (135, 136)
        )
    input_rules.extend((f"-i {BRIDGE_NAME} -j DROP", f"-d {gateway} -j DROP"))

    forward_rules = [
        f"{related} -j RETURN",
        f"{outgoing} -p {icmp} -m conntrack --ctstate RELATED -j RETURN",
    ]
    for incoming_match, outgoing_match in direct:
        forward_rules.extend((f"{incoming_match} -j RETURN", f"{outgoing_match} -j RETURN"))
    if not ipv6:
        for port in policy.backend_ports:
            endpoint = f"{conntrack} --ctorigdst {gateway} --ctorigdstport {port}"
            forward_rules.extend(
                (
                    f"{outgoing} -p tcp {endpoint} --ctdir ORIGINAL -j RETURN",
                    f"{incoming} -p tcp {endpoint} --ctorigsrc {address} --ctdir REPLY -j RETURN",
                )
            )
    forward_rules.extend(
        (
            f"-m conntrack --ctorigdst {gateway} --ctdir ORIGINAL -j DROP",
            f"-o {BRIDGE_NAME} -j DROP",
            f"-i {BRIDGE_NAME} -j DROP",
        )
    )
    return {
        INPUT_CHAIN: tuple(f"-A {INPUT_CHAIN} {rule}" for rule in input_rules),
        FORWARD_CHAIN: tuple(f"-A {FORWARD_CHAIN} {rule}" for rule in forward_rules),
    }


def _read_table(
    binary: str, table: str, settings: Settings, services: AppRuntimeServices
) -> tuple[str, ...]:
    result = services.run_command(
        [f"{binary}-save", "-t", table], settings.command_timeout_apply_sec
    )
    if not result.ok:
        raise CommandError(result)
    return tuple(result.stdout.splitlines())


def _normalized_rule(rule: str) -> tuple[tuple[str, str], ...]:
    tokens = shlex.split(rule)
    pairs = []
    for option, value in zip(tokens[::2], tokens[1::2], strict=True):
        if option in {"-s", "-d", "--ctorigdst", "--ctorigsrc"}:
            address = ipaddress.ip_network(value, strict=False)
            if address.prefixlen == 0:
                continue
            value = str(address)
        pairs.append((option, value))
    return tuple(sorted(pairs))


def _first_ufw_rule(existing_rules: list[str]) -> int:
    return next(
        (
            index
            for index, line in enumerate(existing_rules)
            if " -j ufw-" in line or " -j ufw6-" in line
        ),
        len(existing_rules),
    )


def _matches(
    table: tuple[str, ...], expected: dict[str, tuple[str, ...]], *, name: str = "mangle"
) -> bool:
    for builtin, chain in TABLE_CHAINS[name].items():
        jump = f"-A {builtin} -j {chain}"
        builtin_rules = [line for line in table if line.startswith(f"-A {builtin} ")]
        if builtin_rules.count(jump) != 1:
            return False
        position = builtin_rules.index(jump)
        if (name == "mangle" and position != 0) or (
            name == "filter" and position >= _first_ufw_rule(builtin_rules)
        ):
            return False
        live = [line for line in table if line.startswith(f"-A {chain} ")]
        try:
            if tuple(map(_normalized_rule, live)) != tuple(map(_normalized_rule, expected[chain])):
                return False
        except ValueError:
            return False
    return True


def _restore_text(
    table: tuple[str, ...], rules: dict[str, tuple[str, ...]], *, name: str = "mangle"
) -> str:
    lines = [f"*{name}", *(f":{chain} - [0:0]" for chain in rules)]
    for builtin, chain in TABLE_CHAINS[name].items():
        old_jump = f"-A {builtin} -j {chain}"
        existing = [line for line in table if line.startswith(f"-A {builtin} ") and line != old_jump]
        position = 1 if name == "mangle" else _first_ufw_rule(existing) + 1
        lines.extend(f"-D {builtin} -j {chain}" for _ in range(table.count(old_jump)))
        lines.extend(rules[chain])
        lines.append(f"-I {builtin} {position} -j {chain}")
    return "\n".join([*lines, "COMMIT", ""])


def _apply_table(
    binary: str,
    name: str,
    policy: NginxFirewallPolicy,
    settings: Settings,
    services: AppRuntimeServices,
) -> None:
    rules = render_nginx_firewall_rules(policy, ipv6=binary == "ip6tables", table=name)
    current = _read_table(binary, name, settings, services)
    if _matches(current, rules, name=name):
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as payload:
        payload.write(_restore_text(current, rules, name=name))
        payload.flush()
        result = services.run_command(
            [f"{binary}-restore", "--wait", "5", "--noflush", payload.name],
            settings.command_timeout_apply_sec,
        )
    if not result.ok:
        raise CommandError(result)
    if not _matches(_read_table(binary, name, settings, services), rules, name=name):
        raise RuntimeError(f"{binary} {name} NGINX ingress policy did not match after apply")


def _apply_policy(
    policy: NginxFirewallPolicy, settings: Settings, services: AppRuntimeServices
) -> None:
    for binary in ("iptables", "ip6tables"):
        # Mangle blocks unwanted packets before the filter table accepts any.
        # The filter permit is also needed when UFW's routed policy is DROP.
        _apply_table(binary, "mangle", policy, settings, services)
        _apply_table(binary, "filter", policy, settings, services)


def _save_policy(
    settings: Settings,
    policy: NginxFirewallPolicy,
    previous: NginxFirewallPolicy | None,
    services: AppRuntimeServices,
) -> None:
    try:
        _apply_policy(policy, settings, services)
        if policy != previous:
            write_json_atomic(nginx_firewall_policy_path(settings), asdict(policy))
    except Exception as exc:
        try:
            _apply_policy(previous or DENY_ALL, settings, services)
        except Exception as restore_exc:
            raise RuntimeError(f"unable to restore NGINX ingress firewall: {restore_exc}") from exc
        raise


def reconcile_nginx_firewall(
    settings: Settings,
    backend_ports: Iterable[int],
    *,
    services: AppRuntimeServices | None = None,
) -> None:
    services = services or default_app_runtime_services()
    policy = _policy(backend_ports, settings.nginx_cloudflare_only, settings.cloudflare_ip_list)
    path = nginx_firewall_policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_sec=settings.command_timeout_apply_sec):
        _save_policy(settings, policy, read_nginx_firewall_policy(settings), services)


def restore_nginx_firewall(
    settings: Settings,
    previous: NginxFirewallPolicy | None,
    *,
    services: AppRuntimeServices | None = None,
) -> None:
    services = services or default_app_runtime_services()
    path = nginx_firewall_policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_sec=settings.command_timeout_apply_sec):
        _save_policy(settings, previous or DENY_ALL, read_nginx_firewall_policy(settings), services)


def bootstrap_nginx_firewall(
    settings: Settings, *, services: AppRuntimeServices | None = None
) -> None:
    services = services or default_app_runtime_services()
    path = nginx_firewall_policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_sec=settings.command_timeout_apply_sec):
        previous = read_nginx_firewall_policy(settings)
        _save_policy(settings, previous or DENY_ALL, previous, services)


def audit_nginx_firewall(
    settings: Settings, *, services: AppRuntimeServices | None = None
) -> dict[str, object]:
    services = services or default_app_runtime_services()
    policy = read_nginx_firewall_policy(settings)
    if policy is None:
        raise RuntimeError("NGINX firewall policy is missing")
    for binary in ("iptables", "ip6tables"):
        for table in TABLE_CHAINS:
            expected = render_nginx_firewall_rules(
                policy, ipv6=binary == "ip6tables", table=table
            )
            if not _matches(_read_table(binary, table, settings, services), expected, name=table):
                raise RuntimeError(f"{binary} {table} NGINX ingress firewall differs from approved policy")
    return {"checked": True, "backend_ports": list(policy.backend_ports)}


def _reload_saved_policy(settings: Settings) -> None:
    services = default_app_runtime_services()
    path = nginx_firewall_policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path, timeout_sec=settings.command_timeout_apply_sec):
        try:
            policy = read_nginx_firewall_policy(settings)
        except (OSError, ValueError):
            _apply_policy(DENY_ALL, settings, services)
            raise
        _apply_policy(policy or DENY_ALL, settings, services)
        if policy is None:
            raise RuntimeError("NGINX firewall policy is missing")


if __name__ == "__main__":
    _reload_saved_policy(get_settings())
