from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
from pathlib import Path
import tempfile
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.commands import CommandError, CommandResult
from app.services.observation import Absent, Present
from app.services.guest_exec import (
    EXEC_CIRCUIT_OPEN_RETURN_CODE,
    EXEC_GUARD_BUSY_RETURN_CODE,
    EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
    run_backend_guest_command,
)
from app.services.renderers import container_name, network_name
from app.services.runtime_services import (
    AppRuntimeServices,
    default_app_runtime_services,
)


APP_ISOLATION_CHAIN = "CNC_APP_ISOLATION"
CANARY_SOURCE_CONCURRENCY = 4
logger = get_logger("app.network_isolation")


def _network_subnets(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw_subnets = payload.get("subnets")
    if raw_subnets is None:
        raw_subnets = payload.get("Subnets")
    if not isinstance(raw_subnets, list):
        return []

    subnets: list[str] = []
    for entry in raw_subnets:
        if isinstance(entry, dict):
            raw_value = entry.get("subnet") or entry.get("Subnet")
        else:
            raw_value = entry
        try:
            network = ipaddress.ip_network(str(raw_value), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            subnets.append(str(network))
    return list(dict.fromkeys(subnets))


def _container_ipv4(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    network_settings = payload.get("NetworkSettings")
    if isinstance(network_settings, dict):
        networks = network_settings.get("Networks")
        if isinstance(networks, dict):
            for entry in networks.values():
                if not isinstance(entry, dict):
                    continue
                raw_address = str(entry.get("IPAddress") or "").strip()
                if not raw_address:
                    continue
                try:
                    address = ipaddress.ip_address(raw_address)
                except ValueError:
                    continue
                if address.version == 4:
                    return str(address)
    raw_address = str(payload.get("IPAddress") or "").strip()
    if raw_address:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return None
        if address.version == 4:
            return str(address)
    return None


async def reconcile_app_network_isolation(
    backend_names: set[str],
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> dict[str, object]:
    services = services or default_app_runtime_services()
    subnets: list[str] = []
    network_subnets: dict[str, list[str]] = {}
    for backend_name in sorted(backend_names):
        network = network_name(backend_name)
        observation = services.inspect_network(
            network, settings.command_timeout_status_sec
        )
        if isinstance(observation, Absent):
            continue
        discovered = (
            _network_subnets(observation.value)
            if isinstance(observation, Present)
            else []
        )
        if not discovered:
            detail = (
                "no IPv4 subnets"
                if isinstance(observation, Present)
                else observation.describe()
            )
            raise RuntimeError(
                f"cannot read subnets for {network} ({detail}); "
                "isolation rules left unchanged"
            )
        network_subnets[network] = discovered
        subnets.extend(discovered)

    subnets = list(dict.fromkeys(subnets))
    await _ensure_isolation_chain(subnets, settings, services=services)
    return {
        "chain": APP_ISOLATION_CHAIN,
        "network_subnets": network_subnets,
        "subnets": subnets,
        "rule_count": max(0, len(subnets) * (len(subnets) - 1)),
    }


def audit_app_network_isolation_guard(
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> dict[str, object]:
    services = services or default_app_runtime_services()
    chain_result = services.run_command(
        ["iptables", "-S", APP_ISOLATION_CHAIN],
        settings.command_timeout_status_sec,
    )
    if not chain_result.ok:
        message = (
            chain_result.stderr
            or chain_result.stdout
            or "iptables isolation chain missing"
        )
        raise RuntimeError(message)

    forward_result = services.run_command(
        ["iptables", "-S", "FORWARD"],
        settings.command_timeout_status_sec,
    )
    if not forward_result.ok:
        message = (
            forward_result.stderr
            or forward_result.stdout
            or "iptables FORWARD chain unreadable"
        )
        raise RuntimeError(message)
    forward_rules = [
        line.strip()
        for line in forward_result.stdout.splitlines()
        if line.strip().startswith("-A FORWARD ")
    ]
    if not forward_rules or forward_rules[0] != f"-A FORWARD -j {APP_ISOLATION_CHAIN}":
        raise RuntimeError(
            "output isolation guard is not installed at the FORWARD chain"
        )

    reject_rules = [
        line.strip()
        for line in chain_result.stdout.splitlines()
        if line.strip().startswith(f"-A {APP_ISOLATION_CHAIN} ")
        and " -j REJECT" in line
    ]
    return {
        "checked": True,
        "chain": APP_ISOLATION_CHAIN,
        "forward_jump": True,
        "forward_jump_position": 1,
        "reject_rule_count": len(reject_rules),
    }


def _probe_unavailable(result: CommandResult) -> bool:
    if result.returncode in {
        EXEC_CIRCUIT_OPEN_RETURN_CODE,
        EXEC_GUARD_BUSY_RETURN_CODE,
        EXEC_GUARD_UNAVAILABLE_RETURN_CODE,
        124,
        126,
        127,
    }:
        return True
    combined = f"{result.stderr}\n{result.stdout}".lower()
    markers = (
        "executable file not found",
        "no such file or directory",
        "not found",
        "permission denied",
        "container state improper",
        "no such container",
        "cannot exec",
    )
    return any(marker in combined for marker in markers)


def _probe_from_source(
    source: dict[str, object],
    nodes: list[dict[str, object]],
    settings: Settings,
    services: AppRuntimeServices,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    checks: list[dict[str, object]] = []
    leaks: list[dict[str, object]] = []
    unknown: list[dict[str, object]] = []
    if not source["inspect_ok"]:
        unknown.append(
            {"backend": source["backend"], "reason": "source_container_unavailable"}
        )
        return checks, leaks, unknown
    for target in nodes:
        if source["backend"] == target["backend"]:
            continue
        dns_command = [
            "getent",
            "hosts",
            str(target["container"]),
        ]
        dns_result = run_backend_guest_command(
            str(source["backend"]),
            settings,
            dns_command,
            timeout_sec=settings.command_timeout_status_sec,
            command_runner=services.run_command,
            source="network_isolation",
        )
        dns_resolved = dns_result.ok and bool((dns_result.stdout or "").strip())
        if not dns_result.ok and _probe_unavailable(dns_result):
            unknown.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "reason": "dns_probe_unavailable",
                    "detail": dns_result.stderr or dns_result.stdout,
                }
            )
        if dns_resolved:
            leaks.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "check": "dns",
                    "detail": (dns_result.stdout or "").strip(),
                }
            )

        target_address = str(target.get("address") or "")
        target_port = int(target.get("handoff_port") or 0)
        if not target["inspect_ok"] or not target_address or target_port <= 0:
            unknown.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "reason": "target_private_endpoint_unavailable",
                }
            )
            continue

        connect_command = [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "--connect-timeout",
            "2",
            "--max-time",
            "3",
            "-w",
            "%{http_code}",
            f"http://{target_address}:{target_port}/",
        ]
        connect_result = run_backend_guest_command(
            str(source["backend"]),
            settings,
            connect_command,
            timeout_sec=settings.command_timeout_status_sec,
            command_runner=services.run_command,
            source="network_isolation",
        )
        connected = connect_result.ok
        if not connect_result.ok and _probe_unavailable(connect_result):
            unknown.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "reason": "connect_probe_unavailable",
                    "detail": connect_result.stderr or connect_result.stdout,
                }
            )
        check = {
            "source": source["backend"],
            "target": target["backend"],
            "dns_resolved": dns_resolved,
            "private_connect_ok": connected,
        }
        checks.append(check)
        if connected:
            leaks.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "check": "private_connect",
                    "detail": (connect_result.stdout or "").strip() or "connected",
                }
            )
    return checks, leaks, unknown


def collect_app_network_isolation_canary(
    backends: list[Any],
    settings: Settings,
    *,
    services: AppRuntimeServices | None = None,
) -> dict[str, object]:
    services = services or default_app_runtime_services()
    nodes: list[dict[str, object]] = []
    for backend in sorted(backends, key=lambda item: str(getattr(item, "name", ""))):
        if str(getattr(backend, "kind", "")) != "app" or not bool(
            getattr(backend, "enabled", False)
        ):
            continue
        container = container_name(str(getattr(backend, "name", "")))
        observation = services.inspect_container(
            container, settings.command_timeout_status_sec
        )
        payload = observation.value if isinstance(observation, Present) else None
        address = _container_ipv4(payload)
        nodes.append(
            {
                "backend": str(getattr(backend, "name", "")),
                "container": container,
                "address": address or "",
                "handoff_port": int(getattr(backend, "handoff_port", 0) or 0),
                "inspect_ok": payload is not None,
            }
        )

    checks: list[dict[str, object]] = []
    leaks: list[dict[str, object]] = []
    unknown: list[dict[str, object]] = []
    # Probes from one guest stay sequential, behind that guest's exec guard;
    # different guests are probed concurrently so the check scales with the
    # number of apps rather than the number of pairs.
    with ThreadPoolExecutor(max_workers=CANARY_SOURCE_CONCURRENCY) as pool:
        for source_checks, source_leaks, source_unknown in pool.map(
            lambda source: _probe_from_source(source, nodes, settings, services),
            nodes,
        ):
            checks.extend(source_checks)
            leaks.extend(source_leaks)
            unknown.extend(source_unknown)

    return {
        "checked": True,
        "chain": APP_ISOLATION_CHAIN,
        "backend_count": len(nodes),
        "pair_count": len(checks),
        "ok": not leaks and not unknown,
        "leaks": leaks,
        "unknown": unknown,
        "checks": checks,
    }


