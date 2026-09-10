from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from app.schemas.backends import BackendCloneIn, BackendIn, BackendUpdate
from app.schemas.inputs import InputIn, InputUpdate
from app.services.error_reporting import ErrorCode
from app.services.renderers import SHIELD_PROFILE
from app.services.resource_profile import RESOURCE_SIZES
from app.services.sandbox_profiles import default_app_sandbox_profile
from app.services.validators import ValidationError
from app.ui.errors import operator_coded_error as _operator_coded_error

DOMAIN_INPUT_HINT = "Use a lowercase hostname like api.example.com. Letters, digits, hyphens, and dots only."
TAILNET_PATH_HINT = "Use a private path like /app1 or /docs/api. Each segment must stay lowercase and hyphen-safe."
STATIC_ROOT_HINT = "Use an absolute host path like /srv/site or /var/www/docs. Static roots cannot contain spaces."
VOLUME_BOUNDARY_HINT = (
    "Use app-owned data paths only. Volumes cannot point at CNC-managed host paths, host runtime paths, "
    "or CNC control paths inside the container."
)

DEFAULT_UI_APP_SANDBOX_PROFILE = default_app_sandbox_profile()


def _form_string_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _blank_form_value(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _nonblank_form_values(values: list[str]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


def _schema_validation_message(exc: PydanticValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid form submission"
    error = errors[0]
    loc = error.get("loc") or ()
    field = ".".join(str(part) for part in loc) or "field"
    message = str(error.get("msg") or "invalid value")
    error_type = str(error.get("type") or "")
    ctx = error.get("ctx") if isinstance(error.get("ctx"), dict) else {}
    if error_type in {"int_parsing", "int_type"}:
        return f"{field} must be a whole number"
    if error_type == "greater_than_equal" and "ge" in ctx:
        return f"{field} must be at least {ctx['ge']}"
    if error_type == "less_than_equal" and "le" in ctx:
        return f"{field} must be at most {ctx['le']}"
    return f"{field}: {message}"


def _normalize_backend_form_sandbox_profile(
    *, kind: str, sandbox_profile: object
) -> str | None:
    normalized_kind = kind.strip().lower()
    normalized_sandbox_profile = (
        str(sandbox_profile).strip().lower()
        if isinstance(sandbox_profile, str)
        else None
    )
    if normalized_kind == "app":
        if normalized_sandbox_profile in {
            "docker.io/library/ubuntu:24.04",
            "ubuntu:24.04",
        }:
            return DEFAULT_UI_APP_SANDBOX_PROFILE
        return normalized_sandbox_profile or DEFAULT_UI_APP_SANDBOX_PROFILE
    if normalized_kind == "shield":
        return SHIELD_PROFILE
    return None


def _normalize_backend_form_sandbox_image(
    *, kind: str, sandbox_image: object
) -> str | None:
    return _normalize_backend_form_sandbox_profile(
        kind=kind, sandbox_profile=sandbox_image
    )


def _normalize_backend_form_resources(
    *,
    resource_mode: object,
    resource_size: object,
    memory_high_override: object,
    memory_max_override: object,
    cpu_quota_override: object,
    current_resource_size: object = None,
) -> dict[str, str | None]:
    raw_mode = _form_string_or_default(resource_mode, "auto").strip().lower()
    raw_size = _form_string_or_default(resource_size, "auto").strip().lower()
    current_size = (
        _form_string_or_default(current_resource_size, "small").strip().lower()
    )
    preserved_auto_size = current_size if current_size in RESOURCE_SIZES else "small"
    memory_high = _blank_form_value(memory_high_override)
    memory_max = _blank_form_value(memory_max_override)
    cpu_quota = _blank_form_value(cpu_quota_override)

    if raw_size == "auto":
        return {
            "resource_mode": "auto",
            "resource_size": preserved_auto_size,
            "memory_high_override": None,
            "memory_max_override": None,
            "cpu_quota_override": None,
        }
    if raw_size == "custom":
        return {
            "resource_mode": "manual",
            "resource_size": "small",
            "memory_high_override": memory_high,
            "memory_max_override": memory_max,
            "cpu_quota_override": cpu_quota,
        }
    if raw_size in RESOURCE_SIZES:
        has_legacy_manual_limits = raw_mode == "manual" and bool(
            memory_high or memory_max or cpu_quota
        )
        return {
            "resource_mode": "manual",
            "resource_size": raw_size,
            "memory_high_override": memory_high if has_legacy_manual_limits else None,
            "memory_max_override": memory_max if has_legacy_manual_limits else None,
            "cpu_quota_override": cpu_quota if has_legacy_manual_limits else None,
        }

    normalized_size = raw_size if raw_size in RESOURCE_SIZES else "small"
    normalized_mode = "manual" if raw_mode == "manual" else "auto"
    if normalized_mode == "auto":
        return {
            "resource_mode": "auto",
            "resource_size": normalized_size,
            "memory_high_override": None,
            "memory_max_override": None,
            "cpu_quota_override": None,
        }
    return {
        "resource_mode": "manual",
        "resource_size": normalized_size,
        "memory_high_override": memory_high,
        "memory_max_override": memory_max,
        "cpu_quota_override": cpu_quota,
    }


def _backend_form_base_payload(
    *,
    name: str,
    kind: str,
    port: object,
    static_root: str,
    sandbox_profile: str,
    sandbox_image: str,
    handoff_port: object,
    healthcheck_mode: str,
    healthcheck_path: str,
    healthcheck_host_header: str,
    resource_mode: str,
    resource_size: str,
    memory_high_override: str,
    memory_max_override: str,
    cpu_quota_override: str,
    volumes_json: str,
    notes: str,
    shield_enabled: bool = False,
    shield_code_hash: str | None = None,
    shield_access_code: str | None = None,
    current_resource_size: object = None,
) -> dict[str, object]:
    normalized_kind = _form_string_or_default(kind, "").strip().lower()
    raw_sandbox_profile = _form_string_or_default(sandbox_profile, "")
    raw_sandbox_image = _form_string_or_default(sandbox_image, "")
    normalized_sandbox_profile = _normalize_backend_form_sandbox_profile(
        kind=normalized_kind,
        sandbox_profile=raw_sandbox_profile or raw_sandbox_image,
    )
    resource_payload = _normalize_backend_form_resources(
        resource_mode=resource_mode,
        resource_size=resource_size,
        current_resource_size=current_resource_size,
        memory_high_override=memory_high_override,
        memory_max_override=memory_max_override,
        cpu_quota_override=cpu_quota_override,
    )
    return {
        "name": name,
        "kind": normalized_kind,
        "port": _blank_form_value(port),
        "static_root": _blank_form_value(static_root),
        "sandbox_profile": normalized_sandbox_profile,
        "handoff_port": handoff_port,
        "healthcheck_mode": _blank_form_value(
            _form_string_or_default(healthcheck_mode, "").lower()
        ),
        "healthcheck_path": _blank_form_value(healthcheck_path),
        "healthcheck_host_header": _blank_form_value(
            _form_string_or_default(healthcheck_host_header, "").lower()
        ),
        **resource_payload,
        "shield_enabled": bool(shield_enabled),
        "shield_code_hash": _blank_form_value(shield_code_hash),
        "shield_access_code": _blank_form_value(shield_access_code),
        "volumes_json": _form_string_or_default(volumes_json, "[]").strip() or "[]",
        "notes": _blank_form_value(notes),
    }


def _backend_create_payload_from_form(
    *,
    input_ids: list[str],
    enabled: bool,
    **kwargs: object,
) -> BackendIn:
    try:
        return BackendIn(
            **_backend_form_base_payload(**kwargs),
            enabled=enabled,
            input_ids=_nonblank_form_values(input_ids),
        )
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc


def _backend_update_payload_from_form(**kwargs: object) -> BackendUpdate:
    try:
        return BackendUpdate(**_backend_form_base_payload(**kwargs))
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc


def _backend_clone_payload_from_form(*, name: str, port: str) -> BackendCloneIn:
    try:
        return BackendCloneIn(
            name=_blank_form_value(name),
            port=_blank_form_value(port),
        )
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc


def _backend_inputs_payload_from_form(*, input_ids: list[str]) -> BackendUpdate:
    try:
        return BackendUpdate(input_ids=_nonblank_form_values(input_ids))
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc


def _input_create_payload_from_form(
    *,
    kind: str,
    value: str,
    backend_ids: list[str],
    enabled: bool,
    shield_enabled: bool = False,
    shield_code_hash: str | None = None,
    shield_access_code: str | None = None,
) -> InputIn:
    try:
        return InputIn(
            kind=kind,
            value=value,
            backend_ids=_nonblank_form_values(backend_ids),
            enabled=enabled,
            shield_enabled=shield_enabled,
            shield_code_hash=_blank_form_value(shield_code_hash),
            shield_access_code=_blank_form_value(shield_access_code),
        )
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc


def _input_update_payload_from_form(
    *,
    kind: str,
    value: str,
    backend_ids: list[str],
    enabled: bool,
    shield_enabled: bool = False,
    shield_code_hash: str | None = None,
    shield_access_code: str | None = None,
) -> InputUpdate:
    try:
        return InputUpdate(
            kind=kind,
            value=value,
            backend_ids=_nonblank_form_values(backend_ids),
            enabled=enabled,
            shield_enabled=shield_enabled,
            shield_code_hash=_blank_form_value(shield_code_hash),
            shield_access_code=_blank_form_value(shield_access_code),
        )
    except PydanticValidationError as exc:
        raise ValidationError(_schema_validation_message(exc)) from exc



def operator_validation_error(exc: ValidationError) -> str:
    message = str(exc)
    if message.startswith("hostname"):
        message = f"{message}. {DOMAIN_INPUT_HINT}"
    elif message.startswith("tailnet path") or message.startswith(
        "invalid tailnet path"
    ):
        message = f"{message}. {TAILNET_PATH_HINT}"
    elif message.startswith("tailnet service") or message.startswith(
        "invalid tailnet service"
    ):
        message = f"{message}. Use a Tailscale service name like app-dev."
    elif message.startswith("static_root"):
        message = f"{message}. {STATIC_ROOT_HINT}"
    elif message.startswith("volume source path") or message.startswith(
        "volume target path"
    ):
        message = f"{message}. {VOLUME_BOUNDARY_HINT}"
    return _operator_coded_error(message, ErrorCode.VALIDATION_FAILED)
