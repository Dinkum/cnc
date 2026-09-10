from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models.entities import Backend
from app.services.agent_channel import AGENT_CHANNEL_PATH
from app.services.app_healthchecks import (
    display_backend_healthcheck_mode,
    resolve_backend_healthcheck,
)
from app.services.app_containers import (
    APP_DEBUG_TOOLBELT_VERSION,
    app_debug_tool_commands,
)
from app.services.renderers import container_name, network_name
from app.services.validators import ValidationError, parse_volumes_json


BACKEND_SSH_ACCESS_NOTE = (
    "Backend SSH is ordinary OpenSSH restricted to tailnet source addresses, not the Tailscale-managed SSH feature. "
    "Public-IP SSH is for host/root recovery, not backend users."
)


def cached_service_map(status_payload: object) -> dict[str, dict[str, object]]:
    """Normalize optional cached status without triggering live guest probes."""
    if not isinstance(status_payload, dict):
        return {}
    services = status_payload.get("services")
    if not isinstance(services, list):
        return {}
    return {
        service_name: item
        for item in services
        if isinstance(item, dict)
        and isinstance((service_name := item.get("service")), str)
        and service_name
    }


def backend_status_context(
    backend: Backend,
    service_payload: dict[str, object] | None,
) -> tuple[str, str, str, list[str]]:
    status_data = (
        service_payload.get("data") if isinstance(service_payload, dict) else {}
    )
    active_state = str(status_data.get("ActiveState") or "")
    sub_state = str(status_data.get("SubState") or "")
    if not backend.enabled:
        status_value = "disabled"
    elif backend.kind == "static":
        status_value = "static"
    else:
        status_value = active_state or "unknown"
    service_state = (
        f"{active_state or '-'} / {sub_state or '-'}" if backend.kind == "app" else "-"
    )
    diagnosis = str(status_data.get("RuntimeDiagnosis") or "unknown")
    issues = [
        item.strip()
        for item in str(status_data.get("RuntimeIssues") or "").split(",")
        if item.strip() and item.strip() != "-"
    ]
    return status_value, service_state, diagnosis, issues


