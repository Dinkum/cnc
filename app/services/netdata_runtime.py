from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from app.config import Settings
from app.services.commands import CommandError
from app.services.host_state import write_text_atomic
from app.services.netdata_constants import (
    NETDATA_KICKSTART_URL,
    NETDATA_SYSTEMD_SERVICE,
)
from app.services.runtime_services import AppRuntimeServices


async def reconcile_netdata_native_runtime(
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> dict[str, Any]:
    install_result = await _install_netdata_if_missing(settings, services=services)
    config_changed = configure_netdata_loopback(settings)
    await services.run_command_checked_async(
        ["systemctl", "enable", "--now", NETDATA_SYSTEMD_SERVICE],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if config_changed:
        await services.run_command_checked_async(
            ["systemctl", "restart", NETDATA_SYSTEMD_SERVICE],
            timeout_sec=settings.command_timeout_apply_sec,
        )
    return {
        "required": True,
        "install": install_result,
        "service": NETDATA_SYSTEMD_SERVICE,
        "config_path": str(settings.netdata_config_path),
        "config_changed": config_changed,
        "port": settings.netdata_port,
    }


def netdata_native_runtime_live_matches(settings: Settings) -> bool:
    return _netdata_binary_exists() and _netdata_config_has_loopback(settings)


def configure_netdata_loopback(settings: Settings) -> bool:
    path = settings.netdata_config_path
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    next_content = _set_netdata_web_config(
        content,
        bind_to="127.0.0.1",
        port=settings.netdata_port,
    )
    if path.exists() and content == next_content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, next_content, encoding="utf-8")
    return True


async def _install_netdata_if_missing(
    settings: Settings,
    *,
    services: AppRuntimeServices,
) -> dict[str, Any]:
    if _netdata_binary_exists():
        return {"changed": False, "reason": "netdata binary already present"}
    installer_path = Path("/tmp/cnc-netdata-kickstart.sh")
    await services.run_command_checked_async(
        ["curl", "-L", NETDATA_KICKSTART_URL, "-o", str(installer_path)],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    try:
        await services.run_command_checked_async(
            [
                "env",
                "DISABLE_TELEMETRY=1",
                "sh",
                str(installer_path),
                "--release-channel",
                "stable",
                "--native-only",
                "--dont-wait",
            ],
            timeout_sec=max(settings.command_timeout_apply_sec, 900),
        )
    except CommandError:
        raise
    return {
        "changed": True,
        "installer": str(installer_path),
        "source": NETDATA_KICKSTART_URL,
        "method": "native-package",
    }


def _netdata_binary_exists() -> bool:
    return shutil.which("netdata") is not None or Path("/usr/sbin/netdata").exists()


def _netdata_config_has_loopback(settings: Settings) -> bool:
    path = settings.netdata_config_path
    if not path.exists():
        return False
    parsed = _netdata_web_config(path.read_text(encoding="utf-8"))
    return parsed.get("bind to") == "127.0.0.1" and parsed.get("default port") == str(
        settings.netdata_port
    )


def _netdata_web_config(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_web_section = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_web_section = stripped[1:-1].strip().lower() == "web"
            continue
        if not in_web_section or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip().lower()] = value.strip()
    return values


def _set_netdata_web_config(content: str, *, bind_to: str, port: int) -> str:
    lines = content.splitlines()
    output: list[str] = []
    in_web_section = False
    saw_web_section = False
    saw_bind = False
    saw_port = False

    for raw_line in lines:
        stripped = raw_line.strip()
        is_section = stripped.startswith("[") and stripped.endswith("]")
        if is_section:
            if saw_web_section and in_web_section:
                if not saw_bind:
                    output.append(f"    bind to = {bind_to}")
                if not saw_port:
                    output.append(f"    default port = {port}")
            in_web_section = stripped[1:-1].strip().lower() == "web"
            saw_web_section = saw_web_section or in_web_section
            output.append(raw_line)
            continue
        if in_web_section and "=" in stripped:
            key, _, _value = stripped.partition("=")
            normalized_key = key.strip().lower()
            if normalized_key == "bind to":
                output.append(f"    bind to = {bind_to}")
                saw_bind = True
                continue
            if normalized_key == "default port":
                output.append(f"    default port = {port}")
                saw_port = True
                continue
        output.append(raw_line)

    if saw_web_section and in_web_section:
        if not saw_bind:
            output.append(f"    bind to = {bind_to}")
        if not saw_port:
            output.append(f"    default port = {port}")
    elif not saw_web_section:
        if output and output[-1].strip():
            output.append("")
        output.extend(
            [
                "[web]",
                f"    bind to = {bind_to}",
                f"    default port = {port}",
            ]
        )

    return "\n".join(output).rstrip() + "\n"
