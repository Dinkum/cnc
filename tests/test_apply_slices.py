from __future__ import annotations

from pathlib import Path
import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ApplyRun, Backend, HostApplyState, Input
from app.services.app_containers import write_app_control_assets
from app.services.app_quadlet import quadlet_container_path, quadlet_network_path
from app.services import apply_core
from app.services.apply_core import (
    desired_state_snapshot,
    desired_state_snapshot_hash,
    desired_state_snapshot_json,
)
from app.services.apply_state import build_desired_state
from app.services.apply_service import ApplyServices, run_apply
from app.services.commands import CommandResult
from app.services.runtime_services import AppRuntimeServices


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        systemd_generated_dir=tmp_path / "systemd",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_quadlet_dir=tmp_path / "quadlets",
        tailscale_serve_state_path=tmp_path / "tailscale-serve.json",
        command_timeout_apply_sec=5,
        command_timeout_status_sec=5,
        transient_command_retry_attempts=0,
    )


async def _session_factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'app.db'}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_app(session_factory) -> None:
    async with session_factory() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=13101,
            handoff_port=8000,
            sandbox_profile="ubuntu-24.04-systemd",
            enabled=True,
            volumes_json="[]",
        )
        domain = Input(kind="domain", hostname="web.example.test", enabled=True)
        tailnet = Input(kind="tailnet_path", hostname="/web", enabled=True)
        domain.backends.append(backend)
        tailnet.backends.append(backend)
        session.add_all([backend, domain, tailnet])
        await session.commit()


class RecordingRuntime:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.absent_containers: set[str] = set()

    def run_command(self, command: list[str], timeout_sec: int) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command, 0, "ok", "")

    async def run_command_checked_async(
        self, command: list[str], timeout_sec: int
    ) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command, 0, "ok", "")

    def inspect_container(
        self, container: str, timeout_sec: int
    ) -> tuple[CommandResult, dict[str, Any] | None]:
        self.commands.append(["inspect-container", container])
        if container in self.absent_containers:
            return CommandResult(
                ["inspect-container", container], 1, "[]", "not found"
            ), None
        return CommandResult(["inspect-container", container], 0, "ok", ""), {
            "State": {"Running": True},
            "NetworkSettings": {
                "Networks": {"cnc-net-web": {"IPAddress": "10.88.0.2"}}
            },
        }

    def inspect_network(
        self, network: str, timeout_sec: int
    ) -> tuple[CommandResult, dict[str, Any] | None]:
        self.commands.append(["inspect-network", network])
        return CommandResult(["inspect-network", network], 0, "ok", ""), {
            "subnets": ["10.88.0.0/24"]
        }


class RecordingApply:
    def __init__(self) -> None:
        self.runtime = RecordingRuntime()
        self.runtime_calls: list[dict[str, Any]] = []
        self.ssh_calls: list[list[str]] = []
        self.host_asset_calls = 0

    async def apply_app_backends(self, desired, settings, *, services=None, **kwargs):
        self.runtime_calls.append(dict(kwargs))
        selected = kwargs.get("selected_backend_names")
        names = (
            sorted(selected)
            if selected is not None
            else sorted(desired.runtime_graph.enabled_app_backend_names)
        )
        return {
            "app_containers": [
                f"cnc-app-{name}" for name in sorted(desired.runtime_graph.app_backends)
            ],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": [f"cnc-app-{name}" for name in names],
            "app_containers_stopped": [],
            "app_containers_removed": [],
            "app_networks_removed": [],
            "app_quadlet_files_removed": [],
            "app_legacy_reboot_bridges_removed": [],
            "app_filesystem_artifacts_removed": [],
            "app_disabled_runtime_cleanup": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_healthcheck_status": "ok",
            "app_healthcheck_failures": [],
            "app_network_isolation": {"chain": "CNC_APP_ISOLATION", "subnets": []},
            "app_control_dir": str(settings.app_control_dir),
        }

    async def reconcile_ssh(
        self, backends: list[object], settings: Settings
    ) -> dict[str, object]:
        names = sorted(
            str(
                backend.get("name", backend.get("backend", ""))
                if isinstance(backend, dict)
                else getattr(backend, "name", backend)
            )
            for backend in backends
        )
        self.ssh_calls.append(names)
        return {"ssh_backends": names, "ssh_aliases_enabled": bool(names)}

    def reconcile_host_assets(self, settings: Settings) -> dict[str, object]:
        self.host_asset_calls += 1
        return {
            "changed_units": [],
            "daemon_reloaded": False,
            "timer_states": {},
            "timer_reconciled": False,
        }

    def services(self) -> ApplyServices:
        return ApplyServices(
            runtime_services=AppRuntimeServices(
                inspect_container=self.runtime.inspect_container,
                inspect_network=self.runtime.inspect_network,
                run_command=self.runtime.run_command,
                run_command_checked_async=self.runtime.run_command_checked_async,
            ),
            apply_app_backends=self.apply_app_backends,
            reconcile_backend_ssh_access=self.reconcile_ssh,
            reconcile_managed_systemd_assets=self.reconcile_host_assets,
            verify_tailscale_admin_exposure=lambda _settings: {"checked": False},
            run_control_plane_self_audit=lambda _settings: {"ok": True},
        )


