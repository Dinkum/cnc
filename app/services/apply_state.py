from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.hardening_policy import read_hardening_policy
from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend, ClusterNode, Input
from app.services.app_containers import runtime_spec
from app.services.app_healthchecks import display_backend_healthcheck_mode
from app.services.apply_core import (
    ApplyFailed,
    DesiredState,
    RuntimeAppBackend,
    RuntimeGraph,
)
from app.services.cluster_topology import leader_tailnet_ip
from app.services.inter_app_interfaces import (
    RuntimeInterAppInterface,
    build_runtime_inter_app_interfaces,
    read_inter_app_interfaces,
)
from app.services.netdata_constants import NETDATA_SERVICE_NAME
from app.services.placement_config import (
    LOCAL_NODE_UID,
    PLACEMENT_MODE_FAILOVER,
    PLACEMENT_MODE_LIVE,
    backend_has_local_runtime,
    backend_single_node_uid,
    read_backend_placement,
)
from app.services.port_allocator import PortAllocationError, allocate_ports
from app.services.replica_readiness import backend_replica_readiness_status_map
from app.services.renderers import (
    SHIELD_BACKEND_NAME,
    SHIELD_PORT,
    container_name,
    nginx_filename,
    render_follower_admin_proxy,
    render_nginx_http,
    render_nginx_shield_closed,
    render_nginx_static,
    safe_slug,
)
from app.services.resource_profile import build_resource_profile
from app.services.tailscale_urls import tailscale_service_url
from app.services.tailscale_admin import current_tailscale_admin_hostnames
from app.services.validators import (
    ValidationError,
    validate_backend_collection,
    validate_input_bindings,
)


logger = get_logger("apply")


def _tailnet_service_route_label(input_value: str, settings: Settings) -> str:
    return (
        tailscale_service_url(input_value, settings)
        or f"tailscale subdomain {input_value}"
    )


def _placement_node_uid(backend: Backend) -> str:
    return backend_single_node_uid(backend)


def _backend_is_local(backend: Backend, *, multi_node_enabled: bool = True) -> bool:
    if not multi_node_enabled:
        return _placement_node_uid(backend) == LOCAL_NODE_UID
    return backend_has_local_runtime(backend)


def _cluster_node_map(nodes: list[ClusterNode]) -> dict[str, ClusterNode]:
    return {node.node_uid: node for node in nodes if not node.removed_at}


def _node_tailnet_ip(node: ClusterNode) -> str:
    return str(node.tailnet_ip or node.wireguard_ip or "").strip()


def _active_cluster_nodes(nodes: list[ClusterNode]) -> list[ClusterNode]:
    return sorted(
        (
            node
            for node in nodes
            if not node.removed_at
            and str(node.state or "").strip().lower() == "healthy"
            and _node_tailnet_ip(node)
        ),
        key=lambda item: (item.name.lower(), item.node_uid),
    )


def _backend_upstream_target_for_node(
    backend: Backend,
    nodes_by_uid: dict[str, ClusterNode],
    node_uid: str,
) -> str:
    if backend.port is None:
        return "127.0.0.1:0"
    if node_uid == LOCAL_NODE_UID:
        return f"127.0.0.1:{backend.port}"
    node = nodes_by_uid.get(node_uid)
    if node is None:
        raise ApplyFailed(
            f"output {backend.name} is placed on an unknown node",
            phase="validate",
            details={"backend": backend.name, "node_uid": node_uid},
        )
    node_ip = _node_tailnet_ip(node)
    if not node_ip:
        raise ApplyFailed(
            f"output {backend.name} is placed on a node without a Tailscale address",
            phase="validate",
            details={"backend": backend.name, "node_uid": node_uid},
        )
    return f"{node_ip}:{backend.port}"


def _target_with_nginx_role(target: str, role: str) -> str:
    return f"{target} {role}" if role else target


def _backend_placement_targets(
    backend: Backend, *, multi_node_enabled: bool = True
) -> tuple[tuple[str, str], ...]:
    if not multi_node_enabled:
        return ((_placement_node_uid(backend), ""),)
    placement = read_backend_placement(backend)
    if not placement.enabled:
        return ((placement.active_node_uid, ""),)
    if placement.mode == PLACEMENT_MODE_FAILOVER:
        targets = [(placement.active_node_uid, "")]
        targets.extend(
            (node_uid, "backup")
            for node_uid in placement.selected_node_uids
            if node_uid != placement.active_node_uid
        )
        return tuple(targets)
    if placement.mode == PLACEMENT_MODE_LIVE:
        return tuple((node_uid, "") for node_uid in placement.selected_node_uids)
    return ((placement.active_node_uid, ""),)


