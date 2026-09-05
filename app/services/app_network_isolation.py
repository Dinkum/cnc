from __future__ import annotations

import ipaddress
from pathlib import Path
import tempfile
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.commands import CommandError, CommandResult
from app.services.guest_exec import run_backend_guest_command
from app.services.renderers import container_name, network_name
from app.services.runtime_services import (
    AppRuntimeServices,
    default_app_runtime_services,
)


APP_ISOLATION_CHAIN = "CNC_APP_ISOLATION"
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
        _result, payload = services.inspect_network(
            network, settings.command_timeout_status_sec
        )
        discovered = _network_subnets(payload)
        if discovered:
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
    if result.returncode in {75, 124, 126, 127}:
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
        result, payload = services.inspect_container(
            container, settings.command_timeout_status_sec
        )
        address = _container_ipv4(payload)
        nodes.append(
            {
                "backend": str(getattr(backend, "name", "")),
                "container": container,
                "address": address or "",
                "handoff_port": int(getattr(backend, "handoff_port", 0) or 0),
                "inspect_ok": result.ok and isinstance(payload, dict),
            }
        )

    checks: list[dict[str, object]] = []
    leaks: list[dict[str, object]] = []
    unknown: list[dict[str, object]] = []
    for source in nodes:
        if not source["inspect_ok"]:
            unknown.append(
                {"backend": source["backend"], "reason": "source_container_unavailable"}
            )
            continue
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
    snapshot_result = services.run_command(
        ["iptables-save"],
        settings.command_timeout_apply_sec,
    )
    if not snapshot_result.ok:
        raise CommandError(snapshot_result)

    create_result = services.run_command(
        ["iptables", "-N", APP_ISOLATION_CHAIN],
        settings.command_timeout_apply_sec,
    )
    if (
        not create_result.ok
        and "already exists" not in (create_result.stderr or "").lower()
    ):
        raise CommandError(create_result)

    try:
        await services.run_command_checked_async(
            ["iptables", "-F", APP_ISOLATION_CHAIN],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        for source in subnets:
            for destination in subnets:
                if source == destination:
                    continue
                await services.run_command_checked_async(
                    [
                        "iptables",
                        "-A",
                        APP_ISOLATION_CHAIN,
                        "-s",
                        source,
                        "-d",
                        destination,
                        "-j",
                        "REJECT",
                    ],
                    timeout_sec=settings.command_timeout_apply_sec,
                )

        for _ in range(20):
            delete_result = services.run_command(
                ["iptables", "-D", "FORWARD", "-j", APP_ISOLATION_CHAIN],
                settings.command_timeout_apply_sec,
            )
            if not delete_result.ok:
                break
        await services.run_command_checked_async(
            ["iptables", "-I", "FORWARD", "1", "-j", APP_ISOLATION_CHAIN],
            timeout_sec=settings.command_timeout_apply_sec,
        )
    except Exception:
        restore_result = _restore_iptables_snapshot(
            snapshot_result.stdout,
            settings,
            services=services,
        )
        logger.warning(
            "app.network_isolation.rollback_after_reconcile_failure",
            chain=APP_ISOLATION_CHAIN,
            subnet_count=len(subnets),
            restore_ok=restore_result.ok,
            restore_stderr=restore_result.stderr,
        )
        if not restore_result.ok:
            raise CommandError(restore_result)
        raise


def _restore_iptables_snapshot(
    snapshot: str,
    settings: Settings,
    *,
    services: AppRuntimeServices,
):
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
            handle.write(snapshot)
            if snapshot and not snapshot.endswith("\n"):
                handle.write("\n")
        return services.run_command(
            ["iptables-restore", temp_name],
            settings.command_timeout_apply_sec,
        )
    except OSError as exc:
        return CommandResult(
            ["iptables-restore", "<snapshot>"],
            1,
            "",
            f"iptables snapshot restore file failed: {exc}",
        )
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except OSError:
                logger.warning(
                    "app.network_isolation.restore_snapshot_cleanup_failed",
                    path=temp_name,
                )
