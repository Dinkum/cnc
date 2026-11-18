from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import secrets
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.services.change_scope import ToolScope
from app.services.config_change import ConfigChange, converge_change
from app.logger import get_logger
from app.models.entities import Input
from app.services.cloudflare_api import CloudflareClient, CloudflareError
from app.services.cloudflare_certificates import allow_certificate_retry
from app.services.cloudflare_connection import (
    connection_summary,
    matching_zone,
    section,
    token_fingerprint,
)
from app.services.cloudflare_dns import apply_dns_plan, dns_plan, ipv4_address
from app.services.cloudflare_tunnel import (
    connector_ready,
    tunnel_identity,
    tunnel_summary,
)
from app.services.host_state import LockedStateSectionTransaction
from app.services.operations import (
    OperationHandle,
    create_operation,
    host_mutation_operation,
)

logger = get_logger("cloudflare.domains")


def domain_status(settings: Settings, item: Input) -> dict[str, Any]:
    state = section(settings, "cloudflare_domains").get(str(item.id), {})
    if state.get("hostname") != item.hostname:
        state = {}
    summary = connection_summary(settings)
    tunnel = tunnel_summary(settings)
    zone = matching_zone(settings, item.hostname, require_connected=False)
    configured = item.https_provider == "cloudflare_origin" or bool(
        item.cloudflare_dns_enabled
    )
    return {
        "visible": bool(zone and summary["connected"])
        or configured
        or tunnel["configured"]
        or item.connection == "cloudflare_tunnel",
        "connection": item.connection or "public_ip",
        "tunnel": tunnel,
        "hostname_status": "Not verified"
        if item.connection == "cloudflare_tunnel"
        else "",
        "available": bool(zone and summary["connected"]),
        "zone": zone["name"] if zone else "",
        "provider": item.https_provider or "self_signed",
        "dns_enabled": bool(item.cloudflare_dns_enabled),
        "ipv4": item.cloudflare_dns_ipv4 or "",
        "status": state.get("status", "Not configured"),
        "error": state.get("error", ""),
        "checked_at": state.get("checked_at", ""),
    }


def save_domain_status(settings: Settings, item: Input, **values: Any) -> None:
    with LockedStateSectionTransaction(
        settings.host_state_path, section="cloudflare_domains"
    ) as state:
        old = state.value.get(str(item.id), {})
        if old.get("hostname") != item.hostname:
            old = {}
        state.value[str(item.id)] = {
            **old,
            "hostname": item.hostname,
            "checked_at": datetime.now(UTC).isoformat(),
            **values,
        }
        state.mark_dirty()


