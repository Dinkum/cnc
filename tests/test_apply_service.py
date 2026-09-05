import ast
import errno
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import Base
from app.models.entities import ApplyRun, Backend, HostApplyState, Input, Operation
from app.services import apply_service, netdata_runtime
from app.services.app_runtime import (
    apply_app_resource_limits,
    build_app_container_create_command,
)
from app.services.app_quadlet import quadlet_container_path
from app.services.apply_state import build_desired_state
from app.services.resource_profile import build_resource_profile
from app.services.renderers import container_name
from app.services.runtime_services import AppRuntimeServices
from app.services.apply_service import run_apply
from app.services.commands import CommandError, CommandResult
from app.services.systemd_memory import memory_value_bytes
from app.services.operation_runtime import (
    create_backend_progress_steps,
    output_save_progress_steps,
)


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _stub_live_systemd_memory_for_apply_tests(monkeypatch):
    async def converge(
        unit,
        *,
        memory_high=None,
        memory_max=None,
        **_kwargs,
    ):
        high_bytes = memory_value_bytes(memory_high)
        max_bytes = memory_value_bytes(memory_max)
        return {
            "unit": unit,
            "control_group": "/cnc-apps.slice"
            if unit == "cnc-apps.slice"
            else f"/cnc-apps.slice/{unit}",
            "changed": True,
            "changed_properties": [
                name
                for name, value in (
                    ("MemoryHigh", memory_high),
                    ("MemoryMax", memory_max),
                )
                if value is not None
            ],
            "memory_current_bytes": 64 * 1024 * 1024,
            "memory_high_bytes": high_bytes,
            "memory_max_bytes": max_bytes,
            "memory_max_lowered_below_usage": False,
            "verified": True,
        }

    monkeypatch.setattr(
        "app.services.app_memory_slice.converge_systemd_memory_policy", converge
    )
    monkeypatch.setattr(
        "app.services.app_runtime.converge_systemd_memory_policy", converge
    )


def test_successful_apply_plan_details_leave_unavailable_followers_dirty() -> None:
    plan = apply_service.ApplySlicePlan(
        schema_version=2,
        slice_hashes={
            "cluster_followers": "current-cluster",
            "nginx.managed": "current-nginx",
        },
        previous_slice_hashes={
            "cluster_followers": "previous-cluster",
            "nginx.managed": "current-nginx",
        },
        changed_slices=("cluster_followers",),
        skipped_slices=("nginx.managed",),
    )

    details = apply_service._successful_apply_plan_details(
        plan,
        {
            "cluster_followers_unavailable": [
                {"node_uid": "node-a", "error": "Connection timed out"}
            ]
        },
    )

    assert details["slice_hashes"]["cluster_followers"] == "previous-cluster"
    assert details["slice_hashes"]["nginx.managed"] == "current-nginx"
    assert details["unapplied_slices"] == ["cluster_followers"]


def test_apply_success_event_summary_names_created_output() -> None:
    backend = Backend(id=42, name="web", kind="app", enabled=True)
    desired = type("Desired", (), {"known_app_backends": [backend]})()
    plan = apply_service.ApplySlicePlan(
        schema_version=2,
        slice_hashes={"runtime.backend.42": "current"},
        previous_slice_hashes={"host_base": "previous"},
        changed_slices=("runtime.backend.42",),
        skipped_slices=(),
    )

    output_event = apply_service._apply_output_event_details(desired, plan)

    assert output_event["outputs_created"] == 1
    assert output_event["output_created_names"] == ["web"]
    assert (
        apply_service._apply_success_event_summary(
            degraded_health=False,
            cluster_follower_warnings=False,
            output_event=output_event,
        )
        == "Output created"
    )
    assert apply_service._apply_success_event_subevents(
        run_id=7,
        output_event=output_event,
        degraded_health=False,
    ) == [
        {"label": "run", "value": "#7"},
        {"label": "output", "value": "web"},
        {"label": "status", "value": "success"},
    ]


