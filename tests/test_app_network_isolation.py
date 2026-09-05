import pytest

from app.config import Settings
from app.models.entities import Backend
from app.services.app_network_isolation import (
    APP_ISOLATION_CHAIN,
    audit_app_network_isolation_guard,
    collect_app_network_isolation_canary,
    reconcile_app_network_isolation,
)
from app.services.commands import CommandResult
from app.services.commands import CommandError
from app.services.runtime_services import AppRuntimeServices


class _IsolationHooks:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def inspect_container(self, _container: str, timeout_sec: int = 30):
        payloads = {
            "cnc-app-web": {
                "NetworkSettings": {
                    "Networks": {"cnc-net-web": {"IPAddress": "10.89.1.2"}}
                },
            },
            "cnc-app-test": {
                "NetworkSettings": {
                    "Networks": {"cnc-net-test": {"IPAddress": "10.89.2.2"}}
                },
            },
        }
        payload = payloads.get(_container)
        return CommandResult(
            ["podman", "inspect"], 0 if payload else 1, "", ""
        ), payload

    def inspect_network(self, network: str, timeout_sec: int = 30):
        payloads = {
            "cnc-net-web": {"subnets": [{"subnet": "10.89.1.0/24"}]},
            "cnc-net-test": {"subnets": [{"subnet": "10.89.2.0/24"}]},
        }
        payload = payloads.get(network)
        return CommandResult(
            ["podman", "network", "inspect", network], 0 if payload else 1, "", ""
        ), payload

    def run_command(self, command: list[str], timeout_sec: int = 30):
        self.commands.append(command)
        if command == ["iptables", "-S", APP_ISOLATION_CHAIN]:
            return CommandResult(
                command,
                0,
                f"-N {APP_ISOLATION_CHAIN}\n-A {APP_ISOLATION_CHAIN} -s 10.89.1.0/24 -d 10.89.2.0/24 -j REJECT\n",
                "",
            )
        if command == ["iptables", "-S", "FORWARD"]:
            return CommandResult(
                command, 0, f"-A FORWARD -j {APP_ISOLATION_CHAIN}\n", ""
            )
        if command == ["iptables-save"]:
            return CommandResult(command, 0, "*filter\nCOMMIT\n", "")
        if command[:1] == ["iptables-restore"]:
            return CommandResult(command, 0, "", "")
        if "getent" in command:
            return CommandResult(command, 2, "", "")
        if "curl" in command:
            return CommandResult(command, 7, "", "connection refused")
        if command[:3] == ["iptables", "-D", "FORWARD"]:
            return CommandResult(command, 1, "", "No such rule")
        return CommandResult(command, 0, "", "")

    async def run_command_checked_async(
        self, command: list[str], timeout_sec: int = 30
    ):
        self.commands.append(command)
        return CommandResult(command, 0, "", "")

    def as_services(self) -> AppRuntimeServices:
        return AppRuntimeServices(
            inspect_container=self.inspect_container,
            inspect_network=self.inspect_network,
            run_command=self.run_command,
            run_command_checked_async=self.run_command_checked_async,
        )


@pytest.mark.asyncio
async def test_reconcile_app_network_isolation_blocks_cross_output_subnets() -> None:
    hooks = _IsolationHooks()
    settings = Settings(command_timeout_apply_sec=5, command_timeout_status_sec=5)

    result = await reconcile_app_network_isolation(
        {"web", "test"},
        settings,
        services=hooks.as_services(),
    )

    assert sorted(result["subnets"]) == ["10.89.1.0/24", "10.89.2.0/24"]
    assert result["rule_count"] == 2
    assert ["iptables", "-N", APP_ISOLATION_CHAIN] in hooks.commands
    assert ["iptables-save"] in hooks.commands
    assert ["iptables", "-F", APP_ISOLATION_CHAIN] in hooks.commands
    assert [
        "iptables",
        "-A",
        APP_ISOLATION_CHAIN,
        "-s",
        "10.89.1.0/24",
        "-d",
        "10.89.2.0/24",
        "-j",
        "REJECT",
    ] in hooks.commands
    assert [
        "iptables",
        "-A",
        APP_ISOLATION_CHAIN,
        "-s",
        "10.89.2.0/24",
        "-d",
        "10.89.1.0/24",
        "-j",
        "REJECT",
    ] in hooks.commands
    assert [
        "iptables",
        "-I",
        "FORWARD",
        "1",
        "-j",
        APP_ISOLATION_CHAIN,
    ] in hooks.commands


