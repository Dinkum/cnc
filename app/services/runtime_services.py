from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services import app_containers as app_container_runtime
from app.services import commands as command_runtime
from app.services.commands import CommandResult


InspectPayload = dict[str, Any] | None
InspectContainerFn = Callable[[str, int], tuple[CommandResult, InspectPayload]]
InspectNetworkFn = Callable[[str, int], tuple[CommandResult, InspectPayload]]
RunCommandFn = Callable[[list[str], int], CommandResult]
RunCommandCheckedAsyncFn = Callable[[list[str], int], Awaitable[CommandResult]]


@dataclass
class AppRuntimeServices:
    inspect_container: InspectContainerFn = app_container_runtime.inspect_container
    inspect_network: InspectNetworkFn = app_container_runtime.inspect_network
    run_command: RunCommandFn = command_runtime.run_command
    run_command_checked_async: RunCommandCheckedAsyncFn = (
        command_runtime.run_command_checked_async
    )


def default_app_runtime_services() -> AppRuntimeServices:
    return AppRuntimeServices()