async def _apply(session_factory, settings: Settings, recording: RecordingApply):
    async with session_factory() as session:
        return await run_apply(session, settings, services=recording.services())


@pytest.mark.asyncio
async def test_noop_apply_skips_runtime_nginx_tailscale_and_host_base(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()

    first = await _apply(session_factory, settings, recording)
    assert first.status == "success"
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()

    second = await _apply(session_factory, settings, recording)

    assert second.status == "success"
    assert second.details["changed_slices"] == []
    assert second.details["app_runtime_skipped"] is True
    assert second.details["nginx_skipped"] is True
    assert second.details["tailscale_skipped"] is True
    assert second.details["runtime_assets"]["skipped"] is True
    assert second.details["ssh_access"] == {
        "skipped": True,
        "reason": "ssh_access slice unchanged",
    }
    assert recording.runtime_calls == []
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )
    assert recording.ssh_calls == [["web"]]


@pytest.mark.asyncio
async def test_quadlet_renderer_change_reconciles_existing_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()

    first = await _apply(session_factory, settings, recording)
    assert first.status == "success"
    recording.runtime_calls.clear()
    original_render = apply_core.render_quadlet_container

    def revised_render(*args, **kwargs):
        return original_render(*args, **kwargs) + "# revised runtime contract\n"

    monkeypatch.setattr(apply_core, "render_quadlet_container", revised_render)

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == ["runtime.backend.1"]
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"web"}


@pytest.mark.asyncio
async def test_noop_apply_repairs_legacy_direct_podman_runtime_owner(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()

    first = await _apply(session_factory, settings, recording)
    assert first.status == "success"
    bridge_path = settings.systemd_generated_dir / "cnc-app-web-legacy-restart.service"
    bridge_path.parent.mkdir(parents=True, exist_ok=True)
    bridge_path.write_text("legacy bridge\n", encoding="utf-8")
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == []
    assert response.details["runtime_live_drift_backends"] == ["web"]
    assert response.details["runtime_live_drift_reasons"] == {
        "web": "legacy_direct_podman_runtime_owner"
    }
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"web"}
    assert recording.runtime_calls[0]["cleanup_deleted"] is False
    assert recording.runtime_calls[0]["reconcile_network_isolation"] is False
    assert ["inspect-container", "cnc-app-web"] in recording.runtime.commands
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_apply_can_defer_deleted_guest_filesystem_cleanup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime_calls.clear()

    async with session_factory() as session:
        backend = (
            (await session.execute(select(Backend).where(Backend.name == "web")))
            .scalars()
            .one()
        )
        await session.delete(backend)
        await session.flush()
        services = recording.services()
        services.cleanup_deleted_app_filesystem = False
        response = await run_apply(session, settings, services=services)

    assert response.status == "success"
    assert recording.runtime_calls[-1]["cleanup_deleted"] is True
    assert recording.runtime_calls[-1]["cleanup_deleted_filesystem"] is False


@pytest.mark.asyncio
async def test_domain_route_only_apply_reloads_nginx_without_runtime_or_tailscale(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()

    async with session_factory() as session:
        item = (
            (await session.execute(select(Input).where(Input.kind == "domain")))
            .scalars()
            .one()
        )
        item.hostname = "web-new.example.test"
        await session.commit()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == ["nginx.managed"]
    assert recording.runtime_calls == []
    assert response.details["ssh_access"] == {
        "skipped": True,
        "reason": "ssh_access slice unchanged",
    }
    assert ["nginx", "-t"] in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_runtime_only_apply_reconciles_selected_backend_without_ingress(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()

    async with session_factory() as session:
        backend = (
            (await session.execute(select(Backend).where(Backend.name == "web")))
            .scalars()
            .one()
        )
        backend.memory_high_override = "512M"
        await session.commit()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == ["runtime.backend.1"]
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"web"}
    assert recording.runtime_calls[0]["cleanup_deleted"] is False
    assert recording.runtime_calls[0]["reconcile_network_isolation"] is False
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_disabled_output_create_does_not_reconcile_ssh_access(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()
    recording.ssh_calls.clear()
    recording.runtime.absent_containers.add("cnc-app-draft")

    async with session_factory() as session:
        session.add(
            Backend(
                name="draft",
                kind="app",
                port=13102,
                handoff_port=8000,
                sandbox_profile="ubuntu-24.04-systemd",
                enabled=False,
                volumes_json="[]",
            )
        )
        await session.commit()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == ["runtime.backend.2"]
    assert response.details["runtime_absent_disabled_backends"] == ["draft"]
    assert response.details["app_runtime_skipped"] is True
    assert response.details["ssh_access"] == {
        "skipped": True,
        "reason": "ssh_access slice unchanged",
    }
    assert recording.ssh_calls == []
    assert recording.runtime_calls == []
    assert recording.runtime.commands == [["inspect-container", "cnc-app-draft"]]
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_disabled_output_with_stale_quadlet_assets_reconciles_runtime(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()
    recording.ssh_calls.clear()
    recording.runtime.absent_containers.add("cnc-app-draft")

    async with session_factory() as session:
        draft = Backend(
            name="draft",
            kind="app",
            port=13102,
            handoff_port=8000,
            sandbox_profile="ubuntu-24.04-systemd",
            enabled=False,
            volumes_json="[]",
        )
        session.add(draft)
        await session.flush()
        quadlet_container_path(settings, draft.name).parent.mkdir(
            parents=True, exist_ok=True
        )
        quadlet_container_path(settings, draft.name).write_text(
            "# Managed by CNC. Do not edit by hand.\n",
            encoding="utf-8",
        )
        quadlet_network_path(settings, draft.name).write_text(
            "# Managed by CNC. Do not edit by hand.\n",
            encoding="utf-8",
        )
        await session.commit()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == ["runtime.backend.2"]
    assert "runtime_absent_disabled_backends" not in response.details
    assert "app_runtime_skipped" not in response.details
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"draft"}
    assert recording.ssh_calls == []
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_enabling_auto_backend_reconverges_weighted_memory_for_pool(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    async with session_factory() as session:
        session.add(
            Backend(
                name="draft",
                kind="app",
                port=13102,
                handoff_port=8000,
                sandbox_profile="ubuntu-24.04-systemd",
                enabled=False,
                volumes_json="[]",
            )
        )
        await session.commit()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()
    recording.runtime_calls.clear()
    recording.ssh_calls.clear()

    async with session_factory() as session:
        backend = (
            (await session.execute(select(Backend).where(Backend.name == "draft")))
            .scalars()
            .one()
        )
        backend.enabled = True
        await session.commit()

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == [
        "network_isolation",
        "runtime.backend.1",
        "runtime.backend.2",
        "ssh_access",
    ]
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"draft", "web"}
    assert recording.runtime_calls[0]["reconcile_network_isolation"] is True
    assert recording.ssh_calls == [["draft", "web"]]
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_unchanged_nginx_slice_reconciles_live_managed_file_drift(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)
    recording = RecordingApply()
    await _apply(session_factory, settings, recording)
    recording.runtime.commands.clear()

    managed_file = next(settings.nginx_generated_dir.glob("cnc-*.conf"))
    managed_file.write_text("drift\n", encoding="utf-8")

    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["changed_slices"] == []
    assert response.details["nginx_live_drift_reconciled"] is True
    assert ["nginx", "-t"] in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] in recording.runtime.commands


@pytest.mark.asyncio
async def test_legacy_successful_apply_hash_bootstraps_slices_without_full_apply(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)

    async with session_factory() as session:
        desired = await build_desired_state(session, settings)
        snapshot_json = desired_state_snapshot_json(desired_state_snapshot(desired))
        state_hash = desired_state_snapshot_hash(snapshot_json)
        settings.nginx_generated_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in desired.nginx_files.items():
            (settings.nginx_generated_dir / filename).write_text(
                content, encoding="utf-8"
            )
        settings.tailscale_serve_state_path.parent.mkdir(parents=True, exist_ok=True)
        settings.tailscale_serve_state_path.write_text(
            json.dumps(
                {
                    "paths": desired.tailscale_paths,
                    "services": desired.tailscale_services,
                }
            ),
            encoding="utf-8",
        )
        run = ApplyRun(
            status="success",
            message="legacy apply completed",
            desired_state_hash=state_hash,
            desired_state_json=snapshot_json,
            details_json="{}",
        )
        session.add(run)
        await session.flush()
        session.add(
            HostApplyState(
                id=1,
                last_applied_state_hash=state_hash,
                last_successful_apply_run_id=run.id,
            )
        )
        await session.commit()

    recording = RecordingApply()
    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["slice_hashes_bootstrapped"] is True
    assert response.details["changed_slices"] == []
    assert response.details["app_runtime_skipped"] is True
    assert response.details["nginx_skipped"] is True
    assert response.details["tailscale_skipped"] is True
    assert response.details["ssh_access"] == {
        "skipped": True,
        "reason": "ssh_access slice unchanged",
    }
    assert recording.runtime_calls == []
    assert ["nginx", "-t"] not in recording.runtime.commands
    assert not any(
        command[:2] == ["tailscale", "serve"] for command in recording.runtime.commands
    )


@pytest.mark.asyncio
async def test_first_pass_live_slice_baseline_allows_route_change_without_runtime(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session_factory = await _session_factory(tmp_path)
    await _seed_app(session_factory)

    async with session_factory() as session:
        desired = await build_desired_state(session, settings)
        snapshot_json = desired_state_snapshot_json(desired_state_snapshot(desired))
        state_hash = desired_state_snapshot_hash(snapshot_json)
        settings.nginx_generated_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in desired.nginx_files.items():
            (settings.nginx_generated_dir / filename).write_text(
                content, encoding="utf-8"
            )
        settings.tailscale_serve_state_path.parent.mkdir(parents=True, exist_ok=True)
        settings.tailscale_serve_state_path.write_text(
            json.dumps(
                {
                    "paths": desired.tailscale_paths,
                    "services": desired.tailscale_services,
                }
            ),
            encoding="utf-8",
        )
        backend = (
            (await session.execute(select(Backend).where(Backend.name == "web")))
            .scalars()
            .one()
        )
        write_app_control_assets(
            backend, settings, base_profile=desired.resource_profile
        )
        run = ApplyRun(
            status="success",
            message="legacy apply completed",
            desired_state_hash=state_hash,
            desired_state_json=snapshot_json,
            details_json="{}",
        )
        session.add(run)
        await session.flush()
        session.add(
            HostApplyState(
                id=1,
                last_applied_state_hash=state_hash,
                last_successful_apply_run_id=run.id,
            )
        )
        item = (
            (await session.execute(select(Input).where(Input.kind == "domain")))
            .scalars()
            .one()
        )
        item.hostname = "web-new.example.test"
        await session.commit()

    recording = RecordingApply()
    response = await _apply(session_factory, settings, recording)

    assert response.status == "success"
    assert response.details["live_slice_hashes_bootstrapped"] is True
    assert response.details["changed_slices"] == [
        "apps_memory",
        "network_isolation",
        "nginx.managed",
        "ssh_access",
    ]
    assert len(recording.runtime_calls) == 1
    assert recording.runtime_calls[0]["selected_backend_names"] == {"web"}
    assert recording.ssh_calls == [["web"]]
    assert ["nginx", "-t"] in recording.runtime.commands
    assert ["systemctl", "reload", "nginx"] in recording.runtime.commands
