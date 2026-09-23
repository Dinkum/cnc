"""Explicit form boundaries for independently saved Output settings."""

from collections.abc import Mapping

from pydantic import ValidationError as SchemaValidationError

from app.config import Settings
from app.models.entities import Backend
from app.schemas.backends import BackendUpdate
from app.services.shield_service import make_code_hash
from app.services.validators import ValidationError
from app.ui.forms import (
    _blank_form_value,
    _normalize_backend_form_resources,
    _normalize_backend_form_sandbox_profile,
    _schema_validation_message,
)

SECTION_FIELDS = {
    "general": {"name", "notes"},
    "runtime": {"port", "sandbox_profile", "handoff_port"},
    "resources": {
        "resource_size",
        "memory_high_override",
        "memory_max_override",
        "cpu_quota_override",
    },
    "storage": {"static_root", "volumes_json"},
    "health": {"healthcheck_mode", "healthcheck_path", "healthcheck_host_header"},
    "shield": {"shield_enabled", "shield_access_code"},
}


def backend_section_payload(
    backend: Backend, settings: Settings, section: str, values: Mapping[str, object]
) -> BackendUpdate:
    fields = SECTION_FIELDS.get(section)
    if fields is None:
        raise ValidationError("Unknown settings section.")
    if set(values) - fields:
        raise ValidationError("This form contains fields from another section.")
    if section in {"runtime", "health", "shield"} and backend.kind != "app":
        raise ValidationError("These settings are available for app outputs only.")
    payload: dict[str, object] = {}
    if section == "resources":
        if str(values.get("resource_size")) not in {
            "auto",
            "small",
            "medium",
            "large",
            "custom",
        }:
            raise ValidationError("Choose a resource size.")
        payload = _normalize_backend_form_resources(
            resource_mode="auto",
            resource_size=values.get("resource_size"),
            current_resource_size=backend.resource_size,
            memory_high_override=values.get("memory_high_override"),
            memory_max_override=values.get("memory_max_override"),
            cpu_quota_override=values.get("cpu_quota_override"),
        )
    elif section == "shield":
        enabled = str(values.get("shield_enabled", "false")).lower() in {
            "true",
            "on",
            "1",
        }
        payload["shield_enabled"] = enabled
        code = str(values.get("shield_access_code") or "").strip()
        if code:
            payload["shield_code_hash"] = make_code_hash(
                settings.shield_secret_key, code
            )
            payload["shield_access_code"] = code
        if enabled and not (code or backend.shield_code_hash):
            raise ValidationError("Enter an access code to enable Shield.")
    else:
        for key, value in values.items():
            payload[key] = _blank_form_value(value)
        if section == "general" and not payload.get("name"):
            raise ValidationError("Enter an output name.")
        if section == "runtime":
            payload["sandbox_profile"] = _normalize_backend_form_sandbox_profile(
                kind=backend.kind, sandbox_profile=values.get("sandbox_profile")
            )
        if section == "storage":
            allowed = {"static_root"} if backend.kind == "static" else {"volumes_json"}
            if set(values) - allowed:
                raise ValidationError(
                    "These storage settings do not match this output type."
                )
            if "volumes_json" in values:
                payload["volumes_json"] = str(values.get("volumes_json") or "[]")
        if section == "health":
            if payload.get("healthcheck_mode") not in {"none", "tcp", "http"}:
                raise ValidationError("Choose a health-check mode.")
    try:
        # Only the section's submitted fields may reach the update service. Do
        # not fill missing fields from a stale page snapshot or form defaults.
        return BackendUpdate(**payload)
    except SchemaValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc
