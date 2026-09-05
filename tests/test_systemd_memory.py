import pytest

from app.services.commands import CommandResult
from app.services.systemd_memory import (
    SystemdMemoryPolicyError,
    converge_systemd_memory_policy,
    memory_value_bytes,
)


class _MemoryServices:
    def __init__(
        self,
        *,
        apply_changes: bool = True,
        control_group: str = "/cnc-apps.slice/cnc-app-worker.service",
    ) -> None:
        self.apply_changes = apply_changes
        self.control_group = control_group
        self.commands: list[list[str]] = []
        self.current = "419430400"
        self.high = "343M"
        self.maximum = "515M"

    async def run_command_checked_async(
        self, command: list[str], timeout_sec: float = 30
    ) -> CommandResult:
        self.commands.append(command)
        if command[:2] == ["systemctl", "set-property"] and self.apply_changes:
            for value in command:
                if value.startswith("MemoryHigh="):
                    self.high = value.split("=", 1)[1]
                elif value.startswith("MemoryMax="):
                    self.maximum = value.split("=", 1)[1]
        stdout = ""
        if command[:2] == ["systemctl", "show"]:
            stdout = "\n".join(
                [
                    f"ControlGroup={self.control_group}",
                    f"MemoryCurrent={self.current}",
                    f"MemoryHigh={self.high}",
                    f"MemoryMax={self.maximum}",
                ]
            )
        return CommandResult(command, 0, stdout, "")


@pytest.mark.asyncio
async def test_converge_systemd_memory_policy_applies_and_verifies_live_values() -> (
    None
):
    services = _MemoryServices()

    result = await converge_systemd_memory_policy(
        "cnc-app-worker.service",
        services=services,
        timeout_sec=5,
        memory_high="384M",
        memory_max="576M",
        require_apps_slice=True,
    )

    assert result["verified"] is True
    assert result["changed_properties"] == ["MemoryHigh", "MemoryMax"]
    assert result["memory_high_bytes"] == 384 * 1024 * 1024
    assert result["memory_max_bytes"] == 576 * 1024 * 1024
    assert not any(
        command[:2] == ["systemctl", "restart"] for command in services.commands
    )


@pytest.mark.asyncio
async def test_converge_systemd_memory_policy_fails_when_live_values_do_not_change() -> (
    None
):
    services = _MemoryServices(apply_changes=False)

    with pytest.raises(SystemdMemoryPolicyError) as excinfo:
        await converge_systemd_memory_policy(
            "cnc-app-worker.service",
            services=services,
            timeout_sec=5,
            memory_high="384M",
            memory_max="576M",
            require_apps_slice=True,
        )

    assert set(excinfo.value.details["mismatches"]) == {"MemoryHigh", "MemoryMax"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("384M", 384 * 1024 * 1024),
        ("1.5G", int(1.5 * 1024**3)),
        ("infinity", None),
        ("max", None),
        ("malformed", None),
        (-1, None),
    ],
)
def test_memory_value_bytes_handles_systemd_values(
    value: int | str, expected: int | None
) -> None:
    assert memory_value_bytes(value) == expected


@pytest.mark.asyncio
async def test_converge_systemd_memory_policy_skips_live_write_when_already_current() -> (
    None
):
    services = _MemoryServices()

    result = await converge_systemd_memory_policy(
        "cnc-app-worker.service",
        services=services,
        timeout_sec=5,
        memory_high="343M",
        memory_max="515M",
        require_apps_slice=True,
    )

    assert result["changed"] is False
    assert result["changed_properties"] == []
    assert len(services.commands) == 2
    assert not any(
        command[:2] == ["systemctl", "set-property"] for command in services.commands
    )


@pytest.mark.asyncio
async def test_converge_systemd_memory_policy_rejects_service_outside_apps_slice() -> (
    None
):
    services = _MemoryServices(control_group="/system.slice/cnc-app-worker.service")

    with pytest.raises(SystemdMemoryPolicyError, match="not inside cnc-apps.slice"):
        await converge_systemd_memory_policy(
            "cnc-app-worker.service",
            services=services,
            timeout_sec=5,
            memory_high="384M",
            memory_max="576M",
            require_apps_slice=True,
        )

    assert len(services.commands) == 1
    assert not any(
        command[:2] == ["systemctl", "set-property"] for command in services.commands
    )
