from app.config import Settings
from app.models.entities import Backend
from app.models.entities import Input
from app.services import app_diagnostics
from app.services.app_healthchecks import BackendHealthcheckProbeResult
from app.services.commands import CommandResult


def test_runtime_diagnosis_keeps_unmonitored_health_separate_from_publication() -> None:
    status = app_diagnostics._classify_runtime_diagnosis(
        container_exists=True,
        backend_exec_available=True,
        backend_exec_status="available",
        guest_ready=True,
        target_ip="10.89.1.6",
        healthcheck_enabled=False,
        private_ok=True,
        loopback_publish_present=True,
        backend_port=12001,
        loopback_ok=True,
    )

    assert status == ("ready", "ready", "unmonitored", "app_unmonitored")

    publish_broken = app_diagnostics._classify_runtime_diagnosis(
        container_exists=True,
        backend_exec_available=True,
        backend_exec_status="available",
        guest_ready=True,
        target_ip="10.89.1.6",
        healthcheck_enabled=False,
        private_ok=True,
        loopback_publish_present=False,
        backend_port=12001,
        loopback_ok=True,
    )

    assert publish_broken == (
        "ready",
        "ready",
        "publish_broken",
        "sandbox_publish_broken",
    )


def test_runtime_diagnosis_keeps_guard_contention_distinct_from_guest_failure() -> None:
    busy = app_diagnostics._classify_runtime_diagnosis(
        container_exists=True,
        backend_exec_available=False,
        backend_exec_status="busy",
        guest_ready=False,
        target_ip="10.89.1.6",
        healthcheck_enabled=True,
        private_ok=True,
        loopback_publish_present=True,
        backend_port=12001,
        loopback_ok=True,
    )
    unavailable = app_diagnostics._classify_runtime_diagnosis(
        container_exists=True,
        backend_exec_available=False,
        backend_exec_status="guard_unavailable",
        guest_ready=False,
        target_ip="10.89.1.6",
        healthcheck_enabled=True,
        private_ok=True,
        loopback_publish_present=True,
        backend_port=12001,
        loopback_ok=True,
    )

    assert busy == (
        "ready",
        "observation_deferred",
        "unknown",
        "backend_observation_deferred",
    )
    assert unavailable == (
        "ready",
        "observation_unavailable",
        "unknown",
        "backend_observation_unavailable",
    )


def test_collect_app_backend_diagnostics_omits_inspect_payload_from_error(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_failure_artifact",
        lambda *_args, **_kwargs: {"phase": "verify"},
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=True,
            error="",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running\nactive", "")
            if command[:2] == ["podman", "exec"]
            else CommandResult(command, 0, "", "")
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["ok"] is True
    assert payload["diagnosis"] == "healthy"
    assert payload["sandbox_status"] == "ready"
    assert payload["guest_status"] == "ready"
    assert payload["app_handoff_status"] == "healthy"
    assert payload["inspect_error"] == ""
    assert payload["failure_artifact"] == {"phase": "verify"}
    assert payload["loopback_publish_present"] is True


def test_collect_app_backend_diagnostics_reports_legacy_runtime_maintenance_issue(
    monkeypatch, tmp_path
) -> None:
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "cnc-app-web-legacy-restart.service").write_text(
        "[Service]\n", encoding="utf-8"
    )
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
        systemd_generated_dir=systemd_dir,
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "Config": {"Labels": {}},
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=True,
            error="",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running\nactive", "")
            if command[:2] == ["podman", "exec"]
            else CommandResult(command, 0, "", "")
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["ok"] is True
    assert payload["issues"] == []
    assert payload["runtime_owner"] == "legacy_bridge"
    assert payload["maintenance_issues"] == ["legacy_direct_podman_runtime_owner"]


