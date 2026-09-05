import errno
import io
import json
import logging
from pathlib import Path
import threading
import time

import pytest

from app.config import Settings
from app.models.entities import Backend, Input
from app.services import app_quadlet, app_runtime
from app.services.app_quadlet import (
    quadlet_container_policy_content,
    quadlet_container_path,
    quadlet_network_path,
    render_quadlet_container,
)
from app.services.app_containers import control_dir, spec_path, write_app_control_assets
from app.services.apply_core import ApplyFailed, DesiredState
from app.services.apply_state import _runtime_graph
from app.services.bootstrap_state import (
    bootstrap_lock_path,
    bootstrap_state_path,
    failure_artifact_path,
    read_bootstrap_state,
    write_bootstrap_state,
)
from app.services.commands import CommandError, CommandResult
from app.services.inter_app_interfaces import RuntimeInterAppInterface
from app.services.resource_profile import build_resource_profile
from app.services.runtime_services import AppRuntimeServices
from app.services.sandbox_profiles import (
    app_sandbox_profile_state_path,
    app_sandbox_rootfs_path,
)


def _backend(name: str = "web") -> Backend:
    return Backend(
        name=name,
        kind="app",
        enabled=True,
        port=12001,
        internal_port=8337,
        base_image="docker.io/library/ubuntu:24.04",
        workdir="/srv/web",
        install_command="python3 -V",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
    )


def _settings(tmp_path: Path) -> Settings:
    resolv_path = tmp_path / "resolv.conf"
    resolv_path.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    return Settings(
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_quadlet_dir=tmp_path / "quadlets",
        systemd_generated_dir=tmp_path / "systemd",
        app_container_resolv_conf_path=resolv_path,
        command_timeout_apply_sec=5,
        command_timeout_status_sec=5,
    )


class _FakeHooks:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.inspect_payload: dict | None = None
        self.network_payload: dict | None = None
        self.command_error: CommandError | None = None
        self.stop_result = CommandResult(["podman", "stop"], 0, "", "")
        self.memory_current = "67108864"
        self.memory_high = "infinity"
        self.memory_max = "infinity"
        self.apps_slice_memory_max = "infinity"

    def inspect_container(self, _container: str, timeout_sec: int = 30):
        return CommandResult(
            ["podman", "inspect"], 0 if self.inspect_payload is not None else 1, "", ""
        ), self.inspect_payload

    def inspect_network(self, _network: str, timeout_sec: int = 30):
        return CommandResult(
            ["podman", "network", "inspect"],
            0 if self.network_payload is not None else 1,
            "",
            "",
        ), self.network_payload

    async def run_command_checked_async(
        self, command: list[str], timeout_sec: int = 30
    ):
        self.commands.append(command)
        if self.command_error is not None:
            raise self.command_error
        if command[:2] == ["systemctl", "set-property"]:
            for value in command:
                if value.startswith("MemoryHigh="):
                    self.memory_high = value.split("=", 1)[1]
                elif value.startswith("MemoryMax="):
                    if command[3] == "cnc-apps.slice":
                        self.apps_slice_memory_max = value.split("=", 1)[1]
                    else:
                        self.memory_max = value.split("=", 1)[1]
            return CommandResult(command, 0, "", "")
        if command[:3] == ["systemctl", "show", "cnc-apps.slice"]:
            return CommandResult(
                command,
                0,
                "\n".join(
                    [
                        "ControlGroup=/cnc-apps.slice",
                        "MemoryCurrent=67108864",
                        "MemoryHigh=infinity",
                        f"MemoryMax={self.apps_slice_memory_max}",
                    ]
                ),
                "",
            )
        if command[:2] == ["systemctl", "show"] and command[2].startswith("cnc-app-"):
            if any("MemoryCurrent" in value for value in command):
                return CommandResult(
                    command,
                    0,
                    "\n".join(
                        [
                            f"ControlGroup=/cnc-apps.slice/{command[2]}",
                            f"MemoryCurrent={self.memory_current}",
                            f"MemoryHigh={self.memory_high}",
                            f"MemoryMax={self.memory_max}",
                        ]
                    ),
                    "",
                )
            return CommandResult(
                command,
                0,
                f"/run/systemd/generator/{command[2]}",
                "",
            )
        return CommandResult(command, 0, "", "")

    def run_command(self, command: list[str], timeout_sec: int = 30):
        self.commands.append(command)
        return self.stop_result

    def as_services(self) -> AppRuntimeServices:
        return AppRuntimeServices(
            inspect_container=self.inspect_container,
            inspect_network=self.inspect_network,
            run_command=self.run_command,
            run_command_checked_async=self.run_command_checked_async,
        )


