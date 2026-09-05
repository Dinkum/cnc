from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.services.app_quadlet import quadlet_container_service_name
from app.services.commands import CommandResult, run_command


CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_value(path: Path) -> int | str | None:
    value = _read_text(path)
    if value is None:
        return None
    if value == "max":
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _read_counters(path: Path) -> dict[str, int]:
    counters: dict[str, int] = {}
    for line in (_read_text(path) or "").splitlines():
        key, separator, raw_value = line.partition(" ")
        if not separator:
            continue
        try:
            counters[key] = int(raw_value)
        except ValueError:
            continue
    return counters


def _read_pressure(path: Path) -> dict[str, dict[str, float | int]]:
    pressure: dict[str, dict[str, float | int]] = {}
    for line in (_read_text(path) or "").splitlines():
        label, separator, values = line.partition(" ")
        if not separator:
            continue
        sample: dict[str, float | int] = {}
        for entry in values.split():
            key, value_separator, raw_value = entry.partition("=")
            if not value_separator:
                continue
            try:
                sample[key] = int(raw_value) if key == "total" else float(raw_value)
            except ValueError:
                continue
        if sample:
            pressure[label] = sample
    return pressure


def _process_cgroup_path(pid: object) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    for line in (_read_text(PROC_ROOT / str(pid) / "cgroup") or "").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hierarchy, controllers, path = fields
        if hierarchy == "0" and not controllers and path.startswith("/"):
            return path
    return None


def _existing_cgroup_path(value: object) -> tuple[str, Path] | None:
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    path = CGROUP_ROOT / value.lstrip("/")
    if not path.is_dir():
        return None
    return value, path


def _resolve_container_cgroup(state: dict[str, Any]) -> tuple[str, Path] | None:
    # Podman's State.CgroupPath names the payload cgroup where limits are applied.
    # The PID commonly lives below it in init.scope, whose inherited files read as
    # unbounded and whose usage covers only the init process.
    inspected = _existing_cgroup_path(state.get("CgroupPath"))
    if inspected is not None:
        return inspected

    process_path = _process_cgroup_path(state.get("Pid"))
    resolved = _existing_cgroup_path(process_path)
    if resolved is None:
        return None
    cgroup_path, path = resolved
    if (
        path.name in {"container", "init.scope"}
        and (path.parent / "memory.current").is_file()
    ):
        parent = path.parent
        return f"/{parent.relative_to(CGROUP_ROOT)}", parent
    return cgroup_path, path


def _path_beneath_apps_slice(cgroup_path: str) -> bool:
    return "cnc-apps.slice" in Path(cgroup_path).parts


def _service_control_group(
    backend_name: str,
    *,
    command_runner: Callable[[list[str], float], CommandResult],
    timeout_sec: float,
) -> str | None:
    service = quadlet_container_service_name(backend_name)
    result = command_runner(
        ["systemctl", "show", service, "--property=ControlGroup", "--value"],
        timeout_sec,
    )
    value = result.stdout.strip() if result.ok else ""
    if not value.startswith("/") or not _path_beneath_apps_slice(value):
        return None
    return value


def _ancestor_cgroup(
    cgroup_path: str | None, *, service_name: str | None = None
) -> tuple[str, Path] | None:
    if not isinstance(cgroup_path, str) or not cgroup_path.startswith("/"):
        return None
    current = CGROUP_ROOT / cgroup_path.lstrip("/")
    while current != CGROUP_ROOT and CGROUP_ROOT in current.parents:
        if current.is_dir() and (
            (service_name is not None and current.name == service_name)
            or (
                service_name is None
                and current.name not in {"container", "init.scope"}
                and not current.name.startswith("libpod-payload-")
                and (current / "memory.current").is_file()
            )
        ):
            return f"/{current.relative_to(CGROUP_ROOT)}", current
        current = current.parent
    return None


def _apps_slice_path(selected_path: Path | None = None) -> tuple[str, Path] | None:
    if selected_path is not None:
        for candidate in (selected_path, *selected_path.parents):
            if candidate == CGROUP_ROOT:
                break
            if candidate.name == "cnc-apps.slice" and candidate.is_dir():
                return f"/{candidate.relative_to(CGROUP_ROOT)}", candidate
    return _existing_cgroup_path("/cnc-apps.slice")


def _resource_payload(path: Path) -> dict[str, Any]:
    return {
        "memory": {
            "current": _read_value(path / "memory.current"),
            "high": _read_value(path / "memory.high"),
            "max": _read_value(path / "memory.max"),
            "peak": _read_value(path / "memory.peak"),
            "events": _read_counters(path / "memory.events"),
            "pressure": _read_pressure(path / "memory.pressure"),
        },
        "pids": {
            "current": _read_value(path / "pids.current"),
            "max": _read_value(path / "pids.max"),
            "events": _read_counters(path / "pids.events"),
        },
        "cpu_pressure": _read_pressure(path / "cpu.pressure"),
        "io_pressure": _read_pressure(path / "io.pressure"),
    }


def collect_container_cgroup_diagnostics(
    inspect_payload: dict[str, Any] | None,
    *,
    backend_name: str | None = None,
    command_runner: Callable[[list[str], float], CommandResult] = run_command,
    timeout_sec: float = 5,
) -> dict[str, Any]:
    state = inspect_payload.get("State") if isinstance(inspect_payload, dict) else None
    process_path = (
        _process_cgroup_path(state.get("Pid")) if isinstance(state, dict) else None
    )
    service_name = (
        quadlet_container_service_name(backend_name) if backend_name else None
    )
    service_path = (
        _service_control_group(
            backend_name,
            command_runner=command_runner,
            timeout_sec=timeout_sec,
        )
        if backend_name
        else None
    )
    resolved = _existing_cgroup_path(service_path)
    selection_source = "systemd_service"
    if resolved is None and service_name is not None:
        resolved = _ancestor_cgroup(process_path, service_name=service_name)
        selection_source = "process_ancestry"
    if resolved is None:
        resolved = _resolve_container_cgroup(state) if isinstance(state, dict) else None
        selection_source = "podman_inspect_or_process"
    if resolved is None:
        if backend_name is None and process_path is None and service_path is None:
            return {"available": False}
        return {
            "available": False,
            "process_path": process_path,
            "service_path": service_path,
        }
    cgroup_path, path = resolved
    payload = {
        "available": True,
        "path": cgroup_path,
        "selected_path": cgroup_path,
        "selection_source": selection_source,
        "process_path": process_path,
        "service_path": service_path,
    }
    payload.update(_resource_payload(path))
    apps_slice = _apps_slice_path(path)
    if apps_slice is None:
        payload["parent_slice"] = {"available": False}
    else:
        apps_slice_name, apps_slice_fs_path = apps_slice
        payload["parent_slice"] = {
            "available": True,
            "path": apps_slice_name,
            **_resource_payload(apps_slice_fs_path),
        }
    return payload
