from pathlib import Path

from app.config import Settings
from app.services import runtime_assets
from app.services.commands import CommandResult


MANAGED_ASSET_NAMES = [
    "cnc-admin.service",
    "cnc-auto-size.service",
    "cnc-auto-size.timer",
    "cnc-backend-alerts.service",
    "cnc-backend-alerts.timer",
    "cnc-cloudflare-sync.service",
    "cnc-cloudflare-sync.timer",
    "cnc-update-check.service",
    "cnc-update-check.timer",
    "cnc-edge-public-ingress.slice",
    "cnc-edge-tailnet.slice",
    "cnc-admin-control.slice",
    "cnc-apps.slice",
]


def _write_packaged_systemd_assets(packaging_dir: Path) -> None:
    packaging_dir.mkdir()
    systemd_dir = packaging_dir / "systemd"
    systemd_dir.mkdir()
    service_text = "[Service]\nExecStart=test\n"
    timer_text = "[Timer]\nOnCalendar=hourly\n"
    slice_text = "[Slice]\nMemoryAccounting=yes\nCPUAccounting=yes\n"
    (packaging_dir / "cnc-admin.service").write_text(service_text, encoding="utf-8")
    (packaging_dir / "cnc-auto-size.service").write_text(service_text, encoding="utf-8")
    (packaging_dir / "cnc-auto-size.timer").write_text(timer_text, encoding="utf-8")
    (packaging_dir / "cnc-backend-alerts.service").write_text(
        service_text, encoding="utf-8"
    )
    (packaging_dir / "cnc-backend-alerts.timer").write_text(
        "[Timer]\nOnUnitActiveSec=2min\n", encoding="utf-8"
    )
    (packaging_dir / "cnc-cloudflare-sync.service").write_text(
        service_text, encoding="utf-8"
    )
    (packaging_dir / "cnc-cloudflare-sync.timer").write_text(
        timer_text, encoding="utf-8"
    )
    (packaging_dir / "cnc-update-check.service").write_text(
        service_text, encoding="utf-8"
    )
    (packaging_dir / "cnc-update-check.timer").write_text(timer_text, encoding="utf-8")
    (systemd_dir / "cnc-edge-public-ingress.slice").write_text(
        slice_text, encoding="utf-8"
    )
    (systemd_dir / "cnc-edge-tailnet.slice").write_text(slice_text, encoding="utf-8")
    (systemd_dir / "cnc-admin-control.slice").write_text(slice_text, encoding="utf-8")
    (systemd_dir / "cnc-apps.slice").write_text(slice_text, encoding="utf-8")