def build_backend_llm_help_payload(
    backend: Backend,
    *,
    host: str = "SERVER_IP",
    backend_ssh_host: str | None = None,
    llm_help_url: str | None = None,
    status_value: str | None = None,
    service_state: str | None = None,
    runtime_diagnosis: str | None = None,
    runtime_issues: list[str] | None = None,
    target: str | None = None,
    exposure_value: str | None = None,
) -> dict[str, Any]:
    loaded_inputs = backend.__dict__.get("inputs")
    if not isinstance(loaded_inputs, list):
        loaded_inputs = []
    attached_inputs = [
        {
            "kind": str(item.kind or "domain"),
            "value": str(item.hostname or ""),
        }
        for item in sorted(loaded_inputs, key=lambda item: (item.kind, item.hostname))
    ]
    mount_entries, mount_error = _mount_entries(backend)
    healthcheck_plan = resolve_backend_healthcheck(backend)

    backend_host = str(backend_ssh_host or "").strip() or host
    destination = f"{backend.name}@{backend_host}"
    agent_channel_url = (
        f"wss://{host}{AGENT_CHANNEL_PATH}"
        if host != "SERVER_IP"
        else f"wss://<admin-host>{AGENT_CHANNEL_PATH}"
    )
    agent_exec_frame = json_compact(
        {
            "id": "1",
            "type": "exec",
            "output": backend.name,
            "argv": ["python3", "--version"],
        }
    )
    agent_pty_frame = json_compact(
        {
            "id": "pty",
            "type": "pty.open",
            "output": backend.name,
            "shell": "/bin/bash",
            "cols": 120,
            "rows": 32,
        }
    )
    commands: dict[str, str] = {
        "llm_help": f"llm-help {backend.name}",
        "agent_channel": f"websocat -H 'Authorization: Bearer <admin-access-key>' {shlex.quote(agent_channel_url)}",
        "agent_exec_frame": agent_exec_frame,
        "agent_pty_frame": agent_pty_frame,
        "ssh_shell": f"ssh {destination}",
        "ssh_llm_help": f"ssh {destination} llm-help",
        "ssh_exec_example": f"ssh {destination} {shlex.quote('python3 --version')}",
        "host_llm_help_remote": f"ssh root@{host} llm-help",
        "host_llm_help_backend_remote": f"ssh root@{host} {shlex.quote(f'llm-help {backend.name}')}",
        "host_doctor_app_remote": f"ssh root@{host} {shlex.quote(f'cnc-admin app doctor {backend.name}')}",
    }
    if llm_help_url:
        commands["llm_help_url"] = llm_help_url
    if backend.kind == "app":
        commands["agent_background_frame"] = json_compact(
            {
                "id": "submit",
                "type": "exec",
                "output": backend.name,
                "argv": ["bash", "-lc", "cd /app && npm run build"],
                "background": True,
                "request_key": "<unique-request-key>",
            }
        )
        commands["agent_operation_frame"] = json_compact(
            {
                "id": "status",
                "type": "operation.list",
                "output": backend.name,
            }
        )

    warnings = [
        "Beta agent channel is tenant-wide at the WebSocket connection and output-scoped per frame; every exec or PTY frame must name an output.",
        "Use the beta agent channel for fast structured app commands when you have an admin access key; use backend SSH as the universal fallback.",
        "Foreground WebSocket exec defaults to a 60-second deadline even while producing output. Use background=true for long work; it survives disconnects and has no execution deadline unless timeout_sec is supplied.",
        "Generate a unique request_key for each background command and reuse it only for retries of the identical submission. Retained operation IDs support operation.show, operation.logs (byte offset), and operation.cancel; operation.list rediscovers jobs after reconnecting.",
        "A background job reserves this output's managed execution slot. Busy means wait or inspect its operation, not restart the runtime. A wait ending never cancels the job; cancellation is confirmed only by a terminal result.",
        f"Use the literal backend SSH path `ssh {destination}` for app access; it supports ssh command mode, not scp or sftp.",
        BACKEND_SSH_ACCESS_NOTE,
        (
            f"`ssh {destination}` is a forced-command path into the backend sandbox, not a normal host login shell. "
            "Any `sudo` only affects the sandbox."
        ),
        (
            f"This backend runs in Podman container "
            f"{container_name(backend.name) if backend.kind == 'app' else backend.name}; do not guess docker names "
            "or systemd app units."
        ),
        "Do not use podman exec, podman restart, or podman rm directly unless CNC guidance specifically requires it.",
        "Do not edit generated nginx or cnc-proxy systemd files by hand; use CNC apply and fix flows.",
    ]
    if mount_error:
        warnings.append(f"Volume metadata could not be parsed cleanly: {mount_error}")
    warnings.extend(
        [
            "CNC owns the sandbox boundary, loopback publication, and routing handoff only.",
            "App env, install/update flow, guest systemd units, and process topology inside the sandbox are app-owned.",
            "Do not treat CNC apply, fix, or backend metadata as the app's source of truth for env, guest services, or startup.",
        ]
    )

    help_payload: dict[str, Any] = {
        "backend": backend.name,
        "kind": backend.kind,
        "enabled": bool(backend.enabled),
        "status": {
            "value": status_value or _default_status_value(backend),
            "service_state": service_state
            or ("- / -" if backend.kind == "app" else "-"),
            "diagnosis": runtime_diagnosis or "unknown",
            "issues": list(runtime_issues or []),
        },
        "routing": {
            "inputs": attached_inputs,
            "target": target or _default_target(backend),
            "exposure": exposure_value or _default_exposure(backend),
        },
        "runtime": {
            "container": container_name(backend.name) if backend.kind == "app" else "-",
            "network": network_name(backend.name) if backend.kind == "app" else "-",
            "sandbox_profile": backend.sandbox_profile or "-",
            "port": backend.port,
            "handoff_port": backend.handoff_port,
            "healthcheck_mode": display_backend_healthcheck_mode(backend),
            "healthcheck_path": healthcheck_plan.path or "-",
            "healthcheck_host_header": healthcheck_plan.host_header or "-",
            "debug_toolbelt_version": APP_DEBUG_TOOLBELT_VERSION,
            "debug_tools": app_debug_tool_commands(),
            "mounts": mount_entries,
        },
        "commands": commands,
        "warnings": warnings,
    }
    return help_payload


def build_host_llm_help_payload(
    backends: list[Backend],
    *,
    settings: Settings,
    host: str = "SERVER_IP",
    backend_ssh_host: str | None = None,
    host_llm_help_url: str | None = None,
    backend_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    app_backends = [
        backend for backend in backends if str(backend.kind or "").lower() == "app"
    ]
    static_backends = [
        backend for backend in backends if str(backend.kind or "").lower() == "static"
    ]
    backend_ssh_host_value = str(backend_ssh_host or "").strip() or (
        host if host != "SERVER_IP" else "SERVER_IP"
    )
    agent_channel_url = (
        f"wss://{host}{AGENT_CHANNEL_PATH}"
        if host != "SERVER_IP"
        else f"wss://<admin-host>{AGENT_CHANNEL_PATH}"
    )
    commands = {
        "llm_help": "llm-help",
        "agent_channel": f"websocat -H 'Authorization: Bearer <admin-access-key>' {shlex.quote(agent_channel_url)}",
        "agent_exec_frame": '{"id":"1","type":"exec","output":"<backend>","argv":["python3","--version"]}',
        "agent_pty_frame": '{"id":"pty","type":"pty.open","output":"<backend>","shell":"/bin/bash","cols":120,"rows":32}',
        "apply": "cnc-admin host apply",
        "reconcile_host": "cnc-admin host reconcile",
        "cnc_admin_shell": "cnc-admin app shell <backend>",
        "cnc_admin_exec": "cnc-admin app exec <backend> -- <command>",
        "resources": "cnc-admin input list --json\ncnc-admin route list --json\ncnc-admin output list --json",
        "resource_help": "cnc-admin input --help\ncnc-admin route --help\ncnc-admin output --help",
        "background_exec": "cnc-admin output exec <output> --bg --request-key <unique-request-key> --json -- bash -lc 'cd /app && npm run build'",
        "background_status": "cnc-admin operation show <id> --wait 30 --json\ncnc-admin operation logs <id> --follow --json\ncnc-admin operation cancel <id> --json",
        "doctor_app": "cnc-admin app doctor <backend>",
        "logs_app": "cnc-admin app logs <backend>",
        "fix_backend": "cnc-admin fix <backend>",
        "backup_verify": "cnc-admin backup verify <backend>",
        "backup_import": "cnc-admin backup import <bundle> [--backend-name NAME]",
        "exec_backend": f"ssh <backend>@{backend_ssh_host_value} '<command>'",
        "shell_backend": f"ssh <backend>@{backend_ssh_host_value}",
    }
    if host != "SERVER_IP":
        commands["llm_help_remote"] = f"ssh root@{host} llm-help"
        commands["llm_help_backend_remote"] = f"ssh root@{host} 'llm-help <backend>'"
    if host_llm_help_url:
        commands["llm_help_url"] = host_llm_help_url

    help_payload: dict[str, Any] = {
        "host": host,
        "cnc": {
            "admin_service": "cnc-admin.service",
            "admin_bind": f"{settings.admin_host}:{settings.admin_port}",
            "runtime_manager": "podman",
            "app_control_dir": str(settings.app_control_dir),
            "backend_backup_dir": str(settings.backend_backup_dir),
            "updater_script_path": str(settings.updater_script_path),
            "backend_ssh_model": "ordinary OpenSSH restricted to tailnet source addresses; forced-command path into podman exec; not the Tailscale-managed SSH feature",
            "backend_count": len(backends),
            "app_backend_count": len(app_backends),
            "static_backend_count": len(static_backends),
        },
        "workflow": {
            "start_here": [
                "Run llm-help before guessing how this host is wired.",
                "Use cnc-admin app doctor <backend> before restarting or editing runtime state.",
                "Use cnc-admin app logs <backend> for app startup failures and runtime crashes.",
                "Use cnc-admin fix <backend> when CNC already classifies CNC-owned backend drift or publication problems.",
            ],
            "do": [
                "Manage configuration with input, route, and output commands; route connect/disconnect changes existing input-output links through normal CNC apply.",
                "For long commands use output exec --bg or WebSocket background=true. Connection/wait deadlines do not stop background jobs; inspect the returned operation ID before retrying.",
                "Treat app outputs as Podman containers named cnc-app-<backend>.",
                "Use the beta agent channel for fast structured output commands when an admin access key is available.",
                "Treat `ssh <backend>@<host>` as the container entry path, not as a host shell account.",
                BACKEND_SSH_ACCESS_NOTE,
                "Read mounted app paths and control assets before changing live runtime files.",
                "Prefer CNC apply, doctor, fix, and restore commands over manual host edits.",
            ],
            "do_not": [
                "Do not use docker commands on this host; CNC app runtimes are managed with Podman.",
                "Do not use podman exec, podman restart, or podman rm directly unless CNC guidance specifically requires it.",
                "Do not guess cnc-app-<backend>.service; app outputs are not normal systemd services.",
                "Do not edit /etc/nginx/generated or /etc/systemd/system/cnc-proxy-*.service by hand unless you are intentionally doing emergency surgery.",
                "Do not assume container private IPs are stable across restarts.",
                "Do not use scp or sftp against `ssh <backend>@<host>` backend paths.",
            ],
            "tips": [
                "If public traffic is failing, verify both the edge route and the backend doctor output.",
                "If a backend boots but loopback fails, inspect the proxy or loopback publish path instead of changing app code first.",
                "If you hotfix live code on a mounted path, verify the file is non-empty and syntax-valid before restarting the container.",
            ],
        },
        "commands": commands,
        "backends": backend_payloads or [],
    }
    return help_payload


def build_backend_operator_commands(payload: dict[str, Any]) -> list[dict[str, str]]:
    commands = (
        payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
    )
    ssh_shell = str(commands.get("ssh_shell") or "ssh <backend>@SERVER_IP")
    ssh_llm_help = str(commands.get("ssh_llm_help") or f"{ssh_shell} llm-help")
    ssh_exec = str(
        commands.get("ssh_exec_example") or "ssh <backend>@SERVER_IP '<command>'"
    )
    llm_handoff_lines = [
        "This app is hosted in a service called CNC.",
        "Start with the backend-specific brief instead of a root host shell:",
        ssh_llm_help,
        "If you need to run app commands, stay on the backend SSH path:",
        ssh_shell,
        ssh_exec,
        "Do not start from a root host shell for per-app work.",
    ]
    llm_handoff = "\n".join(llm_handoff_lines)
    return [
        {
            "label": "ssh shell",
            "command": ssh_shell,
            "note": "Interactive shell inside this app container.",
        },
        {
            "label": "ssh exec",
            "command": ssh_exec,
            "note": "One-shot command. Replace `python3 --version` with any command.",
        },
        {
            "label": "llm handoff",
            "command": llm_handoff,
            "note": "Paste this into another LLM so it starts from CNC guidance instead of guessing.",
        },
    ]


def render_backend_llm_help_text(payload: dict[str, Any]) -> str:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    commands = (
        payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
    )
    warnings = (
        payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    )
    inputs = routing.get("inputs") if isinstance(routing.get("inputs"), list) else []
    backend_name = str(payload.get("backend") or "backend")
    ssh_shell = str(commands.get("ssh_shell") or f"ssh {backend_name}@SERVER_IP")
    refresh_command = _command_or_dash(
        commands, "ssh_llm_help", "llm_help_remote", "llm_help"
    )
    local_health_command = _backend_local_health_command(runtime)
    public_health_command = _backend_public_health_command(routing, runtime)
    lines = [
        f"# CNC Backend Context: {backend_name}",
        "",
        "## What CNC Is",
        "- CNC is the host control plane for multi-project hosting.",
        "- App backends run as Podman containers behind CNC-managed routing.",
        f"- Fastest safe app access is `{ssh_shell}`; it enters the app container, not the host shell.",
        f"- `{ssh_shell}` is not a host login for updater, systemctl, firewall, or filesystem work under `/home/{backend_name}`.",
        "- Keep this brief scoped to backend-shell work; if you need host-side CNC controls, stop and switch to host-level `llm-help`.",
        "",
        "## LLM Rules",
        f"- Use `{refresh_command}` once to refresh CNC context before making assumptions about this backend.",
        "- If llm-help returns its bounded unavailable message, do not retry it in a loop; one ordinary backend command is the guest-boundary test.",
        f"- MUST treat `{ssh_shell}` as a backend-sandbox path, not as host access.",
        "- DO NOT treat CNC metadata, apply, or fix as the source of truth for app env, startup, or process roles inside the sandbox.",
        "- DO NOT use raw Podman or generated host-file edits as a first-line recovery path from the backend shell.",
        "- DO NOT keep debugging from the backend shell once the container path cannot exec, the container is inactive, or host-level CNC commands are required.",
        "- After one backend exec timeout, stop shell, doctor, and agent-channel probes; use one host-only app-log collection, then switch to bounded CNC recovery.",
        "- IF backend-local health passes and public traffic still fails, classify the incident as CNC/host-side until proven otherwise.",
        "- IF the sandbox is up but the handoff port fails health, classify the incident as app/runtime-side inside the sandbox until proven otherwise.",
    ]
    lines.extend(
        [
            "",
            "## Backend State",
            _format_kv_block(
                [
                    ("backend", backend_name),
                    ("kind", payload.get("kind") or "-"),
                    ("enabled", "yes" if payload.get("enabled") else "no"),
                    ("status", status.get("value") or "-"),
                    ("service_state", status.get("service_state") or "-"),
                    ("diagnosis", status.get("diagnosis") or "unknown"),
                    ("issues", _format_env_keys(status.get("issues"))),
                    ("container", runtime.get("container") or "-"),
                    ("sandbox_profile", runtime.get("sandbox_profile") or "-"),
                ]
            ),
            "",
            "## Routing",
            _format_kv_block(
                [
                    ("inputs", _format_inputs(inputs)),
                    ("target", routing.get("target") or "-"),
                    ("exposure", routing.get("exposure") or "-"),
                    (
                        "ports",
                        _format_runtime_ports(
                            runtime.get("port"), runtime.get("handoff_port")
                        ),
                    ),
                    (
                        "healthcheck",
                        _format_healthcheck(
                            runtime.get("healthcheck_mode"),
                            runtime.get("healthcheck_path"),
                        ),
                    ),
                ]
            ),
            "",
            "## Sandbox Boundary",
            _format_kv_block(
                [
                    ("sandbox_profile", runtime.get("sandbox_profile") or "-"),
                    ("debug_toolbelt", runtime.get("debug_toolbelt_version") or "-"),
                    ("debug_tools", _format_env_keys(runtime.get("debug_tools"))),
                    ("mounts", _format_mounts(runtime.get("mounts"))),
                ]
            ),
        ]
    )
    lines.extend(f"- {item}" for item in warnings if str(item).strip())
    lines.extend(
        [
            "- CNC creates a persistent Linux guest sandbox, publishes the loopback handoff, and probes the declared health contract.",
            "- PID 1 in the sandbox is guest init; app services should be installed and supervised with normal Linux systemd tooling inside the guest.",
            "- The app inside the sandbox owns env, install/update logic, guest service state, and process topology.",
            "- If you need to change app env, update scripts, or guest supervisor state, do it through the app's own tooling inside the sandbox instead of CNC metadata.",
            "- Quarantine large app trees beside their source so the move stays on one filesystem; do not assume `/tmp` is persistent or shares the source filesystem.",
            "- If you need to change sandbox profile, mounts, loopback publication, or healthcheck contract, do it through CNC.",
            f"- The sandbox shell includes CNC debug tools `{', '.join(app_debug_tool_commands())}` when the base image supports a standard package manager.",
            "- `sudo` may be available for compatibility with VPS-style app runbooks, but it only elevates inside the sandbox and never grants host access.",
            "",
            "## App Onboarding Inside The Guest",
            "- Treat the sandbox like a small VPS: install packages, app files, and service dependencies inside the guest with normal Linux tooling.",
            "- Create the app's own systemd unit under `/etc/systemd/system/<app>.service` inside the guest.",
            "- Use normal guest service commands such as `sudo systemctl daemon-reload` and `sudo systemctl enable --now <app>.service` inside the sandbox.",
            f"- The app service should bind the declared handoff port inside the guest, currently `{runtime.get('handoff_port') or '<handoff-port>'}`.",
            "- Do not create CNC-specific launcher hooks, wrapper scripts, or CNC-owned service metadata for app startup.",
            "",
            "## Verify Next",
            _format_command_block(
                [
                    "# sandbox-local health with CNC's configured contract",
                    local_health_command,
                    "",
                    "# host-side CNC diagnosis for the published sandbox",
                    _command_or_dash(commands, "host_doctor_app_remote"),
                    "",
                    "# public route check when the backend has an enabled domain",
                    public_health_command,
                ]
            ),
            "- If the sandbox path dies, the local health probe fails, or the host doctor output turns unhealthy, stop trusting backend-shell app fixes and switch to host-level CNC recovery.",
            "",
            "## Escalate To Host",
            f"- Stay on the backend sandbox path by default: `{ssh_shell}`.",
            "- Escalate to host-level control only when the next required action is not possible through the container path.",
            "- Escalate when you need `cnc-admin`, host logs, proxy or routing checks, firewall or systemd inspection, or CNC fix/apply flows.",
            "- Escalate when the backend SSH/container path cannot exec, the container is inactive or exited, or backend-local checks show the issue is outside the app container.",
            "- If the next action needs host/root access, stop backend-shell work and ask the user to run the root escalation command; include the exact command, why it is needed, and what output to return.",
            f"- Before interacting at the host level, run `{_command_or_dash(commands, 'host_llm_help_remote')}` to refresh host context and supported CNC actions.",
            _format_command_block(
                [
                    "# Host-wide CNC brief: shows the host control-plane view, supported CNC workflows,",
                    "# backend inventory, and the correct host-side recovery commands.",
                    _command_or_dash(commands, "host_llm_help_remote"),
                    "",
                    "# Host-side brief for one backend: shows the same backend-focused CNC guidance as",
                    "# backend llm-help, but through the host path so it still works during backend outages.",
                    _command_or_dash(commands, "host_llm_help_backend_remote"),
                    "",
                    "# Host-side diagnosis for one backend: checks the managed backend state and reports",
                    "# concrete CNC/runtime issues before you restart, repair, or edit anything.",
                    _command_or_dash(commands, "host_doctor_app_remote"),
                ]
            ),
            "",
            f"- Stop backend-shell recovery if the app answers on `127.0.0.1:{runtime.get('handoff_port') or '<handoff-port>'}` but public routing still fails; that is a host-side CNC problem until proven otherwise.",
            "- Stop backend-shell recovery if the next idea is using `cnc-admin`, reading host logs, touching firewall state, or editing generated proxy/systemd files.",
            "- In those cases, switch to the host-level commands above rather than trying to smuggle host operations through the backend shell.",
            "",
            "## Commands",
            _render_backend_command_block(commands),
        ]
    )
    return "\n".join(lines)


def render_host_llm_help_text(payload: dict[str, Any]) -> str:
    cnc = payload.get("cnc") if isinstance(payload.get("cnc"), dict) else {}
    workflow = (
        payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
    )
    commands = (
        payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
    )
    backends = (
        payload.get("backends") if isinstance(payload.get("backends"), list) else []
    )
    host = payload.get("host") or "SERVER_IP"
    lines = [
        "# CNC Host Context",
        "",
        "## What CNC Is",
        "- CNC is the host control plane for multi-project app and static hosting.",
        "- App backends run as Podman containers; public traffic is routed through CNC-managed proxy/config.",
        "- `cnc-admin` is the supported host control surface for diagnostics, fix flows, and recovery.",
        "- `ssh <backend>@<host>` is a forced-command entry path into the backend container, not a normal host shell account.",
        f"- {BACKEND_SSH_ACCESS_NOTE}",
        "",
        "## LLM Rules",
        "- MUST use `cnc-admin app doctor <backend>` before manual restart, repair, or rollback decisions.",
        "- MUST stop repeated guest probes after one backend exec timeout and use CNC's host-only evidence and recovery path.",
        "- MUST treat unhealthy doctor output, inactive containers, missing loopback publication, or proxy/runtime drift as CNC-owned recovery work.",
        "- MUST distinguish fault ownership from recovery authority: an app-owned failure can still require CNC host recovery when guest exec is unavailable.",
        "- DO NOT assume a successful backend-side update means CNC runtime convergence is complete.",
        "- DO NOT edit generated proxy, systemd, firewall, or container-IP wiring by hand unless you are intentionally doing emergency surgery.",
        "- IF the app can boot in a fresh one-shot process but the managed runtime is wedged, classify the primary fault as app/runtime and the immediate recovery path as CNC-owned.",
        "- IF the app release changes runtime contract such as env, process roles, bind port, or health path, backend-side update alone is insufficient; host-level CNC must verify and converge.",
        "",
        "## Host Summary",
        _format_kv_block(
            [
                ("host", host),
                ("admin_service", cnc.get("admin_service") or "-"),
                ("runtime_manager", cnc.get("runtime_manager") or "-"),
                ("backend_ssh_model", cnc.get("backend_ssh_model") or "-"),
                ("backend_count", cnc.get("backend_count") or 0),
                ("app_backend_count", cnc.get("app_backend_count") or 0),
                ("static_backend_count", cnc.get("static_backend_count") or 0),
            ]
        ),
        "",
        "## Workflow",
    ]
    lines.extend(f"- {item}" for item in _string_list(workflow.get("start_here")))
    lines.extend(f"- {item}" for item in _string_list(workflow.get("do")))
    lines.extend(f"- {item}" for item in _string_list(workflow.get("tips")))
    lines.extend(
        [
            "",
            "## Do Not",
        ]
    )
    lines.extend(f"- {item}" for item in _string_list(workflow.get("do_not")))
    lines.extend(
        [
            "",
            "## Sample Commands",
            _render_host_command_block(commands),
            "",
            "## Backend Inventory",
        ]
    )
    if not backends:
        lines.append("- none")
        return "\n".join(lines)

    for item in backends:
        if not isinstance(item, dict):
            continue
        backend_name = str(item.get("backend") or "backend")
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        routing = item.get("routing") if isinstance(item.get("routing"), dict) else {}
        inputs = (
            routing.get("inputs") if isinstance(routing.get("inputs"), list) else []
        )
        lines.extend(
            [
                f"- `{backend_name}`: status={status.get('value') or 'unknown'}; inputs={_format_inputs(inputs)}; target={routing.get('target') or '-'}",
            ]
        )
    return "\n".join(lines)


def _default_status_value(backend: Backend) -> str:
    if not backend.enabled:
        return "disabled"
    if backend.kind == "static":
        return "static"
    return "unknown"


def _default_target(backend: Backend) -> str:
    if backend.kind == "app":
        return (
            f"http://127.0.0.1:{backend.port}" if backend.port else "port auto on apply"
        )
    return backend.static_root or "-"


def _default_exposure(backend: Backend) -> str:
    if backend.kind == "app":
        if backend.port:
            return f"127.0.0.1:{backend.port}:{backend.handoff_port}/tcp"
        return f"127.0.0.1:AUTO:{backend.handoff_port}/tcp"
    return backend.static_root or "-"


def _mount_entries(backend: Backend) -> tuple[list[dict[str, str]], str | None]:
    if backend.kind == "static":
        if not backend.static_root:
            return [], None
        return [
            {
                "declared": backend.static_root,
                "host_path": backend.static_root,
                "target_path": "-",
            }
        ], None
    try:
        volumes = parse_volumes_json(backend.volumes_json)
    except ValidationError as exc:
        return [], str(exc)
    entries: list[dict[str, str]] = []
    for volume in volumes:
        source, _, remainder = volume.partition(":")
        if not source.startswith("/"):
            continue
        target_path = remainder.split(":", 1)[0] if remainder else ""
        entries.append(
            {
                "declared": volume,
                "host_path": str(Path(source)),
                "target_path": target_path or "-",
            }
        )
    return entries, None


def _format_inputs(inputs: list[object]) -> str:
    labels: list[str] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "domain")
        value = str(item.get("value") or "")
        if value:
            labels.append(f"{kind}: {value}")
    return ", ".join(labels) if labels else "none"


def _format_env_keys(value: Any) -> str:
    if not isinstance(value, list):
        return "none"
    labels = [str(item) for item in value if isinstance(item, str) and item.strip()]
    return ", ".join(labels) if labels else "none"


def _format_mounts(mounts: list[object]) -> str:
    labels: list[str] = []
    for item in mounts:
        if not isinstance(item, dict):
            continue
        host_path = str(item.get("host_path") or "")
        target_path = str(item.get("target_path") or "-")
        if host_path:
            labels.append(f"{host_path}:{target_path}")
    return ", ".join(labels) if labels else "none"


def _format_kv_block(items: list[tuple[str, Any]]) -> str:
    lines = ["```text"]
    lines.extend(f"{key}: {value}" for key, value in items)
    lines.append("```")
    return "\n".join(lines)


def _format_runtime_ports(port: Any, handoff_port: Any) -> str:
    host_port = port if port is not None else "-"
    app_port = handoff_port if handoff_port is not None else "-"
    return f"{host_port} -> {app_port}/tcp"


def _format_healthcheck(mode: Any, path: Any) -> str:
    mode_label = str(mode or "-")
    path_label = str(path or "-")
    if path_label == "-" or path_label == "":
        return mode_label
    return f"{mode_label} ({path_label})"


def _format_command_block(commands: list[str]) -> str:
    lines = ["```bash"]
    lines.extend(command for command in commands if str(command).strip())
    lines.append("```")
    return "\n".join(lines)


def _backend_local_health_command(runtime: dict[str, Any]) -> str:
    port = runtime.get("handoff_port") or "<handoff-port>"
    mode = str(runtime.get("healthcheck_mode") or "-").strip().lower()
    path = str(runtime.get("healthcheck_path") or "/").strip() or "/"
    if path == "-":
        path = "/"
    host_header = str(runtime.get("healthcheck_host_header") or "-").strip()
    if mode == "none":
        return "# no health probe configured"
    if mode == "tcp":
        return f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2).close()\""
    command = [
        "curl",
        "-fsS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}\\n",
        "--max-time",
        "2",
    ]
    if host_header and host_header != "-":
        command.extend(["-H", shlex.quote(f"Host: {host_header}")])
    command.append(shlex.quote(f"http://127.0.0.1:{port}{path}"))
    return " ".join(command)


def _backend_public_health_command(
    routing: dict[str, Any], runtime: dict[str, Any]
) -> str:
    inputs = routing.get("inputs") if isinstance(routing.get("inputs"), list) else []
    domain = next(
        (
            str(item.get("value") or "").strip()
            for item in inputs
            if isinstance(item, dict)
            and str(item.get("kind") or "").strip().lower() == "domain"
            and str(item.get("value") or "").strip()
        ),
        "",
    )
    if not domain:
        return "# no public domain input configured"
    path = str(runtime.get("healthcheck_path") or "/").strip() or "/"
    if path == "-":
        path = "/"
    return f"curl -fsS -o /dev/null -w '%{{http_code}}\\n' --max-time 5 {shlex.quote(f'https://{domain}{path}')}"


def _render_backend_command_block(commands: dict[str, Any]) -> str:
    lines = ["```bash"]
    refresh_command = _command_or_dash(commands, "ssh_llm_help", "llm_help")
    if refresh_command != "-":
        lines.extend(
            [
                "# refresh this backend brief",
                refresh_command,
            ]
        )
    for key, comment in [
        (
            "agent_channel",
            "beta tenant-wide WebSocket channel for structured output commands",
        ),
        ("agent_exec_frame", "send this frame on the agent channel to run one command"),
        (
            "agent_background_frame",
            "long work: replace request_key with a unique value; reuse it only when retrying this submission",
        ),
        (
            "agent_operation_frame",
            "rediscover retained command operations after reconnecting",
        ),
        (
            "agent_pty_frame",
            "send this frame on the agent channel to open an interactive PTY",
        ),
        ("ssh_shell", "primary app shell inside the backend container"),
        ("ssh_exec_example", "run one command inside the backend container"),
    ]:
        if commands.get(key):
            lines.extend([f"# {comment}", str(commands[key])])
    lines.append("```")
    return "\n".join(lines)


def _render_host_command_block(commands: dict[str, Any]) -> str:
    lines = ["```bash"]
    for key, comment in [
        ("llm_help_remote", "refresh host context on the server"),
        ("llm_help_backend_remote", "refresh one backend context on the server"),
        ("llm_help", "refresh host context locally on the server"),
        ("apply", "converge all managed runtime state on the server"),
    ]:
        if commands.get(key):
            lines.extend([f"# {comment}", str(commands[key])])
    for key, comment in [
        ("agent_channel", "open the beta tenant-wide WebSocket command channel"),
        ("agent_exec_frame", "send one output exec frame on the agent channel"),
        ("agent_pty_frame", "send one output PTY frame on the agent channel"),
        ("cnc_admin_shell", "open a backend shell using CNC"),
        ("cnc_admin_exec", "run one backend command using CNC"),
        ("resources", "inspect inputs, connections, and outputs on the CNC host"),
        ("resource_help", "discover supported configuration flags"),
        ("background_exec", "submit a guest-supervised command from the CNC host"),
        (
            "background_status",
            "inspect, follow, or cancel the returned command operation",
        ),
        ("doctor_app", "run CNC diagnostics for one backend"),
        ("logs_app", "inspect app logs for one backend"),
        ("fix_backend", "run CNC fix for one backend"),
        ("shell_backend", "open an app container shell"),
        ("exec_backend", "run one command inside an app container"),
    ]:
        if commands.get(key):
            lines.extend([f"# {comment}", str(commands[key])])
    lines.append("```")
    return "\n".join(lines)


def _command_or_dash(commands: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = commands.get(key)
        if value:
            return str(value)
    return "-"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def json_compact(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))
