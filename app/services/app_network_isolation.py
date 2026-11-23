from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import tempfile
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.commands import CommandError, CommandResult
from app.services.container_runtime import inspect_container_batch
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
CANARY_SOURCE_TIMEOUT_MAX_SEC = 30
logger = get_logger("app.network_isolation")


_CANARY_SOURCE_PROBE = """
import json
import subprocess
import sys

def probe(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return [result.returncode, result.stdout.strip()[:1024], result.stderr.strip()[:1024]]
    except subprocess.TimeoutExpired:
        return [124, "", "probe timed out"]
    except OSError as exc:
        return [127, "", str(exc)]

timeout = float(sys.argv[2])
for index, (name, address, port) in enumerate(json.loads(sys.argv[1])):
    dns = probe(["getent", "hosts", name], min(timeout, 2))
    connect = None
    if address and port:
        connect = probe([
            "curl", "-sS", "-o", "/dev/null", "--connect-timeout", "2",
            "--max-time", "3", "-w", "%{http_code}", f"http://{address}:{port}/",
        ], min(timeout, 4))
    print(json.dumps([index, dns, connect]), flush=True)
"""


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


def _batch_probe_result(raw: object, batch: CommandResult) -> CommandResult:
    if (
        isinstance(raw, list)
        and len(raw) == 3
        and type(raw[0]) is int
        and isinstance(raw[1], str)
        and isinstance(raw[2], str)
    ):
        return CommandResult(batch.command, raw[0], raw[1], raw[2])
    return CommandResult(
        batch.command,
        126,
        "",
        batch.stderr or "canary probe result unavailable",
    )


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
    targets = [target for target in nodes if source["backend"] != target["backend"]]
    if not targets:
        return checks, leaks, unknown
    pairs = []
    for target in targets:
        address = str(target.get("address") or "") if target["inspect_ok"] else ""
        port = max(0, int(target.get("handoff_port") or 0))
        pairs.append((str(target["container"]), address, port))
    probe_timeout = settings.command_timeout_status_sec
    timeout_per_pair = min(2, probe_timeout) + min(4, probe_timeout)
    source_timeout = min(
        CANARY_SOURCE_TIMEOUT_MAX_SEC, timeout_per_pair * len(targets) + 1
    )
    batch = run_backend_guest_command(
        str(source["backend"]),
        settings,
        ["python3", "-c", _CANARY_SOURCE_PROBE, json.dumps(pairs), str(probe_timeout)],
        timeout_sec=source_timeout,
        command_runner=services.run_command,
        source="network_isolation",
    )
    observations = {}
    for line in batch.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, list) and len(row) == 3 and type(row[0]) is int:
            observations[row[0]] = (row[1], row[2])

    for index, (target, (_, address, port)) in enumerate(zip(targets, pairs)):
        dns, connect = observations.get(index, (None, None))
        dns_result = _batch_probe_result(dns, batch)
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

        if not address or not port:
            unknown.append(
                {
                    "source": source["backend"],
                    "target": target["backend"],
                    "reason": "target_private_endpoint_unavailable",
                }
            )
            continue

        connect_result = _batch_probe_result(connect, batch)
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
    use_batch_inspection = services is None
    services = services or default_app_runtime_services()
    nodes: list[dict[str, object]] = []
    enabled = sorted(
        (
            backend
            for backend in backends
            if getattr(backend, "kind", "") == "app"
            and getattr(backend, "enabled", False)
        ),
        key=lambda backend: str(getattr(backend, "name", "")),
    )
    containers = [
        container_name(str(getattr(backend, "name", ""))) for backend in enabled
    ]
    timeout_sec = settings.command_timeout_status_sec
    inspections = (
        inspect_container_batch(
            containers, timeout_sec, command_runner=services.run_command
        )
        if use_batch_inspection and len(containers) > 1
        else {}
    )
    for backend, container in zip(enabled, containers):
        payload = inspections.get(container)
        if payload is None:
            observation = services.inspect_container(container, timeout_sec)
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
    # Each guest holds one exec guard for its batch. Distinct guests run concurrently.
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