@pytest.mark.asyncio
async def test_reconcile_app_network_isolation_restores_snapshot_after_rule_failure() -> (
    None
):
    hooks = _IsolationHooks()
    settings = Settings(command_timeout_apply_sec=5, command_timeout_status_sec=5)

    async def fail_second_rule(command: list[str], timeout_sec: int = 30):
        hooks.commands.append(command)
        if command[:3] == ["iptables", "-A", APP_ISOLATION_CHAIN] and "-d" in command:
            destination = command[command.index("-d") + 1]
            if destination == "10.89.1.0/24":
                raise CommandError(CommandResult(command, 1, "", "append failed"))
        return CommandResult(command, 0, "", "")

    hooks.run_command_checked_async = fail_second_rule

    with pytest.raises(CommandError, match="append failed"):
        await reconcile_app_network_isolation(
            {"web", "test"},
            settings,
            services=hooks.as_services(),
        )

    restore_commands = [
        command for command in hooks.commands if command[:1] == ["iptables-restore"]
    ]
    assert restore_commands


def test_audit_app_network_isolation_guard_reports_installed_chain() -> None:
    hooks = _IsolationHooks()
    settings = Settings(command_timeout_apply_sec=5, command_timeout_status_sec=5)

    result = audit_app_network_isolation_guard(settings, services=hooks.as_services())

    assert result["checked"] is True
    assert result["chain"] == APP_ISOLATION_CHAIN
    assert result["forward_jump"] is True
    assert result["reject_rule_count"] == 1


def test_audit_app_network_isolation_guard_rejects_late_forward_jump() -> None:
    hooks = _IsolationHooks()
    settings = Settings(command_timeout_apply_sec=5, command_timeout_status_sec=5)

    def run_command(command: list[str], timeout_sec: int = 30):
        if command == ["iptables", "-S", "FORWARD"]:
            return CommandResult(
                command,
                0,
                f"-A FORWARD -j ACCEPT\n-A FORWARD -j {APP_ISOLATION_CHAIN}\n",
                "",
            )
        return hooks.run_command(command, timeout_sec)

    services = hooks.as_services()
    services.run_command = run_command

    with pytest.raises(RuntimeError, match="FORWARD chain"):
        audit_app_network_isolation_guard(settings, services=services)


def test_collect_app_network_isolation_canary_passes_when_siblings_are_unreachable(
    tmp_path,
) -> None:
    hooks = _IsolationHooks()
    settings = Settings(
        command_timeout_apply_sec=5,
        command_timeout_status_sec=5,
        app_control_dir=tmp_path / "app-control",
    )
    backends = [
        Backend(name="web", kind="app", enabled=True, handoff_port=8337),
        Backend(name="test", kind="app", enabled=True, handoff_port=8337),
    ]

    result = collect_app_network_isolation_canary(
        backends, settings, services=hooks.as_services()
    )

    assert result["ok"] is True
    assert result["backend_count"] == 2
    assert result["pair_count"] == 2
    assert result["leaks"] == []


def test_collect_app_network_isolation_canary_fails_closed_when_probe_missing(
    tmp_path,
) -> None:
    hooks = _IsolationHooks()
    settings = Settings(
        command_timeout_apply_sec=5,
        command_timeout_status_sec=5,
        app_control_dir=tmp_path / "app-control",
    )
    backends = [
        Backend(name="web", kind="app", enabled=True, handoff_port=8337),
        Backend(name="test", kind="app", enabled=True, handoff_port=8337),
    ]

    def run_command(command: list[str], timeout_sec: int = 30):
        if "curl" in command:
            return CommandResult(command, 127, "", "curl: not found")
        return hooks.run_command(command, timeout_sec)

    services = hooks.as_services()
    services.run_command = run_command

    result = collect_app_network_isolation_canary(backends, settings, services=services)

    assert result["ok"] is False
    assert any(
        item["reason"] == "connect_probe_unavailable" for item in result["unknown"]
    )
