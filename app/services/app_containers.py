from __future__ import annotations

import ipaddress
import json
import os
from typing import Any
from pathlib import Path

from app.services.hardening_policy import read_hardening_policy
from app.config import Settings
from app.models.entities import Backend
from app.services.commands import CommandResult
from app.services.container_runtime import (
    inspect_container as runtime_inspect_container,
    inspect_network as runtime_inspect_network,
)
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.resource_profile import (
    ResourceProfile,
    backend_resource_profile,
    build_resource_profile,
)
from app.services.renderers import network_name
from app.services.renderers import container_name
from app.services.sandbox_profiles import (
    app_sandbox_dir,
    app_sandbox_profile_state_path,
    app_sandbox_rootfs_path,
    get_app_sandbox_profile,
    render_sandbox_profile_state,
)
from app.services.validators import parse_volumes_json


APP_DEBUG_TOOLBELT_VERSION = "v3"
APP_DEBUG_TOOL_COMMANDS = (
    "bash",
    "curl",
    "dig",
    "ip",
    "jq",
    "nslookup",
    "ping",
    "ps",
    "rg",
    "sqlite3",
    "ss",
    "sudo",
    "top",
    "wget",
)
DEFAULT_APP_DNS_SERVERS = ("1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4")
_RESOLV_DNS_CACHE: dict[str, tuple[int | None, list[str]]] = {}


def control_dir(settings: Settings, backend_name: str) -> Path:
    return settings.app_control_dir / backend_name


def spec_path(settings: Settings, backend_name: str) -> Path:
    return control_dir(settings, backend_name) / "spec.json"


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def app_debug_tool_commands() -> list[str]:
    return list(APP_DEBUG_TOOL_COMMANDS)


def _normalize_dns_server(raw: str) -> str | None:
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
        return None
    return str(parsed)


