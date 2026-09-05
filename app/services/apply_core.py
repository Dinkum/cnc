from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from app.config import Settings
from app.models.entities import Backend
from app.services.app_containers import runtime_spec
from app.services.app_quadlet import (
    quadlet_container_policy_content,
    render_quadlet_container,
)
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.error_reporting import CNCError, ErrorCode
from app.services.netdata_constants import NETDATA_SERVICE_NAME, NETDATA_SYSTEMD_SERVICE
from app.services.renderers import network_name
from app.services.resource_profile import (
    ResourceProfile,
    calculate_app_memory_budget_bytes,
)


APPLY_SLICE_SCHEMA_VERSION = 3
CLUSTER_FOLLOWERS_RECONCILER_VERSION = 2


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


@dataclass(frozen=True)
class ApplySlice:
    name: str
    payload: dict[str, Any]
    hash: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def hash_apply_slice_payload(name: str, payload: dict[str, Any]) -> str:
    framed = {
        "schema": APPLY_SLICE_SCHEMA_VERSION,
        "slice": name,
        "payload": payload,
    }
    return hashlib.sha256(canonical_json(framed).encode("utf-8")).hexdigest()


def _slice(name: str, payload: dict[str, Any]) -> ApplySlice:
    return ApplySlice(
        name=name, payload=payload, hash=hash_apply_slice_payload(name, payload)
    )


def runtime_backend_slice_name(backend: Backend) -> str:
    backend_id = getattr(backend, "id", None)
    suffix = str(backend_id) if backend_id is not None else str(backend.name)
    return f"runtime.backend.{suffix}"


def nginx_managed_slice_payload(nginx_files: dict[str, str]) -> dict[str, Any]:
    return {
        "files": [
            {"name": filename, "content": nginx_files[filename]}
            for filename in sorted(nginx_files)
        ]
    }


def tailscale_slice_payload(
    paths: dict[str, str], services: dict[str, str]
) -> dict[str, Any]:
    return {
        "paths": [{"path": path, "target": paths[path]} for path in sorted(paths)],
        "services": [
            {"service": service, "target": services[service]}
            for service in sorted(services)
        ],
    }


def cluster_followers_slice_payload(desired: DesiredState) -> dict[str, Any]:
    return {
        "reconciler_version": CLUSTER_FOLLOWERS_RECONCILER_VERSION,
        "nodes": list(desired.cluster_nodes),
        "app_nginx_files": {
            node_uid: [
                {"name": filename, "content": files[filename]}
                for filename in sorted(files)
            ]
            for node_uid, files in sorted(desired.cluster_node_app_nginx_files.items())
        },
        "admin_nginx_files": {
            node_uid: [
                {"name": filename, "content": files[filename]}
                for filename in sorted(files)
            ]
            for node_uid, files in sorted(
                desired.cluster_node_admin_nginx_files.items()
            )
        },
        "static_replicas": [
            {
                "backend": backend,
                "replica_root": replica["replica_root"],
                "content_hash": replica["content_hash"],
            }
            for backend, replica in sorted(desired.static_replicas.items())
        ],
    }


def _runtime_slice_spec(
    backend: Backend,
    settings: Settings,
    resource_profile: ResourceProfile,
    *,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
) -> dict[str, Any]:
    spec = dict(
        runtime_spec(
            backend,
            settings,
            base_profile=resource_profile,
            inter_app_interfaces=inter_app_interfaces,
        )
    )
    resource_mode = str(spec.get("resource_mode") or "auto")
    has_resource_override = any(
        str(getattr(backend, key, "") or "").strip()
        for key in ("memory_high_override", "memory_max_override", "cpu_quota_override")
    )
    if (
        resource_profile.mode == "auto"
        and resource_mode == "auto"
        and not has_resource_override
    ):
        # CPU remains a live Podman setting. Memory belongs to the generated
        # service, so its calculated values must participate in convergence.
        spec.pop("cpu_quota", None)
    return spec


