from __future__ import annotations

import re
from dataclasses import dataclass
import time
from urllib.parse import urlsplit

from app.config import Settings
from app.logger import get_logger
from app.services.commands import (
    CommandResult,
    command_result_is_retryable,
    run_command,
)


URL_RE = re.compile(r"https?://[^\s]+")
MAPPING_RE = re.compile(r"^\s*\|--\s+(?P<path>\S+)\s+\S+\s+(?P<target>\S+)\s*$")
APPROVED_ADMIN_PATH = "/"
APPROVED_ADMIN_PORT = 443
ADMIN_TARGETS = {
    "127.0.0.1:9090",
    "localhost:9090",
    "[::1]:9090",
    "127.0.0.1:9091",
    "localhost:9091",
    "[::1]:9091",
}
logger = get_logger("tailscale.admin")
_ADMIN_HOSTNAME_CACHE_TTL_SEC = 60.0
_admin_hostname_cache: tuple[float, tuple[str, ...]] = (0.0, ())


class TailscaleAdminExposureError(RuntimeError):
    pass


@dataclass(frozen=True)
class TailscaleMapping:
    mode: str
    url: str
    port: int
    path: str
    target: str

    @property
    def targets_admin(self) -> bool:
        normalized = (
            self.target.removeprefix("http://").removeprefix("https://").rstrip("/")
        )
        return normalized in ADMIN_TARGETS


def _parse_status_mappings(status_text: str, *, mode: str) -> list[TailscaleMapping]:
    current_url: str | None = None
    mappings: list[TailscaleMapping] = []

    for raw_line in status_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        mapping_match = MAPPING_RE.match(raw_line)
        if mapping_match is not None and current_url is not None:
            split_url = urlsplit(current_url)
            port = split_url.port or (443 if split_url.scheme == "https" else 80)
            mappings.append(
                TailscaleMapping(
                    mode=mode,
                    url=current_url,
                    port=port,
                    path=mapping_match.group("path"),
                    target=mapping_match.group("target"),
                )
            )
            continue
        url_match = URL_RE.search(line)
        if url_match is not None:
            current_url = url_match.group(0)
            continue
    return mappings


def _admin_mapping_hostnames(mappings: list[TailscaleMapping]) -> tuple[str, ...]:
    hostnames: list[str] = []
    for mapping in mappings:
        if not mapping.targets_admin:
            continue
        if mapping.port != APPROVED_ADMIN_PORT or mapping.path != APPROVED_ADMIN_PATH:
            continue
        hostname = (urlsplit(mapping.url).hostname or "").strip().lower().rstrip(".")
        if hostname:
            hostnames.append(hostname)
    return tuple(dict.fromkeys(hostnames))


def current_tailscale_admin_hostnames(settings: Settings) -> tuple[str, ...]:
    global _admin_hostname_cache
    now = time.monotonic()
    expires_at, cached_hostnames = _admin_hostname_cache
    if now < expires_at:
        return cached_hostnames

    hostnames: tuple[str, ...] = ()
    try:
        ip_result = _run_tailscale_command(settings, ["tailscale", "ip", "-4"])
        if not ip_result.ok:
            _admin_hostname_cache = (now + _ADMIN_HOSTNAME_CACHE_TTL_SEC, ())
            return ()
        serve_result = _run_tailscale_command(
            settings, ["tailscale", "serve", "status"]
        )
        funnel_result = _run_tailscale_command(
            settings, ["tailscale", "funnel", "status"]
        )
        mappings: list[TailscaleMapping] = []
        if serve_result.ok:
            mappings.extend(_parse_status_mappings(serve_result.stdout, mode="serve"))
        if funnel_result.ok:
            mappings.extend(_parse_status_mappings(funnel_result.stdout, mode="funnel"))
        hostnames = _admin_mapping_hostnames(mappings)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.warning("tailscale.admin.hostnames.failed", error=str(exc))
        hostnames = ()

    _admin_hostname_cache = (now + _ADMIN_HOSTNAME_CACHE_TTL_SEC, hostnames)
    return hostnames


def verify_tailscale_admin_exposure(settings: Settings) -> dict[str, object]:
    ip_result = _run_tailscale_command(
        settings,
        ["tailscale", "ip", "-4"],
    )
    if not ip_result.ok:
        return {"checked": False, "reason": "tailscale_not_connected"}

    serve_result = _run_tailscale_command(
        settings,
        ["tailscale", "serve", "status"],
    )
    funnel_result = _run_tailscale_command(
        settings,
        ["tailscale", "funnel", "status"],
    )
    if not serve_result.ok:
        raise TailscaleAdminExposureError(
            serve_result.stderr
            or serve_result.stdout
            or "tailscale serve status failed"
        )
    if not funnel_result.ok:
        raise TailscaleAdminExposureError(
            funnel_result.stderr
            or funnel_result.stdout
            or "tailscale funnel status failed"
        )

    serve_mappings = _parse_status_mappings(serve_result.stdout, mode="serve")
    funnel_status_text = funnel_result.stdout
    funnel_is_tailnet_only = "tailnet only" in funnel_status_text.lower()
    funnel_mappings = (
        []
        if funnel_is_tailnet_only
        else _parse_status_mappings(funnel_status_text, mode="funnel")
    )
    errors: list[str] = []

    if any(mapping.port == APPROVED_ADMIN_PORT for mapping in funnel_mappings):
        errors.append(
            "tailscale funnel exposes HTTPS port 443; admin access must remain tailnet-only"
        )
    if any(mapping.targets_admin for mapping in funnel_mappings):
        errors.append("tailscale funnel maps a public route to a CNC admin target")

    for mapping in serve_mappings:
        if not mapping.targets_admin:
            continue
        if mapping.port != APPROVED_ADMIN_PORT or mapping.path != APPROVED_ADMIN_PATH:
            errors.append(
                f"tailscale serve exposes CNC admin target via unapproved mapping {mapping.path} on port {mapping.port}"
            )

    if errors:
        raise TailscaleAdminExposureError("; ".join(errors))

    return {
        "checked": True,
        "serve_mappings": [
            {
                "port": mapping.port,
                "path": mapping.path,
                "target": mapping.target,
            }
            for mapping in serve_mappings
        ],
        "funnel_mappings": [
            {
                "port": mapping.port,
                "path": mapping.path,
                "target": mapping.target,
            }
            for mapping in funnel_mappings
        ],
        "funnel_tailnet_only": funnel_is_tailnet_only,
    }


def _run_tailscale_command(settings: Settings, command: list[str]) -> CommandResult:
    attempts = settings.transient_command_retry_attempts + 1
    for attempt in range(1, attempts + 1):
        result = run_command(command, timeout_sec=settings.command_timeout_status_sec)
        if result.ok or attempt >= attempts or not command_result_is_retryable(result):
            return result
        delay_sec = settings.transient_command_retry_backoff_sec * attempt
        logger.warning(
            "tailscale.command.retrying",
            command=" ".join(command),
            attempt=attempt,
            max_attempts=attempts,
            backoff_sec=delay_sec,
            error=result.stderr or result.stdout,
        )
        if delay_sec > 0:
            time.sleep(delay_sec)
    raise AssertionError("tailscale command retry loop exited unexpectedly")