def input_signature(item: Input) -> str:
    value = {
        "id": item.id,
        "hostname": item.hostname,
        "kind": item.kind,
        "enabled": item.enabled,
        "https_enabled": item.https_enabled,
        "connection": item.connection or "public_ip",
        "provider": item.https_provider,
        "dns_enabled": item.cloudflare_dns_enabled,
        "ipv4": item.cloudflare_dns_ipv4,
        "backend_ids": sorted(item.backend_ids),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def build_domain_preview(
    settings: Settings,
    item: Input,
    *,
    provider: str,
    manage_dns: bool,
    ipv4: str,
    connection: str | None = None,
    route_confirmed: bool = False,
) -> dict[str, Any]:
    if item.kind != "domain":
        raise CloudflareError(
            "Cloudflare options are available for domain Inputs only."
        )
    connection = connection or item.connection or "public_ip"
    if connection not in {"public_ip", "cloudflare_tunnel"}:
        raise CloudflareError("Choose Public IP or Cloudflare Tunnel.")
    tunnel_id = ""
    if connection == "cloudflare_tunnel":
        summary = tunnel_summary(settings)
        if not summary["connected"] or not connector_ready():
            raise CloudflareError(
                "The Tunnel connector is unavailable. Retest it in Settings first."
            )
        tunnel_id = tunnel_identity(settings.cloudflare_tunnel_token)
        if manage_dns and not route_confirmed:
            raise CloudflareError(
                "Confirm that this hostname is configured in your Cloudflare Tunnel before managing DNS."
            )
        provider = item.https_provider or "self_signed"
    if provider not in {"self_signed", "cloudflare_origin"}:
        raise CloudflareError("Choose a supported certificate type.")
    zone = matching_zone(settings, item.hostname)
    if (
        manage_dns
        or (
            connection == "public_ip"
            and provider == "cloudflare_origin"
            and item.https_provider != provider
        )
    ) and not zone:
        raise CloudflareError("Connect Cloudflare with access to this domain first.")
    if (
        connection == "public_ip"
        and provider == "cloudflare_origin"
        and (not item.enabled or not item.backends)
    ):
        raise CloudflareError(
            "Enable this Input and attach an Output before requesting an Origin CA certificate."
        )
    if manage_dns and (not item.enabled or not item.backends):
        raise CloudflareError(
            "Enable this Input and attach an Output before publishing DNS."
        )
    state = section(settings, "cloudflare_domains").get(str(item.id), {})
    owned = (
        str(state.get("record_id") or "")
        if state.get("hostname") == item.hostname
        else ""
    )
    plan = (
        dns_plan(
            CloudflareClient(settings.cloudflare_api_token),
            zone_id=zone["id"],
            hostname=item.hostname,
            ipv4="" if tunnel_id else ipv4_address(ipv4),
            tunnel_id=tunnel_id,
            owned_record_id=owned,
        )
        if manage_dns
        else None
    )
    result = {
        "input_id": item.id,
        "hostname": item.hostname,
        "provider": provider,
        "connection": connection,
        "tunnel_fingerprint": token_fingerprint(settings.cloudflare_tunnel_token)
        if tunnel_id
        else "",
        "tunnel_id": tunnel_id,
        "manage_dns": manage_dns,
        "ipv4": plan["ipv4"] if plan else "",
        "zone_id": zone["id"] if zone else "",
        "signature": input_signature(item),
        "token_fingerprint": token_fingerprint(settings.cloudflare_api_token),
        "certificate": "Cloudflare Tunnel · hostname configured in Cloudflare"
        if tunnel_id
        else "Cloudflare Origin CA · enables HTTPS · supports Full (strict)"
        if provider == "cloudflare_origin"
        else "Self-signed · supports Full",
        "dns": plan,
        "dns_note": "Existing DNS records will remain; CNC will stop managing this hostname."
        if item.cloudflare_dns_enabled and not manage_dns
        else "DNS will stay unchanged."
        if not manage_dns
        else "",
        "conflict": plan["conflict"] if plan else "",
        "created_at": time.time(),
    }
    if not result["conflict"]:
        preview_id = secrets.token_urlsafe(24)
        with LockedStateSectionTransaction(
            settings.host_state_path, section="cloudflare_previews"
        ) as state:
            fresh = {
                key: value
                for key, value in state.value.items()
                if value.get("created_at", 0) > time.time() - 600
            }
            fresh = dict(list(fresh.items())[-19:])
            fresh[preview_id] = result
            state.replace(fresh)
        result = {**result, "preview_id": preview_id}
    return result


def consume_preview(settings: Settings, item: Input, preview_id: str) -> dict[str, Any]:
    with LockedStateSectionTransaction(
        settings.host_state_path, section="cloudflare_previews"
    ) as state:
        preview = state.value.get(preview_id)
        if not preview or preview.get("created_at", 0) < time.time() - 600:
            raise CloudflareError("This preview expired. Preview the changes again.")
        if preview["signature"] != input_signature(item) or preview[
            "token_fingerprint"
        ] != token_fingerprint(settings.cloudflare_api_token):
            raise CloudflareError(
                "The Input or Cloudflare connection changed. Preview again."
            )
        if preview.get("tunnel_fingerprint") and preview[
            "tunnel_fingerprint"
        ] != token_fingerprint(settings.cloudflare_tunnel_token):
            raise CloudflareError("The Tunnel changed. Preview again.")
        state.value.pop(preview_id)
        state.mark_dirty()
    return preview


async def apply_domain_preview(
    session: AsyncSession,
    settings: Settings,
    item: Input,
    preview: dict[str, Any],
    operation: OperationHandle,
) -> None:
    dns = preview["dns"]
    state = section(settings, "cloudflare_domains").get(str(item.id), {})
    owned = (
        str(state.get("record_id") or "")
        if state.get("hostname") == item.hostname
        else ""
    )
    if dns:
        live = await asyncio.to_thread(
            dns_plan,
            CloudflareClient(settings.cloudflare_api_token),
            zone_id=preview["zone_id"],
            hostname=item.hostname,
            ipv4=preview["ipv4"],
            tunnel_id=preview.get("tunnel_id", ""),
            owned_record_id=owned,
        )
        if live["fingerprint"] != dns["fingerprint"] or live["conflict"]:
            raise CloudflareError("DNS changed after the preview. Preview again.")
    apply_operation = await create_operation(
        settings, kind="apply_host", actor="ui", phase="Preparing HTTPS"
    )
    previous_connection = (
        state.get("previous_connection")
        if state.get("pending_dns")
        else item.connection
    )
    previous_connection = previous_connection or "public_ip"
    next_connection = preview.get("connection", "public_ip")
    if dns:
        save_domain_status(
            settings,
            item,
            pending_dns=True,
            previous_connection=previous_connection,
            keep_previous_connection=previous_connection != next_connection,
            status="Needs attention",
            error="Finish this domain's DNS review before automatic DNS updates resume.",
        )
    elif state.get("pending_dns"):
        save_domain_status(settings, item, keep_previous_connection=False)
    item.connection = next_connection
    if item.connection == "public_ip" and preview["provider"] == "cloudflare_origin":
        allow_certificate_retry(settings, item.hostname)
        item.https_enabled = True
    item.https_provider = preview["provider"]
    await operation.update(phase="Preparing HTTPS")
    mutation = await converge_change(
        session,
        settings,
        change=ConfigChange("cloudflare.domain.https", tools=ToolScope.DOMAIN_CONNECTION),
        operation_handle=apply_operation,
    )
    if not mutation.applied:
        raise CloudflareError(
            "HTTPS could not be applied. DNS was not changed; inspect the apply error and retry."
        )
    await session.refresh(item)
    if dns and mutation.apply_response.details.get("cluster_followers_unavailable"):
        raise CloudflareError(
            "Follower readiness could not be confirmed. DNS was not changed; retry after the follower is reachable."
        )
    if dns:
        await operation.update(phase="Updating DNS")
        record_id = await asyncio.to_thread(
            apply_dns_plan,
            CloudflareClient(settings.cloudflare_api_token),
            preview["zone_id"],
            dns,
            owned_record_id=owned,
        )
        save_domain_status(
            settings,
            item,
            record_id=record_id,
            zone_id=preview["zone_id"],
            confirmed_tunnel_id=preview.get("tunnel_id", ""),
            keep_previous_connection=False,
        )
    item.cloudflare_dns_enabled = preview["manage_dns"]
    item.cloudflare_dns_ipv4 = preview["ipv4"] or None
    await session.commit()
    if dns and previous_connection != next_connection:
        cleanup_operation = await create_operation(
            settings, kind="apply_host", actor="ui", phase="Finishing connection"
        )
        cleanup = await converge_change(
            session,
            settings,
            change=ConfigChange("cloudflare.domain.connection", tools=ToolScope.DOMAIN_CONNECTION),
            operation_handle=cleanup_operation,
        )
        if not cleanup.applied:
            raise CloudflareError(
                "DNS was updated, but the previous connection could not be retired. Review this domain again."
            )
    save_domain_status(
        settings,
        item,
        pending_dns=False,
        keep_previous_connection=False,
        status="DNS managed · hostname not verified"
        if item.connection == "cloudflare_tunnel" and item.cloudflare_dns_enabled
        else "Hostname not verified"
        if item.connection == "cloudflare_tunnel"
        else "Ready",
        error="",
    )


async def sync_cloudflare_domains(
    settings: Settings, session: AsyncSession
) -> dict[str, Any]:
    from app.services.apply_state import build_desired_state
    from app.services.cloudflare_certificates import (
        origin_hosts,
        prepare_origin_certificates,
        reconcile_follower_certificates,
    )
    from app.services.origin_tls import reload_origin_certificate
    from app.services.nginx_runtime import active_routes_directory

    results = []
    async with host_mutation_operation(
        settings,
        kind="cloudflare_domains_sync",
        phase="Checking domains",
        defer_on_update_blocker=True,
    ) as operation:
        inputs = list(
            (
                await session.scalars(
                    select(Input)
                    .options(selectinload(Input.backends))
                    .where(Input.kind == "domain", Input.enabled.is_(True))
                )
            ).all()
        )
        active_files = {
            path.name: path.read_text()
            for path in active_routes_directory(settings).glob("cnc-*.conf")
        }
        active_origins = set(origin_hosts(active_files, settings.app_control_dir))
        for item in inputs:
            if not item.backends:
                continue
            if not item.cloudflare_dns_enabled and not (
                item.https_enabled and item.https_provider == "cloudflare_origin"
            ):
                continue
            try:
                state = section(settings, "cloudflare_domains").get(str(item.id), {})
                if state.get("hostname") == item.hostname and state.get("pending_dns"):
                    raise CloudflareError(
                        "The previous DNS change did not finish. Review this domain again to resume DNS management."
                    )
                if item.connection == "cloudflare_tunnel":
                    if not item.cloudflare_dns_enabled:
                        continue
                    confirmed = (
                        state.get("confirmed_tunnel_id")
                        if state.get("hostname") == item.hostname
                        else None
                    )
                    if not confirmed or confirmed != tunnel_identity(
                        settings.cloudflare_tunnel_token
                    ):
                        raise CloudflareError(
                            "The Tunnel changed. Review this domain and confirm its hostname route before updating DNS."
                        )
                if item.hostname in active_origins:
                    files = {
                        name: content
                        for name, content in active_files.items()
                        if item.hostname
                        in origin_hosts({name: content}, settings.app_control_dir)
                    }
                    pending = await asyncio.to_thread(
                        prepare_origin_certificates, files, settings
                    )
                    if pending:
                        await asyncio.to_thread(
                            reload_origin_certificate,
                            item.hostname,
                            settings.app_control_dir,
                        )
                if item.cloudflare_dns_enabled:
                    zone = matching_zone(settings, item.hostname)
                    if not zone:
                        raise CloudflareError(
                            "Reconnect Cloudflare to manage DNS for this domain."
                        )
                    state = section(settings, "cloudflare_domains").get(
                        str(item.id), {}
                    )
                    owned = (
                        str(state.get("record_id") or "")
                        if state.get("hostname") == item.hostname
                        else ""
                    )
                    client = CloudflareClient(settings.cloudflare_api_token)
                    plan = await asyncio.to_thread(
                        dns_plan,
                        client,
                        zone_id=zone["id"],
                        hostname=item.hostname,
                        ipv4=""
                        if item.connection == "cloudflare_tunnel"
                        else ipv4_address(item.cloudflare_dns_ipv4 or ""),
                        tunnel_id=tunnel_identity(settings.cloudflare_tunnel_token)
                        if item.connection == "cloudflare_tunnel"
                        else "",
                        owned_record_id=owned,
                    )
                    record_id = await asyncio.to_thread(
                        apply_dns_plan,
                        client,
                        zone["id"],
                        plan,
                        owned_record_id=owned,
                    )
                    save_domain_status(
                        settings, item, record_id=record_id, zone_id=zone["id"]
                    )
                save_domain_status(
                    settings,
                    item,
                    status="DNS managed · hostname not verified"
                    if item.connection == "cloudflare_tunnel"
                    else "Ready",
                    error="",
                )
                results.append({"hostname": item.hostname, "ok": True})
            except (
                CloudflareError,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                save_domain_status(
                    settings, item, status="Needs attention", error=str(exc)
                )
                results.append(
                    {"hostname": item.hostname, "ok": False, "error": str(exc)}
                )
        if settings.multi_node_enabled and any(
            item.https_provider == "cloudflare_origin" and item.https_enabled
            for item in inputs
        ):
            desired = await build_desired_state(session, settings)
            for node in desired.cluster_nodes:
                files = desired.cluster_node_app_nginx_files.get(
                    str(node["node_uid"]), {}
                )
                if not origin_hosts(files, Path("/var/lib/cnc/app-control")):
                    continue
                try:
                    await asyncio.to_thread(
                        reconcile_follower_certificates,
                        str(node["node_uid"]),
                        str(node["tailnet_ip"]),
                        files,
                        settings,
                        reload=True,
                    )
                except (CloudflareError, OSError, ValueError, RuntimeError) as exc:
                    results.append(
                        {"node": node["node_uid"], "ok": False, "error": str(exc)}
                    )
        await operation.complete(
            "success" if all(result["ok"] for result in results) else "partial",
            phase="Domain check complete",
            details={"domains": results},
        )
    return {"ok": all(result["ok"] for result in results), "domains": results}
