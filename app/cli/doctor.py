from __future__ import annotations

import argparse
import asyncio

from app.cli.common import (
    CommandResult,
    bind_command,
    load_backend_async,
    load_shield_status_payload,
)
from app.config import Settings


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    doctor = subparsers.add_parser("doctor")
    doctor_subparsers = doctor.add_subparsers(dest="doctor_command", required=True)

    doctor_app = doctor_subparsers.add_parser("app")
    doctor_app.add_argument("backend")
    doctor_app.add_argument("--json", action="store_true", dest="json_output")
    bind_command(
        doctor_app,
        handler=_run_doctor_app_command,
        formatter=format_doctor_text,
        needs_settings=True,
    )


def format_doctor_text(payload: dict[str, object]) -> str:
    bootstrap = (
        payload.get("bootstrap") if isinstance(payload.get("bootstrap"), dict) else {}
    )
    state = (
        bootstrap.get("state")
        if isinstance(bootstrap, dict) and isinstance(bootstrap.get("state"), dict)
        else {}
    )
    diagnosis = str(payload.get("diagnosis") or "").strip()
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    maintenance_issues = (
        payload.get("maintenance_issues")
        if isinstance(payload.get("maintenance_issues"), list)
        else []
    )
    observation_issues = (
        payload.get("observation_issues")
        if isinstance(payload.get("observation_issues"), list)
        else []
    )
    diagnosis_text = {
        "healthy": "healthy",
        "app_unmonitored": "guest and publication ready; app health unmonitored",
        "backend_observation_deferred": "guest observation deferred by another CNC command",
        "backend_observation_unavailable": "guest observation guard unavailable",
        "sandbox_observation_unavailable": "sandbox observation unavailable",
        "backend_exec_unavailable": "backend exec unavailable",
        "sandbox_missing": "sandbox missing",
        "guest_init_broken": "guest init broken",
        "sandbox_publish_broken": "guest systemd up but sandbox publish path broken",
        "app_service_down": "guest systemd up but app service down",
    }.get(diagnosis, diagnosis or "-")
    cgroup = payload.get("cgroup") if isinstance(payload.get("cgroup"), dict) else {}
    memory = cgroup.get("memory") if isinstance(cgroup.get("memory"), dict) else {}
    memory_events = (
        memory.get("events") if isinstance(memory.get("events"), dict) else {}
    )
    pids = cgroup.get("pids") if isinstance(cgroup.get("pids"), dict) else {}
    parent_slice = (
        cgroup.get("parent_slice")
        if isinstance(cgroup.get("parent_slice"), dict)
        else {}
    )
    parent_memory = (
        parent_slice.get("memory")
        if isinstance(parent_slice.get("memory"), dict)
        else {}
    )

    def reachability(name: str) -> str:
        health_status = str(payload.get(f"{name}_health_status") or "")
        if health_status == "unmonitored":
            return "unmonitored"
        value = payload.get(f"{name}_reachable")
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "unknown"

    lines = [
        f"backend: {payload.get('backend')}",
        f"container: {payload.get('container')}",
        f"ok: {'yes' if payload.get('ok') else 'no'}",
        f"diagnosis: {diagnosis_text}",
        f"sandbox_status: {payload.get('sandbox_status') or '-'}",
        f"guest_status: {payload.get('guest_status') or '-'}",
        f"app_handoff_status: {payload.get('app_handoff_status') or '-'}",
        f"runtime_owner: {payload.get('runtime_owner_label') or payload.get('runtime_owner') or '-'}",
        f"backend_exec_available: {'yes' if payload.get('backend_exec_available') else 'no'}",
        f"backend_exec_status: {payload.get('backend_exec_status') or '-'}",
        f"backend_exec_error: {payload.get('backend_exec_error') or '-'}",
        f"backend_exec_circuit: {'open' if payload.get('backend_exec_circuit') else 'closed'}",
        f"issues: {', '.join(str(item) for item in issues) if issues else 'none'}",
        "observation_issues: "
        + (
            ", ".join(str(item) for item in observation_issues)
            if observation_issues
            else "none"
        ),
        "maintenance_issues: "
        + (
            ", ".join(str(item) for item in maintenance_issues)
            if maintenance_issues
            else "none"
        ),
        f"private_ip: {payload.get('private_ip') or '-'}",
        f"private_reachable: {reachability('private')}",
        f"loopback_port: {payload.get('loopback_port') or '-'}",
        f"loopback_reachable: {reachability('loopback')}",
        f"cgroup_process_path: {cgroup.get('process_path') or '-'}",
        f"cgroup_service_path: {cgroup.get('service_path') or '-'}",
        f"cgroup_selected_path: {cgroup.get('selected_path') or cgroup.get('path') or '-'}",
        f"cgroup_memory: {memory.get('current', '-')} / {memory.get('max', '-')}",
        f"cgroup_memory_high: {memory.get('high', '-')}",
        f"cgroup_memory_peak: {memory.get('peak', '-')}",
        f"apps_slice: {parent_slice.get('path') or '-'}",
        f"apps_slice_memory: {parent_memory.get('current', '-')} / {parent_memory.get('max', '-')}",
        f"cgroup_oom: {memory_events.get('oom', 0)}; oom_kill: {memory_events.get('oom_kill', 0)}; max: {memory_events.get('max', 0)}",
        f"cgroup_pids: {pids.get('current', '-')} / {pids.get('max', '-')}",
        f"bootstrap_status: {bootstrap.get('status') if isinstance(bootstrap, dict) else '-'}",
        f"bootstrap_lock: {'held' if isinstance(bootstrap, dict) and bootstrap.get('lock_active') else 'idle'}",
        f"bootstrap_stale: {'yes' if isinstance(bootstrap, dict) and bootstrap.get('stale') else 'no'}",
        f"bootstrap_started_at: {state.get('started_at') if isinstance(state, dict) and state.get('started_at') else '-'}",
        f"bootstrap_failure_phase: {state.get('failure_phase') if isinstance(state, dict) and state.get('failure_phase') else '-'}",
        f"dns_servers: {', '.join(payload.get('dns_servers') or []) if isinstance(payload.get('dns_servers'), list) else '-'}",
    ]
    recommended_actions = (
        payload.get("recommended_actions")
        if isinstance(payload.get("recommended_actions"), list)
        else []
    )
    if recommended_actions:
        lines.append("recommended_next:")
        for item in recommended_actions:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "next")
            command = str(item.get("command") or "-")
            note = str(item.get("note") or "").strip()
            lines.append(f"  {label}: {command}")
            if note:
                lines.append(f"    {note}")

    shield_status = (
        payload.get("shield_status")
        if isinstance(payload.get("shield_status"), dict)
        else {}
    )
    if shield_status:
        lines.append("shield_status:")
        lines.append(
            f"  server_enabled: {'yes' if shield_status.get('server_enabled') else 'no'}"
        )
        lines.append(
            f"  backend_exists: {'yes' if shield_status.get('backend_exists') else 'no'}"
        )
        lines.append(
            f"  backend_enabled: {'yes' if shield_status.get('backend_enabled') else 'no'}"
        )
        lines.append(
            f"  service_active: {'yes' if shield_status.get('service_active') else 'no'}"
        )
        lines.append(f"  output_state: {shield_status.get('output_state') or '-'}")
        lines.append(f"  ready: {'yes' if shield_status.get('ready') else 'no'}")
        input_values = shield_status.get("input_values")
        if isinstance(input_values, list) and input_values:
            lines.append(
                f"  input_values: {', '.join(str(item) for item in input_values)}"
            )
        else:
            lines.append("  input_values: none")
    return "\n".join(lines)


async def doctor_app_async(
    backend_name: str,
    settings: Settings,
) -> CommandResult:
    from app.services.app_diagnostics import collect_app_backend_diagnostics
    from app.services.status_service import app_action_hints

    backend = await load_backend_async(backend_name)
    if backend is None:
        return 1, None, f"backend not found: {backend_name}"
    payload = await asyncio.to_thread(
        collect_app_backend_diagnostics, backend, settings
    )
    payload["shield_status"] = await load_shield_status_payload(settings)
    payload["recommended_actions"] = app_action_hints(backend, payload)
    return 0 if payload.get("ok") else 2, payload, None


async def _run_doctor_app_command(
    args: argparse.Namespace, settings: Settings | None
) -> CommandResult:
    assert settings is not None
    return await doctor_app_async(args.backend, settings)