def render_apply_slices(
    desired: DesiredState, settings: Settings
) -> dict[str, ApplySlice]:
    slices: dict[str, ApplySlice] = {}
    app_memory_budget = (
        desired.resource_profile.app_memory_budget_bytes
        or calculate_app_memory_budget_bytes(settings)
    )
    slices["apps_memory"] = _slice(
        "apps_memory",
        {
            "unit": "cnc-apps.slice",
            "memory_max_bytes": app_memory_budget,
            "host_memory_reserve_bytes": desired.resource_profile.host_memory_reserve_bytes,
        },
    )
    interface_links = tuple(desired.runtime_graph.inter_app_interfaces)
    for backend in sorted(
        desired.known_app_backends, key=lambda item: (item.id or 0, item.name)
    ):
        name = runtime_backend_slice_name(backend)
        payload: dict[str, Any] = {
            "backend_id": backend.id,
            "backend": backend.name,
            "enabled": bool(backend.enabled),
        }
        if backend.enabled:
            related_interfaces = tuple(
                item
                for item in interface_links
                if item.source_backend == backend.name
                or item.target_backend == backend.name
            )
            payload["runtime_spec"] = _runtime_slice_spec(
                backend,
                settings,
                desired.resource_profile,
                inter_app_interfaces=related_interfaces,
            )
            payload["quadlet_container_sha256"] = hashlib.sha256(
                quadlet_container_policy_content(
                    render_quadlet_container(
                        backend,
                        settings,
                        base_profile=desired.resource_profile,
                        inter_app_interfaces=related_interfaces,
                    )
                ).encode("utf-8")
            ).hexdigest()
        slices[name] = _slice(name, payload)

    slices["nginx.managed"] = _slice(
        "nginx.managed", nginx_managed_slice_payload(desired.nginx_files)
    )
    slices["tailscale"] = _slice(
        "tailscale",
        tailscale_slice_payload(desired.tailscale_paths, desired.tailscale_services),
    )
    slices["cluster_followers"] = _slice(
        "cluster_followers",
        cluster_followers_slice_payload(desired),
    )
    slices["ssh_access"] = _slice(
        "ssh_access",
        {
            "app_backends": [
                {"backend_id": backend.id, "backend": backend.name}
                | (
                    {
                        "ssh_public_key_sha256": hashlib.sha256(
                            backend.ssh_public_key.encode("utf-8")
                        ).hexdigest()
                    }
                    if backend.ssh_public_key
                    else {}
                )
                for backend in sorted(
                    desired.enabled_app_backends,
                    key=lambda item: (item.id or 0, item.name),
                )
            ]
        },
    )
    slices["shield"] = _slice(
        "shield",
        {
            "required": bool(desired.shield_required),
            "outputs": [
                {
                    "backend": name,
                    "code_hash_sha256": hashlib.sha256(
                        code_hash.encode("utf-8")
                    ).hexdigest(),
                }
                for name, code_hash in sorted(desired.shield_output_code_hashes.items())
            ],
            "container_image": settings.shield_container_image,
            "port": settings.shield_port,
        },
    )
    slices["netdata"] = _slice(
        "netdata",
        {
            "required": bool(desired.netdata_required),
            "install": "native-kickstart",
            "config_path": str(settings.netdata_config_path),
            "port": settings.netdata_port,
            "service": NETDATA_SYSTEMD_SERVICE,
            "tailnet_service": NETDATA_SERVICE_NAME,
        },
    )
    slices["network_isolation"] = _slice(
        "network_isolation",
        {
            "backend_networks": [
                {"backend": name, "network": network_name(name)}
                for name in sorted(desired.runtime_graph.enabled_app_backend_names)
            ],
            "policy": "deny-private-cross-output",
            "inter_app_interfaces": [
                {
                    "name": item.name,
                    "source_backend": item.source_backend,
                    "target_backend": item.target_backend,
                    "network": item.network,
                    "direction": item.direction,
                }
                for item in interface_links
            ],
        },
    )
    slices["host_base"] = _slice(
        "host_base",
        {
            "managed_systemd_assets": "cnc service/timer units",
            "nginx_generated_dir": str(settings.nginx_generated_dir),
        },
    )
    return {name: slices[name] for name in sorted(slices)}


def desired_state_snapshot(desired: DesiredState) -> dict[str, Any]:
    generated_nginx_files = {
        filename: desired.nginx_files[filename]
        for filename in sorted(desired.nginx_files)
    }
    return {
        "generated_nginx_files": generated_nginx_files,
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
