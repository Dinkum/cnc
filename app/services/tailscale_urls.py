from __future__ import annotations

from app.config import Settings


def normalize_tailnet_dns_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower().rstrip(".")
    return normalized if normalized.endswith(".ts.net") else ""


def infer_tailnet_dns_name(settings: Settings) -> str:
    configured = normalize_tailnet_dns_name(settings.tailscale_tailnet_dns_name)
    if configured:
        return configured
    for hostname in settings.trusted_host_list:
        normalized = str(hostname or "").strip().lower().rstrip(".")
        if normalized.endswith(".ts.net") and "." in normalized:
            return normalized.split(".", 1)[1]
    return ""


def configured_tailnet_admin_host(settings: Settings) -> str:
    for hostname in settings.trusted_host_list:
        normalized = str(hostname or "").strip().lower().rstrip(".")
        if normalized.endswith(".ts.net") and "." in normalized:
            return normalized
    return ""


def tailscale_service_url(service: str, settings: Settings) -> str:
    service_name = str(service or "").strip().lower().rstrip(".")
    tailnet_dns_name = infer_tailnet_dns_name(settings)
    if not service_name or not tailnet_dns_name:
        return ""
    return f"https://{service_name}.{tailnet_dns_name}"
