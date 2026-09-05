from pathlib import Path

import pytest

from app.config import Settings
from app.services.app_memory_slice import reconcile_apps_slice
from app.services.commands import CommandError, CommandResult
from app.services.resource_profile import build_resource_profile
from app.services.systemd_memory import SystemdMemoryPolicyError


class _Services:
    def __init__(
        self, *, fail_verify: bool = False, apply_live_change: bool = True
    ) -> None:
        self.commands: list[list[str]] = []
        self.fail_verify = fail_verify
        self.apply_live_change = apply_live_change
        self.memory_max = "infinity"

    async def run_command_checked_async(
        self, command: list[str], timeout_sec: float = 30
    ) -> CommandResult:
        self.commands.append(command)
        result = CommandResult(command, 0, "", "")
        if self.fail_verify and command[:2] == ["systemd-analyze", "verify"]:
            raise CommandError(CommandResult(command, 1, "", "invalid unit"))
        if command[:2] == ["systemctl", "set-property"] and self.apply_live_change:
            self.memory_max = next(
                value.split("=", 1)[1]
                for value in command
                if value.startswith("MemoryMax=")
            )
        if command[:2] == ["systemctl", "show"]:
            result.stdout = "\n".join(
                [
                    "ControlGroup=/cnc-apps.slice",
                    "MemoryCurrent=268435456",
                    "MemoryHigh=infinity",
                    f"MemoryMax={self.memory_max}",
                ]
            )
        return result


@pytest.mark.asyncio
async def test_reconcile_apps_slice_installs_budget_and_verifies(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.services.resource_profile._detect_total_memory_bytes",
        lambda: 1024 * 1024 * 1024,
    )
    settings = Settings(
        systemd_generated_dir=tmp_path,
        auto_memory_reserve_percent=25,
    )
    profile = build_resource_profile(settings, 1)
    services = _Services()

    result = await reconcile_apps_slice(profile, settings, services=services)

    drop_in = tmp_path / "cnc-apps.slice.d" / "50-memory.conf"
    assert "MemoryMax=805306368" in drop_in.read_text(encoding="utf-8")
    assert result["memory_max_bytes"] == 805306368
    assert services.commands == [
        ["systemctl", "daemon-reload"],
        ["systemd-analyze", "verify", str(tmp_path / "cnc-apps.slice")],
        [
            "systemctl",
            "show",
            "cnc-apps.slice",
            "--property=ControlGroup,MemoryCurrent,MemoryHigh,MemoryMax",
        ],
        [
            "systemctl",
            "set-property",
            "--runtime",
            "cnc-apps.slice",
            "MemoryMax=805306368",
        ],
        [
            "systemctl",
            "show",
            "cnc-apps.slice",
            "--property=ControlGroup,MemoryCurrent,MemoryHigh,MemoryMax",
        ],
    ]
    assert result["live_policy"]["verified"] is True


@pytest.mark.asyncio
async def test_reconcile_apps_slice_restores_previous_drop_in_on_verify_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.services.resource_profile._detect_total_memory_bytes",
        lambda: 1024 * 1024 * 1024,
    )
    settings = Settings(systemd_generated_dir=tmp_path)
    profile = build_resource_profile(settings, 1)
    drop_in = tmp_path / "cnc-apps.slice.d" / "50-memory.conf"
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text("previous\n", encoding="utf-8")
    services = _Services(fail_verify=True)

    with pytest.raises(CommandError):
        await reconcile_apps_slice(profile, settings, services=services)

    assert drop_in.read_text(encoding="utf-8") == "previous\n"
    assert services.commands[-1] == ["systemctl", "daemon-reload"]


@pytest.mark.asyncio
async def test_reconcile_apps_slice_restores_drop_in_when_live_policy_does_not_converge(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "app.services.resource_profile._detect_total_memory_bytes",
        lambda: 1024 * 1024 * 1024,
    )
    settings = Settings(systemd_generated_dir=tmp_path)
    profile = build_resource_profile(settings, 1)
    drop_in = tmp_path / "cnc-apps.slice.d" / "50-memory.conf"
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text("previous\n", encoding="utf-8")
    services = _Services(apply_live_change=False)

    with pytest.raises(SystemdMemoryPolicyError, match="did not converge"):
        await reconcile_apps_slice(profile, settings, services=services)

    assert drop_in.read_text(encoding="utf-8") == "previous\n"
    assert services.commands[-1] == ["systemctl", "daemon-reload"]