@pytest.mark.asyncio
async def test_resolve_generated_service_fragment_uses_systemd_control_plane(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    hooks = _FakeHooks()

    fragment = await app_runtime._resolve_generated_service_fragment(
        "cnc-app-web.service",
        settings,
        services=hooks.as_services(),
    )

    assert fragment == "/run/systemd/generator/cnc-app-web.service"
    assert hooks.commands == [
        [
            "systemctl",
            "show",
            "cnc-app-web.service",
            "--property=FragmentPath",
            "--value",
        ]
    ]


def _write_empty_rootfs_tar(path: Path) -> None:
    import tarfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w"):
        pass


def _write_hostile_rootfs_tar(path: Path) -> None:
    import tarfile

    path.parent.mkdir(parents=True, exist_ok=True)
    link = tarfile.TarInfo("bin/sh")
    link.type = tarfile.SYMTYPE
    link.linkname = "/usr/bin/dash"
    payload = b"owned\n"
    file_through_link = tarfile.TarInfo("bin/sh/escape")
    file_through_link.size = len(payload)
    with tarfile.open(path, "w") as archive:
        archive.addfile(link)
        archive.addfile(file_through_link, io.BytesIO(payload))


def test_render_quadlet_container_maps_current_runtime_contract(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.resource_profile.os.cpu_count", lambda: 2)
    settings = _settings(tmp_path)
    backend = _backend()
    backend.memory_high_override = "512M"
    backend.memory_max_override = "768M"
    backend.cpu_quota_override = "100%"
    backend.healthcheck_mode = "http"
    backend.healthcheck_path = "/health"
    backend.healthcheck_host_header = "web.example.com"
    backend.volumes_json = '["/srv/web-data:/srv/app/data:Z"]'
    base_profile = build_resource_profile(settings, [backend])

    content = render_quadlet_container(backend, settings, base_profile=base_profile)

    assert "ContainerName=cnc-app-web" in content
    assert f"Rootfs={app_sandbox_rootfs_path(settings, backend.name)}" in content
    assert "Network=cnc-net-web.network" in content
    assert "PublishPort=127.0.0.1:12001:8337/tcp" in content
    assert "DNS=1.1.1.1" in content
    assert "Volume=/srv/web-data:/srv/app/data:Z" in content
    assert "Label=io.cnc.backend=web" in content
    assert "Exec=/sbin/init" in content
    assert "--systemd=always" in content
    assert "Slice=cnc-apps.slice" in content
    assert "MemoryHigh=512M" in content
    assert "MemoryMax=768M" in content
    assert "--memory-reservation=" not in content
    assert "--memory=" not in content
    assert "--cpus=2" in content
    assert "HealthCmd=/usr/bin/curl" in content
    assert "-H 'Host: web.example.com'" in content
    assert "KillMode=mixed" in content
    assert "TimeoutStopSec=60" in content


def test_render_quadlet_container_uses_valid_tcp_healthcheck(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    backend.healthcheck_mode = "tcp"
    base_profile = build_resource_profile(settings, [backend])

    content = render_quadlet_container(backend, settings, base_profile=base_profile)

    assert (
        "HealthCmd=/usr/bin/timeout 3 /bin/bash -c "
        "'</dev/tcp/127.0.0.1/8337'" in content
    )


def test_quadlet_container_policy_ignores_live_cpu_and_memory_resources() -> None:
    first = "\n".join(
        [
            "[Container]",
            "HealthCmd=/usr/bin/timeout 3 /bin/bash -c '</dev/tcp/127.0.0.1/8337'",
            "PodmanArgs=--memory-reservation=196M --memory=260M --cpu-shares=1024 --cpus=0.25",
        ]
    )
    second = first.replace(
        "--memory-reservation=196M --memory=260M --cpu-shares=1024 --cpus=0.25",
        "--memory-reservation=384M --memory=512M --cpu-shares=2048 --cpus=0.5",
    )

    assert quadlet_container_policy_content(first) != quadlet_container_policy_content(
        second
    )

    live_base = "[Container]\nPodmanArgs=--cpu-shares=1024 --cpus=0.25"
    first_service = live_base + "\n[Service]\nMemoryHigh=256M\nMemoryMax=512M\n"
    second_service = live_base + "\n[Service]\nMemoryHigh=384M\nMemoryMax=768M\n"
    assert quadlet_container_policy_content(
        first_service
    ) == quadlet_container_policy_content(second_service)


def test_quadlet_memory_change_is_applied_live_without_service_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    backend.memory_high_override = "256M"
    backend.memory_max_override = "512M"
    backend.cpu_quota_override = "50%"
    first_profile = build_resource_profile(settings, [backend])
    app_quadlet.write_app_quadlet_assets(backend, settings, base_profile=first_profile)

    backend.memory_high_override = "384M"
    backend.memory_max_override = "768M"
    backend.cpu_quota_override = "75%"
    second_profile = build_resource_profile(settings, [backend])
    details = app_quadlet.write_app_quadlet_assets(
        backend, settings, base_profile=second_profile
    )

    assert details["container_changed"] is True
    assert details["container_restart_required"] is False


def test_render_quadlet_container_maps_inter_app_interface(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.resource_profile.os.cpu_count", lambda: 2)
    settings = _settings(tmp_path)
    source = _backend("web")
    target = _backend("api")
    base_profile = build_resource_profile(settings, [source, target])
    link = RuntimeInterAppInterface(
        name="cnc0",
        source_backend="web",
        source_backend_id=1,
        target_backend="api",
        target_backend_id=2,
        target_handoff_port=8337,
        network="cnc-if-web-cnc0",
        env_key="CNC_INTERFACE_CNC0_URL",
        url="http://cnc0:8337",
    )

    source_content = render_quadlet_container(
        source,
        settings,
        base_profile=base_profile,
        inter_app_interfaces=(link,),
    )
    target_content = render_quadlet_container(
        target,
        settings,
        base_profile=base_profile,
        inter_app_interfaces=(link,),
    )

    assert "Network=cnc-if-web-cnc0.network\n" in source_content
    assert "Environment=CNC_INTERFACE_CNC0_URL=http://cnc0:8337" in source_content
    assert "Network=cnc-if-web-cnc0.network:alias=cnc0" in target_content
    assert "CNC_INTERFACE_CNC0_URL" not in target_content


def test_write_app_quadlet_assets_reports_read_only_directory(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend("batch")
    base_profile = build_resource_profile(settings, [backend])
    original_write_bytes = Path.write_bytes

    def fake_write_bytes(path: Path, data: bytes) -> int:
        if path.name == ".cnc-app-batch.container.tmp":
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)

    with pytest.raises(app_quadlet.AppQuadletWriteError) as exc_info:
        app_quadlet.write_app_quadlet_assets(
            backend, settings, base_profile=base_profile
        )

    details = exc_info.value.details
    assert details["backend"] == "batch"
    assert details["asset"] == "container"
    assert details["operation"] == "write"
    assert details["errno"] == errno.EROFS
    assert details["path"] == str(quadlet_container_path(settings, "batch"))
    assert details["temp_path"].endswith("/.cnc-app-batch.container.tmp")
    assert "Read-only file system" in details["error"]
    assert "operator_hint" in details


@pytest.mark.asyncio
async def test_apply_app_backends_removes_runtime_for_deleted_managed_backend(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend("web")
    deleted_backend = _backend("app-dev")
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(deleted_backend, settings, base_profile=base_profile)
    quadlet_container_path(settings, deleted_backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    quadlet_container_path(settings, deleted_backend.name).write_text(
        "# Managed by CNC. Do not edit by hand.\n",
        encoding="utf-8",
    )
    quadlet_network_path(settings, deleted_backend.name).write_text(
        "# Managed by CNC. Do not edit by hand.\n",
        encoding="utf-8",
    )
    bootstrap_state_path(settings, deleted_backend.name).write_text(
        '{"status":"failed"}', encoding="utf-8"
    )
    failure_artifact_path(settings, deleted_backend.name).write_text(
        '{"phase":"app_healthcheck"}', encoding="utf-8"
    )
    bootstrap_lock_path(settings, deleted_backend.name).write_text("", encoding="utf-8")

    hooks = _FakeHooks()

    def fake_run(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if command[:2] == ["podman", "ps"]:
            return CommandResult(
                command,
                0,
                (
                    '[{"Names":["cnc-app-app-dev"],"Labels":{"io.cnc.managed":"true",'
                    '"io.cnc.backend":"app-dev"}}]'
                ),
                "",
            )
        if command[:3] == ["podman", "network", "ls"]:
            return CommandResult(
                command,
                0,
                (
                    '[{"Name":"cnc-net-app-dev","Labels":{"io.cnc.managed":"true",'
                    '"io.cnc.backend":"app-dev"}}]'
                ),
                "",
            )
        return CommandResult(command, 0, "", "")

    hooks.run_command = fake_run
    desired = DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[backend],
        resource_profile=base_profile,
        route_contracts=[],
        backend_contracts=[],
    )

    result = await app_runtime.apply_app_backends(
        desired, settings, services=hooks.as_services()
    )

    assert result["app_containers_removed"] == ["cnc-app-app-dev"]
    assert result["app_networks_removed"] == ["cnc-net-app-dev"]
    assert len(result["app_quadlet_files_removed"]) == 2
    assert (
        str(control_dir(settings, deleted_backend.name))
        in result["app_filesystem_artifacts_removed"]
    )
    assert (
        str(app_sandbox_rootfs_path(settings, deleted_backend.name).parent)
        in result["app_filesystem_artifacts_removed"]
    )
    assert ["systemctl", "stop", "cnc-app-app-dev.service"] in hooks.commands
    assert ["podman", "rm", "-f", "cnc-app-app-dev"] in hooks.commands
    assert ["podman", "network", "rm", "cnc-net-app-dev"] in hooks.commands
    assert ["systemctl", "daemon-reload"] in hooks.commands
    assert not spec_path(settings, deleted_backend.name).exists()
    assert not control_dir(settings, deleted_backend.name).exists()
    assert not app_sandbox_rootfs_path(settings, deleted_backend.name).parent.exists()
    assert not quadlet_container_path(settings, deleted_backend.name).exists()
    assert not quadlet_network_path(settings, deleted_backend.name).exists()
    assert not bootstrap_state_path(settings, deleted_backend.name).exists()
    assert not failure_artifact_path(settings, deleted_backend.name).exists()


@pytest.mark.asyncio
async def test_deleted_guest_filesystem_waits_for_explicit_finalizer(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    deleted_backend = _backend("app-dev")
    base_profile = build_resource_profile(settings, [])
    write_app_control_assets(deleted_backend, settings, base_profile=base_profile)
    rootfs = app_sandbox_rootfs_path(settings, deleted_backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "guest-data.txt").write_text("keep until commit", encoding="utf-8")
    desired = DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[],
        resource_profile=base_profile,
        route_contracts=[],
        backend_contracts=[],
    )

    result = await app_runtime.apply_app_backends(
        desired,
        settings,
        services=_FakeHooks().as_services(),
        cleanup_deleted_filesystem=False,
    )

    assert result["app_filesystem_artifacts_removed"] == []
    assert control_dir(settings, deleted_backend.name).exists()
    assert rootfs.exists()

    removed = await app_runtime.remove_deleted_app_filesystem_artifacts(
        {deleted_backend.name}, settings
    )

    assert removed == [
        str(control_dir(settings, deleted_backend.name)),
        str(rootfs.parent),
    ]
    assert not control_dir(settings, deleted_backend.name).exists()
    assert not rootfs.parent.exists()
    assert not bootstrap_lock_path(settings, deleted_backend.name).exists()


@pytest.mark.asyncio
async def test_apply_app_backends_removes_disabled_quadlet_runtime_without_guest_cleanup(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend("draft")
    backend.enabled = False
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(backend, settings, base_profile=base_profile)
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()
    quadlet_container_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    quadlet_container_path(settings, backend.name).write_text(
        "# Managed by CNC. Do not edit by hand.\n",
        encoding="utf-8",
    )
    quadlet_network_path(settings, backend.name).write_text(
        "# Managed by CNC. Do not edit by hand.\n",
        encoding="utf-8",
    )
    hooks = _FakeHooks()
    hooks.inspect_payload = {"State": {"Running": True}}
    hooks.network_payload = {"Name": "cnc-net-draft"}
    desired = DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[backend],
        resource_profile=base_profile,
        route_contracts=[],
        backend_contracts=[],
        runtime_graph=_runtime_graph(
            backends=[backend],
            resource_profile=base_profile,
            route_contracts=[],
            backend_contracts=[],
            settings=settings,
        ),
    )

    result = await app_runtime.apply_app_backends(
        desired, settings, services=hooks.as_services()
    )

    assert result["app_containers_stopped"] == ["cnc-app-draft"]
    assert result["app_containers_removed"] == ["cnc-app-draft"]
    assert result["app_networks_removed"] == ["cnc-net-draft"]
    assert len(result["app_quadlet_files_removed"]) == 2
    assert result["app_disabled_runtime_cleanup"][0]["backend"] == "draft"
    assert [
        "systemctl",
        "stop",
        "cnc-app-draft.service",
    ] in hooks.commands
    assert ["podman", "rm", "-f", "cnc-app-draft"] in hooks.commands
    assert [
        "systemctl",
        "stop",
        "cnc-net-draft-network.service",
    ] in hooks.commands
    assert ["podman", "network", "rm", "cnc-net-draft"] in hooks.commands
    assert ["systemctl", "daemon-reload"] in hooks.commands
    assert spec_path(settings, backend.name).exists()
    assert rootfs.exists()
    assert not quadlet_container_path(settings, backend.name).exists()
    assert not quadlet_network_path(settings, backend.name).exists()


@pytest.mark.asyncio
async def test_apply_app_backends_removes_filesystem_only_orphan_runtime(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend("web")
    orphan_backend = _backend("sample-orphan")
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(orphan_backend, settings, base_profile=base_profile)
    bootstrap_state_path(settings, orphan_backend.name).write_text(
        '{"status":"running"}', encoding="utf-8"
    )
    rootfs = app_sandbox_rootfs_path(settings, orphan_backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()
    hooks = _FakeHooks()
    desired = DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[backend],
        resource_profile=base_profile,
        route_contracts=[],
        backend_contracts=[],
    )

    result = await app_runtime.apply_app_backends(
        desired, settings, services=hooks.as_services()
    )

    assert (
        str(control_dir(settings, orphan_backend.name))
        in result["app_filesystem_artifacts_removed"]
    )
    assert (
        str(app_sandbox_rootfs_path(settings, orphan_backend.name).parent)
        in result["app_filesystem_artifacts_removed"]
    )
    assert not control_dir(settings, orphan_backend.name).exists()
    assert not app_sandbox_rootfs_path(settings, orphan_backend.name).parent.exists()


@pytest.mark.asyncio
async def test_apply_app_backends_cleans_new_runtime_when_publish_fails(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend("sample-rollback")
    base_profile = build_resource_profile(settings, [backend])
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()
    app_sandbox_profile_state_path(settings, backend.name).write_text(
        json.dumps(app_runtime._expected_profile_state(backend), sort_keys=True),
        encoding="utf-8",
    )

    hooks = _FakeHooks()
    started = False

    def fake_inspect_container(_container: str, timeout_sec: int = 30):
        if not started:
            return CommandResult(["podman", "inspect"], 1, "", ""), None
        return CommandResult(["podman", "inspect"], 0, "", ""), {
            "Id": "abc123",
            "State": {"Running": True},
        }

    async def fake_run_command_checked_async(command: list[str], timeout_sec: int = 30):
        nonlocal started
        if command[:2] == ["systemctl", "set-property"] or (
            command[:2] == ["systemctl", "show"]
            and any("MemoryCurrent" in value for value in command)
        ):
            return await _FakeHooks.run_command_checked_async(
                hooks, command, timeout_sec
            )
        hooks.commands.append(command)
        if command == ["systemctl", "start", "cnc-app-sample-rollback.service"]:
            started = True
        return CommandResult(command, 0, "", "")

    hooks.inspect_container = fake_inspect_container
    hooks.run_command_checked_async = fake_run_command_checked_async
    monkeypatch.setattr(
        app_runtime,
        "_guest_systemd_state",
        lambda *_args, **_kwargs: {
            "ready": True,
            "system_state": "running",
            "multi_user_target": "active",
        },
    )
    desired = DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[backend],
        known_app_backends=[backend],
        resource_profile=base_profile,
        route_contracts=[],
        backend_contracts=[],
        runtime_graph=_runtime_graph(
            backends=[backend],
            resource_profile=base_profile,
            route_contracts=[],
            backend_contracts=[],
            settings=settings,
        ),
    )

    with pytest.raises(ApplyFailed) as excinfo:
        await app_runtime.apply_app_backends(
            desired, settings, services=hooks.as_services(), operation_id=81
        )

    assert excinfo.value.details["selected_backends"] == ["sample-rollback"]
    assert excinfo.value.details["completed_backends"] == []
    assert excinfo.value.details["failed_backend"] == "sample-rollback"
    assert excinfo.value.details["pending_backends"] == []
    assert excinfo.value.details["operation_id"] == 81
    assert ["systemctl", "stop", "cnc-app-sample-rollback.service"] in hooks.commands
    assert ["podman", "rm", "-f", "cnc-app-sample-rollback"] in hooks.commands
    assert [
        "systemctl",
        "stop",
        "cnc-net-sample-rollback-network.service",
    ] in hooks.commands
    assert ["podman", "network", "rm", "cnc-net-sample-rollback"] in hooks.commands
    assert ["systemctl", "daemon-reload"] in hooks.commands
    assert not spec_path(settings, backend.name).exists()
    assert not quadlet_container_path(settings, backend.name).exists()
    assert not quadlet_network_path(settings, backend.name).exists()
    assert not app_sandbox_rootfs_path(settings, backend.name).parent.exists()


@pytest.mark.asyncio
async def test_prepare_phase_auto_repairs_missing_container_with_saved_spec(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(backend, settings, base_profile=base_profile)
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()
    bootstrap_state_path(settings, backend.name).write_text(
        '{"status":"failed"}', encoding="utf-8"
    )
    failure_artifact_path(settings, backend.name).write_text(
        '{"phase":"app_bootstrap"}', encoding="utf-8"
    )
    bootstrap_lock_path(settings, backend.name).write_text("", encoding="utf-8")

    hooks = _FakeHooks()
    monkeypatch.setattr(
        app_runtime, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=hooks.as_services(),
    )

    details = await reconciler._prepare_phase()

    assert details["action"] == "create"
    assert details["saved_spec_present"] is False
    assert details["auto_repair"]["reason"] == "container_missing_saved_spec"
    assert not spec_path(settings, backend.name).exists()
    assert not bootstrap_state_path(settings, backend.name).exists()
    assert not failure_artifact_path(settings, backend.name).exists()
    assert not bootstrap_lock_path(settings, backend.name).exists()
    assert hooks.commands == []


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_provisions_systemd_guest_by_default(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    result = await app_runtime._seed_persistent_guest_rootfs(
        backend,
        settings,
        services=hooks.as_services(),
    )

    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    profile_state = app_sandbox_profile_state_path(settings, backend.name).read_text(
        encoding="utf-8"
    )

    assert result["seeded"] is True
    assert result["seed_revision"] == 4
    assert result["provisioned_steps"] == [
        "provision_package_catalog",
        "provision_guest_runtime",
        "provision_guest_tools",
        "provision_package_cleanup",
        "profile_self_test",
    ]
    assert rootfs_path.is_dir()
    assert '"seed_revision": 4' in profile_state
    assert any(command[:2] == ["podman", "create"] for command in hooks.commands)
    assert any(command[:2] == ["podman", "export"] for command in hooks.commands)
    provision_commands = [
        command[7]
        for command in hooks.commands
        if command[:4] == ["podman", "run", "--rm", "--rootfs"]
        and command[5:7] == ["/bin/bash", "-lc"]
    ]
    assert provision_commands[:4] == [
        "export DEBIAN_FRONTEND=noninteractive; apt-get update",
        (
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get install -y systemd systemd-sysv dbus"
        ),
        (
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get install -y "
            "bash ca-certificates curl git jq procps psmisc ripgrep sqlite3 sudo wget "
            "dnsutils iproute2 iputils-ping"
        ),
        "apt-get clean; rm -rf /var/lib/apt/lists/*",
    ]
    bootstrap_state = read_bootstrap_state(settings, backend.name)
    assert bootstrap_state is not None
    seed_events = [
        (event["name"], event["status"])
        for event in bootstrap_state["events"]
        if event["kind"] == "seed"
    ]
    assert ("seed_container_create", "running") in seed_events
    assert ("seed_container_create", "succeeded") in seed_events
    assert ("provision_package_catalog", "running") in seed_events
    assert ("provision_package_catalog", "succeeded") in seed_events
    assert ("provision_guest_runtime", "running") in seed_events
    assert ("provision_guest_runtime", "succeeded") in seed_events
    assert ("provision_guest_tools", "running") in seed_events
    assert ("provision_guest_tools", "succeeded") in seed_events
    assert ("provision_package_cleanup", "running") in seed_events
    assert ("provision_package_cleanup", "succeeded") in seed_events
    assert ("profile_self_test", "running") in seed_events
    assert ("profile_self_test", "succeeded") in seed_events
    assert ("rootfs_activate", "succeeded") in seed_events
    assert bootstrap_state["reconcile_phase"] == "create"
    assert bootstrap_state["reconcile_details"]["substep"] == "rootfs_activate"
    assert any(
        command[:4] == ["podman", "run", "--rm", "--rootfs"]
        and command[5:]
        == [
            "/bin/bash",
            "-lc",
            "test -x /sbin/init && systemctl --version >/dev/null",
        ]
        for command in hooks.commands
    )


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_rejects_hostile_export_tar(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_hostile_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    with pytest.raises(RuntimeError, match="escapes destination"):
        await app_runtime._seed_persistent_guest_rootfs(
            backend,
            settings,
            services=hooks.as_services(),
            allow_reseed=True,
        )

    assert not rootfs_path.exists()
    assert any(command[:2] == ["podman", "rm"] for command in hooks.commands)


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_skips_implicit_reseed_when_profile_revision_changes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    rootfs_path.mkdir(parents=True, exist_ok=True)
    (rootfs_path / "stale.txt").write_text("old", encoding="utf-8")
    app_sandbox_profile_state_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    app_sandbox_profile_state_path(settings, backend.name).write_text(
        '{"sandbox_profile":"ubuntu-24.04-systemd","seed_image":"docker.io/library/ubuntu:24.04","init_command":["/sbin/init"],"seed_revision":1}',
        encoding="utf-8",
    )

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    result = await app_runtime._seed_persistent_guest_rootfs(
        backend,
        settings,
        services=hooks.as_services(),
    )

    assert result["seeded"] is False
    assert result["reseed_required"] is True
    assert result["reseed_skipped"] is True
    assert result["seed_revision"] == 1
    assert result["expected_seed_revision"] == 4
    assert (rootfs_path / "stale.txt").read_text(encoding="utf-8") == "old"
    assert not any(command[:2] == ["podman", "create"] for command in hooks.commands)
    assert not any(command[:2] == ["podman", "export"] for command in hooks.commands)


@pytest.mark.asyncio
async def test_create_phase_preserves_profile_state_when_reseed_is_skipped(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    base_profile = build_resource_profile(settings, [backend])
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    rootfs_path.mkdir(parents=True, exist_ok=True)
    (rootfs_path / "stale.txt").write_text("old", encoding="utf-8")
    profile_state_path = app_sandbox_profile_state_path(settings, backend.name)
    profile_state_path.parent.mkdir(parents=True, exist_ok=True)
    original_profile_state = {
        "sandbox_profile": "ubuntu-24.04-systemd",
        "seed_image": "docker.io/library/ubuntu:24.04",
        "init_command": ["/sbin/init"],
        "seed_revision": 1,
    }
    profile_state_path.write_text(
        json.dumps(original_profile_state, sort_keys=True),
        encoding="utf-8",
    )
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=hooks.as_services(),
    )
    reconciler.create_action = "create"

    result = await reconciler._create_phase()

    persisted_profile_state = json.loads(profile_state_path.read_text(encoding="utf-8"))
    assert result["guest_rootfs"]["reseed_skipped"] is True
    assert result["guest_rootfs"]["expected_seed_revision"] == 4
    assert persisted_profile_state["seed_revision"] == 1
    assert persisted_profile_state == original_profile_state
    assert (rootfs_path / "stale.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_create_phase_restarts_reused_runtime_when_quadlet_container_changes(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    hooks.inspect_payload = {
        "Config": {"Labels": {"PODMAN_SYSTEMD_UNIT": "cnc-app-web.service"}},
        "State": {"Running": True},
    }
    base_profile = build_resource_profile(settings, [backend])
    quadlet_container_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    quadlet_container_path(settings, backend.name).write_text(
        "# stale runtime contract\n", encoding="utf-8"
    )

    async def fake_seed(*_args, **_kwargs):
        return {
            "seeded": False,
            "reseed_skipped": False,
            "rootfs": str(app_sandbox_rootfs_path(settings, backend.name)),
        }

    monkeypatch.setattr(app_runtime, "_seed_persistent_guest_rootfs", fake_seed)
    monkeypatch.setattr(
        app_runtime, "_app_container_running", lambda *_args, **_kwargs: True
    )
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=hooks.as_services(),
    )
    reconciler.create_action = "reuse"
    reconciler.inspect_payload = hooks.inspect_payload

    details = await reconciler._create_phase()

    assert details["quadlet_container_restarted"] is True
    assert reconciler.result.recreated is True
    assert ["systemctl", "daemon-reload"] in hooks.commands
    assert ["systemctl", "restart", "cnc-app-web.service"] in hooks.commands


@pytest.mark.asyncio
async def test_create_phase_applies_memory_only_change_without_guest_restart(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    settings = _settings(tmp_path)
    backend = _backend()
    backend.id = 42
    backend.memory_high_override = "256M"
    backend.memory_max_override = "512M"
    initial_profile = build_resource_profile(settings, [backend])
    app_quadlet.write_app_quadlet_assets(
        backend, settings, base_profile=initial_profile
    )

    backend.memory_high_override = "384M"
    backend.memory_max_override = "768M"
    desired_profile = build_resource_profile(settings, [backend])
    hooks = _FakeHooks()
    hooks.inspect_payload = {
        "Config": {"Labels": {"PODMAN_SYSTEMD_UNIT": "cnc-app-web.service"}},
        "State": {"Running": True},
    }

    async def fake_seed(*_args, **_kwargs):
        return {
            "seeded": False,
            "reseed_skipped": False,
            "rootfs": str(app_sandbox_rootfs_path(settings, backend.name)),
        }

    monkeypatch.setattr(app_runtime, "_seed_persistent_guest_rootfs", fake_seed)
    monkeypatch.setattr(
        app_runtime, "_app_container_running", lambda *_args, **_kwargs: True
    )
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=desired_profile,
        services=hooks.as_services(),
        operation_id=73,
    )
    reconciler.create_action = "reuse"
    reconciler.inspect_payload = hooks.inspect_payload

    details = await reconciler._create_phase()

    assert details["quadlet_container_restarted"] is False
    assert reconciler.result.recreated is False
    assert not any(
        command[:2] == ["systemctl", "restart"] for command in hooks.commands
    )
    assert [
        "systemctl",
        "set-property",
        "--runtime",
        "cnc-app-web.service",
        "MemoryHigh=384M",
        "MemoryMax=768M",
    ] in hooks.commands
    assert details["resource_limits"]["memory_policy"]["verified"] is True
    event = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "app.memory.policy.converged"
    )
    assert event.context["app_id"] == "cnc.admin"
    assert event.context["backend_id"] == 42
    assert event.context["backend"] == "web"
    assert event.context["operation_id"] == 73
    assert event.context["verified"] is True


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_reseeds_when_explicitly_allowed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    rootfs_path.mkdir(parents=True, exist_ok=True)
    (rootfs_path / "stale.txt").write_text("old", encoding="utf-8")
    app_sandbox_profile_state_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    app_sandbox_profile_state_path(settings, backend.name).write_text(
        '{"sandbox_profile":"ubuntu-24.04-systemd","seed_image":"docker.io/library/ubuntu:24.04","init_command":["/sbin/init"],"seed_revision":1}',
        encoding="utf-8",
    )

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    result = await app_runtime._seed_persistent_guest_rootfs(
        backend,
        settings,
        services=hooks.as_services(),
        allow_reseed=True,
    )

    assert result["seeded"] is True
    assert result["seed_revision"] == 4
    assert result["provisioned_steps"] == [
        "provision_package_catalog",
        "provision_guest_runtime",
        "provision_guest_tools",
        "provision_package_cleanup",
        "profile_self_test",
    ]
    assert not (rootfs_path / "stale.txt").exists()
    quarantine_rootfs = Path(str(result["quarantine_rootfs"]))
    assert quarantine_rootfs.is_dir()
    assert (quarantine_rootfs / "stale.txt").read_text(encoding="utf-8") == "old"
    assert any(command[:2] == ["podman", "create"] for command in hooks.commands)
    assert any(command[:2] == ["podman", "export"] for command in hooks.commands)


@pytest.mark.asyncio
async def test_reseed_app_backend_rootfs_requires_backup_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    backend = _backend()

    with pytest.raises(ValueError, match="successful backend backup"):
        await app_runtime.reseed_app_backend_rootfs(
            backend, settings, services=_FakeHooks().as_services()
        )


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_preserves_guest_systemd_units_on_reseed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    service_path = rootfs_path / "etc/systemd/system/web.service"
    wants_path = rootfs_path / "etc/systemd/system/multi-user.target.wants/web.service"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    wants_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("[Unit]\nDescription=Web\n", encoding="utf-8")
    wants_path.symlink_to("../web.service")
    app_sandbox_profile_state_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    app_sandbox_profile_state_path(settings, backend.name).write_text(
        '{"sandbox_profile":"ubuntu-24.04-systemd","seed_image":"docker.io/library/ubuntu:24.04","init_command":["/sbin/init"],"seed_revision":1}',
        encoding="utf-8",
    )

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    result = await app_runtime._seed_persistent_guest_rootfs(
        backend,
        settings,
        services=hooks.as_services(),
        allow_reseed=True,
    )

    assert result["preserved_steps"] == [
        "preserved_guest_systemd_state",
        "restored_guest_systemd_state",
    ]
    assert service_path.read_text(encoding="utf-8") == "[Unit]\nDescription=Web\n"
    assert wants_path.is_symlink()
    assert wants_path.resolve() == service_path.resolve()


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_removes_rootfs_when_profile_self_test_fails(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
            return CommandResult(command, 0, "", "")
        if command[:4] == ["podman", "run", "--rm", "--rootfs"] and command[5:] == [
            "/bin/bash",
            "-lc",
            "test -x /sbin/init && systemctl --version >/dev/null",
        ]:
            raise CommandError(CommandResult(command, 1, "", "systemctl missing"))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    with pytest.raises(CommandError, match="systemctl missing"):
        await app_runtime._seed_persistent_guest_rootfs(
            backend,
            settings,
            services=hooks.as_services(),
            allow_reseed=True,
        )

    assert not rootfs_path.exists()
    assert not app_sandbox_profile_state_path(settings, backend.name).exists()


@pytest.mark.asyncio
async def test_seed_persistent_guest_rootfs_keeps_existing_rootfs_when_reseed_self_test_fails(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    rootfs_path = app_sandbox_rootfs_path(settings, backend.name)
    rootfs_path.mkdir(parents=True, exist_ok=True)
    (rootfs_path / "old.txt").write_text("working", encoding="utf-8")
    app_sandbox_profile_state_path(settings, backend.name).parent.mkdir(
        parents=True, exist_ok=True
    )
    app_sandbox_profile_state_path(settings, backend.name).write_text(
        '{"sandbox_profile":"ubuntu-24.04-systemd","seed_image":"docker.io/library/ubuntu:24.04","init_command":["/sbin/init"],"seed_revision":1}',
        encoding="utf-8",
    )

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
            return CommandResult(command, 0, "", "")
        if command[:4] == ["podman", "run", "--rm", "--rootfs"] and command[5:] == [
            "/bin/bash",
            "-lc",
            "test -x /sbin/init && systemctl --version >/dev/null",
        ]:
            raise CommandError(CommandResult(command, 1, "", "systemctl missing"))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked

    with pytest.raises(CommandError, match="systemctl missing"):
        await app_runtime._seed_persistent_guest_rootfs(
            backend,
            settings,
            services=hooks.as_services(),
            allow_reseed=True,
        )

    assert (rootfs_path / "old.txt").read_text(encoding="utf-8") == "working"


@pytest.mark.asyncio
async def test_prepare_phase_auto_repairs_missing_saved_spec_by_removing_container(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    base_profile = build_resource_profile(settings, [backend])
    hooks = _FakeHooks()
    hooks.inspect_payload = {"State": {"Running": True}}

    monkeypatch.setattr(
        app_runtime, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=hooks.as_services(),
    )

    details = await reconciler._prepare_phase()

    assert details["action"] == "create"
    assert details["auto_repair"]["reason"] == "missing_saved_spec"
    assert reconciler.result.recreated is True
    assert hooks.commands == [
        ["systemctl", "stop", "cnc-app-web.service"],
        ["podman", "rm", "-f", "cnc-app-web"],
    ]


@pytest.mark.asyncio
async def test_bootstrap_app_backend_writes_failed_state_and_stops_container_on_exec_failure(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    hooks.inspect_payload = {"Id": "cid-123"}
    hooks.stop_result = CommandResult(["podman", "stop"], 0, "", "")

    def fake_run(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if command[:2] == ["podman", "exec"]:
            return CommandResult(command, 124, "", "timed out after 5s")
        if command[:2] == ["systemctl", "stop"]:
            return hooks.stop_result
        return CommandResult(command, 0, "", "")

    hooks.run_command = fake_run

    monkeypatch.setattr(
        app_runtime,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: {"ok": False, "issues": ["loopback_unreachable"]},
    )

    with pytest.raises(app_runtime.ApplyFailed, match="bootstrap failed") as excinfo:
        await app_runtime._bootstrap_app_backend(
            backend,
            settings,
            services=hooks.as_services(),
            stop_on_failure=True,
        )

    state = read_bootstrap_state(settings, backend.name)

    assert excinfo.value.phase == "app_bootstrap"
    assert state is not None
    assert state["status"] == "failed"
    assert state["failure_phase"] == "app_bootstrap"
    assert "guest exec timed out" in str(state["last_log_excerpt"])
    assert excinfo.value.details["bootstrap_cleanup"]["action"] == "stopped"
    assert ["systemctl", "stop", "cnc-app-web.service"] in hooks.commands
    assert (
        len(
            [command for command in hooks.commands if command[:2] == ["podman", "exec"]]
        )
        == 1
    )


def test_guest_systemd_state_uses_one_bounded_exec_probe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()

    def fake_run(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        return CommandResult(command, 0, "degraded\nactive", "")

    hooks.run_command = fake_run

    state = app_runtime._guest_systemd_state(
        backend,
        settings,
        services=hooks.as_services(),
        timeout_sec=5,
        wait=True,
    )

    assert state == {
        "ready": True,
        "system_state": "degraded",
        "multi_user_target": "active",
        "exec_timed_out": False,
        "exec_outcome": "completed",
        "error": "",
    }
    assert len(hooks.commands) == 1
    assert hooks.commands[0][:7] == [
        "podman",
        "exec",
        "cnc-app-web",
        "timeout",
        "--signal=TERM",
        "--kill-after=0.1s",
        "4s",
    ]
    assert hooks.commands[0][-3:] == [
        "/bin/sh",
        "-c",
        (
            "until systemctl show-environment >/dev/null 2>&1; do sleep 0.1; done; "
            "systemctl is-system-running --wait; "
            "systemctl is-active multi-user.target"
        ),
    ]


@pytest.mark.asyncio
async def test_bootstrap_readiness_wait_uses_only_the_remaining_deadline(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.command_timeout_apply_sec = 1
    settings.command_timeout_status_sec = 1
    backend = _backend()
    hooks = _FakeHooks()
    hooks.inspect_payload = {"Id": "cid-123", "State": {"Running": True}}
    exec_timeouts: list[float] = []

    def fake_run(command: list[str], timeout_sec: float = 30):
        hooks.commands.append(command)
        if command[:2] != ["podman", "exec"]:
            return CommandResult(command, 0, "", "")
        exec_timeouts.append(timeout_sec)
        if len(exec_timeouts) == 1:
            time.sleep(0.15)
            return CommandResult(command, 3, "starting\ninactive", "")
        return CommandResult(command, 0, "running\nactive", "")

    hooks.run_command = fake_run
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=build_resource_profile(settings, [backend]),
        services=hooks.as_services(),
    )

    result = await reconciler._bootstrap_phase()

    assert result["bootstrapped"] is True
    assert len(exec_timeouts) == 2
    assert exec_timeouts[0] <= 1
    assert exec_timeouts[1] < 0.95
    exec_commands = [
        command for command in hooks.commands if command[:2] == ["podman", "exec"]
    ]
    assert "--wait" not in exec_commands[0][-1]
    assert "--wait" in exec_commands[1][-1]


@pytest.mark.asyncio
async def test_runtime_readiness_runs_off_event_loop_and_briefly_waits_for_guard(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    caller_thread = threading.get_ident()
    observed: list[tuple[int, float]] = []

    def fake_guest_command(
        _backend_name,
        _settings,
        command,
        *,
        timeout_sec,
        command_runner,
        bypass_circuit=False,
        wait_sec=0,
        source="guest_probe",
    ):
        observed.append((threading.get_ident(), wait_sec))
        return CommandResult(command, 0, "running\nactive", "")

    monkeypatch.setattr(app_runtime, "run_backend_guest_command", fake_guest_command)
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=build_resource_profile(settings, [backend]),
        services=_FakeHooks().as_services(),
    )

    result = await reconciler._bootstrap_phase()

    assert result["guest_system_state"] == "running"
    assert len(observed) == 1
    assert observed[0][0] != caller_thread
    assert 0 < observed[0][1] <= app_runtime._RUNTIME_GUEST_EXEC_GUARD_WAIT_SEC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exec_outcome", "stop_on_failure"),
    [
        ("timeout", False),
        ("guard_busy", True),
        ("guard_unavailable", True),
    ],
)
async def test_bootstrap_observation_failure_preserves_unowned_or_guarded_runtime(
    tmp_path: Path,
    exec_outcome: str,
    stop_on_failure: bool,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    hooks = _FakeHooks()
    hooks.inspect_payload = {"Id": "cid-123", "State": {"Running": True}}
    initial_state = {
        "ready": False,
        "system_state": "unknown",
        "multi_user_target": "unknown",
        "exec_timed_out": exec_outcome == "timeout",
        "exec_outcome": exec_outcome,
        "error": exec_outcome,
    }

    with pytest.raises(app_runtime.ApplyFailed) as excinfo:
        await app_runtime._bootstrap_app_backend(
            backend,
            settings,
            services=hooks.as_services(),
            initial_guest_state=initial_state,
            stop_on_failure=stop_on_failure,
        )

    cleanup = excinfo.value.details["bootstrap_cleanup"]
    assert cleanup["action"] == "preserved"
    assert cleanup["reason"] == (
        exec_outcome
        if exec_outcome in {"guard_busy", "guard_unavailable"}
        else "runtime_not_started_by_reconcile"
    )
    if exec_outcome in {"guard_busy", "guard_unavailable"}:
        assert excinfo.value.details["bootstrap_observation"] == {
            "status": "unavailable",
            "reason": exec_outcome,
            "guest_observed": False,
        }
        assert read_bootstrap_state(settings, backend.name) is None
    else:
        assert read_bootstrap_state(settings, backend.name)["status"] == "failed"
    assert not any(command[:2] == ["systemctl", "stop"] for command in hooks.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exec_outcome", "returncode"),
    [
        ("guard_busy", app_runtime.EXEC_GUARD_BUSY_RETURN_CODE),
        ("guard_unavailable", app_runtime.EXEC_GUARD_UNAVAILABLE_RETURN_CODE),
    ],
)
async def test_reconciler_guard_contention_preserves_bootstrap_state_and_artifact(
    monkeypatch,
    tmp_path: Path,
    exec_outcome: str,
    returncode: int,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    write_bootstrap_state(
        settings,
        backend.name,
        {
            "status": "succeeded",
            "reconcile_phase": "steady_state",
            "reconcile_phase_status": "succeeded",
            "reconcile_details": {"container": "cnc-app-web"},
        },
    )
    prior_bootstrap_state = read_bootstrap_state(settings, backend.name)
    artifact_path = failure_artifact_path(settings, backend.name)
    prior_artifact = '{"sentinel":"existing failure evidence"}'
    artifact_path.write_text(prior_artifact, encoding="utf-8")

    def blocked_guest_command(_backend_name, _settings, command, **_kwargs):
        return CommandResult(command, returncode, "", exec_outcome)

    monkeypatch.setattr(
        app_runtime,
        "run_backend_guest_command",
        blocked_guest_command,
    )
    hooks = _FakeHooks()
    hooks.inspect_payload = {"Id": "cid-123", "State": {"Running": True}}
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=build_resource_profile(settings, [backend]),
        services=hooks.as_services(),
    )
    reconciler.result.created = True

    with pytest.raises(app_runtime.ApplyFailed) as excinfo:
        await reconciler._run_phase("bootstrap", reconciler._bootstrap_phase)

    assert excinfo.value.details["bootstrap_observation"] == {
        "status": "unavailable",
        "reason": exec_outcome,
        "guest_observed": False,
    }
    assert read_bootstrap_state(settings, backend.name) == prior_bootstrap_state
    assert artifact_path.read_text(encoding="utf-8") == prior_artifact
    assert reconciler.result.phases["bootstrap"]["status"] == "failed"
    assert not any(command[:2] == ["systemctl", "stop"] for command in hooks.commands)


@pytest.mark.asyncio
async def test_create_phase_auto_repairs_stale_runtime_state_on_podman_update_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(backend, settings, base_profile=base_profile)
    app_quadlet.write_app_quadlet_assets(backend, settings, base_profile=base_profile)
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()

    hooks = _FakeHooks()
    hooks.inspect_payload = {"State": {"Running": False}}
    hooks.network_payload = {"dns_enabled": False}
    hooks.command_error = CommandError(
        CommandResult(
            ["podman", "update", "cnc-app-web"],
            125,
            "",
            "error opening /run/crun/abc/status: No such file or directory",
        )
    )

    async def fake_run_checked(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if command[:2] == ["systemctl", "set-property"]:
            for value in command:
                if value.startswith("MemoryHigh="):
                    hooks.memory_high = value.split("=", 1)[1]
                elif value.startswith("MemoryMax="):
                    hooks.memory_max = value.split("=", 1)[1]
            return CommandResult(command, 0, "", "")
        if command[:2] == ["systemctl", "show"] and any(
            "MemoryCurrent" in value for value in command
        ):
            return CommandResult(
                command,
                0,
                "\n".join(
                    [
                        "ControlGroup=/cnc-apps.slice/cnc-app-web.service",
                        f"MemoryCurrent={hooks.memory_current}",
                        f"MemoryHigh={hooks.memory_high}",
                        f"MemoryMax={hooks.memory_max}",
                    ]
                ),
                "",
            )
        if command[:2] == ["systemctl", "show"]:
            return CommandResult(
                command,
                0,
                f"/run/systemd/generator/{command[2]}",
                "",
            )
        if command[:2] == ["systemctl", "enable"]:
            return CommandResult(command, 0, "", "")
        if command[:2] == ["podman", "update"]:
            raise hooks.command_error
        if (
            command[:2] == ["podman", "export"]
            and len(command) >= 4
            and command[2] == "-o"
        ):
            _write_empty_rootfs_tar(Path(command[3]))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fake_run_checked
    services = hooks.as_services()
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=services,
    )
    reconciler.create_action = "reuse"

    details = await reconciler._create_phase()

    assert details["auto_repair"] is not None
    assert details["auto_repair"]["reason"] == "stale_runtime_state"
    assert reconciler.result.recreated is True


@pytest.mark.asyncio
async def test_prepare_phase_migrates_legacy_direct_podman_container_to_quadlet(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    base_profile = build_resource_profile(settings, [backend])
    write_app_control_assets(backend, settings, base_profile=base_profile)
    rootfs = app_sandbox_rootfs_path(settings, backend.name)
    rootfs.mkdir(parents=True)
    (rootfs / "etc").mkdir()
    bridge_path = settings.systemd_generated_dir / "cnc-app-web-legacy-restart.service"
    bridge_path.parent.mkdir(parents=True, exist_ok=True)
    bridge_path.write_text("legacy bridge\n", encoding="utf-8")

    hooks = _FakeHooks()
    hooks.inspect_payload = {
        "Config": {"Labels": {"io.cnc.managed": "true", "io.cnc.backend": "web"}},
        "HostConfig": {"RestartPolicy": {"Name": ""}},
        "State": {"Running": True},
    }
    hooks.network_payload = {"dns_enabled": False}

    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=base_profile,
        services=hooks.as_services(),
    )
    details = await reconciler._prepare_phase()

    assert details["action"] == "create"
    assert details["auto_repair"] == {
        "reason": "legacy_direct_podman_runtime_owner",
        "actions": [
            "podman rm -f cnc-app-web",
            "removed spec.json",
            "removed bootstrap.lock",
        ],
    }
    assert reconciler.create_action == "create"
    assert reconciler.result.recreated is True
    assert not spec_path(settings, backend.name).exists()
    assert rootfs.exists()
    assert ["systemctl", "stop", "cnc-app-web.service"] in hooks.commands
    assert ["podman", "rm", "-f", "cnc-app-web"] in hooks.commands


@pytest.mark.asyncio
async def test_observe_app_health_defaults_to_strict_http_probe(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    backend.healthcheck_mode = None
    backend.healthcheck_path = None
    backend.inputs = [Input(kind="domain", hostname="web.example.com", enabled=True)]

    hooks = _FakeHooks()

    def fake_run(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        return CommandResult(command, 0, "400", "")

    hooks.run_command = fake_run

    result = await app_runtime._observe_app_health(
        backend,
        settings,
        host="127.0.0.1",
        port=12001,
        services=hooks.as_services(),
    )

    assert result["ok"] is False
    assert result["checked"] is True
    assert result["status"] == "unhealthy"
    assert result["http_status"] == 400
    assert hooks.commands == [
        [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "2",
            "-H",
            "Host: web.example.com",
            "http://127.0.0.1:12001/",
        ]
    ]


@pytest.mark.asyncio
async def test_verify_phase_reports_disabled_healthcheck_as_unmonitored(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = _backend()
    backend.healthcheck_mode = "none"
    hooks = _FakeHooks()
    reconciler = app_runtime.AppRuntimeReconciler(
        backend,
        settings,
        base_profile=build_resource_profile(settings, [backend]),
        services=hooks.as_services(),
    )
    reconciler.result.target_ip = "10.89.0.2"

    result = await reconciler._verify_phase()

    assert result["healthcheck_status"] == "unmonitored"
    assert result["healthchecks"]["loopback"] == {
        "ok": True,
        "checked": False,
        "status": "unmonitored",
        "mode": "none",
        "host": "127.0.0.1",
        "port": 12001,
    }
    assert hooks.commands == []