@pytest.mark.asyncio
async def test_desired_state_exposes_netdata_on_reserved_tailnet_service(
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(tmp_path, netdata_enabled=True)

    async with maker() as session:
        desired = await build_desired_state(session, settings)

    assert desired.netdata_required is True
    assert desired.tailscale_services == {"netdata": "http://127.0.0.1:19999"}


@pytest.mark.asyncio
async def test_netdata_reconcile_installs_native_agent_and_binds_loopback(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _apply_settings(
        tmp_path,
        netdata_enabled=True,
        netdata_config_path=tmp_path / "netdata.conf",
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(netdata_runtime, "_netdata_binary_exists", lambda: False)

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    services = AppRuntimeServices(run_command_checked_async=fake_run_checked)

    result = await netdata_runtime.reconcile_netdata_native_runtime(
        settings, services=services
    )

    assert result["install"]["method"] == "native-package"
    assert [
        "curl",
        "-L",
        netdata_runtime.NETDATA_KICKSTART_URL,
        "-o",
        "/tmp/cnc-netdata-kickstart.sh",
    ] in commands
    assert [
        "env",
        "DISABLE_TELEMETRY=1",
        "sh",
        "/tmp/cnc-netdata-kickstart.sh",
        "--release-channel",
        "stable",
        "--native-only",
        "--dont-wait",
    ] in commands
    assert ["systemctl", "enable", "--now", "netdata.service"] in commands
    assert ["systemctl", "restart", "netdata.service"] in commands
    assert "bind to = 127.0.0.1" in settings.netdata_config_path.read_text(
        encoding="utf-8"
    )
    assert "default port = 19999" in settings.netdata_config_path.read_text(
        encoding="utf-8"
    )


def _apply_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        systemd_generated_dir=tmp_path / "systemd",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        app_sandbox_dir=tmp_path / "sandboxes",
        app_quadlet_dir=tmp_path / "quadlets",
        **overrides,
    )


def test_apply_progress_callbacks_match_progress_catalogs() -> None:
    create_catalog_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in create_backend_progress_steps()
    }
    output_save_catalog_pairs = {
        (str(step["headline"]), str(step["substep"]))
        for step in output_save_progress_steps()
    }
    source = Path("app/services/apply_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    emitted_pairs: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = node.func.id if isinstance(node.func, ast.Name) else ""
        if function_name != "_emit_apply_progress" or len(node.args) < 3:
            continue
        phase_node, substate_node = node.args[1], node.args[2]
        if isinstance(phase_node, ast.Constant) and isinstance(
            substate_node, ast.Constant
        ):
            emitted_pairs.append(
                (str(phase_node.value), str(substate_node.value), node.lineno)
            )

    assert emitted_pairs
    assert [
        (phase, substate, line)
        for phase, substate, line in emitted_pairs
        if (phase, substate) not in create_catalog_pairs
    ] == []
    assert [
        (phase, substate, line)
        for phase, substate, line in emitted_pairs
        if (phase, substate) not in output_save_catalog_pairs
    ] == []


def _write_empty_rootfs_tar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w"):
        pass


def _is_guest_readiness_command(command: list[str]) -> bool:
    return (
        command[:2] == ["podman", "exec"]
        and len(command) >= 3
        and "systemctl is-system-running" in command[-1]
        and "systemctl is-active multi-user.target" in command[-1]
    )


def _guest_readiness_result(command: list[str], *, ready: bool) -> CommandResult:
    return CommandResult(
        command,
        0 if ready else 3,
        "running\nactive" if ready else "starting\ninactive",
        "",
    )


def _handle_single_guest_checked(
    command: list[str],
    app_state: dict[str, bool],
    *,
    commands: list[list[str]] | None = None,
) -> bool:
    if commands is not None:
        commands.append(command)
    if command[:3] == ["podman", "network", "create"]:
        app_state["network_created"] = True
        return True
    if command == ["systemctl", "daemon-reload"]:
        return True
    if command[:2] == ["systemctl", "enable"]:
        return True
    if command[:2] == ["systemctl", "start"] and command[-1].endswith(
        "-network.service"
    ):
        app_state["network_created"] = True
        return True
    if command[:2] == ["systemctl", "start"] and command[-1].startswith("cnc-app-"):
        app_state["created"] = True
        app_state["running"] = True
        return True
    if command[:2] == ["podman", "create"]:
        if "--rootfs" in command:
            app_state["created"] = True
        return True
    if command[:2] == ["podman", "export"] and len(command) >= 4 and command[2] == "-o":
        _write_empty_rootfs_tar(Path(command[3]))
        return True
    if command[:3] == ["podman", "rm", "-f"]:
        app_state["created"] = False
        app_state["running"] = False
        return True
    if command[:2] == ["podman", "start"]:
        app_state["running"] = True
        return True
    return False


def _handle_single_guest_run(
    command: list[str],
    app_state: dict[str, bool],
    *,
    commands: list[list[str]] | None = None,
) -> CommandResult | None:
    if commands is not None:
        commands.append(command)
    if _is_guest_readiness_command(command):
        ready = bool(app_state.get("guest_ready", app_state.get("running", False)))
        return _guest_readiness_result(command, ready=ready)
    if command[:3] == ["podman", "stop", "-t"]:
        app_state["running"] = False
        return CommandResult(command, 0, "", "")
    if command[:2] == ["systemctl", "stop"] and command[-1].startswith("cnc-app-"):
        app_state["running"] = False
        app_state["created"] = False
        return CommandResult(command, 0, "", "")
    if command[:2] == ["systemctl", "stop"] and command[-1].endswith(
        "-network.service"
    ):
        app_state["network_created"] = False
        return CommandResult(command, 0, "", "")
    return None


def _handle_multi_guest_checked(
    command: list[str],
    app_state: dict[str, dict[str, bool]],
    *,
    commands: list[list[str]] | None = None,
) -> bool:
    if commands is not None:
        commands.append(command)
    if command[:3] == ["podman", "network", "create"]:
        app_state[command[-1].removeprefix("cnc-net-")]["network_created"] = True
        return True
    if command == ["systemctl", "daemon-reload"]:
        return True
    if command[:2] == ["systemctl", "enable"]:
        return True
    if command[:2] == ["systemctl", "start"] and command[-1].endswith(
        "-network.service"
    ):
        name = command[-1].removeprefix("cnc-net-").removesuffix("-network.service")
        app_state[name]["network_created"] = True
        return True
    if command[:2] == ["systemctl", "start"] and command[-1].startswith("cnc-app-"):
        name = command[-1].removeprefix("cnc-app-").removesuffix(".service")
        app_state[name]["created"] = True
        app_state[name]["running"] = True
        return True
    if command[:2] == ["podman", "create"]:
        if "--rootfs" in command:
            name = command[command.index("--name") + 1].removeprefix("cnc-app-")
            app_state[name]["created"] = True
        return True
    if command[:2] == ["podman", "export"] and len(command) >= 4 and command[2] == "-o":
        _write_empty_rootfs_tar(Path(command[3]))
        return True
    if command[:3] == ["podman", "rm", "-f"]:
        name = command[-1].removeprefix("cnc-app-")
        if name in app_state:
            app_state[name]["created"] = False
            app_state[name]["running"] = False
        return True
    if command[:2] == ["podman", "start"]:
        app_state[command[-1].removeprefix("cnc-app-")]["running"] = True
        return True
    return False


def _handle_multi_guest_run(
    command: list[str],
    app_state: dict[str, dict[str, bool]],
    *,
    commands: list[list[str]] | None = None,
) -> CommandResult | None:
    if commands is not None:
        commands.append(command)
    if _is_guest_readiness_command(command):
        state = app_state[command[2].removeprefix("cnc-app-")]
        ready = bool(state.get("guest_ready", state.get("running", False)))
        return _guest_readiness_result(command, ready=ready)
    if command[:2] == ["systemctl", "stop"] and command[-1].startswith("cnc-app-"):
        name = command[-1].removeprefix("cnc-app-").removesuffix(".service")
        app_state[name]["created"] = False
        app_state[name]["running"] = False
        return CommandResult(command, 0, "", "")
    if command[:2] == ["systemctl", "stop"] and command[-1].endswith(
        "-network.service"
    ):
        name = command[-1].removeprefix("cnc-net-").removesuffix("-network.service")
        app_state[name]["network_created"] = False
        return CommandResult(command, 0, "", "")
    return None


@pytest.fixture(autouse=True)
def apply_services(monkeypatch):
    async def fake_reconcile(
        backends: list[object], settings: Settings
    ) -> dict[str, object]:
        names = sorted(
            str(
                backend.get("name", backend.get("backend", ""))
                if isinstance(backend, dict)
                else getattr(backend, "name", backend)
            )
            for backend in backends
        )
        return {
            "ssh_backends": names,
            "ssh_aliases_enabled": bool(names),
        }

    monkeypatch.setattr(apply_service, "reconcile_backend_ssh_access", fake_reconcile)
    monkeypatch.setattr(
        apply_service,
        "verify_tailscale_admin_exposure",
        lambda _settings: {"checked": False},
    )
    monkeypatch.setattr(
        apply_service, "run_control_plane_self_audit", lambda _settings: {"ok": True}
    )
    monkeypatch.setattr(
        apply_service,
        "reconcile_managed_systemd_assets",
        lambda _settings: {
            "changed_units": [],
            "daemon_reloaded": False,
            "timer_states": {},
            "timer_reconciled": False,
        },
    )

    async def fake_generated_fragment(
        service_name: str,
        _settings: Settings,
        *,
        services: AppRuntimeServices,
    ) -> str:
        del services
        return f"/run/systemd/generator/{service_name}"

    monkeypatch.setattr(
        "app.services.app_runtime._resolve_generated_service_fragment",
        fake_generated_fragment,
    )


@pytest.fixture(autouse=True)
def _stub_pushover(monkeypatch):
    async def fake_notify(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(
        "app.services.apply_service.send_pushover_notification_async", fake_notify
    )


def test_build_app_container_create_command_includes_resource_limits() -> None:
    settings = Settings(
        auto_resource_limits=False,
        default_memory_high="300M",
        default_memory_max="420M",
        default_cpu_quota="60%",
    )
    backend = Backend(
        name="web",
        kind="app",
        port=12000,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
        memory_high_override="768M",
        memory_max_override="1G",
        cpu_quota_override="100%",
    )
    base_profile = build_resource_profile(settings, 3)

    command = build_app_container_create_command(
        backend, settings, base_profile=base_profile
    )

    assert "--memory-reservation" not in command
    assert "--memory" not in command
    assert "--cpus" in command
    assert command[command.index("--cpus") + 1] == "1"


def test_build_app_container_create_command_uses_shared_base_profile_without_overrides() -> (
    None
):
    settings = Settings(
        auto_resource_limits=False,
        default_memory_high="300M",
        default_memory_max="420M",
        default_cpu_quota="60%",
    )
    backend = Backend(
        name="smokeapp",
        kind="app",
        port=12001,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/smokeapp",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
    )
    base_profile = build_resource_profile(settings, 5)

    command = build_app_container_create_command(
        backend, settings, base_profile=base_profile
    )

    assert "--memory-reservation" not in command
    assert "--memory" not in command
    assert command[command.index("--cpus") + 1] == "0.6"


def test_write_staged_managed_dir_stays_inside_target_directory(tmp_path: Path) -> None:
    parent = tmp_path / "readonly-parent"
    target = parent / "generated"
    target.mkdir(parents=True, exist_ok=True)
    (target / "unmanaged.txt").write_text("keep", encoding="utf-8")
    original_mode = parent.stat().st_mode
    parent.chmod(0o555)
    try:
        staged = apply_service._write_staged_managed_dir(
            target,
            {"cnc-host-web.conf": "server {}"},
        )
    finally:
        parent.chmod(original_mode)

    assert staged.parent == target
    assert (staged / "cnc-host-web.conf").read_text(encoding="utf-8") == "server {}"


@pytest.mark.asyncio
async def test_run_apply_reports_sync_files_error_when_nginx_staging_is_read_only(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        transient_command_retry_backoff_sec=0.0,
    )

    def fail_stage(*_args, **_kwargs):
        raise OSError(
            errno.EROFS,
            "Read-only file system",
            str(settings.nginx_generated_dir / ".generated.stage.test"),
        )

    monkeypatch.setattr(
        "app.services.apply_service._write_staged_managed_dir", fail_stage
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "sync_files"
    assert "Read-only file system" in str(result.details["error"])
    assert result.details["target_dir"] == str(settings.nginx_generated_dir)
    assert result.message == "apply failed"
    assert result.details["failure_mode"] == "clean"
    assert result.details["manual_review_required"] is False


@pytest.mark.asyncio
async def test_run_apply_reports_quadlet_preflight_error_before_live_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(tmp_path)

    def fail_quadlet_preflight(_settings: Settings) -> dict[str, object]:
        raise apply_service.AppQuadletWriteError(
            "app Quadlet directory is not writable",
            details={
                "reason": "quadlet_write_failed",
                "operation": "preflight",
                "path": str(settings.app_quadlet_dir / ".cnc-write-test.tmp"),
                "temp_path": str(settings.app_quadlet_dir / ".cnc-write-test.1.tmp"),
                "quadlet_dir": str(settings.app_quadlet_dir),
                "errno": errno.EROFS,
                "error": "Read-only file system",
                "operator_hint": "check cnc-admin.service write paths",
            },
        )

    monkeypatch.setattr(
        "app.services.apply_service.verify_app_quadlet_dir_writable",
        fail_quadlet_preflight,
    )

    async with maker() as session:
        backend = Backend(
            name="batch",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/batch",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "app_runtime"
    assert result.details["reason"] == "quadlet_write_failed"
    assert result.details["operation"] == "preflight"
    assert result.details["quadlet_dir"] == str(settings.app_quadlet_dir)
    assert result.details["failure_mode"] == "clean"
    assert result.details["manual_review_required"] is False
    assert result.details["live_mutation_phases"] == []


def test_apply_failure_notification_facts_include_quadlet_write_context(
    tmp_path: Path,
) -> None:
    settings = _apply_settings(tmp_path)
    facts = dict(
        apply_service._apply_failure_notification_facts(
            {
                "phase": "app_runtime",
                "reason": "quadlet_write_failed",
                "quadlet_dir": str(settings.app_quadlet_dir),
                "errno": errno.EROFS,
                "operator_hint": "restart cnc-admin",
                "error": "Read-only file system",
                "process": {"systemd_unit": "cnc-auto-size.service"},
            },
            run_id=17,
        )
    )

    assert facts["run_id"] == "#17"
    assert facts["phase"] == "app_runtime"
    assert facts["reason"] == "quadlet_write_failed"
    assert facts["service"] == "cnc-auto-size.service"
    assert facts["target"] == str(settings.app_quadlet_dir)
    assert facts["errno"] == errno.EROFS
    assert facts["hint"] == "restart cnc-admin"
    assert facts["error"] == "Read-only file system"


@pytest.mark.asyncio
async def test_run_apply_fails_when_tailscale_admin_exposure_is_unsafe(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )

    def fail_verify(_settings: Settings) -> dict[str, object]:
        raise apply_service.TailscaleAdminExposureError(
            "tailscale funnel exposes HTTPS port 443"
        )

    monkeypatch.setattr(
        "app.services.apply_service.verify_tailscale_admin_exposure", fail_verify
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "tailscale_admin_verify"
    assert "HTTPS port 443" in str(result.details["error"])


@pytest.mark.asyncio
async def test_run_apply_succeeds_with_self_audit_warning(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )

    def fail_audit(_settings: Settings) -> dict[str, object]:
        raise apply_service.ControlPlaneSelfAuditError(
            [
                {
                    "check": "firewall_restrictions",
                    "message": "firewall exposes 80/tcp with a broad accept rule",
                    "severity": "warning",
                }
            ]
        )

    monkeypatch.setattr(
        "app.services.apply_service.run_control_plane_self_audit", fail_audit
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "success"
    assert result.details["runtime_assets"]["timer_reconciled"] is False
    assert result.details["control_plane_self_audit"]["ok"] is False
    assert result.details["control_plane_self_audit"]["blocks_apply"] is False
    assert (
        result.details["control_plane_self_audit"]["findings"][0]["check"]
        == "firewall_restrictions"
    )
    assert (
        result.details["control_plane_self_audit"]["findings"][0]["severity"]
        == "warning"
    )


@pytest.mark.asyncio
async def test_run_apply_fails_with_blocking_self_audit_finding(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )

    def fail_audit(_settings: Settings) -> dict[str, object]:
        raise apply_service.ControlPlaneSelfAuditError(
            [
                {
                    "check": "admin_loopback_bind",
                    "message": "admin listener is not loopback-only",
                    "severity": "blocking",
                }
            ]
        )

    monkeypatch.setattr(
        "app.services.apply_service.run_control_plane_self_audit", fail_audit
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "control_plane_self_audit"
    assert result.details["blocks_apply"] is True
    assert result.details["failure_mode"] == "partial"


@pytest.mark.asyncio
async def test_run_apply_retries_transient_blocking_self_audit_finding(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
    )
    attempts = 0

    def flaky_audit(_settings: Settings) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise apply_service.ControlPlaneSelfAuditError(
                [
                    {
                        "check": "tailscale_admin_exposure",
                        "message": "tailscale serve exposed an old admin mapping",
                        "severity": "blocking",
                    }
                ]
            )
        return {"tailscale_admin_exposure": {"checked": True}}

    monkeypatch.setattr(
        "app.services.apply_service.run_control_plane_self_audit", flaky_audit
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "success"
    assert attempts == 2
    assert result.details["control_plane_self_audit"]["ok"] is True
    assert result.details["control_plane_self_audit"]["blocking_retry_count"] == 1


@pytest.mark.asyncio
async def test_run_apply_persists_desired_state_snapshot_and_last_applied_pointer(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        backend = Backend(
            name="site",
            kind="static",
            static_root="/srv/site",
            enabled=True,
        )
        item = Input(hostname="site.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)
        stored_run = (await session.execute(select(ApplyRun))).scalar_one()
        operation = (await session.execute(select(Operation))).scalar_one()
        apply_state = await session.get(HostApplyState, 1)

    assert result.status == "success"
    assert stored_run.operation_id == operation.id
    assert stored_run.config_revision == result.details["config_revision"]
    assert stored_run.desired_state_hash == result.details["desired_state_hash"]
    assert stored_run.desired_state_json
    assert stored_run.generated_nginx_files_json
    assert "cnc-host-site-example-com.conf" in json.loads(
        stored_run.generated_nginx_files_json
    )
    assert (
        json.loads(stored_run.route_contracts_json)[0]["input_value"]
        == "site.example.com"
    )
    assert json.loads(stored_run.backend_contracts_json)[0]["backend"] == "site"
    assert (
        json.loads(stored_run.runtime_graph_json)["routes"][0]["input_value"]
        == "site.example.com"
    )
    assert json.loads(stored_run.resource_profile_json)["backend_count"] == 0
    assert operation.config_revision == stored_run.config_revision
    assert operation.desired_state_hash == stored_run.desired_state_hash
    assert apply_state is not None
    assert apply_state.last_applied_state_hash == stored_run.desired_state_hash
    assert apply_state.last_successful_operation_id == operation.id
    assert apply_state.last_successful_apply_run_id == stored_run.id
    assert apply_state.last_applied_at is not None


@pytest.mark.asyncio
async def test_run_apply_fails_when_runtime_assets_reconcile_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )

    monkeypatch.setattr(
        "app.services.apply_service.reconcile_managed_systemd_assets",
        lambda _settings: (_ for _ in ()).throw(
            RuntimeError("systemctl daemon-reload failed")
        ),
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "runtime_assets"
    assert "systemctl daemon-reload failed" in str(result.details["error"])


@pytest.mark.asyncio
async def test_apply_app_resource_limits_uses_backend_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        auto_resource_limits=False,
        default_memory_high="300M",
        default_memory_max="420M",
        default_cpu_quota="60%",
    )
    backend = Backend(
        name="web",
        kind="app",
        port=12000,
        base_image="docker.io/library/ubuntu:24.04",
        internal_port=8337,
        workdir="/srv/web",
        install_command="apt-get update",
        start_command="python3 -m http.server 8337",
        env_json="{}",
        volumes_json="[]",
        enabled=True,
        memory_high_override="512M",
        memory_max_override="768M",
        cpu_quota_override="100%",
    )
    base_profile = build_resource_profile(settings, 2)
    commands: list[list[str]] = []
    memory_high = "infinity"
    memory_max = "infinity"

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal memory_high, memory_max
        commands.append(command)
        if command[:2] == ["systemctl", "set-property"]:
            for value in command:
                if value.startswith("MemoryHigh="):
                    memory_high = value.split("=", 1)[1]
                elif value.startswith("MemoryMax="):
                    memory_max = value.split("=", 1)[1]
        stdout = ""
        if command[:2] == ["systemctl", "show"]:
            stdout = "\n".join(
                [
                    "ControlGroup=/cnc-apps.slice/cnc-app-web.service",
                    "MemoryCurrent=268435456",
                    f"MemoryHigh={memory_high}",
                    f"MemoryMax={memory_max}",
                ]
            )
        return CommandResult(command=command, returncode=0, stdout=stdout, stderr="")

    details = await apply_app_resource_limits(
        backend,
        settings,
        base_profile=base_profile,
        services=AppRuntimeServices(run_command_checked_async=fake_run_checked),
    )

    assert details == {
        "memory_high": "512M",
        "memory_max": "768M",
        "cpu_quota": "100%",
        "cpu_shares": 1024,
        "memory_policy": {
            "unit": "cnc-app-web.service",
            "control_group": "/cnc-apps.slice/cnc-app-web.service",
            "changed": True,
            "changed_properties": ["MemoryHigh", "MemoryMax"],
            "memory_current_bytes": 67108864,
            "memory_high_bytes": 536870912,
            "memory_max_bytes": 805306368,
            "memory_max_lowered_below_usage": False,
            "verified": True,
        },
    }
    assert commands == [
        [
            "podman",
            "update",
            "--cpu-shares",
            "1024",
            "--cpus",
            "1",
            container_name("web"),
        ]
    ]


@pytest.mark.asyncio
async def test_reconcile_shield_runtime_restarts_generated_quadlet_without_enable(
    tmp_path: Path,
) -> None:
    settings = _apply_settings(
        tmp_path,
        shield_state_dir=tmp_path / "shield",
        shield_env_file_path=tmp_path / "shield" / "shield.env",
        shield_config_path=tmp_path / "shield" / "shield-config.json",
    )
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    desired = apply_service.DesiredState(
        nginx_files={},
        tailscale_paths={},
        tailscale_services={},
        enabled_app_backends=[],
        known_app_backends=[],
        resource_profile=build_resource_profile(settings, 0),
        route_contracts=[],
        backend_contracts=[],
        shield_required=True,
        shield_output_code_hashes={},
    )

    result = await apply_service._reconcile_shield_runtime(
        desired,
        settings,
        services=apply_service.ApplyServices(
            runtime_services=AppRuntimeServices(
                run_command_checked_async=fake_run_checked
            )
        ),
    )

    assert result["required"] is True
    assert ["systemctl", "daemon-reload"] in commands
    assert ["systemctl", "restart", "cnc-shield.service"] in commands
    assert not [
        command for command in commands if command[:2] == ["systemctl", "enable"]
    ]


@pytest.mark.asyncio
async def test_apply_success_allocates_port_and_publishes_loopback(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        app_container_dns_servers="1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4",
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": False,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
            },
            "NetworkSettings": {
                "Networks": {"cnc-net-web": {"IPAddress": "10.88.0.8"}}
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: False,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

        refreshed = (
            await session.execute(select(Backend).where(Backend.id == backend.id))
        ).scalar_one()
        assert refreshed.port == 12000

    nginx_files = list((tmp_path / "nginx").glob("cnc-host-*.conf"))
    assert len(nginx_files) == 1
    create_commands = [
        command
        for command in commands
        if command[:2] == ["podman", "create"] and "--rootfs" in command
    ]
    assert not create_commands
    quadlet_content = quadlet_container_path(settings, "web").read_text(
        encoding="utf-8"
    )
    assert "PublishPort=127.0.0.1:12000:8337/tcp" in quadlet_content
    assert "Rootfs=" in quadlet_content
    assert "Exec=/sbin/init" in quadlet_content
    assert ["systemctl", "start", "cnc-app-web.service"] in commands
    assert ["nginx", "-t"] in commands
    assert ["systemctl", "reload", "nginx"] in commands


@pytest.mark.asyncio
async def test_apply_configures_tailscale_path_input(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": False,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
            },
            "NetworkSettings": {
                "Networks": {"cnc-net-web": {"IPAddress": "10.88.0.8"}}
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        item = Input(kind="tailnet_path", hostname="/app1", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

    assert ["tailscale", "ip", "-4"] in commands
    assert [
        "tailscale",
        "serve",
        "--bg",
        "--yes",
        "--https=443",
        "--set-path=/app1",
        "http://127.0.0.1:12000",
    ] in commands
    state_payload = json.loads(
        (tmp_path / "tailscale-state.json").read_text(encoding="utf-8")
    )
    assert state_payload["paths"] == {"/app1": "http://127.0.0.1:12000"}
    assert state_payload["services"] == {}


@pytest.mark.asyncio
async def test_apply_rejects_static_tailnet_route_when_root_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
    )
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        backend = Backend(
            name="sample-static",
            kind="static",
            static_root=str(tmp_path / "missing-static-root"),
            enabled=True,
        )
        item = Input(kind="tailnet_path", hostname="/static-test", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "validate"
    assert "static output sample-static root does not exist" in str(
        result.details["error"]
    )
    assert not any(command[:2] == ["tailscale", "serve"] for command in commands)


@pytest.mark.asyncio
async def test_apply_configures_tailscale_service_input(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": False,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["tailscale", "status", "--self", "--json"]:
            commands.append(command)
            return CommandResult(
                command,
                0,
                json.dumps({"Self": {"HostName": "cnc-host", "Tags": ["tag:server"]}}),
                "",
            )
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
            },
            "NetworkSettings": {
                "Networks": {"cnc-net-app-dev": {"IPAddress": "10.88.0.9"}}
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="app-dev",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        item = Input(kind="tailnet_service", hostname="app-dev", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

    assert [
        "tailscale",
        "serve",
        "--yes",
        "--service=svc:app-dev",
        "--https=443",
        "http://127.0.0.1:12000",
    ] in commands
    assert commands.index(["tailscale", "status", "--self", "--json"]) < commands.index(
        [
            "tailscale",
            "serve",
            "--yes",
            "--service=svc:app-dev",
            "--https=443",
            "http://127.0.0.1:12000",
        ]
    )
    state_payload = json.loads(
        (tmp_path / "tailscale-state.json").read_text(encoding="utf-8")
    )
    assert state_payload["paths"] == {}
    assert state_payload["services"] == {"app-dev": "http://127.0.0.1:12000"}


@pytest.mark.asyncio
async def test_apply_tailscale_paths_retries_transient_command_failure(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
        transient_command_retry_attempts=1,
        transient_command_retry_backoff_sec=0.0,
    )
    serve_attempts = 0

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        nonlocal serve_attempts
        if command == ["tailscale", "ip", "-4"]:
            return CommandResult(command, 0, "100.64.0.1", "")
        if command[:4] == ["tailscale", "serve", "--bg", "--yes"]:
            serve_attempts += 1
            if serve_attempts == 1:
                raise CommandError(
                    CommandResult(command, 124, "", "timed out after 300s")
                )
            return CommandResult(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(apply_service, "run_command_checked_async", fake_run_checked)

    payload = await apply_service._apply_tailscale_paths(
        {"/app1": "http://127.0.0.1:12000"},
        settings,
    )

    assert payload["tailscale_paths"] == [
        {"path": "/app1", "target": "http://127.0.0.1:12000"}
    ]
    assert serve_attempts == 2
    state_payload = json.loads(
        settings.tailscale_serve_state_path.read_text(encoding="utf-8")
    )
    assert state_payload["paths"] == {"/app1": "http://127.0.0.1:12000"}


@pytest.mark.asyncio
async def test_apply_tailscale_paths_treats_missing_stale_handler_as_removed(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(tailscale_serve_state_path=tmp_path / "tailscale-state.json")
    settings.tailscale_serve_state_path.write_text(
        json.dumps({"paths": {"/stale": "http://127.0.0.1:12000"}, "services": {}}),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        if command == ["tailscale", "ip", "-4"]:
            return CommandResult(command, 0, "100.64.0.1", "")
        if command in (
            [
                "tailscale",
                "serve",
                "--bg",
                "--yes",
                "--https=443",
                "--set-path=/stale",
                "off",
            ],
            [
                "tailscale",
                "serve",
                "--bg",
                "--yes",
                "--https=443",
                "--set-path=/stale/",
                "off",
            ],
        ):
            raise CommandError(
                CommandResult(
                    command,
                    1,
                    "",
                    "error: failed to remove web serve: handler does not exist",
                )
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(apply_service, "run_command_checked_async", fake_run_checked)

    payload = await apply_service._apply_tailscale_paths({}, settings)

    assert payload["tailscale_paths"] == []
    assert commands == [
        ["tailscale", "ip", "-4"],
        [
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--https=443",
            "--set-path=/stale",
            "off",
        ],
        [
            "tailscale",
            "serve",
            "--bg",
            "--yes",
            "--https=443",
            "--set-path=/stale/",
            "off",
        ],
    ]
    state_payload = json.loads(
        settings.tailscale_serve_state_path.read_text(encoding="utf-8")
    )
    assert state_payload["paths"] == {}


@pytest.mark.asyncio
async def test_apply_tailscale_services_treats_missing_stale_handler_as_removed(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(tailscale_serve_state_path=tmp_path / "tailscale-state.json")
    settings.tailscale_serve_state_path.write_text(
        json.dumps(
            {"paths": {}, "services": {"stale-service": "http://127.0.0.1:12000"}}
        ),
        encoding="utf-8",
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["tailscale", "ip", "-4"]:
            return CommandResult(command, 0, "100.64.0.1", "")
        if command == [
            "tailscale",
            "serve",
            "--yes",
            "--service=svc:stale-service",
            "--https=443",
            "off",
        ]:
            raise CommandError(
                CommandResult(
                    command,
                    1,
                    "",
                    "error: failed to remove web serve: handler does not exist",
                )
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(apply_service, "run_command_checked_async", fake_run_checked)

    payload = await apply_service._apply_tailscale_services({}, settings)

    assert payload["tailscale_services"] == []
    state_payload = json.loads(
        settings.tailscale_serve_state_path.read_text(encoding="utf-8")
    )
    assert state_payload["services"] == {}


@pytest.mark.asyncio
async def test_apply_tailscale_services_removes_stale_services(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(tailscale_serve_state_path=tmp_path / "tailscale-state.json")
    settings.tailscale_serve_state_path.write_text(
        json.dumps(
            {
                "paths": {"/app1": "http://127.0.0.1:12000"},
                "services": {"old-app": "http://127.0.0.1:12001"},
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(apply_service, "run_command_checked_async", fake_run_checked)

    payload = await apply_service._apply_tailscale_services(
        {"app-dev": "http://127.0.0.1:12002"},
        settings,
    )

    assert [
        "tailscale",
        "serve",
        "--yes",
        "--service=svc:old-app",
        "--https=443",
        "off",
    ] in commands
    assert [
        "tailscale",
        "serve",
        "--yes",
        "--service=svc:app-dev",
        "--https=443",
        "http://127.0.0.1:12002",
    ] in commands
    assert payload["tailscale_services"] == [
        {"service": "app-dev", "target": "http://127.0.0.1:12002"}
    ]
    state_payload = json.loads(
        settings.tailscale_serve_state_path.read_text(encoding="utf-8")
    )
    assert state_payload["paths"] == {"/app1": "http://127.0.0.1:12000"}
    assert state_payload["services"] == {"app-dev": "http://127.0.0.1:12002"}


@pytest.mark.asyncio
async def test_run_apply_blocks_tailscale_service_on_untagged_host_before_live_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        tailscale_serve_state_path=tmp_path / "tailscale-state.json",
        port_range_start=12000,
        port_range_end=12010,
    )
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        if command == ["tailscale", "status", "--self", "--json"]:
            return CommandResult(
                command,
                0,
                json.dumps({"Self": {"HostName": "cnc-host", "Tags": None}}),
                "",
            )
        raise AssertionError(
            f"unexpected live command before tailscale service precheck: {command}"
        )

    async def fail_app_runtime(*_args, **_kwargs):
        raise AssertionError(
            "app runtime should not run before tailscale service host precheck"
        )

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fail_app_runtime
    )

    async with maker() as session:
        backend = Backend(
            name="batch",
            kind="app",
            port=12004,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/batch",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(kind="tailnet_service", hostname="os", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.message == "apply failed"
    assert result.details["phase"] == "tailscale_services"
    assert result.details["failure_mode"] == "clean"
    assert result.details["manual_review_required"] is False
    assert "tag-based Tailscale identity" in result.details["error"]
    assert result.details["tailscale_service_host_precheck"] == {
        "ok": False,
        "services": ["os"],
        "tagged_host": False,
        "tags": [],
        "hostname": "cnc-host",
    }
    assert commands == [["tailscale", "status", "--self", "--json"]]
    assert not settings.tailscale_serve_state_path.exists()


@pytest.mark.asyncio
async def test_apply_allocates_around_disabled_runtime_backend(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        app_container_dns_servers="1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4",
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "api-disabled": {
            "created": False,
            "running": False,
            "bootstrapped": False,
            "network_created": False,
        },
        "api-enabled": {
            "created": False,
            "running": False,
            "bootstrapped": False,
            "network_created": False,
        },
    }

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_multi_guest_checked(command, app_state):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        name = container.removeprefix("cnc-app-")
        state = app_state[name]
        if not state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": state["running"],
                "Status": "running" if state["running"] else "configured",
            },
            "NetworkSettings": {
                "Networks": {f"cnc-net-{name}": {"IPAddress": "10.88.0.8"}}
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        name = network.removeprefix("cnc-net-")
        if app_state[name]["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_multi_guest_run(command, app_state)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        disabled_backend = Backend(
            name="api-disabled",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/api-disabled",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=False,
        )
        enabled_backend = Backend(
            name="api-enabled",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/api-enabled",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add_all([disabled_backend, enabled_backend])
        await session.flush()
        item = Input(hostname="api.example.com", enabled=True)
        item.backends = [enabled_backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

        refreshed = (
            (await session.execute(select(Backend).order_by(Backend.id.asc())))
            .scalars()
            .all()
        )
        assert refreshed[0].port == 12000
        assert refreshed[1].port == 12001


@pytest.mark.asyncio
async def test_apply_keeps_host_convergence_when_app_health_fails(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        app_container_dns_servers="1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4",
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {"created": False, "running": False, "bootstrapped": False}

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        if command[:2] == ["python3", "-c"] or command[:1] == ["curl"]:
            raise CommandError(
                CommandResult(
                    command=command, returncode=1, stdout="", stderr="connect failed"
                )
            )
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    "cnc-net-web": {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state)
        if handled is not None:
            return handled
        if command[:1] == ["curl"] or command[:2] == ["python3", "-c"]:
            return CommandResult(command, 1, "", "connect failed")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()
        backend_id = backend.id

        result = await run_apply(session, settings)
        assert result.status == "success"
        assert result.message == "apply completed with app health warnings"
        assert result.details["app_healthcheck_status"] == "degraded"
        assert result.details["app_healthcheck_failures"][0]["backend"] == "web"
        assert {
            item["target"] for item in result.details["app_healthcheck_failures"]
        } == {"loopback"}

        refreshed = (
            await session.execute(select(Backend).where(Backend.id == backend_id))
        ).scalar_one()
        assert refreshed.port == 12000
    assert not (tmp_path / "app-control" / "web" / "failure.json").exists()


@pytest.mark.asyncio
async def test_apply_app_backend_publishes_loopback_directly(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": False,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    "cnc-net-web": {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

    create_commands = [
        command
        for command in commands
        if command[:2] == ["podman", "create"] and "--rootfs" in command
    ]
    assert not create_commands
    quadlet_content = quadlet_container_path(settings, "web").read_text(
        encoding="utf-8"
    )
    assert "PublishPort=127.0.0.1:12000:8337/tcp" in quadlet_content
    assert not [
        command for command in commands if command[:2] == ["systemctl", "enable"]
    ]
    assert ["systemctl", "start", "cnc-net-web-network.service"] in commands
    assert ["systemctl", "start", "cnc-app-web.service"] in commands


@pytest.mark.asyncio
async def test_apply_rolls_back_nginx_files_on_validation_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )
    (tmp_path / "nginx").mkdir(parents=True, exist_ok=True)
    previous = tmp_path / "nginx" / "cnc-host-existing.conf"
    previous.write_text("old-config", encoding="utf-8")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["nginx", "-t"]:
            result = CommandResult(
                command=command, returncode=1, stdout="", stderr="bad config"
            )
            raise CommandError(result)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.flush()
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add(item)
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "error"

    assert previous.read_text(encoding="utf-8") == "old-config"


@pytest.mark.asyncio
async def test_apply_leaves_previous_nginx_when_app_runtime_fails_before_cutover(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    previous = nginx_dir / "cnc-host-web-example-com.conf"
    previous.write_text("old-config", encoding="utf-8")
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        raise apply_service.ApplyFailed("backend converge failed", phase="app_runtime")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()
        previous_run = ApplyRun(
            status="success",
            message="previous apply",
            desired_state_hash="previous",
            details_json="{}",
        )
        session.add(previous_run)
        await session.flush()
        session.add(
            HostApplyState(
                id=1,
                last_applied_state_hash="previous",
                last_successful_apply_run_id=previous_run.id,
            )
        )
        await session.commit()

        result = await run_apply(session, settings)
        apply_state = await session.get(HostApplyState, 1)
        assert result.status == "error"
        assert result.details["phase"] == "app_runtime"
        assert result.message == "apply partially failed"
        assert result.details["failure_mode"] == "partial"
        assert result.details["manual_review_required"] is True
        assert result.details["convergence_invalidated"] is True
        assert apply_state is not None
        assert apply_state.last_successful_apply_run_id is None
        assert apply_state.last_applied_state_hash is None
        assert result.details["nginx_rollback"]["mode"] == "remove_unloaded_candidate"
        assert result.details["nginx_rollback"]["attempted"] is False
        assert result.details["nginx_rollback"]["status"] == "not_staged"

    assert previous.read_text(encoding="utf-8") == "old-config"
    assert commands.count(["systemctl", "reload", "nginx"]) == 0


@pytest.mark.asyncio
async def test_apply_leaves_previous_nginx_when_ssh_fails_before_cutover(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    previous = nginx_dir / "cnc-host-web-example-com.conf"
    previous.write_text("old-config", encoding="utf-8")
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        return {
            "app_containers": ["cnc-app-web"],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": ["cnc-app-web"],
            "app_containers_stopped": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_control_dir": str(tmp_path / "app-control"),
        }

    async def fail_ssh(*_args, **_kwargs):
        raise RuntimeError("ssh alias update exploded")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )
    monkeypatch.setattr(
        "app.services.apply_service.reconcile_backend_ssh_access", fail_ssh
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "error"
        assert result.message == "apply partially failed"
        assert result.details["phase"] == "ssh_access"
        assert result.details["failure_mode"] == "partial"
        assert result.details["manual_review_required"] is True
        assert result.details["nginx_rollback"]["mode"] == "remove_unloaded_candidate"
        assert result.details["nginx_rollback"]["attempted"] is False
        assert result.details["nginx_rollback"]["status"] == "not_staged"

    assert previous.read_text(encoding="utf-8") == "old-config"
    assert commands.count(["systemctl", "reload", "nginx"]) == 0


@pytest.mark.asyncio
async def test_apply_prepares_runtime_and_ssh_before_nginx_cutover(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )
    phase_events: list[str] = []
    progress_events: list[tuple[str, str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if command == ["nginx", "-t"]:
            phase_events.append("nginx_validate")
        elif command == ["systemctl", "reload", "nginx"]:
            phase_events.append("nginx_reload")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        phase_events.append("app_runtime")
        return {
            "app_containers": ["cnc-app-web"],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": ["cnc-app-web"],
            "app_containers_stopped": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_control_dir": str(tmp_path / "app-control"),
        }

    async def fake_reconcile_backend_ssh_access(*_args, **_kwargs):
        phase_events.append("ssh_access")
        return {"ssh_access": {"configured": ["web"]}}

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )
    monkeypatch.setattr(
        "app.services.apply_service.reconcile_backend_ssh_access",
        fake_reconcile_backend_ssh_access,
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(
            session,
            settings,
            services=apply_service.ApplyServices(
                progress_callback=lambda phase, substate: progress_events.append(
                    (phase, substate)
                ),
            ),
        )

    assert result.status == "success"
    assert phase_events == [
        "app_runtime",
        "ssh_access",
        "nginx_validate",
        "nginx_reload",
    ]
    assert result.details["apply_phase_order"][:4] == [
        "app_runtime",
        "ssh_access",
        "sync_files",
        "nginx_validate",
    ]
    assert ("Save desired state", "Building host plan") in progress_events
    assert ("Prepare host", "Checking tailnet service host") not in progress_events
    assert ("Prepare host", "Checking admin exposure") in progress_events
    assert ("Plan runtime", "Inspecting app container") in progress_events
    assert ("Apply host access", "Reconciling backend SSH") in progress_events
    assert ("Apply host access", "Validating host proxy config") in progress_events
    assert ("Verify output", "Verifying admin exposure") in progress_events
    assert ("Verify output", "Auditing control plane") in progress_events
    assert ("Apply host access", "Updating tailnet paths") not in progress_events
    assert ("Apply host access", "Verifying admin exposure") not in progress_events
    assert progress_events.index(
        ("Prepare host", "Checking admin exposure")
    ) < progress_events.index(("Plan runtime", "Inspecting app container"))
    assert progress_events.index(
        ("Verify output", "Verifying admin exposure")
    ) < progress_events.index(("Verify output", "Auditing control plane"))


@pytest.mark.asyncio
async def test_apply_swaps_generated_nginx_tree_without_touching_non_managed_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )
    nginx_dir = tmp_path / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    (nginx_dir / "cnc-host-stale.conf").write_text("stale", encoding="utf-8")
    (nginx_dir / "manual.conf").write_text("manual", encoding="utf-8")

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        return {
            "app_containers": [],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": [],
            "app_containers_stopped": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_control_dir": str(tmp_path / "app-control"),
        }

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

    assert (nginx_dir / "manual.conf").read_text(encoding="utf-8") == "manual"
    assert not (nginx_dir / "cnc-host-stale.conf").exists()
    assert (nginx_dir / "cnc-host-web-example-com.conf").exists()


@pytest.mark.asyncio
async def test_apply_renders_multi_app_upstream_for_shared_hostname(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        return {
            "app_containers": [],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": [],
            "app_containers_stopped": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_control_dir": str(tmp_path / "app-control"),
            "proxy_unit_files": [],
            "proxy_units_started": [],
            "proxy_units_stopped": [],
        }

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )

    async with maker() as session:
        backend_a = Backend(
            name="api-a",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/api-a",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        backend_b = Backend(
            name="api-b",
            kind="app",
            port=12001,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/api-b",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="api.example.com", enabled=True)
        item.backends = [backend_a, backend_b]
        session.add_all([backend_a, backend_b, item])
        await session.commit()

        result = await run_apply(session, settings)
        assert result.status == "success"

    nginx_content = (tmp_path / "nginx" / "cnc-host-api-example-com.conf").read_text(
        encoding="utf-8"
    )
    assert "server 127.0.0.1:12000;" in nginx_content
    assert "server 127.0.0.1:12001;" in nginx_content


@pytest.mark.asyncio
async def test_apply_bootstraps_app_backend_container(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": False,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    "cnc-net-web": {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "failure.json").write_text(
        json.dumps({"phase": "verify", "status": "failed"}), encoding="utf-8"
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update && apt-get install -y python3",
            start_command="python3 -m http.server 8337",
            healthcheck_path="/",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "success"
    assert container_name("web") in result.details["app_containers_created"]
    assert result.details["app_containers_bootstrapped"] == []
    assert (tmp_path / "sandboxes" / "web" / "profile.json").exists()
    assert (tmp_path / "nginx" / "cnc-host-web-example-com.conf").exists()
    create_commands = [
        command
        for command in commands
        if command[:2] == ["podman", "create"] and "--rootfs" in command
    ]
    assert not create_commands
    quadlet_content = quadlet_container_path(settings, "web").read_text(
        encoding="utf-8"
    )
    assert "PublishPort=127.0.0.1:12000:8337/tcp" in quadlet_content
    assert ["systemctl", "start", "cnc-app-web.service"] in commands
    bootstrap_state = json.loads(
        (tmp_path / "app-control" / "web" / "bootstrap.json").read_text(
            encoding="utf-8"
        )
    )
    assert bootstrap_state["status"] == "succeeded"
    assert bootstrap_state["container_name"] == container_name("web")
    assert bootstrap_state["reconcile_phase"] == "steady_state"
    assert bootstrap_state["reconcile_phase_status"] == "succeeded"
    events = bootstrap_state["events"]
    phase_events = [event for event in events if event["kind"] == "phase"]
    assert [f"{event['name']}:{event['status']}" for event in phase_events] == [
        "prepare:running",
        "prepare:succeeded",
        "create:running",
        "create:succeeded",
        "bootstrap:running",
        "bootstrap:succeeded",
        "publish:running",
        "publish:succeeded",
        "verify:running",
        "verify:succeeded",
        "steady_state:running",
        "steady_state:succeeded",
    ]
    seed_events = [event for event in events if event["kind"] == "seed"]
    assert "provision_guest_tools:running" in [
        f"{event['name']}:{event['status']}" for event in seed_events
    ]
    assert "rootfs_activate:succeeded" in [
        f"{event['name']}:{event['status']}" for event in seed_events
    ]
    assert not (tmp_path / "app-control" / "web" / "failure.json").exists()
    phase_names = [
        phase["phase"] for phase in result.details["app_reconcile_phases"]["web"]
    ]
    assert phase_names == [
        "prepare",
        "create",
        "bootstrap",
        "publish",
        "verify",
        "steady_state",
    ]
    assert all(
        phase["status"] == "succeeded"
        for phase in result.details["app_reconcile_phases"]["web"]
    )


@pytest.mark.asyncio
async def test_apply_auto_recovers_rebuild_required_dns_change(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        app_container_dns_servers="9.9.9.9,1.1.1.1",
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {
        "created": True,
        "running": True,
        "bootstrapped": True,
        "network_created": True,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            if command[:3] == ["podman", "rm", "-f"]:
                app_state["created"] = False
                app_state["running"] = False
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: False,
    )

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "spec.json").write_text(
        """
{
  "seed_image": "docker.io/library/ubuntu:24.04",
  "sandbox_profile": "ubuntu-24.04-systemd",
  "guest_rootfs": "/var/lib/cnc/sandboxes/web/rootfs",
  "dns_servers": ["8.8.8.8"],
  "port": 12000,
  "handoff_port": 8337,
  "network": "cnc-net-web",
  "init_command": ["/sbin/init"],
  "volumes": []
}
""".strip(),
        encoding="utf-8",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "success"
    assert ["podman", "rm", "-f", container] in commands
    assert any(command[:2] == ["podman", "create"] for command in commands)
    prepare_phase = result.details["app_reconcile_phases"]["web"][0]
    assert prepare_phase["phase"] == "prepare"
    assert prepare_phase["details"]["auto_repair"] == {
        "reason": "rebuild_required_settings",
        "actions": [
            "podman rm -f cnc-app-web",
            "removed spec.json",
            "removed bootstrap.json",
        ],
        "rebuild_required_keys": ["dns_servers", "guest_rootfs"],
    }


@pytest.mark.asyncio
async def test_apply_auto_recovers_missing_container_with_saved_spec(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {
        "created": False,
        "running": False,
        "bootstrapped": True,
        "network_created": False,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: False,
    )

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "spec.json").write_text(
        json.dumps(
            {
                "network": network,
                "seed_image": "docker.io/library/ubuntu:24.04",
                "sandbox_profile": "ubuntu-24.04-systemd",
                "guest_rootfs": "/var/lib/cnc/sandboxes/web/rootfs",
                "handoff_port": 8337,
                "port": 12000,
                "dns_servers": ["1.1.1.1"],
                "volumes": [],
                "init_command": ["/sbin/init"],
            }
        ),
        encoding="utf-8",
    )
    (control_dir / "bootstrap.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "success"
    assert any(command[:2] == ["podman", "create"] for command in commands)
    prepare_phase = result.details["app_reconcile_phases"]["web"][0]
    assert prepare_phase["details"]["auto_repair"] == {
        "reason": "container_missing_saved_spec",
        "actions": [
            "removed spec.json",
            "removed bootstrap.json",
        ],
    }


@pytest.mark.asyncio
async def test_apply_auto_recovers_missing_saved_spec_with_live_container(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {
        "created": True,
        "running": True,
        "bootstrapped": True,
        "network_created": True,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            if command[:3] == ["podman", "rm", "-f"]:
                app_state["created"] = False
                app_state["running"] = False
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: False,
    )

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "bootstrap.json").write_text(
        json.dumps({"status": "succeeded"}), encoding="utf-8"
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "success"
    assert ["podman", "rm", "-f", container] in commands
    assert any(command[:2] == ["podman", "create"] for command in commands)
    prepare_phase = result.details["app_reconcile_phases"]["web"][0]
    assert prepare_phase["details"]["auto_repair"] == {
        "reason": "missing_saved_spec",
        "actions": [
            "podman rm -f cnc-app-web",
            "removed bootstrap.json",
        ],
    }


@pytest.mark.asyncio
async def test_apply_backfills_legacy_saved_spec_before_reuse(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        app_container_dns_servers="1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4",
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {
        "created": True,
        "running": True,
        "bootstrapped": True,
        "network_created": True,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state, commands=commands):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state, commands=commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "spec.json").write_text(
        json.dumps(
            {
                "seed_image": "docker.io/library/ubuntu:24.04",
                "sandbox_profile": "ubuntu-24.04-systemd",
                "guest_rootfs": str(tmp_path / "sandboxes" / "web" / "rootfs"),
                "handoff_port": 8337,
                "network": "cnc-net-web",
                "init_command": ["/sbin/init"],
                "volumes": [],
            }
        ),
        encoding="utf-8",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12001,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json='{"PYTHONUNBUFFERED":"1"}',
            volumes_json="[]",
            no_new_privileges=False,
            drop_capabilities=False,
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "success"
    saved_spec = json.loads((control_dir / "spec.json").read_text(encoding="utf-8"))
    profile = result.details["resource_profile"]
    assert saved_spec["port"] == 12001
    assert saved_spec["memory_high"] == profile["memory_high"]
    assert saved_spec["memory_max"] == profile["memory_max"]
    assert saved_spec["cpu_quota"] == profile["cpu_quota"]
    assert saved_spec["dns_servers"] == ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"]
    assert result.details["nginx_files"] == ["cnc-host-web-example-com.conf"]
    assert saved_spec["runtime_owner"] == "quadlet"
    assert ["systemctl", "start", "cnc-app-web.service"] in commands


@pytest.mark.asyncio
async def test_apply_rejects_bridge_dns_network_until_explicit_rebuild(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        systemd_generated_dir=tmp_path / "systemd",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "app-control",
        app_quadlet_dir=tmp_path / "quadlets",
        app_container_dns_servers="1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4",
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {
        "created": True,
        "running": True,
        "bootstrapped": True,
        "network_created": True,
    }
    commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        commands.append(command)
        if command[:3] == ["podman", "rm", "-f"]:
            app_state["created"] = False
            app_state["running"] = False
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        if command[:3] == ["podman", "network", "rm"]:
            app_state["network_created"] = False
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        if command[:3] == ["podman", "network", "create"]:
            app_state["network_created"] = True
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        if command[:2] == ["podman", "create"]:
            app_state["created"] = True
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        if command[:2] == ["podman", "start"]:
            app_state["running"] = True
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target,
            "dns_enabled": True,
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if (
            command[:2] == ["podman", "exec"]
            and command[-1] == "test -f /var/lib/cnc/.bootstrap-complete"
        ):
            return CommandResult(command, 0 if app_state["bootstrapped"] else 1, "", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "spec.json").write_text(
        json.dumps(
            {
                "runtime_owner": "quadlet",
                "network": network,
                "base_image": "docker.io/library/ubuntu:24.04",
                "internal_port": 8337,
                "port": 12000,
                "dns_servers": ["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"],
                "volumes": [],
                "no_new_privileges": True,
                "drop_capabilities": True,
            }
        ),
        encoding="utf-8",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "app_network"
    assert result.details["reason"] == "bridge_dns_enabled"
    assert ["podman", "network", "rm", network] not in commands


@pytest.mark.asyncio
async def test_apply_rejects_duplicate_bootstrap_for_same_backend(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    app_state = {
        "created": False,
        "running": False,
        "network_created": False,
        "guest_ready": False,
    }

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        if _handle_single_guest_checked(command, app_state):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(container: str, timeout_sec: int):
        if not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", container], 125, "", "missing"
            ), None
        payload = {
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    "cnc-net-web": {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", container], 0, "[]", ""), payload

    def fake_inspect_network(network: str, timeout_sec: int):
        if app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", network], 0, "[]", ""
            ), {"name": network}
        return CommandResult(
            ["podman", "network", "inspect", network], 125, "", "missing"
        ), None

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        handled = _handle_single_guest_run(command, app_state)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)
    monkeypatch.setattr(
        "app.services.app_runtime.bootstrap_lock_is_held",
        lambda *_args, **_kwargs: True,
    )

    control_dir = tmp_path / "app-control" / "web"
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / "bootstrap.json").write_text(
        json.dumps({"status": "running", "started_at": "2026-03-25T00:00:00Z"}),
        encoding="utf-8",
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=None,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "app_bootstrap"
    assert result.details["bootstrap_lock_active"] is True


@pytest.mark.asyncio
async def test_apply_stops_container_after_bootstrap_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = _apply_settings(
        tmp_path,
        port_range_start=12000,
        port_range_end=12010,
    )
    container = container_name("web")
    network = "cnc-net-web"
    app_state = {"created": False, "running": False, "network_created": False}
    checked_commands: list[list[str]] = []
    run_commands: list[list[str]] = []

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        checked_commands.append(command)
        if _handle_single_guest_checked(command, app_state):
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def fake_inspect(target: str, timeout_sec: int):
        if target != container or not app_state["created"]:
            return CommandResult(
                ["podman", "inspect", target], 125, "", "missing"
            ), None
        payload = {
            "Id": "abc123",
            "State": {
                "Running": app_state["running"],
                "Status": "running" if app_state["running"] else "configured",
                "StartedAt": "2026-03-25T00:00:00Z",
            },
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.88.0.8",
                    }
                }
            },
        }
        return CommandResult(["podman", "inspect", target], 0, "[]", ""), payload

    def fake_inspect_network(target: str, timeout_sec: int):
        if target != network or not app_state["network_created"]:
            return CommandResult(
                ["podman", "network", "inspect", target], 125, "", "missing"
            ), None
        return CommandResult(["podman", "network", "inspect", target], 0, "[]", ""), {
            "name": target
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        if _is_guest_readiness_command(command):
            run_commands.append(command)
            return _guest_readiness_result(command, ready=False)
        handled = _handle_single_guest_run(command, app_state, commands=run_commands)
        if handled is not None:
            return handled
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr("app.services.apply_service.inspect_container", fake_inspect)
    monkeypatch.setattr(
        "app.services.apply_service.inspect_network", fake_inspect_network
    )
    monkeypatch.setattr("app.services.apply_service.run_command", fake_run)

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="exit 1",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        session.add(backend)
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.details["phase"] == "app_bootstrap"
    assert result.details["bootstrap_cleanup"]["action"] == "stopped"
    assert result.details["bootstrap_cleanup"]["command"] == [
        "systemctl",
        "stop",
        f"{container}.service",
    ]
    assert ["systemctl", "stop", f"{container}.service"] in run_commands
    assert app_state["running"] is False


@pytest.mark.asyncio
async def test_run_apply_sends_pushover_alert_on_failure(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
    )
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    async def fake_build_desired_state(_session, _settings):
        raise apply_service.ApplyFailed(
            "nginx explode", phase="nginx_validate", details={"error": "nginx explode"}
        )

    monkeypatch.setattr(
        "app.services.apply_service.send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(
        "app.services.apply_service.build_desired_state", fake_build_desired_state
    )

    async with maker() as session:
        result = await run_apply(session, settings)

    assert result.status == "error"
    assert sent
    assert sent[0]["title"] == "CNC apply failed"
    assert "nginx explode" in str(sent[0]["message"])


@pytest.mark.asyncio
async def test_run_apply_sends_partial_failure_pushover_message(
    monkeypatch, tmp_path: Path
) -> None:
    maker = await _make_session(tmp_path / "app.db")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        nginx_generated_dir=tmp_path / "nginx",
        apply_backup_dir=tmp_path / "backups",
        apply_lock_path=tmp_path / "apply.lock",
        app_quadlet_dir=tmp_path / "quadlets",
        port_range_start=12000,
        port_range_end=12010,
    )
    sent: list[dict[str, object]] = []

    async def fake_notify(_settings, **kwargs):
        sent.append(kwargs)
        return True

    async def fake_run_checked(
        command: list[str], timeout_sec: int = 30
    ) -> CommandResult:
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    async def fake_apply_app_backends(*_args, **_kwargs):
        return {
            "app_containers": ["cnc-app-web"],
            "app_containers_created": [],
            "app_containers_recreated": [],
            "app_containers_bootstrapped": [],
            "app_containers_running": ["cnc-app-web"],
            "app_containers_stopped": [],
            "app_container_dns": {},
            "app_reconcile_phases": {},
            "app_control_dir": str(tmp_path / "app-control"),
        }

    async def fail_ssh(*_args, **_kwargs):
        raise RuntimeError("ssh alias update exploded")

    monkeypatch.setattr(
        "app.services.apply_service.send_pushover_notification_async", fake_notify
    )
    monkeypatch.setattr(
        "app.services.apply_service.run_command_checked_async", fake_run_checked
    )
    monkeypatch.setattr(
        "app.services.apply_service.apply_app_backends", fake_apply_app_backends
    )
    monkeypatch.setattr(
        "app.services.apply_service.reconcile_backend_ssh_access", fail_ssh
    )

    async with maker() as session:
        backend = Backend(
            name="web",
            kind="app",
            port=12000,
            base_image="docker.io/library/ubuntu:24.04",
            internal_port=8337,
            workdir="/srv/web",
            install_command="apt-get update",
            start_command="python3 -m http.server 8337",
            env_json="{}",
            volumes_json="[]",
            enabled=True,
        )
        item = Input(hostname="web.example.com", enabled=True)
        item.backends = [backend]
        session.add_all([backend, item])
        await session.commit()

        result = await run_apply(session, settings)

    assert result.status == "error"
    assert result.message == "apply partially failed"
    assert result.details["convergence_invalidated"] is True
    assert sent
    assert sent[0]["title"] == "CNC apply failed"
    assert "Apply partially failed." in str(sent[0]["message"])
    assert "manual review: required" in str(sent[0]["message"])
