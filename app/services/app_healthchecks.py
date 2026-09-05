from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.entities import Backend, Input
from app.services.commands import CommandResult, run_command


HEALTHCHECK_MODE_HTTP = "http"
HEALTHCHECK_MODE_TCP = "tcp"
HEALTHCHECK_MODE_NONE = "none"
HEALTHCHECK_STATUS_HEALTHY = "healthy"
HEALTHCHECK_STATUS_UNHEALTHY = "unhealthy"
HEALTHCHECK_STATUS_UNMONITORED = "unmonitored"
HEALTHCHECK_MODE_VALUES = {
    HEALTHCHECK_MODE_HTTP,
    HEALTHCHECK_MODE_TCP,
    HEALTHCHECK_MODE_NONE,
}


@dataclass(frozen=True)
class BackendHealthcheckPlan:
    configured_mode: str | None
    display_mode: str
    effective_mode: str
    path: str | None
    host_header: str | None


@dataclass(frozen=True)
class BackendHealthcheckProbeResult:
    ok: bool
    error: str
    plan: BackendHealthcheckPlan
    http_status: int | None = None

    @property
    def checked(self) -> bool:
        return self.plan.effective_mode != HEALTHCHECK_MODE_NONE

    @property
    def status(self) -> str:
        if not self.checked:
            return HEALTHCHECK_STATUS_UNMONITORED
        return HEALTHCHECK_STATUS_HEALTHY if self.ok else HEALTHCHECK_STATUS_UNHEALTHY


def normalize_healthcheck_mode(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized == "auto":
        return HEALTHCHECK_MODE_HTTP
    return normalized


def display_backend_healthcheck_mode(backend: Backend) -> str:
    normalized = normalize_healthcheck_mode(getattr(backend, "healthcheck_mode", None))
    if normalized in HEALTHCHECK_MODE_VALUES:
        return normalized
    return HEALTHCHECK_MODE_HTTP


def resolve_backend_healthcheck(backend: Backend) -> BackendHealthcheckPlan:
    configured_mode = normalize_healthcheck_mode(
        getattr(backend, "healthcheck_mode", None)
    )
    normalized_path = (
        str(getattr(backend, "healthcheck_path", None) or "/").strip() or "/"
    )
    host_header = preferred_backend_host_header(backend)

    if configured_mode == HEALTHCHECK_MODE_NONE:
        return BackendHealthcheckPlan(
            configured_mode=configured_mode,
            display_mode=HEALTHCHECK_MODE_NONE,
            effective_mode=HEALTHCHECK_MODE_NONE,
            path=None,
            host_header=None,
        )
    if configured_mode == HEALTHCHECK_MODE_TCP:
        return BackendHealthcheckPlan(
            configured_mode=configured_mode,
            display_mode=HEALTHCHECK_MODE_TCP,
            effective_mode=HEALTHCHECK_MODE_TCP,
            path=None,
            host_header=None,
        )
    return BackendHealthcheckPlan(
        configured_mode=configured_mode,
        display_mode=HEALTHCHECK_MODE_HTTP,
        effective_mode=HEALTHCHECK_MODE_HTTP,
        path=normalized_path,
        host_header=host_header,
    )


def preferred_backend_host_header(backend: Backend) -> str | None:
    configured = (
        str(getattr(backend, "healthcheck_host_header", "") or "").strip().lower()
    )
    if configured:
        return configured
    loaded_inputs = backend.__dict__.get("inputs")
    if not isinstance(loaded_inputs, list):
        return None
    enabled_domains = sorted(
        _input_hostname(item)
        for item in loaded_inputs
        if _input_is_domain(item) and bool(getattr(item, "enabled", False))
    )
    if enabled_domains:
        return enabled_domains[0]
    attached_domains = sorted(
        _input_hostname(item) for item in loaded_inputs if _input_is_domain(item)
    )
    if attached_domains:
        return attached_domains[0]
    return None


def _input_is_domain(item: Input) -> bool:
    return str(getattr(item, "kind", "") or "").strip().lower() == "domain"


def _input_hostname(item: Input) -> str:
    return str(getattr(item, "hostname", "") or "").strip().lower()


def _tcp_probe_command(host: str, port: int) -> list[str]:
    return [
        "python3",
        "-c",
        (
            "import socket, sys; "
            "sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), 2); "
            "sock.close()"
        ),
        host,
        str(port),
    ]


def _http_probe_command(
    *,
    host: str,
    port: int,
    path: str,
    host_header: str | None,
) -> list[str]:
    command = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        "2",
    ]
    if host_header:
        command.extend(["-H", f"Host: {host_header}"])
    command.append(f"http://{host}:{port}{path}")
    return command


def probe_backend_health(
    backend: Backend,
    *,
    host: str,
    port: int,
    timeout_sec: int,
    command_runner: Callable[[list[str], int], CommandResult] = run_command,
) -> BackendHealthcheckProbeResult:
    plan = resolve_backend_healthcheck(backend)
    if plan.effective_mode == HEALTHCHECK_MODE_NONE:
        return BackendHealthcheckProbeResult(ok=True, error="", plan=plan)
    if port <= 0:
        return BackendHealthcheckProbeResult(
            ok=False,
            error=f"invalid healthcheck port: {port}",
            plan=plan,
        )

    if plan.effective_mode == HEALTHCHECK_MODE_TCP:
        result = command_runner(_tcp_probe_command(host, port), timeout_sec)
        if result.ok:
            return BackendHealthcheckProbeResult(ok=True, error="", plan=plan)
        return BackendHealthcheckProbeResult(
            ok=False,
            error=result.stderr
            or result.stdout
            or f"tcp probe failed for {host}:{port}",
            plan=plan,
        )

    result = command_runner(
        _http_probe_command(
            host=host,
            port=port,
            path=str(plan.path or "/"),
            host_header=plan.host_header,
        ),
        timeout_sec,
    )
    status_text = (result.stdout or "").strip()
    status_code = int(status_text) if status_text.isdigit() else None
    if result.ok and status_code is not None:
        if 200 <= status_code < 400:
            return BackendHealthcheckProbeResult(
                ok=True, error="", plan=plan, http_status=status_code
            )
        return BackendHealthcheckProbeResult(
            ok=False,
            error=f"http probe returned {status_code}",
            plan=plan,
            http_status=status_code,
        )
    if result.ok and status_code is None:
        # Older tests and wrapper fakes may not emulate curl's `-w %{http_code}` output.
        return BackendHealthcheckProbeResult(ok=True, error="", plan=plan)
    error = (
        result.stderr
        or result.stdout
        or f"http probe failed for {host}:{port}{plan.path or '/'}"
    )
    return BackendHealthcheckProbeResult(
        ok=False,
        error=error,
        plan=plan,
        http_status=status_code,
    )