def _discover_dns_servers(path: Path) -> tuple[int | None, list[str]]:
    cache_key = str(path)
    try:
        mtime_ns: int | None = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    cached = _RESOLV_DNS_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime_ns:
        return mtime_ns, list(cached[1])

    discovered: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("nameserver"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        normalized = _normalize_dns_server(parts[1])
        if normalized is not None:
            discovered.append(normalized)
    discovered = list(dict.fromkeys(discovered))
    _RESOLV_DNS_CACHE[cache_key] = (mtime_ns, discovered)
    return mtime_ns, list(discovered)


def configured_app_dns_servers(settings: Settings) -> list[str]:
    configured = settings.app_container_dns_servers.replace("\n", ",").split(",")
    resolved = [_normalize_dns_server(entry) for entry in configured]
    explicit = list(dict.fromkeys(entry for entry in resolved if entry is not None))
    if explicit:
        return explicit

    _, discovered = _discover_dns_servers(settings.app_container_resolv_conf_path)
    if discovered:
        return discovered
    return list(DEFAULT_APP_DNS_SERVERS)


def runtime_spec(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile | None = None,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
) -> dict[str, Any]:
    base_profile = base_profile or build_resource_profile(settings, [backend])
    resource_profile = backend_resource_profile(backend, base_profile)
    sandbox_profile = get_app_sandbox_profile(backend.sandbox_profile)
    return {
        "runtime_owner": "quadlet",
        "hardening": read_hardening_policy(backend).model_dump(exclude_defaults=True),
        "quadlet_container_unit": f"{container_name(backend.name)}.container",
        "quadlet_container_service": f"{container_name(backend.name)}.service",
        "quadlet_network_unit": f"{network_name(backend.name)}.network",
        "quadlet_network_service": f"{network_name(backend.name)}-network.service",
        "network": network_name(backend.name),
        "sandbox_profile": sandbox_profile.profile_id,
        "seed_image": sandbox_profile.seed_image,
        "seed_revision": sandbox_profile.seed_revision,
        "sandbox_dir": str(app_sandbox_dir(settings, backend.name)),
        "guest_rootfs": str(app_sandbox_rootfs_path(settings, backend.name)),
        "port": int(backend.port or 0),
        "handoff_port": int(backend.handoff_port),
        "resource_mode": resource_profile.mode,
        "resource_size": resource_profile.resource_size,
        "memory_high": resource_profile.memory_high,
        "memory_max": resource_profile.memory_max,
        "cpu_quota": resource_profile.cpu_quota,
        "cpu_shares": resource_profile.cpu_shares,
        "dns_servers": configured_app_dns_servers(settings),
        "volumes": parse_volumes_json(backend.volumes_json),
        "init_command": list(sandbox_profile.init_command),
        "inter_app_interfaces": [
            {
                "name": item.name,
                "source_backend": item.source_backend,
                "target_backend": item.target_backend,
                "target_handoff_port": item.target_handoff_port,
                "network": item.network,
                "env_key": item.env_key,
                "url": item.url,
                "role": "source" if item.source_backend == backend.name else "target",
                "direction": item.direction,
            }
            for item in sorted(
                inter_app_interfaces,
                key=lambda entry: (
                    entry.source_backend,
                    entry.target_backend,
                    entry.name,
                ),
            )
        ],
    }


def render_guest_contract_summary(backend: Backend) -> str:
    if backend.kind != "app":
        raise ValueError("guest contract summary requested for non-app backend")
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    return "\n".join(
        [
            "CNC app backend contract",
            f"- sandbox profile: {profile.profile_id}",
            f"- seed image: {profile.seed_image}",
            f"- guest init: {' '.join(profile.init_command)}",
            f"- handoff port: {backend.handoff_port}",
            "- app runtime ownership: guest-local systemd units and normal Linux tooling",
            "- cnc ownership: sandbox lifecycle, ssh entry, loopback publish, and health probes",
        ]
    )


def write_app_control_assets(
    backend: Backend,
    settings: Settings,
    *,
    write_spec: bool = True,
    write_profile_state: bool = True,
    base_profile: ResourceProfile | None = None,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
) -> None:
    backend_control_dir = control_dir(settings, backend.name)
    backend_control_dir.mkdir(parents=True, exist_ok=True)
    if write_spec:
        _write_text_atomic(
            spec_path(settings, backend.name),
            json.dumps(
                runtime_spec(
                    backend,
                    settings,
                    base_profile=base_profile,
                    inter_app_interfaces=inter_app_interfaces,
                ),
                indent=2,
                sort_keys=True,
            ),
        )
    if not write_profile_state:
        return
    profile = get_app_sandbox_profile(backend.sandbox_profile)
    _write_text_atomic(
        app_sandbox_profile_state_path(settings, backend.name),
        render_sandbox_profile_state(profile),
    )


def read_saved_spec(settings: Settings, backend_name: str) -> dict[str, Any] | None:
    path = spec_path(settings, backend_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def inspect_container(
    container: str, timeout_sec: int
) -> tuple[CommandResult, dict[str, Any] | None]:
    return runtime_inspect_container(container, timeout_sec)


def inspect_network(
    network: str, timeout_sec: int
) -> tuple[CommandResult, dict[str, Any] | None]:
    return runtime_inspect_network(network, timeout_sec)


def network_uses_bridge_dns(inspect_payload: dict[str, Any] | None) -> bool:
    if not isinstance(inspect_payload, dict):
        return False
    return bool(inspect_payload.get("dns_enabled"))


def container_ipv4_address(
    backend: Backend, inspect_payload: dict[str, Any] | None
) -> str | None:
    if not isinstance(inspect_payload, dict):
        return None
    network_settings = inspect_payload.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        return None
    networks = network_settings.get("Networks")
    if not isinstance(networks, dict):
        return None
    payload = networks.get(network_name(backend.name))
    if not isinstance(payload, dict):
        return None
    value = payload.get("IPAddress")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def container_state_payload(
    container: str, inspect_payload: dict[str, Any] | None
) -> dict[str, str]:
    state = inspect_payload.get("State") if isinstance(inspect_payload, dict) else {}
    if not isinstance(state, dict):
        state = {}
    running = bool(state.get("Running"))
    status = str(state.get("Status") or ("running" if running else "unknown"))
    return {
        "ActiveState": "active" if running else "inactive",
        "SubState": status,
        "ActiveEnterTimestamp": str(state.get("StartedAt") or ""),
        "ContainerName": container,
    }
