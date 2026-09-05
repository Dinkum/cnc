import json

from app.config import Settings
from app.models.entities import Backend
from app.services import app_repair
from app.services.commands import CommandResult


def test_repair_app_backend_clears_stale_runtime_state(monkeypatch, tmp_path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    backend_dir = settings.app_control_dir / "web"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "spec.json").write_text("{}", encoding="utf-8")
    (backend_dir / "bootstrap.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    (backend_dir / "bootstrap.lock").write_text("", encoding="utf-8")

    diagnostics = [
        {
            "backend": "web",
            "issues": ["container_missing", "bootstrap_failed"],
            "saved_spec_present": True,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "failed"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_repair, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert "removed spec.json" in payload["actions"]
    assert not (backend_dir / "spec.json").exists()
    assert not (backend_dir / "bootstrap.json").exists()
    assert not (backend_dir / "bootstrap.lock").exists()


def test_repair_app_backend_recreates_bridge_dns_runtime(monkeypatch, tmp_path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    backend_dir = settings.app_control_dir / "web"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "spec.json").write_text("{}", encoding="utf-8")
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["bridge_dns_enabled"],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": False,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]

    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_repair, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "rm", "-f", "cnc-app-web"] in commands
    assert ["podman", "network", "rm", "cnc-net-web"] in commands


def test_repair_app_backend_unmounts_storage_orphan_before_removal(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["storage_orphan", "mounted_storage_orphan"],
            "saved_spec_present": True,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "failed"},
            "external_containers": [{"Id": "abc123", "Names": ["cnc-app-web"]}],
            "mounted_external_containers": [
                {"Id": "abc123", "Mountpoint": "/overlay/abc/merged"}
            ],
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert commands[:2] == [
        ["podman", "unmount", "--force", "abc123"],
        ["podman", "rm", "--storage", "--force", "abc123"],
    ]


def test_repair_app_backend_removes_container_without_saved_spec(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["missing_saved_spec"],
            "saved_spec_present": False,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]

    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "rm", "-f", "cnc-app-web"] in commands


def test_repair_app_backend_clears_stale_name_registration(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["stale_name_registration"],
            "saved_spec_present": False,
            "container_exists": False,
            "container_name_registered": True,
            "network_exists": False,
            "bootstrap": {"status": "missing"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "container_name_registered": False,
            "network_exists": False,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "rm", "-f", "cnc-app-web"] in commands


def test_repair_app_backend_removes_orphan_network(monkeypatch, tmp_path) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["network_orphan"],
            "saved_spec_present": False,
            "container_exists": False,
            "container_name_registered": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "container_name_registered": False,
            "network_exists": False,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "network", "rm", "cnc-net-web"] in commands


def test_repair_app_backend_clears_failed_bootstrap_runtime(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    backend_dir = settings.app_control_dir / "web"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "spec.json").write_text("{}", encoding="utf-8")
    (backend_dir / "bootstrap.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["bootstrap_failed"],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "failed"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_repair, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "rm", "-f", "cnc-app-web"] in commands
    assert "removed spec.json" in payload["actions"]
    assert not (backend_dir / "bootstrap.json").exists()


def test_repair_app_backend_clears_stuck_bootstrap_runtime(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    backend_dir = settings.app_control_dir / "web"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "spec.json").write_text("{}", encoding="utf-8")
    (backend_dir / "bootstrap.json").write_text(
        json.dumps({"status": "running"}), encoding="utf-8"
    )

    diagnostics = [
        {
            "backend": "web",
            "issues": ["bootstrap_stuck"],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "stuck", "lock_active": False, "stale": True},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]
    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_repair, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        app_repair,
        "run_command",
        lambda command, timeout_sec=30: CommandResult(command, 0, "", ""),
    )

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert not (backend_dir / "spec.json").exists()
    assert not (backend_dir / "bootstrap.json").exists()


def test_repair_app_backend_restarts_container_for_loopback_outage(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["loopback_unreachable"],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
            "ok": True,
        },
    ]

    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["systemctl", "restart", "cnc-app-web.service"] in commands