def test_collect_app_backend_diagnostics_reports_mounted_storage_orphan(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 125, "", "no such object"
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                125,
                "",
                "no such network",
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_bootstrap_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics,
        "_load_external_containers",
        lambda *_args, **_kwargs: [{"Id": "abc123", "Names": ["cnc-app-web"]}],
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics,
        "_load_mounted_containers",
        lambda *_args, **_kwargs: [
            {
                "Id": "abc123",
                "Mountpoint": "/var/lib/containers/storage/overlay/abc/merged",
            }
        ],
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="probe failed",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running\nactive", "")
            if command[:2] == ["podman", "exec"]
            else CommandResult(command, 0, "", "")
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert "storage_orphan" in payload["issues"]
    assert "mounted_storage_orphan" in payload["issues"]
    assert payload["diagnosis"] == "sandbox_missing"
    assert payload["mounted_external_containers"] == [
        {"Id": "abc123", "Mountpoint": "/var/lib/containers/storage/overlay/abc/merged"}
    ]


def test_collect_app_backend_diagnostics_reports_stale_name_registration(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 125, "", "no such object"
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                125,
                "",
                "no such network",
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics, "read_saved_spec", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_bootstrap_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="probe failed",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running\nactive", "")
            if command[:2] == ["podman", "exec"]
            else CommandResult(command, 0, "", "")
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["container_name_registered"] is True
    assert "stale_name_registration" in payload["issues"]


def test_collect_app_backend_diagnostics_classifies_guest_init_broken(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "failed"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="probe skipped",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )

    def fake_run(command, timeout_sec=30):
        if command[:2] == ["podman", "exec"] and command[3:5] == [
            "systemctl",
            "is-system-running",
        ]:
            return CommandResult(command, 1, "", "starting")
        if command[:2] == ["podman", "exec"] and command[3:6] == [
            "systemctl",
            "is-active",
            "multi-user.target",
        ]:
            return CommandResult(command, 3, "", "inactive")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_diagnostics, "run_command", fake_run)

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["diagnosis"] == "guest_init_broken"
    assert payload["sandbox_status"] == "ready"
    assert payload["guest_status"] == "init_broken"
    assert payload["app_handoff_status"] == "unknown"


def test_collect_app_backend_diagnostics_classifies_app_service_down_after_guest_ready(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )

    def fake_probe(backend, host=None, port=None, **_kwargs):
        return BackendHealthcheckProbeResult(
            ok=False,
            error=f"health failed on {host}:{port}",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        )

    monkeypatch.setattr(app_diagnostics, "probe_backend_health", fake_probe)
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running\nactive", "")
            if command[:2] == ["podman", "exec"]
            else CommandResult(command, 0, "", "")
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["diagnosis"] == "app_service_down"
    assert payload["sandbox_status"] == "ready"
    assert payload["guest_status"] == "ready"
    assert payload["app_handoff_status"] == "down"
    assert payload["app_handoff_error"] == "health failed on 10.89.1.6:8337"


def test_collect_app_backend_diagnostics_classifies_unavailable_exec_path(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
        command_timeout_status_sec=3,
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )

    def fake_run(command, timeout_sec=30):
        if command[:3] == ["podman", "exec", "cnc-app-web"]:
            return CommandResult(command, 124, "", f"timed out after {timeout_sec}s")
        raise AssertionError(f"unexpected command after exec probe failed: {command}")

    monkeypatch.setattr(app_diagnostics, "run_command", fake_run)
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="timed out after TCP connect",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["ok"] is False
    assert payload["diagnosis"] == "backend_exec_unavailable"
    assert payload["guest_status"] == "exec_unavailable"
    assert payload["backend_exec_available"] is False
    assert payload["backend_exec_status"] == "timeout"
    assert payload["backend_exec_error"].startswith("timed out after ")
    assert "podman_exec_unavailable" in payload["issues"]
    assert "guest_system_unready" not in payload["issues"]
    assert payload["bootstrap"]["guest_system_state"] == "missing"
    assert payload["bootstrap"]["guest_multi_user_target"] == "missing"


def test_collect_app_backend_diagnostics_defers_on_inspect_timeout(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 124, "", "timed out after 5s"
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(["podman", "network", "inspect", "cnc-net-web"], 0, "", ""),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_bootstrap_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "_container_name_registered",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("name probe must be skipped when inspect times out")
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="timed out",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["diagnosis"] == "sandbox_observation_unavailable"
    assert payload["observation_deferred"] is True
    assert payload["container_observation_available"] is False
    assert payload["observation_issues"] == ["container_inspect_unavailable"]
    assert "container_missing" not in payload["issues"]
    assert "storage_orphan" not in payload["issues"]
    assert "stale_name_registration" not in payload["issues"]


def test_collect_app_backend_diagnostics_reports_network_orphan(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 125, "", "no such object"
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"name":"cnc-net-web"}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics, "read_saved_spec", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_bootstrap_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=False,
            error="probe failed",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 0, "", ""),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["network_exists"] is True
    assert "network_orphan" in payload["issues"]


def test_collect_app_backend_diagnostics_flags_legacy_loopback_proxy_publish(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        systemd_generated_dir=tmp_path / "systemd",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.systemd_generated_dir.mkdir(parents=True, exist_ok=True)
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    (settings.systemd_generated_dir / "cnc-proxy-web.service").write_text(
        "", encoding="utf-8"
    )
    (settings.systemd_generated_dir / "cnc-proxy-web.socket").write_text(
        "", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
    )

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {"PortBindings": {}},
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "probe_backend_health",
        lambda backend, **_kwargs: BackendHealthcheckProbeResult(
            ok=True,
            error="",
            plan=app_diagnostics.resolve_backend_healthcheck(backend),
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 0, "", ""),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["loopback_reachable"] is True
    assert payload["loopback_publish_present"] is False
    assert payload["legacy_loopback_proxy_present"] is True
    assert payload["ok"] is False
    assert "loopback_publish_missing" in payload["issues"]
    assert "legacy_loopback_proxy_present" in payload["issues"]


def test_collect_app_backend_diagnostics_defaults_to_strict_http_with_domain_host_header(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        app_container_resolv_conf_path=tmp_path / "resolv.conf",
    )
    settings.app_container_resolv_conf_path.write_text(
        "nameserver 1.1.1.1\n", encoding="utf-8"
    )
    backend = Backend(
        id=1,
        name="web",
        kind="app",
        port=12001,
        internal_port=8337,
        env_json="{}",
        volumes_json="[]",
        healthcheck_mode=None,
        healthcheck_path=None,
    )
    backend.inputs = [Input(kind="domain", hostname="web.example.com", enabled=True)]

    monkeypatch.setattr(
        app_diagnostics,
        "inspect_container",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "inspect", "cnc-app-web"], 0, '[{"Id":"abc"}]', ""
            ),
            {
                "Id": "abc",
                "State": {
                    "Running": True,
                    "Status": "running",
                    "StartedAt": "2026-03-26T00:00:00Z",
                },
                "HostConfig": {
                    "PortBindings": {
                        "8337/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12001"}],
                    }
                },
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.6"}}
                },
            },
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "inspect_network",
        lambda *_args, **_kwargs: (
            CommandResult(
                ["podman", "network", "inspect", "cnc-net-web"],
                0,
                '[{"dns_enabled":false}]',
                "",
            ),
            {"name": "cnc-net-web", "dns_enabled": False},
        ),
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_saved_spec",
        lambda *_args, **_kwargs: {"dns_servers": ["1.1.1.1"]},
    )
    monkeypatch.setattr(
        app_diagnostics,
        "read_bootstrap_state",
        lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    monkeypatch.setattr(
        app_diagnostics, "read_failure_artifact", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_diagnostics, "bootstrap_state_age_seconds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_external_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics, "_container_name_registered", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        app_diagnostics, "_load_mounted_containers", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        app_diagnostics,
        "run_command",
        lambda command, timeout_sec=30: (
            CommandResult(command, 0, "running", "")
            if command[:2] == ["podman", "exec"]
            and command[3:5] == ["systemctl", "is-system-running"]
            else (
                CommandResult(command, 0, "active", "")
                if command[:2] == ["podman", "exec"]
                and command[3:6] == ["systemctl", "is-active", "multi-user.target"]
                else CommandResult(command, 0, "400", "")
            )
        ),
    )

    payload = app_diagnostics.collect_app_backend_diagnostics(backend, settings)

    assert payload["ok"] is False
    assert payload["healthcheck_mode"] == "http"
    assert payload["healthcheck_effective_mode"] == "http"
    assert payload["healthcheck_path"] == "/"
    assert payload["healthcheck_host_header"] == "web.example.com"