def test_inspect_managed_systemd_assets_reports_drift_without_mutating(
    monkeypatch, tmp_path: Path
) -> None:
    packaging_dir = tmp_path / "packaging"
    systemd_dir = tmp_path / "systemd"
    _write_packaged_systemd_assets(packaging_dir)
    wrapper_path = tmp_path / "bin" / "cnc-ssh-backend-root"
    settings = Settings(ssh_backend_root_wrapper_path=wrapper_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["systemctl", "is-enabled"]:
            return CommandResult(command, 1, "disabled", "")
        if command[:2] == ["systemctl", "is-active"]:
            return CommandResult(command, 3, "inactive", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(runtime_assets, "run_command", fake_run)

    payload = runtime_assets.inspect_managed_systemd_assets(
        settings,
        packaging_dir=packaging_dir,
        systemd_unit_dir=systemd_dir,
    )

    assert payload["changed_units"] == MANAGED_ASSET_NAMES
    assert payload["changed_files"] == [str(wrapper_path)]
    assert payload["daemon_reload_needed"] is True
    assert payload["admin_restart_required"] is True
    assert payload["timer_reconcile_needed"] is True
    assert (systemd_dir / "cnc-auto-size.service").exists() is False
    assert wrapper_path.exists() is False
    assert ["systemctl", "daemon-reload"] not in commands
    assert ["systemctl", "enable", "--now", "cnc-auto-size.timer"] not in commands


def test_reconcile_managed_systemd_assets_installs_missing_units_and_enables_timer(
    monkeypatch, tmp_path: Path
) -> None:
    packaging_dir = tmp_path / "packaging"
    systemd_dir = tmp_path / "systemd"
    _write_packaged_systemd_assets(packaging_dir)
    wrapper_path = tmp_path / "bin" / "cnc-ssh-backend-root"
    settings = Settings(ssh_backend_root_wrapper_path=wrapper_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["systemctl", "is-enabled"]:
            return CommandResult(command, 1, "disabled", "")
        if command[:2] == ["systemctl", "is-active"]:
            return CommandResult(command, 3, "inactive", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(runtime_assets, "run_command", fake_run)

    payload = runtime_assets.reconcile_managed_systemd_assets(
        settings,
        packaging_dir=packaging_dir,
        systemd_unit_dir=systemd_dir,
    )

    assert payload["changed_units"] == MANAGED_ASSET_NAMES
    assert payload["changed_files"] == [str(wrapper_path)]
    assert payload["daemon_reloaded"] is True
    assert payload["admin_restart_required"] is True
    assert payload["timer_reconciled"] is True
    assert (systemd_dir / "cnc-admin.service").exists()
    assert (systemd_dir / "cnc-auto-size.service").exists()
    assert (systemd_dir / "cnc-auto-size.timer").exists()
    assert (systemd_dir / "cnc-backend-alerts.service").exists()
    assert (systemd_dir / "cnc-backend-alerts.timer").exists()
    assert (systemd_dir / "cnc-cloudflare-sync.service").exists()
    assert (systemd_dir / "cnc-cloudflare-sync.timer").exists()
    assert (systemd_dir / "cnc-update-check.service").exists()
    assert (systemd_dir / "cnc-update-check.timer").exists()
    assert (systemd_dir / "cnc-edge-public-ingress.slice").exists()
    assert (systemd_dir / "cnc-edge-tailnet.slice").exists()
    assert (systemd_dir / "cnc-admin-control.slice").exists()
    assert (
        wrapper_path.read_text(encoding="utf-8")
        == runtime_assets.backend_ssh_root_wrapper_script()
    )
    assert wrapper_path.stat().st_mode & 0o777 == 0o755
    assert ["systemctl", "daemon-reload"] in commands
    assert ["systemctl", "enable", "--now", "cnc-auto-size.timer"] in commands
    assert ["systemctl", "enable", "--now", "cnc-backend-alerts.timer"] in commands
    assert ["systemctl", "enable", "--now", "cnc-cloudflare-sync.timer"] in commands
    assert ["systemctl", "enable", "--now", "cnc-update-check.timer"] in commands


def test_reconcile_managed_systemd_assets_skips_reload_when_units_match(
    monkeypatch, tmp_path: Path
) -> None:
    packaging_dir = tmp_path / "packaging"
    systemd_dir = tmp_path / "systemd"
    _write_packaged_systemd_assets(packaging_dir)
    systemd_dir.mkdir()
    for source_name, destination_name in runtime_assets.MANAGED_SYSTEMD_ASSETS:
        source = packaging_dir / source_name
        destination = systemd_dir / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    wrapper_path = tmp_path / "bin" / "cnc-ssh-backend-root"
    settings = Settings(ssh_backend_root_wrapper_path=wrapper_path)
    wrapper_path.parent.mkdir(parents=True)
    wrapper_path.write_text(
        runtime_assets.backend_ssh_root_wrapper_script(), encoding="utf-8"
    )
    wrapper_path.chmod(0o755)
    commands: list[list[str]] = []

    def fake_run(command: list[str], timeout_sec: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["systemctl", "is-enabled"]:
            return CommandResult(command, 0, "enabled", "")
        if command[:2] == ["systemctl", "is-active"]:
            return CommandResult(command, 0, "active", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr(runtime_assets, "run_command", fake_run)

    payload = runtime_assets.reconcile_managed_systemd_assets(
        settings,
        packaging_dir=packaging_dir,
        systemd_unit_dir=systemd_dir,
    )

    assert payload["changed_units"] == []
    assert payload["changed_files"] == []
    assert payload["daemon_reloaded"] is False
    assert payload["admin_restart_required"] is False
    assert payload["timer_reconciled"] is False
    assert ["systemctl", "daemon-reload"] not in commands
    assert ["systemctl", "enable", "--now", "cnc-auto-size.timer"] not in commands
    assert ["systemctl", "enable", "--now", "cnc-backend-alerts.timer"] not in commands
    assert ["systemctl", "enable", "--now", "cnc-cloudflare-sync.timer"] not in commands
    assert ["systemctl", "enable", "--now", "cnc-update-check.timer"] not in commands
