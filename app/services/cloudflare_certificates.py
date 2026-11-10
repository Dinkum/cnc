from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shlex
from typing import Any

from app.config import Settings
from app.models.entities import Input
from app.services.cloudflare_api import CloudflareClient, CloudflareError
from app.services.cloudflare_connection import matching_zone, section
from app.services.host_state import LockedStateSectionTransaction
from app.services.origin_tls import (
    DEFAULT_CONTROL_DIR,
    certificate_path,
    install_origin_certificate,
    origin_certificate_path,
    origin_request,
)


def input_certificate_path(
    item: Input | None,
    control_dir: Path = DEFAULT_CONTROL_DIR,
    *,
    connection: str | None = None,
) -> str | None:
    if (
        item is None
        or not item.https_enabled
        or (connection or item.connection) == "cloudflare_tunnel"
    ):
        return None
    if item.https_provider == "cloudflare_origin":
        return str(origin_certificate_path(item.hostname, control_dir))
    return str(certificate_path(control_dir))


def origin_hosts(files: dict[str, str], control_dir: Path) -> list[str]:
    hosts = []
    for content in files.values():
        match = re.search(r"^\s*server_name ([a-z0-9.-]+);", content, re.MULTILINE)
        if (
            match
            and f'ssl_certificate "{origin_certificate_path(match[1], control_dir)}";'
            in content
        ):
            hosts.append(match[1])
    return sorted(set(hosts))


def _issue(settings: Settings, hostname: str, csr: str, node_uid: str) -> str:
    if not matching_zone(settings, hostname):
        raise CloudflareError(
            f"Reconnect Cloudflare with access to {hostname} to issue its origin certificate."
        )
    key = f"{node_uid}:{hostname}"
    previous = section(settings, "cloudflare_certificates").get(key, {})
    if previous.get("uncertain"):
        raise CloudflareError(
            "The previous certificate request has an unknown outcome. Preview and apply the domain settings to retry deliberately."
        )
    try:
        result = CloudflareClient(settings.cloudflare_api_token).issue_certificate(
            hostname, csr
        )
    except CloudflareError as exc:
        with LockedStateSectionTransaction(
            settings.host_state_path, section="cloudflare_certificates"
        ) as state:
            state.value[key] = {
                **previous,
                "error": str(exc),
                "uncertain": exc.uncertain,
            }
            state.mark_dirty()
        raise
    with LockedStateSectionTransaction(
        settings.host_state_path, section="cloudflare_certificates"
    ) as state:
        state.value[key] = {
            "certificate_id": result.get("id"),
            "expires_on": result.get("expires_on"),
            "issued_at": datetime.now(UTC).isoformat(),
            "error": "",
            "uncertain": False,
        }
        state.mark_dirty()
    return result["certificate"]


def allow_certificate_retry(settings: Settings, hostname: str) -> None:
    with LockedStateSectionTransaction(
        settings.host_state_path, section="cloudflare_certificates"
    ) as state:
        for key, value in state.value.items():
            if key.endswith(":" + hostname):
                value["uncertain"] = False
        state.mark_dirty()


def prepare_origin_certificates(
    files: dict[str, str], settings: Settings
) -> dict[str, str]:
    pending = {}
    for hostname in origin_hosts(files, settings.app_control_dir):
        request = origin_request(hostname, settings.app_control_dir)
        if request["ready"]:
            if request["reload_required"]:
                pending[
                    str(origin_certificate_path(hostname, settings.app_control_dir))
                ] = str(request["fingerprint"])
            continue
        certificate = _issue(settings, hostname, str(request["csr"]), "leader")
        pending[str(origin_certificate_path(hostname, settings.app_control_dir))] = (
            install_origin_certificate(hostname, certificate, settings.app_control_dir)
        )
    return pending


def reconcile_follower_certificates(
    node_uid: str,
    node_ip: str,
    files: dict[str, str],
    settings: Settings,
    *,
    reload: bool = False,
) -> None:
    from app.services.cluster_ingress import _ssh_checked

    hosts = origin_hosts(files, DEFAULT_CONTROL_DIR)
    if not hosts:
        return
    source = Path(__file__).with_name("origin_tls.py").read_bytes()
    encoded = base64.b64encode(source).decode()
    _ssh_checked(
        node_uid,
        node_ip,
        settings,
        "set -eu\ninstall -d -m 0755 /usr/local/lib/cnc\nprintf %s "
        + shlex.quote(encoded)
        + " | base64 -d > /usr/local/lib/cnc/origin_tls.py\nchmod 0644 /usr/local/lib/cnc/origin_tls.py",
    )
    helper = "python3 /usr/local/lib/cnc/origin_tls.py --control-dir /var/lib/cnc/app-control"
    for hostname in hosts:
        output = _ssh_checked(
            node_uid,
            node_ip,
            settings,
            f"{helper} --origin-request {shlex.quote(hostname)}",
        )
        try:
            request: dict[str, Any] = json.loads(output)
        except ValueError:
            raise CloudflareError(
                "Follower returned an invalid certificate request."
            ) from None
        changed = not request.get("ready") or request.get("reload_required")
        if not request.get("ready"):
            csr = str(request.get("csr") or "")
            if (
                not csr.startswith("-----BEGIN CERTIFICATE REQUEST-----")
                or len(csr) > 16384
            ):
                raise CloudflareError("Follower returned an invalid CSR.")
            certificate = _issue(settings, hostname, csr, node_uid)
            data = base64.b64encode(certificate.encode()).decode()
            _ssh_checked(
                node_uid,
                node_ip,
                settings,
                f"{helper} --origin-install {shlex.quote(hostname)} --certificate-data {shlex.quote(data)}",
            )
        if reload and changed:
            _ssh_checked(
                node_uid,
                node_ip,
                settings,
                f"{helper} --origin-reload {shlex.quote(hostname)}",
            )
