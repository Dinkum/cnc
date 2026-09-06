from __future__ import annotations

import os
from pathlib import Path
import shlex
import time
from typing import Any

from app.services.hardening_policy import (
    hardening_podman_args,
    hardening_volumes,
    read_hardening_policy,
)
from app.config import Settings
from app.logger import get_logger
from app.models.entities import Backend
from app.services.app_containers import (
    app_sandbox_rootfs_path,
    configured_app_dns_servers,
)
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.resource_profile import ResourceProfile, backend_resource_profile
from app.services.renderers import container_name, network_name
from app.services.validators import parse_volumes_json
from app.services.sandbox_profiles import get_app_sandbox_profile


QUADLET_HEADER = "# Managed by CNC. Do not edit by hand."
QUADLET_WRITE_OPERATOR_HINT = (
    "The service running host apply must be able to write app_quadlet_dir in its "
    "active mount namespace; check systemctl cat cnc-admin cnc-auto-size "
    "cnc-backend-alerts cnc-cloudflare-sync for ProtectSystem and ReadWritePaths "
    "drift, then restart or rerun the affected service after correcting the unit"
)
logger = get_logger("apply.quadlet")
_LIVE_RESOURCE_PODMAN_ARG_PREFIXES = (
    "--cpu-shares=",
    "--cpus=",
)


class AppQuadletWriteError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


def quadlet_container_unit_name(backend_name: str) -> str:
    return f"{container_name(backend_name)}.container"


def quadlet_container_service_name(backend_name: str) -> str:
    return f"{container_name(backend_name)}.service"


def quadlet_network_unit_name(backend_name: str) -> str:
    return f"{network_name(backend_name)}.network"


def quadlet_network_service_name(backend_name: str) -> str:
    return f"{network_name(backend_name)}-network.service"


def quadlet_interface_network_unit_name(network: str) -> str:
    return f"{network}.network"


def quadlet_interface_network_service_name(network: str) -> str:
    return f"{network}-network.service"


def quadlet_container_path(settings: Settings, backend_name: str) -> Path:
    return settings.app_quadlet_dir / quadlet_container_unit_name(backend_name)


def quadlet_network_path(settings: Settings, backend_name: str) -> Path:
    return settings.app_quadlet_dir / quadlet_network_unit_name(backend_name)


def quadlet_interface_network_path(settings: Settings, network: str) -> Path:
    return settings.app_quadlet_dir / quadlet_interface_network_unit_name(network)


def _cpu_quota_to_cpus(raw: str, host_cpu_count: int | None) -> str:
    normalized = str(raw or "").strip()
    if normalized.endswith("%"):
        normalized = normalized[:-1]
    quota_percent = float(normalized)
    host_cpus = (
        host_cpu_count if isinstance(host_cpu_count, int) and host_cpu_count > 0 else 1
    )
    cpus = (quota_percent / 100.0) * host_cpus
    return f"{cpus:.2f}".rstrip("0").rstrip(".")


def _healthcheck_command(backend: Backend) -> str | None:
    mode = str(backend.healthcheck_mode or "http").strip().lower() or "http"
    if mode == "auto":
        mode = "http"
    if mode == "none":
        return None
    if mode == "tcp":
        return (
            f"/usr/bin/timeout 3 /bin/bash -c "
            f"'</dev/tcp/127.0.0.1/{backend.handoff_port}'"
        )
    path = str(backend.healthcheck_path or "/").strip() or "/"
    host_header = str(backend.healthcheck_host_header or "").strip()
    url = f"http://127.0.0.1:{backend.handoff_port}{path}".replace("%", "%%")
    command = f"/usr/bin/curl -fsS --max-time 3 {shlex.quote(url)}"
    if host_header:
        command = (
            f"/usr/bin/curl -fsS --max-time 3 -H {shlex.quote(f'Host: {host_header}'.replace('%', '%%'))} "
            f"{shlex.quote(url)}"
        )
    return command