async def _ensure_isolation_chain(
    subnets: list[str],
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> None:
    """Publish the whole isolation policy in one iptables transaction.

    Flushing the live chain and appending rules one at a time leaves apps able
    to reach each other until the last rule lands, and a cancellation in
    between leaves them open indefinitely. ``iptables-restore --noflush``
    swaps the filter table in a single commit: the old policy holds until the
    new one is complete, and a rejected transaction changes nothing. Rules
    outside this chain and its FORWARD jump are left as they are.
    """
    current = services.run_command(
        ["iptables-save", "-t", "filter"],
        settings.command_timeout_apply_sec,
    )
    if not current.ok:
        raise CommandError(current)
    jump = f"-A FORWARD -j {APP_ISOLATION_CHAIN}"
    existing_jumps = sum(
        1 for line in (current.stdout or "").splitlines() if line.strip() == jump
    )
    lines = ["*filter", f":{APP_ISOLATION_CHAIN} - [0:0]"]
    lines.extend(
        f"-A {APP_ISOLATION_CHAIN} -s {source} -d {destination} -j REJECT"
        for source in subnets
        for destination in subnets
        if source != destination
    )
    lines.extend(f"-D FORWARD -j {APP_ISOLATION_CHAIN}" for _ in range(existing_jumps))
    lines.extend([f"-I FORWARD 1 -j {APP_ISOLATION_CHAIN}", "COMMIT", ""])
    result = _iptables_restore_noflush("\n".join(lines), settings, services=services)
    if not result.ok:
        logger.warning(
            "app.network_isolation.transaction_rejected",
            chain=APP_ISOLATION_CHAIN,
            subnet_count=len(subnets),
            stderr=result.stderr,
        )
        raise CommandError(result)


def _iptables_restore_noflush(
    rules: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> CommandResult:
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="cnc-iptables-",
            suffix=".rules",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(rules)
        return services.run_command(
            ["iptables-restore", "--noflush", temp_name],
            settings.command_timeout_apply_sec,
        )
    except OSError as exc:
        return CommandResult(
            ["iptables-restore", "--noflush", "<rules>"],
            1,
            "",
            f"iptables rules file failed: {exc}",
        )
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except OSError:
                logger.warning(
                    "app.network_isolation.rules_file_cleanup_failed",
                    path=temp_name,
                )