async def _validate_remote_placement_readiness(
    session: AsyncSession,
    settings: Settings,
    backends: list[Backend],
) -> None:
    if not settings.multi_node_enabled:
        return
    failures: list[dict[str, str]] = []
    backend_node_uids: dict[int, tuple[str, ...]] = {}
    for backend in backends:
        if backend.kind != "app" or not backend.enabled:
            continue
        backend_node_uids[int(backend.id)] = tuple(
            node_uid
            for node_uid, _nginx_role in _backend_placement_targets(
                backend, multi_node_enabled=settings.multi_node_enabled
            )
        )
    statuses_by_backend = await backend_replica_readiness_status_map(
        session,
        settings,
        backends=backends,
        backend_node_uids=backend_node_uids,
    )
    for backend in backends:
        if backend.kind != "app" or not backend.enabled:
            continue
        node_uids = backend_node_uids.get(int(backend.id), ())
        statuses = statuses_by_backend.get(int(backend.id), {})
        for node_uid in node_uids:
            status = statuses[node_uid]
            if status.ready:
                continue
            failures.append(
                {
                    "backend": backend.name,
                    "node_uid": node_uid,
                    "reason": status.reason,
                }
            )
    if failures:
        first = failures[0]
        raise ApplyFailed(
            (
                f"remote placement for output {first['backend']} is not ready: "
                f"{first['node_uid']} {first['reason']}"
            ),
            phase="validate_replica_readiness",
            details={"replica_readiness_failures": failures},
        )


def _backend_upstream_targets(
    backend: Backend,
    nodes_by_uid: dict[str, ClusterNode],
    *,
    multi_node_enabled: bool = True,
) -> list[str]:
    return [
        _target_with_nginx_role(
            _backend_upstream_target_for_node(backend, nodes_by_uid, node_uid),
            nginx_role,
        )
        for node_uid, nginx_role in _backend_placement_targets(
            backend, multi_node_enabled=multi_node_enabled
        )
    ]


def _backend_upstream_target(
    backend: Backend,
    nodes_by_uid: dict[str, ClusterNode],
    *,
    multi_node_enabled: bool = True,
) -> str:
    node_uid, _nginx_role = _backend_placement_targets(
        backend, multi_node_enabled=multi_node_enabled
    )[0]
    return _backend_upstream_target_for_node(backend, nodes_by_uid, node_uid)


def _backend_cluster_upstream_target_for_node(
    backend: Backend,
    *,
    leader_tailnet_ip: str,
    nodes_by_uid: dict[str, ClusterNode],
    node_uid: str,
) -> str:
    if backend.port is None:
        return "127.0.0.1:0"
    if node_uid == LOCAL_NODE_UID:
        return f"{leader_tailnet_ip}:80"
    node = nodes_by_uid.get(node_uid)
    if node is None:
        raise ApplyFailed(
            f"output {backend.name} is placed on an unknown node",
            phase="validate",
            details={"backend": backend.name, "node_uid": node_uid},
        )
    node_ip = _node_tailnet_ip(node)
    if not node_ip:
        raise ApplyFailed(
            f"output {backend.name} is placed on a node without a Tailscale address",
            phase="validate",
            details={"backend": backend.name, "node_uid": node_uid},
        )
    # Remote app containers bind their published port to the node Tailscale IP,
    # so follower nginx should use that address even when it runs on the owner.
    return f"{node_ip}:{backend.port}"


def _backend_cluster_upstream_targets(
    backend: Backend,
    *,
    leader_tailnet_ip: str,
    nodes_by_uid: dict[str, ClusterNode],
    multi_node_enabled: bool = True,
) -> list[str]:
    return [
        _target_with_nginx_role(
            _backend_cluster_upstream_target_for_node(
                backend,
                leader_tailnet_ip=leader_tailnet_ip,
                nodes_by_uid=nodes_by_uid,
                node_uid=node_uid,
            ),
            nginx_role,
        )
        for node_uid, nginx_role in _backend_placement_targets(
            backend, multi_node_enabled=multi_node_enabled
        )
    ]


