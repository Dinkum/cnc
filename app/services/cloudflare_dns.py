from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any
from uuid import UUID

from app.services.cloudflare_api import CloudflareClient, CloudflareError, resource_id


def ipv4_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise CloudflareError("Enter the serving node's IPv4 address.") from None
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise CloudflareError("Enter the serving node's public IPv4 address.")
    return str(address)


def records_fingerprint(records: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(records, key=lambda row: str(row.get("id"))), sort_keys=True
        ).encode()
    ).hexdigest()


def dns_plan(
    client: CloudflareClient,
    *,
    zone_id: str,
    hostname: str,
    ipv4: str,
    owned_record_id: str = "",
    tunnel_id: str = "",
) -> dict[str, Any]:
    if tunnel_id:
        try:
            tunnel_id = str(UUID(tunnel_id))
        except ValueError:
            raise CloudflareError(
                "Reconnect Cloudflare Tunnel before managing its DNS."
            ) from None
    record_type = "CNAME" if tunnel_id else "A"
    content = f"{tunnel_id}.cfargotunnel.com" if tunnel_id else ipv4
    records = client.records(zone_id, hostname)
    addresses = [row for row in records if row["type"] in {"A", "AAAA", "CNAME", "NS"}]
    result: dict[str, Any] = {
        "hostname": hostname,
        "ipv4": ipv4,
        "tunnel_id": tunnel_id,
        "type": record_type,
        "content": content,
        "record_id": "",
        "action": "create",
        "before": "No address record",
        "after": f"{record_type} {hostname} → {content} · Proxied",
        "fingerprint": records_fingerprint(records),
        "conflict": "",
    }
    if not addresses:
        return result
    if len(addresses) == 1 and addresses[0]["type"] in {"A", "CNAME"}:
        record = addresses[0]
        result["record_id"] = resource_id(record["id"])
        result["before"] = (
            f"{record['type']} {hostname} → {record['content']} · {'Proxied' if record['proxied'] else 'DNS only'}"
        )
        if (
            record["type"] == record_type
            and str(record["content"]).rstrip(".").lower() == content
            and record["proxied"] is True
        ):
            result["action"] = "keep"
            return result
        if result["record_id"] == owned_record_id:
            result["action"] = "update"
            return result
    result["action"] = "conflict"
    result["conflict"] = (
        "An existing address record conflicts with this change. Resolve it in Cloudflare, then preview again."
    )
    result["before"] = "; ".join(f"{row['type']} {row['content']}" for row in addresses)
    return result


def apply_dns_plan(
    client: CloudflareClient,
    zone_id: str,
    plan: dict[str, Any],
    *,
    owned_record_id: str = "",
) -> str:
    live = dns_plan(
        client,
        zone_id=zone_id,
        hostname=plan["hostname"],
        ipv4=plan["ipv4"],
        tunnel_id=plan.get("tunnel_id", ""),
        owned_record_id=owned_record_id,
    )
    if live["fingerprint"] != plan["fingerprint"] or live["conflict"]:
        raise CloudflareError(
            "DNS changed after the preview. Preview again before applying."
        )
    if live["action"] == "keep":
        return live["record_id"]
    path = f"/zones/{resource_id(zone_id)}/dns_records"
    body = {
        "type": live["type"],
        "name": plan["hostname"],
        "content": live["content"],
        "proxied": True,
        "ttl": 1,
    }
    if live["action"] == "update":
        path += f"/{resource_id(live['record_id'])}"
    result = client.request(
        "PATCH" if live["action"] == "update" else "POST", path, body=body
    ).get("result", {})
    return resource_id(result.get("id"))