def test_repair_app_backend_clears_legacy_loopback_proxy_state(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
        systemd_generated_dir=tmp_path / "systemd",
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
    backend_dir = settings.app_control_dir / "web"
    backend_dir.mkdir(parents=True, exist_ok=True)
    settings.systemd_generated_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "spec.json").write_text("{}", encoding="utf-8")
    (backend_dir / "bootstrap.json").write_text("{}", encoding="utf-8")
    (backend_dir / "bootstrap.lock").write_text("", encoding="utf-8")
    (settings.systemd_generated_dir / "cnc-proxy-web.service").write_text(
        "", encoding="utf-8"
    )
    (settings.systemd_generated_dir / "cnc-proxy-web.socket").write_text(
        "", encoding="utf-8"
    )
    commands: list[list[str]] = []

    diagnostics = [
        {
            "backend": "web",
            "issues": ["loopback_publish_missing", "legacy_loopback_proxy_present"],
            "saved_spec_present": True,
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
        },
        {
            "backend": "web",
            "issues": [],
            "saved_spec_present": False,
            "container_exists": False,
            "network_exists": True,
            "bootstrap": {"status": "missing"},
            "ok": True,
        },
    ]

    monkeypatch.setattr(
        app_repair,
        "collect_app_backend_diagnostics",
        lambda *_args, **_kwargs: diagnostics.pop(0),
    )
    monkeypatch.setattr(
        app_repair, "bootstrap_lock_is_held", lambda *_args, **_kwargs: False
    )

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert payload["changed"] is True
    assert payload["ok"] is True
    assert ["podman", "rm", "-f", "cnc-app-web"] in commands
    assert [
        "systemctl",
        "stop",
        "cnc-proxy-web.socket",
        "cnc-proxy-web.service",
    ] in commands
    assert ["systemctl", "daemon-reload"] in commands
    assert not (settings.systemd_generated_dir / "cnc-proxy-web.service").exists()
    assert not (settings.systemd_generated_dir / "cnc-proxy-web.socket").exists()
    assert not (backend_dir / "spec.json").exists()


def test_repair_app_backend_exec_unavailable_restarts_quadlet_once(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(
        app_control_dir=tmp_path / "app-control",
        apply_lock_path=tmp_path / "apply.lock",
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
    commands: list[list[str]] = []
    diagnostic_calls = 0

    def fake_diagnostics(*_args, **_kwargs):
        nonlocal diagnostic_calls
        diagnostic_calls += 1
        return {
            "backend": "web",
            "diagnosis": "backend_exec_unavailable",
            "issues": ["backend_exec_timeout", "loopback_unreachable"],
            "container_exists": True,
            "network_exists": True,
            "bootstrap": {"status": "succeeded"},
        }

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(app_repair, "collect_app_backend_diagnostics", fake_diagnostics)
    monkeypatch.setattr(app_repair, "run_command", fake_run)

    payload = app_repair.repair_app_backend(backend, settings)

    assert diagnostic_calls == 1
    assert payload["plan"] == [{"action": "restart_quadlet"}]
    assert payload["ok"] is True
    assert commands == [["systemctl", "restart", "cnc-app-web.service"]]


def test_repair_plan_deduplicates_overlapping_restart_issues() -> None:
    plan = app_repair.plan_app_backend_repair(
        {
            "diagnosis": "app_service_down",
            "issues": ["private_unreachable", "loopback_unreachable"],
            "container_exists": True,
            "bootstrap": {"status": "succeeded"},
        }
    )

    assert plan == [{"action": "restart_quadlet"}]


def test_repair_plan_does_not_restart_for_exec_guard_contention() -> None:
    for exec_status in ("busy", "guard_unavailable"):
        plan = app_repair.plan_app_backend_repair(
            {
                "diagnosis": "backend_exec_unavailable",
                "backend_exec_status": exec_status,
                "container_exists": True,
            }
        )

        assert plan == []


def test_repair_plan_never_mutates_while_guest_observation_is_deferred() -> None:
    destructive_issues = [
        "bootstrap_failed",
        "network_missing",
        "private_ip_missing",
        "loopback_unreachable",
    ]

    for diagnosis in (
        "backend_observation_deferred",
        "backend_observation_unavailable",
        "sandbox_observation_unavailable",
    ):
        plan = app_repair.plan_app_backend_repair(
            {
                "diagnosis": diagnosis,
                "issues": destructive_issues,
                "container_exists": True,
                "network_exists": True,
                "bootstrap": {"status": "failed"},
            }
        )

        assert plan == []