def _cluster_node_payload(nodes: list[ClusterNode]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "node_uid": node.node_uid,
            "name": node.name,
            "tailnet_ip": _node_tailnet_ip(node),
            "state": str(node.state or ""),
        }
        for node in nodes
    )


def _leader_admin_tailnet_host(settings: Settings) -> str:
    candidates: list[str] = []
    if settings.cluster_join_tailnet_base_url:
        candidates.append(settings.cluster_join_tailnet_base_url)
    candidates.extend(
        part.strip()
        for part in str(settings.admin_allowed_hosts or "").replace(",", " ").split()
    )
    for candidate in candidates:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if host.endswith(".ts.net"):
            return host
    hostnames = current_tailscale_admin_hostnames(settings)
    return hostnames[0] if hostnames else ""


def _follower_admin_server_names(
    node: ClusterNode,
    *,
    leader_admin_host: str,
) -> tuple[str, ...]:
    names = [_node_tailnet_ip(node), str(node.name or "").strip().lower().rstrip(".")]
    labels = [part for part in leader_admin_host.split(".") if part]
    if len(labels) >= 3 and names[-1] and "." not in names[-1]:
        names.append(f"{names[-1]}.{'.'.join(labels[-3:])}")
    return tuple(dict.fromkeys(name for name in names if name))


def _static_replica_root(backend: Backend) -> str:
    return f"/var/lib/cnc/static-replicas/{safe_slug(backend.name)}"


def _hash_static_root(static_root: str, *, backend: Backend, hostname: str) -> str:
    root = Path(static_root)
    if not root.exists() or not root.is_dir():
        raise ApplyFailed(
            f"static backend {backend.name} root does not exist: {static_root}",
            phase="render_cluster_followers",
            details={
                "backend": backend.name,
                "hostname": hostname,
                "static_root": static_root,
            },
        )
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() or item.is_symlink()
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink:")
            digest.update(path.readlink().as_posix().encode("utf-8"))
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _render_route_target(
    input_kind: str,
    input_value: str,
    attached_backends: list[Backend],
    settings: Settings,
    nodes_by_uid: dict[str, ClusterNode],
) -> str:
    if not attached_backends:
        return "no outputs attached"
    primary_backend = attached_backends[0]
    if primary_backend.kind == "shield":
        return f"{input_value} -> Shield access gate"
    if primary_backend.kind == "app":
        if input_kind == "tailnet_path":
            return (
                f"tailnet {input_value} -> http://"
                f"{_backend_upstream_target(primary_backend, nodes_by_uid, multi_node_enabled=settings.multi_node_enabled)}"
            )
        if input_kind == "tailnet_service":
            return (
                f"{_tailnet_service_route_label(input_value, settings)} -> http://"
                f"{_backend_upstream_target(primary_backend, nodes_by_uid, multi_node_enabled=settings.multi_node_enabled)}"
            )
        return ", ".join(
            f"http://{_backend_upstream_target(backend, nodes_by_uid, multi_node_enabled=settings.multi_node_enabled)}"
            for backend in attached_backends
            if backend.port is not None
        )
    if input_kind == "tailnet_path":
        return f"tailnet {input_value} -> {primary_backend.static_root or '-'}"
    if input_kind == "tailnet_service":
        return f"{_tailnet_service_route_label(input_value, settings)} -> {primary_backend.static_root or '-'}"
    return primary_backend.static_root or "-"


def _static_tailnet_target(backend: Backend, input_value: str) -> str:
    static_root = str(backend.static_root or "").strip()
    if not static_root:
        raise ApplyFailed(
            f"static output {backend.name} requires a static root before tailnet route {input_value} can be served",
            phase="validate",
            details={"backend": backend.name, "input": input_value},
        )
    if not Path(static_root).exists():
        raise ApplyFailed(
            f"static output {backend.name} root does not exist: {static_root}",
            phase="validate",
            details={
                "backend": backend.name,
                "input": input_value,
                "static_root": static_root,
            },
        )
    return static_root


def _shield_input_route_key(item: Input) -> str:
    input_id = getattr(item, "id", None)
    if input_id is not None:
        return f"input:{input_id}"
    return f"input:{str(item.hostname or '').strip().lower()[:120]}"


def _shielded_app_backend(attached_backends: list[Backend]) -> Backend | None:
    for backend in attached_backends:
        if backend.kind == "app" and bool(getattr(backend, "shield_enabled", False)):
            return backend
    return None


def _route_shield_config(
    *,
    item: Input,
    attached_backends: list[Backend],
    shield_ready: bool,
) -> tuple[bool, str, str | None]:
    input_kind = str(item.kind or "domain").strip().lower() or "domain"
    if input_kind == "domain" and bool(getattr(item, "shield_enabled", False)):
        if not shield_ready:
            raise ApplyFailed(
                f"shielded input {item.hostname} requires an enabled shield output with a shield input",
                phase="validate",
                details={"input": item.hostname},
            )
        if not getattr(item, "shield_code_hash", None):
            raise ApplyFailed(
                f"shielded input {item.hostname} requires an access code",
                phase="validate",
                details={"input": item.hostname},
            )
        return True, _shield_input_route_key(item), str(item.shield_code_hash)

    fallback_backend = _shielded_app_backend(attached_backends)
    if fallback_backend is None:
        return False, "global", None
    if not shield_ready:
        raise ApplyFailed(
            f"shielded output {fallback_backend.name} requires an enabled shield output with a shield input",
            phase="validate",
            details={"backend": fallback_backend.name, "hostname": item.hostname},
        )
    if not fallback_backend.shield_code_hash:
        raise ApplyFailed(
            f"shielded output {fallback_backend.name} requires an access code",
            phase="validate",
            details={"backend": fallback_backend.name, "hostname": item.hostname},
        )
    return True, fallback_backend.name, fallback_backend.shield_code_hash


def _route_contracts(
    inputs: list[Input],
    hostname_to_backends: dict[str, list[Backend]],
    tailscale_path_to_backends: dict[str, list[Backend]],
    tailscale_service_to_backends: dict[str, list[Backend]],
    settings: Settings,
    nodes_by_uid: dict[str, ClusterNode],
) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = []
    for item in inputs:
        input_kind = str(item.kind or "domain").strip().lower() or "domain"
        input_value = str(item.hostname or "").strip()
        attached_backends = (
            hostname_to_backends.get(input_value, [])
            if input_kind in {"domain", "shield"}
            else (
                tailscale_path_to_backends.get(input_value, [])
                if input_kind == "tailnet_path"
                else tailscale_service_to_backends.get(input_value, [])
            )
        )
        contracts.append(
            {
                "input_kind": input_kind,
                "input_value": input_value,
                "enabled": bool(item.enabled),
                "backend_names": [backend.name for backend in attached_backends],
                "target": _render_route_target(
                    input_kind, input_value, attached_backends, settings, nodes_by_uid
                ),
            }
        )
    return sorted(
        contracts, key=lambda item: (str(item["input_kind"]), str(item["input_value"]))
    )


def _backend_contracts(backends: list[Backend]) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = []
    for backend in sorted(backends, key=lambda item: item.name):
        placement = read_backend_placement(backend)
        contracts.append(
            {
                "backend": backend.name,
                "kind": backend.kind,
                "enabled": bool(backend.enabled),
                "port": backend.port,
                "handoff_port": backend.handoff_port,
                "sandbox_profile": backend.sandbox_profile or "",
                "hardening": read_hardening_policy(backend).model_dump(
                    exclude_defaults=True
                ),
                "healthcheck_path": backend.healthcheck_path or "",
                "placement_node_uid": _placement_node_uid(backend),
                "placement_mode": placement.mode,
                "placement_active_node_uid": placement.active_node_uid,
                "placement_node_uids": list(placement.selected_node_uids),
                "inter_app_interfaces": [
                    {
                        "name": item.name,
                        "target_backend_id": item.target_backend_id,
                        "status": item.status,
                        "direction": item.direction,
                    }
                    for item in read_inter_app_interfaces(backend)
                ],
            }
        )
    return contracts


def _runtime_interface_source_contract(
    item: RuntimeInterAppInterface,
) -> dict[str, object]:
    return {
        "name": item.name,
        "target_backend": item.target_backend,
        "target_handoff_port": item.target_handoff_port,
        "env_key": item.env_key,
        "url": item.url,
        "network": item.network,
        "direction": item.direction,
    }


def _runtime_interface_network_contract(
    item: RuntimeInterAppInterface,
    *,
    backend_name: str,
) -> dict[str, object]:
    return {
        "network": item.network,
        "source_backend": item.source_backend,
        "target_backend": item.target_backend,
        "alias": item.name if item.target_backend == backend_name else "",
        "role": "source" if item.source_backend == backend_name else "target",
        "direction": item.direction,
    }