def render_quadlet_network(backend: Backend) -> str:
    network = network_name(backend.name)
    lines = [
        QUADLET_HEADER,
        "",
        "[Unit]",
        f"Description=CNC app network {backend.name}",
        "",
        "[Network]",
        f"NetworkName={network}",
        "Driver=bridge",
        "DisableDNS=true",
        "Label=io.cnc.managed=true",
        f"Label=io.cnc.backend={backend.name}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_quadlet_interface_network(link: RuntimeInterAppInterface) -> str:
    lines = [
        QUADLET_HEADER,
        "",
        "[Unit]",
        f"Description=CNC app interface {link.source_backend}:{link.name}",
        "",
        "[Network]",
        f"NetworkName={link.network}",
        "Driver=bridge",
        "Label=io.cnc.managed=true",
        "Label=io.cnc.kind=interface",
        f"Label=io.cnc.interface={link.source_backend}:{link.name}",
        f"Label=io.cnc.source_backend={link.source_backend}",
        f"Label=io.cnc.target_backend={link.target_backend}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def _network_line(unit_name: str, aliases: tuple[str, ...] = ()) -> str:
    if not aliases:
        return f"Network={unit_name}"
    options = ",".join(f"alias={alias}" for alias in aliases)
    return f"Network={unit_name}:{options}"


def render_quadlet_container(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
    rootfs_path: Path | str | None = None,
    publish_host: str = "127.0.0.1",
) -> str:
    resource_profile = backend_resource_profile(backend, base_profile)
    sandbox_profile = get_app_sandbox_profile(backend.sandbox_profile)
    container = container_name(backend.name)
    network_unit = quadlet_network_unit_name(backend.name)
    source_interfaces = tuple(
        item for item in inter_app_interfaces if item.source_backend == backend.name
    )
    target_interfaces = tuple(
        item for item in inter_app_interfaces if item.target_backend == backend.name
    )
    if backend.port is None:
        raise ValueError(f"app backend {backend.name} requires allocated port")
    rootfs = rootfs_path or app_sandbox_rootfs_path(settings, backend.name)
    publish_address = str(publish_host or "127.0.0.1").strip() or "127.0.0.1"

    policy = read_hardening_policy(backend)
    podman_args = [
        "--systemd=always",
        *hardening_podman_args(policy),
        f"--cpu-shares={resource_profile.cpu_shares or 1024}",
        f"--cpus={_cpu_quota_to_cpus(resource_profile.cpu_quota, resource_profile.host_cpu_count)}",
    ]
    lines = [
        QUADLET_HEADER,
        "",
        "[Unit]",
        f"Description=CNC app backend {backend.name}",
        "",
        "[Container]",
        f"ContainerName={container}",
        f"HostName={container}",
        f"Rootfs={rootfs}",
        _network_line(network_unit),
        *(
            _network_line(
                quadlet_interface_network_unit_name(link.network),
                aliases=(link.name,),
            )
            for link in sorted(target_interfaces, key=lambda item: item.network)
        ),
        *(
            _network_line(quadlet_interface_network_unit_name(link.network))
            for link in sorted(source_interfaces, key=lambda item: item.network)
        ),
        "LogDriver=journald",
        f"PublishPort={publish_address}:{backend.port}:{backend.handoff_port}/tcp",
        "Label=io.cnc.managed=true",
        f"Label=io.cnc.backend={backend.name}",
        f"Exec={' '.join(sandbox_profile.init_command)}",
    ]
    for link in sorted(source_interfaces, key=lambda item: item.name):
        lines.append(f"Environment={link.env_key}={link.url}")
    for dns_server in configured_app_dns_servers(settings):
        lines.append(f"DNS={dns_server}")
    for volume in hardening_volumes(policy, parse_volumes_json(backend.volumes_json)):
        lines.append(f"Volume={volume}")
    health_cmd = _healthcheck_command(backend)
    if health_cmd:
        lines.extend(
            [
                f"HealthCmd={health_cmd}",
                "HealthInterval=30s",
                "HealthTimeout=5s",
                "HealthRetries=3",
                "HealthStartPeriod=30s",
            ]
        )
    lines.extend(
        [
            f"PodmanArgs={' '.join(podman_args)}",
            "",
            "[Service]",
            "Slice=cnc-apps.slice",
            f"MemoryHigh={resource_profile.memory_high}",
            f"MemoryMax={resource_profile.memory_max}",
            "Restart=always",
            "RestartSec=5",
            "KillMode=mixed",
            "TimeoutStopSec=60",
            f"TimeoutStartSec={max(90, int(settings.command_timeout_apply_sec))}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    return "\n".join(lines)


def quadlet_container_policy_content(content: str) -> str:
    """Return the restart-requiring portion of a generated container Quadlet."""
    policy_lines: list[str] = []
    for line in content.splitlines():
        if line.startswith(("MemoryHigh=", "MemoryMax=")):
            # systemd applies these service properties live; the same values stay
            # in Quadlet so restarts and boots retain CNC's desired policy.
            continue
        if not line.startswith("PodmanArgs="):
            policy_lines.append(line)
            continue
        args = shlex.split(line.removeprefix("PodmanArgs="))
        stable_args = [
            arg
            for arg in args
            if not arg.startswith(_LIVE_RESOURCE_PODMAN_ARG_PREFIXES)
        ]
        if stable_args:
            policy_lines.append(f"PodmanArgs={shlex.join(stable_args)}")
    return "\n".join(policy_lines) + "\n"


def _path_diagnostics(path: Path) -> dict[str, Any]:
    try:
        stat_result = path.stat()
    except OSError as exc:
        return {
            "path": str(path),
            "exists": False,
            "stat_error": str(exc),
            "errno": exc.errno,
        }
    return {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "mode": oct(stat_result.st_mode & 0o777),
        "uid": stat_result.st_uid,
        "gid": stat_result.st_gid,
    }


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mountinfo_diagnostics(path: Path) -> dict[str, Any]:
    mountinfo_path = Path("/proc/self/mountinfo")
    if not mountinfo_path.exists():
        return {"available": False, "reason": "proc_self_mountinfo_missing"}
    target = str(path.expanduser().absolute())
    best: dict[str, Any] | None = None
    try:
        lines = mountinfo_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as exc:
        return {"available": False, "error": str(exc), "errno": exc.errno}

    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            continue
        mount_point = _decode_mountinfo_path(left_fields[4])
        normalized_mount = mount_point.rstrip("/") or "/"
        if normalized_mount == "/":
            matches_target = target.startswith("/")
        else:
            matches_target = target == normalized_mount or target.startswith(
                f"{normalized_mount}/"
            )
        if not matches_target:
            continue
        options = left_fields[5].split(",") if left_fields[5] else []
        candidate = {
            "available": True,
            "mount_point": mount_point,
            "filesystem": right_fields[0],
            "source": right_fields[1],
            "options": options,
            "super_options": right_fields[2].split(",") if right_fields[2] else [],
            "readonly": "ro" in options,
        }
        if best is None or len(mount_point) > len(str(best.get("mount_point") or "")):
            best = candidate

    return best or {"available": True, "matched": False, "target": target}


def _process_cgroups() -> list[str]:
    try:
        return (
            Path("/proc/self/cgroup")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return []


def _process_systemd_unit(cgroups: list[str]) -> str:
    for line in cgroups:
        _, _, cgroup_path = line.partition("::")
        if not cgroup_path:
            _, _, cgroup_path = line.rpartition(":")
        for part in reversed(cgroup_path.split("/")):
            if part.endswith((".service", ".timer", ".scope")):
                return part
    return ""


def _process_diagnostics() -> dict[str, Any]:
    cgroups = _process_cgroups()
    return {
        "pid": os.getpid(),
        "euid": os.geteuid(),
        "egid": os.getegid(),
        "systemd_unit": _process_systemd_unit(cgroups),
        "cgroups": cgroups,
    }


def _quadlet_write_error_details(
    *,
    operation: str,
    path: Path,
    temp_path: Path,
    exc: OSError,
    backend_name: str | None = None,
    asset: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "reason": "quadlet_write_failed",
        "operation": operation,
        "path": str(path),
        "temp_path": str(temp_path),
        "quadlet_dir": str(path.parent),
        "errno": exc.errno,
        "error": str(exc),
        "directory": _path_diagnostics(path.parent),
        "mount": _mountinfo_diagnostics(path.parent),
        "process": _process_diagnostics(),
        "operator_hint": QUADLET_WRITE_OPERATOR_HINT,
    }
    if backend_name is not None:
        details["backend"] = backend_name
    if asset is not None:
        details["asset"] = asset
    return details


def verify_app_quadlet_dir_writable(settings: Settings) -> dict[str, Any]:
    directory = settings.app_quadlet_dir
    probe_path = directory / f".cnc-write-test.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe_path.write_bytes(b"")
        probe_path.chmod(0o644)
    except OSError as exc:
        details = _quadlet_write_error_details(
            operation="preflight",
            path=directory / ".cnc-write-test.tmp",
            temp_path=probe_path,
            exc=exc,
        )
        logger.error("app.quadlet.preflight_failed", **details)
        raise AppQuadletWriteError(
            "app Quadlet directory is not writable", details=details
        ) from exc
    finally:
        try:
            probe_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "app.quadlet.preflight_cleanup_failed",
                path=str(probe_path),
                errno=exc.errno,
                error=str(exc),
            )

    return {
        "quadlet_dir": str(directory),
        "writable": True,
        "mount": _mountinfo_diagnostics(directory),
        "process": _process_diagnostics(),
    }


def _write_text_atomic(
    path: Path,
    content: str,
    *,
    backend_name: str,
    asset: str,
) -> bool:
    encoded = content.encode("utf-8")
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        if path.exists() and path.read_bytes() == encoded:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(encoded)
        temp_path.chmod(0o644)
        temp_path.replace(path)
    except OSError as exc:
        details = _quadlet_write_error_details(
            operation="write",
            backend_name=backend_name,
            asset=asset,
            path=path,
            temp_path=temp_path,
            exc=exc,
        )
        logger.error("app.quadlet.write_failed", **details)
        raise AppQuadletWriteError(
            f"failed to write {asset} Quadlet asset for app backend {backend_name}: {exc}",
            details=details,
        ) from exc
    return True


def write_app_quadlet_assets(
    backend: Backend,
    settings: Settings,
    *,
    base_profile: ResourceProfile,
    inter_app_interfaces: tuple[RuntimeInterAppInterface, ...] = (),
) -> dict[str, Any]:
    network_path = quadlet_network_path(settings, backend.name)
    container_path = quadlet_container_path(settings, backend.name)
    changed_files: list[str] = []
    related_interfaces = tuple(
        item
        for item in inter_app_interfaces
        if item.source_backend == backend.name or item.target_backend == backend.name
    )
    for link in related_interfaces:
        interface_network_path = quadlet_interface_network_path(settings, link.network)
        if _write_text_atomic(
            interface_network_path,
            render_quadlet_interface_network(link),
            backend_name=backend.name,
            asset="interface network",
        ):
            changed_files.append(str(interface_network_path))
    if _write_text_atomic(
        network_path,
        render_quadlet_network(backend),
        backend_name=backend.name,
        asset="network",
    ):
        changed_files.append(str(network_path))
    container_content = render_quadlet_container(
        backend,
        settings,
        base_profile=base_profile,
        inter_app_interfaces=related_interfaces,
    )
    try:
        previous_container_content = container_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous_container_content = None
    if _write_text_atomic(
        container_path,
        container_content,
        backend_name=backend.name,
        asset="container",
    ):
        changed_files.append(str(container_path))
    container_changed = str(container_path) in changed_files
    container_restart_required = container_changed and (
        previous_container_content is None
        or quadlet_container_policy_content(previous_container_content)
        != quadlet_container_policy_content(container_content)
    )
    return {
        "container_unit": quadlet_container_unit_name(backend.name),
        "container_service": quadlet_container_service_name(backend.name),
        "network_unit": quadlet_network_unit_name(backend.name),
        "network_service": quadlet_network_service_name(backend.name),
        "changed_files": changed_files,
        "container_changed": container_changed,
        "container_restart_required": container_restart_required,
    }


def remove_app_quadlet_assets(settings: Settings, backend_name: str) -> list[str]:
    removed: list[str] = []
    for path in (
        quadlet_container_path(settings, backend_name),
        quadlet_network_path(settings, backend_name),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(str(path))
    return removed


def remove_interface_quadlet_asset(settings: Settings, network: str) -> list[str]:
    removed: list[str] = []
    path = quadlet_interface_network_path(settings, network)
    try:
        path.unlink()
    except FileNotFoundError:
        return removed
    removed.append(str(path))
    return removed


def managed_quadlet_backend_names(settings: Settings) -> set[str]:
    if not settings.app_quadlet_dir.exists():
        return set()
    backend_names: set[str] = set()
    for path in settings.app_quadlet_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("cnc-app-") and name.endswith(".container"):
            backend_name = name.removeprefix("cnc-app-").removesuffix(".container")
        elif name.startswith("cnc-net-") and name.endswith(".network"):
            backend_name = name.removeprefix("cnc-net-").removesuffix(".network")
        else:
            continue
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError, UnicodeDecodeError):
            continue
        if first_line == QUADLET_HEADER:
            backend_names.add(backend_name)
    return backend_names


def managed_quadlet_interface_network_names(settings: Settings) -> set[str]:
    if not settings.app_quadlet_dir.exists():
        return set()
    network_names: set[str] = set()
    for path in settings.app_quadlet_dir.iterdir():
        if (
            not path.is_file()
            or not path.name.startswith("cnc-if-")
            or not path.name.endswith(".network")
        ):
            continue
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError, UnicodeDecodeError):
            continue
        if first_line == QUADLET_HEADER:
            network_names.add(path.name.removesuffix(".network"))
    return network_names
