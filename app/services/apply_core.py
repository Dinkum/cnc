from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from app.models.entities import Backend
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.error_reporting import CNCError, ErrorCode
from app.services.resource_profile import (
    ResourceProfile,
)


CLUSTER_FOLLOWERS_RECONCILER_VERSION = 3


@dataclass(frozen=True)
class RuntimeAppBackend:
    backend: str
    enabled: bool
    container: str
    network: str
    port: int | None
    handoff_port: int
    sandbox_profile: str
    sandbox_dir: str
    guest_rootfs: str
    volumes: tuple[str, ...]
    dns_servers: tuple[str, ...]
    resource_mode: str
    resource_size: str
    memory_high: str
    memory_max: str
    cpu_quota: str
    cpu_shares: int
    healthcheck_mode: str
    healthcheck_path: str
    healthcheck_host_header: str
    hardening: dict[str, Any] = field(default_factory=dict)
    inter_app_interfaces: tuple[dict[str, Any], ...] = ()
    interface_networks: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "container": self.container,
            "network": self.network,
            "port": self.port,
            "handoff_port": self.handoff_port,
            "sandbox_profile": self.sandbox_profile,
            "hardening": self.hardening,
            "sandbox_dir": self.sandbox_dir,
            "guest_rootfs": self.guest_rootfs,
            "volumes": list(self.volumes),
            "dns_servers": list(self.dns_servers),
            "resource_mode": self.resource_mode,
            "resource_size": self.resource_size,
            "memory_high": self.memory_high,
            "memory_max": self.memory_max,
            "cpu_quota": self.cpu_quota,
            "cpu_shares": self.cpu_shares,
            "healthcheck": {
                "mode": self.healthcheck_mode,
                "path": self.healthcheck_path,
                "host_header": self.healthcheck_host_header,
            },
            "inter_app_interfaces": list(self.inter_app_interfaces),
            "interface_networks": list(self.interface_networks),
            "publish": {
                "host_ip": "127.0.0.1",
                "host_port": self.port,
                "container_port": self.handoff_port,
                "protocol": "tcp",
            }
            if self.port is not None
            else None,
        }


@dataclass(frozen=True)
class RuntimeGraph:
    app_backends: dict[str, RuntimeAppBackend] = field(default_factory=dict)
    route_contracts: tuple[dict[str, Any], ...] = ()
    backend_contracts: tuple[dict[str, Any], ...] = ()
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = ()

    @property
    def known_app_backend_names(self) -> set[str]:
        return set(self.app_backends)

    @property
    def enabled_app_backend_names(self) -> set[str]:
        return {name for name, node in self.app_backends.items() if node.enabled}

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_backends": {
                name: self.app_backends[name].as_dict()
                for name in sorted(self.app_backends)
            },
            "routes": list(self.route_contracts),
            "backends": list(self.backend_contracts),
            "inter_app_interfaces": [
                {
                    "name": item.name,
                    "source_backend": item.source_backend,
                    "target_backend": item.target_backend,
                    "target_handoff_port": item.target_handoff_port,
                    "network": item.network,
                    "env_key": item.env_key,
                    "url": item.url,
                    "direction": item.direction,
                }
                for item in self.inter_app_interfaces
            ],
        }


@dataclass
class DesiredState:
    nginx_files: dict[str, str]
    tailscale_paths: dict[str, str]
    tailscale_services: dict[str, str]
    enabled_app_backends: list[Backend]
    known_app_backends: list[Backend]
    resource_profile: ResourceProfile
    route_contracts: list[dict[str, Any]]
    backend_contracts: list[dict[str, Any]]
    shield_required: bool = False
    shield_output_code_hashes: dict[str, str] = field(default_factory=dict)
    netdata_required: bool = False
    runtime_graph: RuntimeGraph = field(default_factory=RuntimeGraph)
    cluster_nodes: tuple[dict[str, Any], ...] = ()
    cluster_node_app_nginx_files: dict[str, dict[str, str]] = field(
        default_factory=dict
    )
    cluster_node_admin_nginx_files: dict[str, dict[str, str]] = field(
        default_factory=dict
    )
    static_replicas: dict[str, dict[str, str]] = field(default_factory=dict)
    nginx_container: bool = False
    nginx_backend_ports: tuple[int, ...] = ()
    nginx_static_roots: tuple[str, ...] = ()
    nginx_public_http: bool = False
    nginx_public_https: bool = False
    nginx_tunnel: bool = False




def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


















def desired_state_snapshot(desired: DesiredState) -> dict[str, Any]:
    generated_nginx_files = {
        filename: desired.nginx_files[filename]
        for filename in sorted(desired.nginx_files)
    }
    return {
        "generated_nginx_files": generated_nginx_files,
        **(
            {
                "nginx_ingress": {
                    "backend_ports": list(desired.nginx_backend_ports),
                    "static_roots": list(desired.nginx_static_roots),
                    "public_http": desired.nginx_public_http,
                    "public_https": desired.nginx_public_https,
                    "tunnel": desired.nginx_tunnel,
                }
            }
            if desired.nginx_container
            else {}
        ),
        "tailscale_paths": {
            path: desired.tailscale_paths[path]
            for path in sorted(desired.tailscale_paths)
        },
        "tailscale_services": {
            service: desired.tailscale_services[service]
            for service in sorted(desired.tailscale_services)
        },
        "runtime_graph": desired.runtime_graph.as_dict(),
        "route_contracts": desired.route_contracts,
        "backend_contracts": desired.backend_contracts,
        "resource_profile": desired.resource_profile.as_dict(),
        "shield_required": desired.shield_required,
        "shield_output_code_hashes": {
            name: "<configured>" for name in sorted(desired.shield_output_code_hashes)
        },
        "netdata_required": desired.netdata_required,
        "cluster_nodes": list(desired.cluster_nodes),
        "cluster_node_app_nginx_files": {
            node_uid: {filename: files[filename] for filename in sorted(files)}
            for node_uid, files in sorted(desired.cluster_node_app_nginx_files.items())
        },
        "cluster_node_admin_nginx_files": {
            node_uid: {filename: files[filename] for filename in sorted(files)}
            for node_uid, files in sorted(
                desired.cluster_node_admin_nginx_files.items()
            )
        },
        "static_replicas": {
            backend: {
                "replica_root": replica["replica_root"],
                "content_hash": replica["content_hash"],
            }
            for backend, replica in sorted(desired.static_replicas.items())
        },
    }


def desired_state_snapshot_json(snapshot: dict[str, Any]) -> str:
    return canonical_json(snapshot)


def desired_state_snapshot_hash(snapshot_json: str) -> str:
    return hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()


def config_revision_for_state_hash(desired_state_hash: str) -> str:
    return f"cfg-{desired_state_hash[:16]}"


class ApplyFailed(CNCError):
    def __init__(
        self,
        message: str,
        *,
        phase: str = "apply",
        details: dict[str, Any] | None = None,
        error_code: ErrorCode = ErrorCode.APPLY_FAILED,
    ) -> None:
        self.phase = phase
        self.details = details or {}
        super().__init__(
            error_code,
            message,
            details={"phase": phase, **self.details},
        )

    def as_details(self) -> dict[str, Any]:
        return super().as_details()


@dataclass
class AppRuntimeReconcileResult:
    backend: str
    container: str
    created: bool = False
    recreated: bool = False
    bootstrapped: bool = False
    target_ip: str | None = None
    dns_servers: list[str] | None = None
    phases: dict[str, dict[str, Any]] | None = None


def clip_output(content: str, max_chars: int = 500) -> str:
    if len(content) <= max_chars:
        return content
    return f"{content[:max_chars]}...[truncated]"