def _runtime_graph(
    *,
    backends: list[Backend],
    resource_profile,
    route_contracts: list[dict[str, object]],
    backend_contracts: list[dict[str, object]],
    settings: Settings,
) -> RuntimeGraph:
    app_backends: dict[str, RuntimeAppBackend] = {}
    local_app_backends = sorted(
        (
            item
            for item in backends
            if item.kind == "app"
            and _backend_is_local(item, multi_node_enabled=settings.multi_node_enabled)
        ),
        key=lambda item: item.name,
    )
    local_app_names = {backend.name for backend in local_app_backends}
    inter_app_interfaces = build_runtime_inter_app_interfaces(
        backends,
        local_backend_names=local_app_names,
    )
    for backend in local_app_backends:
        related_interfaces = tuple(
            item
            for item in inter_app_interfaces
            if item.source_backend == backend.name
            or item.target_backend == backend.name
        )
        source_interfaces = tuple(
            _runtime_interface_source_contract(item)
            for item in related_interfaces
            if item.source_backend == backend.name
        )
        interface_networks = tuple(
            _runtime_interface_network_contract(item, backend_name=backend.name)
            for item in related_interfaces
        )
        spec = runtime_spec(backend, settings, base_profile=resource_profile)
        app_backends[backend.name] = RuntimeAppBackend(
            backend=backend.name,
            enabled=bool(backend.enabled),
            container=container_name(backend.name),
            network=str(spec["network"]),
            port=int(backend.port) if backend.port is not None else None,
            handoff_port=int(backend.handoff_port),
            sandbox_profile=str(spec["sandbox_profile"]),
            hardening=dict(spec["hardening"]),
            sandbox_dir=str(spec["sandbox_dir"]),
            guest_rootfs=str(spec["guest_rootfs"]),
            volumes=tuple(str(volume) for volume in spec.get("volumes", [])),
            dns_servers=tuple(str(server) for server in spec.get("dns_servers", [])),
            resource_mode=str(spec["resource_mode"]),
            resource_size=str(spec["resource_size"]),
            memory_high=str(spec["memory_high"]),
            memory_max=str(spec["memory_max"]),
            cpu_quota=str(spec["cpu_quota"]),
            cpu_shares=int(spec.get("cpu_shares") or 1024),
            healthcheck_mode=display_backend_healthcheck_mode(backend),
            healthcheck_path=str(backend.healthcheck_path or "/"),
            healthcheck_host_header=str(backend.healthcheck_host_header or ""),
            inter_app_interfaces=source_interfaces,
            interface_networks=interface_networks,
        )
    return RuntimeGraph(
        app_backends=app_backends,
        route_contracts=tuple(dict(item) for item in route_contracts),
        backend_contracts=tuple(dict(item) for item in backend_contracts),
        inter_app_interfaces=inter_app_interfaces,
    )


def _build_cluster_follower_ingress(
    *,
    settings: Settings,
    cluster_nodes: list[ClusterNode],
    hostname_to_backends: dict[str, list[Backend]],
    hostname_to_input: dict[str, Input],
    shield_ready: bool,
    nodes_by_uid: dict[str, ClusterNode],
) -> tuple[
    tuple[dict[str, str], ...],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    if not settings.multi_node_enabled:
        return (), {}, {}, {}

    active_nodes = _active_cluster_nodes(cluster_nodes)
    if not active_nodes:
        return (), {}, {}, {}

    leader_ip = ""
    if hostname_to_backends:
        try:
            leader_ip = leader_tailnet_ip(settings)
        except ValueError as exc:
            raise ApplyFailed(
                "leader Tailscale address is required before rendering follower ingress",
                phase="render_cluster_followers",
                details={"error": str(exc)},
            ) from exc

    static_replicas: dict[str, dict[str, str]] = {}
    app_node_files: dict[str, dict[str, str]] = {}
    admin_node_files: dict[str, dict[str, str]] = {}
    shield_target = f"{leader_ip}:{SHIELD_PORT}" if leader_ip else ""
    leader_admin_host = _leader_admin_tailnet_host(settings)
    for node in active_nodes:
        app_files: dict[str, str] = {}
        admin_files: dict[str, str] = {}
        if leader_admin_host:
            admin_files["cnc-admin-proxy.conf"] = render_follower_admin_proxy(
                leader_admin_host,
                node_uid=node.node_uid,
                server_names=_follower_admin_server_names(
                    node,
                    leader_admin_host=leader_admin_host,
                ),
            )
        for hostname, attached_backends in hostname_to_backends.items():
            filename = nginx_filename(hostname)
            primary_backend = attached_backends[0]
            if primary_backend.kind == "app":
                route_input = hostname_to_input.get(hostname)
                shield_enabled, shield_route_key, _shield_code_hash = (
                    _route_shield_config(
                        item=route_input
                        or Input(kind="domain", hostname=hostname, enabled=True),
                        attached_backends=attached_backends,
                        shield_ready=shield_ready,
                    )
                )
                app_files[filename] = render_nginx_http(
                    hostname,
                    [
                        target
                        for backend in attached_backends
                        for target in _backend_cluster_upstream_targets(
                            backend,
                            leader_tailnet_ip=leader_ip,
                            nodes_by_uid=nodes_by_uid,
                            multi_node_enabled=settings.multi_node_enabled,
                        )
                        if backend.port is not None
                    ],
                    cloudflare_only=settings.nginx_cloudflare_only,
                    cloudflare_ips=settings.cloudflare_ip_list,
                    shield_enabled=shield_enabled,
                    shield_port=SHIELD_PORT,
                    shield_target=shield_target,
                    shield_output_key=shield_route_key,
                )
            elif primary_backend.kind == "shield":
                app_files[filename] = render_nginx_shield_closed(
                    hostname,
                    cloudflare_only=settings.nginx_cloudflare_only,
                    cloudflare_ips=settings.cloudflare_ip_list,
                )
            else:
                static_root = str(primary_backend.static_root or "").strip()
                if not static_root:
                    raise ApplyFailed(
                        f"static backend {primary_backend.name} missing static_root",
                        phase="render_cluster_followers",
                        details={"backend": primary_backend.name, "hostname": hostname},
                    )
                route_input = hostname_to_input.get(hostname)
                shield_enabled, shield_route_key, _shield_code_hash = (
                    _route_shield_config(
                        item=route_input
                        or Input(kind="domain", hostname=hostname, enabled=True),
                        attached_backends=attached_backends,
                        shield_ready=shield_ready,
                    )
                )
                content_hash = _hash_static_root(
                    static_root,
                    backend=primary_backend,
                    hostname=hostname,
                )
                replica_root = _static_replica_root(primary_backend)
                static_replicas[primary_backend.name] = {
                    "backend": primary_backend.name,
                    "source_root": static_root,
                    "replica_root": replica_root,
                    "content_hash": content_hash,
                }
                app_files[filename] = render_nginx_static(
                    hostname,
                    replica_root,
                    cloudflare_only=settings.nginx_cloudflare_only,
                    cloudflare_ips=settings.cloudflare_ip_list,
                    shield_enabled=shield_enabled,
                    shield_port=SHIELD_PORT,
                    shield_target=shield_target,
                    shield_output_key=shield_route_key,
                )
        app_node_files[node.node_uid] = app_files
        admin_node_files[node.node_uid] = admin_files

    return (
        _cluster_node_payload(active_nodes),
        app_node_files,
        admin_node_files,
        static_replicas,
    )


async def build_desired_state(
    session: AsyncSession, settings: Settings
) -> DesiredState:
    backends = (
        (await session.execute(select(Backend).options(selectinload(Backend.inputs))))
        .scalars()
        .all()
    )
    inputs = (
        (
            await session.execute(
                select(Input)
                .options(selectinload(Input.backends))
                .order_by(Input.id.asc())
            )
        )
        .scalars()
        .all()
    )
    cluster_nodes = (
        (
            await session.execute(
                select(ClusterNode).where(ClusterNode.removed_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    nodes_by_uid = _cluster_node_map(list(cluster_nodes))
    active_cluster_nodes = (
        _active_cluster_nodes(list(cluster_nodes))
        if settings.multi_node_enabled
        else []
    )
    ingress_allow_ips = settings.cloudflare_ip_list + tuple(
        _node_tailnet_ip(node) for node in active_cluster_nodes
    )

    enabled_backends = [backend for backend in backends if backend.enabled]
    enabled_runtime_backends = [
        backend
        for backend in enabled_backends
        if backend.kind == "app"
        and _backend_is_local(backend, multi_node_enabled=settings.multi_node_enabled)
    ]
    resource_profile = build_resource_profile(settings, enabled_runtime_backends)
    logger.info(
        "apply.resource_profile.computed",
        mode=resource_profile.mode,
        backend_count=resource_profile.backend_count,
        host_cpu_count=resource_profile.host_cpu_count,
        host_memory_bytes=resource_profile.host_memory_bytes,
        memory_high=resource_profile.memory_high,
        memory_max=resource_profile.memory_max,
        cpu_quota=resource_profile.cpu_quota,
    )

    try:
        validate_backend_collection(backends)
    except ValidationError as exc:
        raise ApplyFailed(str(exc), phase="validate") from exc
    for backend in backends:
        logger.info(
            "apply.backend.validated",
            backend_id=backend.id,
            backend_name=backend.name,
            kind=backend.kind,
            enabled=backend.enabled,
            port=backend.port,
        )

    runtime_backends = [backend for backend in backends if backend.kind == "app"]
    ports_before_allocation = {backend.id: backend.port for backend in runtime_backends}

    try:
        allocate_ports(
            runtime_backends, settings.port_range_start, settings.port_range_end
        )
    except PortAllocationError as exc:
        raise ApplyFailed(str(exc), phase="allocate_ports") from exc

    if any(
        backend.port != ports_before_allocation.get(backend.id)
        for backend in runtime_backends
    ):
        await session.flush()

    await _validate_remote_placement_readiness(session, settings, backends)

    try:
        validated_bindings = validate_input_bindings(inputs)
    except ValidationError as exc:
        raise ApplyFailed(str(exc), phase="validate") from exc

    hostname_to_backends: dict[str, list[Backend]] = {}
    hostname_to_input: dict[str, Input] = {}
    tailscale_path_to_backends: dict[str, list[Backend]] = {}
    tailscale_service_to_backends: dict[str, list[Backend]] = {}
    for binding in validated_bindings:
        if binding.input_kind in {"domain", "shield"}:
            hostname_to_backends[binding.input_value] = (
                binding.attached_enabled_backends
            )
            matching_input = next(
                (
                    item
                    for item in inputs
                    if str(item.kind or "domain").strip().lower() == binding.input_kind
                    and str(item.hostname or "").strip().lower() == binding.input_value
                ),
                None,
            )
            if matching_input is not None:
                hostname_to_input[binding.input_value] = matching_input
        elif binding.input_kind == "tailnet_path":
            tailscale_path_to_backends[binding.input_value] = (
                binding.attached_enabled_backends
            )
        elif binding.input_kind == "tailnet_service":
            tailscale_service_to_backends[binding.input_value] = (
                binding.attached_enabled_backends
            )

    shield_ready = _shield_prerequisites_ready(inputs)
    shield_output_code_hashes: dict[str, str] = {}
    nginx_files: dict[str, str] = {}
    for hostname, attached_backends in hostname_to_backends.items():
        filename = nginx_filename(hostname)
        primary_backend = attached_backends[0]
        if primary_backend.kind == "app":
            route_input = hostname_to_input.get(hostname)
            shield_enabled, shield_route_key, shield_code_hash = _route_shield_config(
                item=route_input
                or Input(kind="domain", hostname=hostname, enabled=True),
                attached_backends=attached_backends,
                shield_ready=shield_ready,
            )
            if shield_enabled and shield_code_hash:
                shield_output_code_hashes[shield_route_key] = shield_code_hash
            nginx_files[filename] = render_nginx_http(
                hostname,
                [
                    target
                    for backend in attached_backends
                    for target in _backend_upstream_targets(
                        backend,
                        nodes_by_uid,
                        multi_node_enabled=settings.multi_node_enabled,
                    )
                    if backend.port is not None
                ],
                cloudflare_only=settings.nginx_cloudflare_only,
                cloudflare_ips=ingress_allow_ips,
                shield_enabled=shield_enabled,
                shield_port=SHIELD_PORT,
                shield_output_key=shield_route_key,
            )
        elif primary_backend.kind == "shield":
            if len(attached_backends) != 1:
                raise ApplyFailed(
                    f"shield input {hostname} must attach exactly one shield output",
                    phase="validate",
                    details={"hostname": hostname},
                )
            nginx_files[filename] = render_nginx_shield_closed(
                hostname,
                cloudflare_only=settings.nginx_cloudflare_only,
                cloudflare_ips=ingress_allow_ips,
            )
        else:
            if not primary_backend.static_root:
                raise ApplyFailed(
                    f"static backend {primary_backend.name} missing static_root",
                    phase="render_nginx",
                    details={"backend": primary_backend.name, "hostname": hostname},
                )
            route_input = hostname_to_input.get(hostname)
            shield_enabled, shield_route_key, shield_code_hash = _route_shield_config(
                item=route_input
                or Input(kind="domain", hostname=hostname, enabled=True),
                attached_backends=attached_backends,
                shield_ready=shield_ready,
            )
            if shield_enabled and shield_code_hash:
                shield_output_code_hashes[shield_route_key] = shield_code_hash
            nginx_files[filename] = render_nginx_static(
                hostname,
                primary_backend.static_root,
                cloudflare_only=settings.nginx_cloudflare_only,
                cloudflare_ips=ingress_allow_ips,
                shield_enabled=shield_enabled,
                shield_port=SHIELD_PORT,
                shield_output_key=shield_route_key,
            )

    (
        cluster_node_payload,
        cluster_node_app_nginx_files,
        cluster_node_admin_nginx_files,
        static_replicas,
    ) = _build_cluster_follower_ingress(
        settings=settings,
        cluster_nodes=list(cluster_nodes),
        hostname_to_backends=hostname_to_backends,
        hostname_to_input=hostname_to_input,
        shield_ready=shield_ready,
        nodes_by_uid=nodes_by_uid,
    )

    tailscale_paths: dict[str, str] = {}
    for path, attached_backends in tailscale_path_to_backends.items():
        primary_backend = attached_backends[0]
        if primary_backend.kind == "app":
            tailscale_paths[path] = (
                f"http://{_backend_upstream_target(primary_backend, nodes_by_uid, multi_node_enabled=settings.multi_node_enabled)}"
            )
        else:
            tailscale_paths[path] = _static_tailnet_target(primary_backend, path)

    tailscale_services: dict[str, str] = {}
    for service, attached_backends in tailscale_service_to_backends.items():
        primary_backend = attached_backends[0]
        if primary_backend.kind == "app":
            tailscale_services[service] = (
                f"http://{_backend_upstream_target(primary_backend, nodes_by_uid, multi_node_enabled=settings.multi_node_enabled)}"
            )
        else:
            tailscale_services[service] = _static_tailnet_target(
                primary_backend, service
            )
    if settings.netdata_enabled:
        tailscale_services[NETDATA_SERVICE_NAME] = (
            f"http://127.0.0.1:{settings.netdata_port}"
        )

    enabled_app_backends: list[Backend] = []
    known_app_backends: list[Backend] = []
    for backend in backends:
        if backend.kind == "app":
            if not _backend_is_local(
                backend, multi_node_enabled=settings.multi_node_enabled
            ):
                continue
            known_app_backends.append(backend)
            if backend.enabled:
                enabled_app_backends.append(backend)

    route_contracts = _route_contracts(
        inputs,
        hostname_to_backends,
        tailscale_path_to_backends,
        tailscale_service_to_backends,
        settings,
        nodes_by_uid,
    )
    backend_contracts = _backend_contracts(backends)

    return DesiredState(
        nginx_files=nginx_files,
        tailscale_paths=tailscale_paths,
        tailscale_services=tailscale_services,
        enabled_app_backends=enabled_app_backends,
        known_app_backends=known_app_backends,
        resource_profile=resource_profile,
        route_contracts=route_contracts,
        backend_contracts=backend_contracts,
        shield_required=shield_ready or bool(shield_output_code_hashes),
        shield_output_code_hashes=shield_output_code_hashes,
        netdata_required=bool(settings.netdata_enabled),
        runtime_graph=_runtime_graph(
            backends=backends,
            resource_profile=resource_profile,
            route_contracts=route_contracts,
            backend_contracts=backend_contracts,
            settings=settings,
        ),
        cluster_nodes=cluster_node_payload,
        cluster_node_app_nginx_files=cluster_node_app_nginx_files,
        cluster_node_admin_nginx_files=cluster_node_admin_nginx_files,
        static_replicas=static_replicas,
    )


def _shield_prerequisites_ready(inputs: list[Input]) -> bool:
    for item in inputs:
        if not item.enabled:
            continue
        if str(item.kind or "domain").strip().lower() != "shield":
            continue
        enabled_backends = [backend for backend in item.backends if backend.enabled]
        if (
            len(enabled_backends) == 1
            and enabled_backends[0].kind == "shield"
            and (enabled_backends[0].name == SHIELD_BACKEND_NAME)
        ):
            return True
    return False
