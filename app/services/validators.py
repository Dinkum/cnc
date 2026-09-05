from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from app.config import Settings, get_settings
from app.models.entities import Backend, Input
from app.services.error_reporting import CNCError, ErrorCode
from app.services.netdata_constants import NETDATA_PORT
from app.services.renderers import (
    SHIELD_AUTH_PATH,
    SHIELD_BACKEND_NAME,
    SHIELD_PROFILE,
    SHIELD_PUBLIC_PATH_PREFIX,
)
from app.services.sandbox_profiles import get_app_sandbox_profile


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
HEALTHCHECK_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
HEALTHCHECK_MODE_RE = re.compile(r"^(http|tcp|none)$")
INPUT_KIND_RE = re.compile(r"^(domain|shield|tailnet_path|tailnet_service)$")
TAILNET_PATH_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9-]+$")
RESOURCE_MODE_RE = re.compile(r"^(auto|manual)$")
RESOURCE_SIZE_RE = re.compile(r"^(small|medium|large)$")
MEMORY_LIMIT_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>[KMGTP]I?B?|B)?$", re.IGNORECASE
)
CPU_QUOTA_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)%?$")
RESERVED_TAILNET_PATH_PREFIXES = (
    "/api",
    "/static",
    "/ui",
    SHIELD_PUBLIC_PATH_PREFIX,
    SHIELD_AUTH_PATH,
)
RESERVED_TAILNET_SERVICE_NAMES = ("netdata",)
RESERVED_HOST_PORTS = (NETDATA_PORT,)
PROTECTED_HOST_PATHS = (
    "/boot",
    "/dev",
    "/proc",
    "/root",
    "/run",
    "/sys",
)
PROTECTED_CONTAINER_PATHS = ("/cnc", "/var/lib/cnc")


class ValidationError(CNCError):
    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode = ErrorCode.VALIDATION_FAILED,
    ) -> None:
        super().__init__(
            error_code,
            message,
            status_code=400,
            severity="warning",
        )


@dataclass(frozen=True)
class ValidatedInputBinding:
    input_kind: str
    input_value: str
    attached_enabled_backends: list[Backend]


@dataclass(frozen=True)
class ParsedVolumeBinding:
    raw: str
    source: str
    target: str
    options: str | None


def ensure_hostname(hostname: str) -> str:
    raw = hostname.strip()
    if "://" in raw:
        parsed = urlsplit(raw)
        raw = parsed.hostname or raw
    else:
        raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" in raw:
            raw = raw.rsplit("@", 1)[-1]
        if ":" in raw and raw.count(":") == 1:
            raw = raw.rsplit(":", 1)[0]
    normalized = raw.strip().lower().rstrip(".")
    if not normalized:
        raise ValidationError("hostname cannot be empty")
    if len(normalized) > 253:
        raise ValidationError("hostname is too long")
    labels = normalized.split(".")
    if any(not label for label in labels):
        raise ValidationError("hostname cannot contain empty labels")
    for label in labels:
        if len(label) > 63:
            raise ValidationError(f"hostname label too long: {label}")
        if label.startswith("-") or label.endswith("-"):
            raise ValidationError(
                f"hostname label cannot start or end with '-': {label}"
            )
        if not HOSTNAME_LABEL_RE.match(label):
            raise ValidationError(f"hostname label has invalid characters: {label}")
    return normalized


def ensure_input_kind(value: str) -> str:
    normalized = value.strip().lower()
    if not INPUT_KIND_RE.match(normalized):
        raise ValidationError(f"invalid input kind: {value}")
    return normalized


def ensure_tailscale_path(value: str) -> str:
    raw = value.strip().lower()
    normalized = "/" + raw.strip("/")
    if normalized == "/":
        raise ValidationError("tailnet path cannot be /")
    if any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in RESERVED_TAILNET_PATH_PREFIXES
    ):
        raise ValidationError(
            f"tailnet path is reserved for system routing: {normalized}"
        )
    labels = normalized.lstrip("/").split("/")
    if any(not TAILNET_PATH_LABEL_RE.match(label) for label in labels):
        raise ValidationError(f"invalid tailnet path: {value}")
    return normalized


def ensure_tailscale_service(value: str) -> str:
    raw = value.strip().lower()
    if raw.startswith("svc:"):
        raw = raw[4:]
    if "." in raw:
        raw = raw.split(".", 1)[0]
    if not NAME_RE.match(raw):
        raise ValidationError(
            f"tailnet service name must match [a-z0-9][a-z0-9-]{{0,62}}, got: {value}"
        )
    if raw in RESERVED_TAILNET_SERVICE_NAMES:
        raise ValidationError(
            f"tailnet service name is reserved for system routing: {raw}"
        )
    return raw


def ensure_input_value(kind: str, value: str) -> str:
    normalized_kind = ensure_input_kind(kind)
    if normalized_kind in {"domain", "shield"}:
        return ensure_hostname(value)
    if normalized_kind == "tailnet_path":
        return ensure_tailscale_path(value)
    return ensure_tailscale_service(value)


def ensure_backend_name(name: str) -> str:
    normalized = name.strip().lower()
    if not NAME_RE.match(normalized):
        raise ValidationError(
            f"backend name must match [a-z0-9][a-z0-9-]{{0,62}}, got: {name}"
        )
    return normalized


def parse_volumes_json(payload: str) -> list[str]:
    try:
        parsed = json.loads(payload or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"volumes_json must be valid JSON array: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValidationError("volumes_json must decode to array")
    normalized: list[str] = []
    for item in parsed:
        volume = str(item).strip()
        ensure_safe_value(volume, field_name="volume entry")
        if " " in volume:
            raise ValidationError(f"volume entry cannot contain spaces: {volume}")
        if ":" not in volume:
            raise ValidationError(f"volume entry must include ':' mapping: {volume}")
        normalized.append(volume)
    return normalized


def parse_volume_binding(volume: str) -> ParsedVolumeBinding:
    source, separator, remainder = volume.partition(":")
    if separator != ":" or not remainder:
        raise ValidationError(f"volume entry must include ':' mapping: {volume}")
    target, options_separator, options = remainder.partition(":")
    normalized_target = ensure_absolute_path(target, field_name="volume target path")
    normalized_options = options.strip() if options_separator else None
    return ParsedVolumeBinding(
        raw=volume,
        source=source.strip(),
        target=normalized_target,
        options=normalized_options or None,
    )


def ensure_safe_value(value: str, field_name: str) -> str:
    if CONTROL_CHAR_RE.search(value):
        raise ValidationError(f"{field_name} contains control characters")
    if any(char in value for char in ["{", "}", ";"]):
        raise ValidationError(f"{field_name} contains forbidden characters")
    return value


def ensure_absolute_path(value: str, field_name: str) -> str:
    normalized = value.strip()
    ensure_safe_value(normalized, field_name=field_name)
    if not normalized.startswith("/"):
        raise ValidationError(f"{field_name} must be absolute: {value}")
    if any(char.isspace() for char in normalized):
        raise ValidationError(f"{field_name} cannot contain whitespace: {value}")
    return normalized


def _normalize_posix_absolute_path(value: str) -> str:
    normalized = posixpath.normpath(value.strip())
    pure_path = PurePosixPath(normalized)
    if not pure_path.is_absolute():
        raise ValidationError(f"path must be absolute: {value}")
    return str(pure_path)


def _paths_overlap(candidate: str, protected: str) -> bool:
    return (
        candidate == protected
        or candidate.startswith(protected + "/")
        or protected.startswith(candidate + "/")
    )


def _settings_protected_host_paths(settings: Settings) -> tuple[str, ...]:
    protected: set[str] = set(PROTECTED_HOST_PATHS)
    managed_paths = [
        settings.managed_env_file_path,
        settings.nginx_generated_dir,
        settings.host_state_path,
        settings.systemd_generated_dir,
        settings.apply_backup_dir,
        settings.backend_backup_dir,
        settings.app_control_dir,
        settings.app_sandbox_dir,
        settings.app_quadlet_dir,
        settings.tailscale_serve_state_path,
        settings.ssh_backend_home_root,
        settings.ssh_backend_root_wrapper_path,
        settings.ssh_backend_sshd_config_path,
        settings.ssh_backend_sudoers_path,
        settings.ssh_backend_authorized_keys_path,
        settings.ssh_backend_authorized_keys_source_path,
        settings.update_log_dir,
        settings.updater_script_path,
        settings.shield_state_dir,
        settings.shield_db_path,
        settings.shield_env_file_path,
        settings.shield_config_path,
    ]
    for item in managed_paths:
        protected.add(_normalize_posix_absolute_path(str(item)))
    for candidate in (settings.sqlite_path, str(settings.log_path)):
        if candidate.startswith("/"):
            protected.add(_normalize_posix_absolute_path(candidate))
    return tuple(sorted(protected))


def _ensure_path_is_not_protected(
    value: str,
    *,
    field_name: str,
    protected_paths: tuple[str, ...],
) -> str:
    normalized = _normalize_posix_absolute_path(value)
    for protected in protected_paths:
        if _paths_overlap(normalized, protected):
            raise ValidationError(
                f"{field_name} cannot overlap protected host path {protected}: {value}"
            )
    return normalized


def _ensure_path_is_not_reserved_in_container(value: str, *, field_name: str) -> str:
    normalized = _normalize_posix_absolute_path(value)
    for protected in PROTECTED_CONTAINER_PATHS:
        if _paths_overlap(normalized, protected):
            raise ValidationError(
                f"{field_name} cannot overlap cnc-reserved container path {protected}: {value}"
            )
    return normalized


def _validate_backend_mount_boundaries(backend: Backend, *, settings: Settings) -> None:
    protected_host_paths = _settings_protected_host_paths(settings)
    if backend.static_root:
        _ensure_path_is_not_protected(
            backend.static_root,
            field_name=f"static_root for {backend.name}",
            protected_paths=protected_host_paths,
        )
    if backend.kind != "app":
        return
    for volume in parse_volumes_json(backend.volumes_json):
        binding = parse_volume_binding(volume)
        if binding.source.startswith("/"):
            _ensure_path_is_not_protected(
                binding.source,
                field_name=f"volume source path for {backend.name}",
                protected_paths=protected_host_paths,
            )
        _ensure_path_is_not_reserved_in_container(
            binding.target,
            field_name=f"volume target path for {backend.name}",
        )


def ensure_healthcheck_path(value: str) -> str:
    normalized = value.strip() or "/"
    if not HEALTHCHECK_PATH_RE.match(normalized):
        raise ValidationError(f"invalid healthcheck_path: {value}")
    return normalized


def ensure_healthcheck_mode(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized == "auto":
        return "http"
    if not HEALTHCHECK_MODE_RE.match(normalized):
        raise ValidationError(f"{field_name} must be one of http, tcp, none")
    return normalized


def ensure_healthcheck_host_header(value: str | None, *, field_name: str) -> str | None:
    normalized = str(value or "").strip().lower().rstrip(".")
    if not normalized:
        return None
    return ensure_hostname(normalized)


def ensure_resource_mode(value: str | None, *, field_name: str) -> str:
    normalized = str(value or "auto").strip().lower() or "auto"
    if not RESOURCE_MODE_RE.match(normalized):
        raise ValidationError(f"{field_name} must be auto or manual")
    return normalized


def ensure_resource_size(value: str | None, *, field_name: str) -> str:
    normalized = str(value or "small").strip().lower() or "small"
    if not RESOURCE_SIZE_RE.match(normalized):
        raise ValidationError(f"{field_name} must be small, medium, or large")
    return normalized


def ensure_memory_limit(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized:
        return None
    match = MEMORY_LIMIT_RE.match(normalized)
    if not match:
        raise ValidationError(f"{field_name} must look like 512M, 1G, or 1536MiB")
    try:
        number = float(match.group("number"))
    except ValueError as exc:
        raise ValidationError(f"{field_name} has invalid numeric value") from exc
    if number <= 0:
        raise ValidationError(f"{field_name} must be greater than 0")
    unit = (match.group("unit") or "B").upper()
    unit_aliases = {
        "K": "K",
        "KB": "KB",
        "KI": "KI",
        "KIB": "KIB",
        "M": "M",
        "MB": "MB",
        "MI": "MI",
        "MIB": "MIB",
        "G": "G",
        "GB": "GB",
        "GI": "GI",
        "GIB": "GIB",
        "T": "T",
        "TB": "TB",
        "TI": "TI",
        "TIB": "TIB",
        "P": "P",
        "PB": "PB",
        "PI": "PI",
        "PIB": "PIB",
        "B": "B",
    }
    canonical_unit = unit_aliases.get(unit)
    if canonical_unit is None:
        raise ValidationError(f"{field_name} has unsupported unit: {unit}")
    if canonical_unit == "B":
        return f"{int(number)}B" if number.is_integer() else f"{number:g}B"
    return f"{number:g}{canonical_unit}"


def ensure_cpu_quota(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    match = CPU_QUOTA_RE.match(normalized)
    if not match:
        raise ValidationError(f"{field_name} must look like 50% or 100%")
    try:
        number = float(match.group("number"))
    except ValueError as exc:
        raise ValidationError(f"{field_name} has invalid numeric value") from exc
    if number <= 0:
        raise ValidationError(f"{field_name} must be greater than 0")
    if number > 100:
        raise ValidationError(f"{field_name} must be 100% or lower")
    return f"{number:g}%"


def validate_backend_shape(backend: Backend) -> None:
    settings = get_settings()
    normalized_updates: dict[str, object] = {}
    normalized_name = ensure_backend_name(backend.name)
    if backend.kind not in {"static", "app", "shield"}:
        raise ValidationError(f"invalid kind: {backend.kind}")
    if normalized_name == SHIELD_BACKEND_NAME and backend.kind != "shield":
        raise ValidationError(
            f"backend name {SHIELD_BACKEND_NAME} is reserved for the shield output"
        )
    if backend.kind == "shield":
        if normalized_name != SHIELD_BACKEND_NAME:
            raise ValidationError(f"shield backend must be named {SHIELD_BACKEND_NAME}")
        if getattr(backend, "shield_enabled", False):
            raise ValidationError("shield backend cannot be shielded by itself")
        normalized_updates["sandbox_profile"] = SHIELD_PROFILE
        normalized_updates["static_root"] = None
        normalized_updates["port"] = None
        normalized_updates["healthcheck_mode"] = None
        normalized_updates["healthcheck_path"] = None
        normalized_updates["healthcheck_host_header"] = None
    normalized_updates["resource_mode"] = ensure_resource_mode(
        getattr(backend, "resource_mode", None),
        field_name=f"resource_mode for {backend.name}",
    )
    normalized_updates["resource_size"] = ensure_resource_size(
        getattr(backend, "resource_size", None),
        field_name=f"resource_size for {backend.name}",
    )
    if backend.port is not None and not (1 <= backend.port <= 65535):
        raise ValidationError(
            f"backend {backend.name} has invalid port: {backend.port}"
        )
    if backend.kind == "app" and backend.port in RESERVED_HOST_PORTS:
        raise ValidationError(
            f"backend {backend.name} uses reserved host port: {backend.port}"
        )
    if backend.kind == "static":
        if not backend.static_root:
            raise ValidationError(f"static backend {backend.name} requires static_root")
        ensure_safe_value(
            backend.static_root, field_name=f"static_root for {backend.name}"
        )
        if not Path(backend.static_root).is_absolute():
            raise ValidationError(
                f"static_root must be absolute: {backend.static_root}"
            )
        if any(char.isspace() for char in backend.static_root):
            raise ValidationError(
                f"static_root cannot contain whitespace: {backend.static_root}"
            )
    if backend.kind == "app":
        sandbox_profile = str(backend.sandbox_profile or "").strip().lower()
        if not sandbox_profile:
            raise ValidationError(
                f"app backend {backend.name} requires sandbox_profile"
            )
        try:
            get_app_sandbox_profile(sandbox_profile)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        normalized_updates["sandbox_profile"] = sandbox_profile
        if backend.handoff_port <= 0 or backend.handoff_port > 65535:
            raise ValidationError(
                f"app backend {backend.name} has invalid handoff_port"
            )
        raw_healthcheck_mode = str(backend.healthcheck_mode or "").strip().lower()
        normalized_mode = (
            ensure_healthcheck_mode(
                raw_healthcheck_mode,
                field_name=f"healthcheck_mode for {backend.name}",
            )
            if raw_healthcheck_mode
            else None
        )
        normalized_updates["healthcheck_mode"] = normalized_mode
        if normalized_mode == "http" or (
            normalized_mode is None and backend.healthcheck_path is not None
        ):
            normalized_updates["healthcheck_path"] = ensure_healthcheck_path(
                str(backend.healthcheck_path or "/")
            )
        else:
            normalized_updates["healthcheck_path"] = None
        normalized_updates["healthcheck_host_header"] = ensure_healthcheck_host_header(
            backend.healthcheck_host_header,
            field_name=f"healthcheck_host_header for {backend.name}",
        )
        normalized_updates["memory_high_override"] = ensure_memory_limit(
            backend.memory_high_override,
            field_name=f"memory_high_override for {backend.name}",
        )
        normalized_updates["memory_max_override"] = ensure_memory_limit(
            backend.memory_max_override,
            field_name=f"memory_max_override for {backend.name}",
        )
        normalized_updates["cpu_quota_override"] = ensure_cpu_quota(
            backend.cpu_quota_override,
            field_name=f"cpu_quota_override for {backend.name}",
        )
        memory_high_override = normalized_updates["memory_high_override"]
        memory_max_override = normalized_updates["memory_max_override"]
        if isinstance(memory_high_override, str) and isinstance(
            memory_max_override, str
        ):
            memory_high_bytes = _parse_memory_limit_bytes(memory_high_override)
            memory_max_bytes = _parse_memory_limit_bytes(memory_max_override)
            if (
                memory_high_bytes is not None
                and memory_max_bytes is not None
                and memory_high_bytes > memory_max_bytes
            ):
                raise ValidationError(
                    f"memory_high_override for {backend.name} cannot exceed memory_max_override"
                )
        if any(
            normalized_updates.get(key)
            for key in (
                "memory_high_override",
                "memory_max_override",
                "cpu_quota_override",
            )
        ):
            normalized_updates["resource_mode"] = "manual"
    elif getattr(backend, "shield_enabled", False):
        raise ValidationError(
            f"shield can only be enabled on app backends: {backend.name}"
        )
    for key, value in normalized_updates.items():
        setattr(backend, key, value)
    parse_volumes_json(backend.volumes_json)
    _validate_backend_mount_boundaries(backend, settings=settings)


def validate_backend_collection(backends: list[Backend]) -> None:
    for backend in backends:
        validate_backend_shape(backend)

    shield_backends = [backend for backend in backends if backend.kind == "shield"]
    if len(shield_backends) > 1:
        raise ValidationError("only one shield backend is allowed")

    port_owner: dict[int, str] = {}
    for backend in backends:
        if backend.kind != "app" or backend.port is None:
            continue
        existing = port_owner.get(backend.port)
        if existing is not None:
            raise ValidationError(
                f"duplicate backend port: {backend.port}",
                error_code=ErrorCode.APPLY_PORT_COLLISION,
            )
        port_owner[backend.port] = backend.name

    for backend in backends:
        _validate_healthcheck_host_header_binding(backend)


def _validate_healthcheck_host_header_binding(backend: Backend) -> None:
    configured = str(backend.healthcheck_host_header or "").strip().lower().rstrip(".")
    if backend.kind != "app" or not configured:
        return
    loaded_inputs = backend.__dict__.get("inputs")
    if not isinstance(loaded_inputs, list):
        return
    attached_domains = sorted(
        ensure_hostname(str(item.hostname or ""))
        for item in loaded_inputs
        if str(item.kind or "domain").strip().lower() == "domain"
    )
    if configured in attached_domains:
        return
    if attached_domains:
        expected = ", ".join(attached_domains)
        raise ValidationError(
            f"healthcheck_host_header for {backend.name} must match an attached domain input: {expected}"
        )
    raise ValidationError(
        f"healthcheck_host_header for {backend.name} requires an attached domain input"
    )


def validate_input_bindings(inputs: list[Input]) -> list[ValidatedInputBinding]:
    bindings: list[ValidatedInputBinding] = []
    for item in inputs:
        if not item.enabled and not getattr(item, "shield_enabled", False):
            continue
        input_kind = ensure_input_kind(str(item.kind or "domain"))
        input_value = ensure_input_value(input_kind, item.hostname)
        if getattr(item, "shield_enabled", False):
            if input_kind != "domain":
                raise ValidationError("shield can only be enabled on domain inputs")
            if not str(getattr(item, "shield_code_hash", "") or "").strip():
                raise ValidationError("shielded input requires an access code")
        if not item.enabled:
            continue
        attached_enabled_backends = [
            backend for backend in item.backends if backend.enabled
        ]
        if not attached_enabled_backends:
            continue
        kinds = {backend.kind for backend in attached_enabled_backends}
        if len(kinds) > 1:
            raise ValidationError(f"input {input_value} mixes backend kinds")
        if kinds == {"shield"}:
            if input_kind != "shield":
                raise ValidationError(
                    "shield output must be attached to a shield input"
                )
            if len(attached_enabled_backends) != 1:
                raise ValidationError(
                    "shield input must attach exactly one enabled shield output"
                )
            backend_name = attached_enabled_backends[0].name
            if backend_name != SHIELD_BACKEND_NAME:
                raise ValidationError(
                    f"shield input must attach the {SHIELD_BACKEND_NAME} output"
                )
        if (
            input_kind == "domain"
            and kinds == {"static"}
            and len(attached_enabled_backends) > 1
        ):
            raise ValidationError(
                f"input {input_value} cannot attach multiple static outputs"
            )
        if (
            input_kind in {"tailnet_path", "tailnet_service"}
            and len(attached_enabled_backends) > 1
        ):
            label = (
                "tailnet path" if input_kind == "tailnet_path" else "tailnet service"
            )
            raise ValidationError(
                f"{label} {input_value} must attach exactly one enabled output"
            )
        bindings.append(
            ValidatedInputBinding(
                input_kind=input_kind,
                input_value=input_value,
                attached_enabled_backends=attached_enabled_backends,
            )
        )
    return bindings


def _parse_memory_limit_bytes(value: str) -> int | None:
    match = MEMORY_LIMIT_RE.match(value.strip().upper())
    if not match:
        return None
    try:
        number = float(match.group("number"))
    except ValueError:
        return None
    unit = (match.group("unit") or "B").upper()
    multipliers = {
        "B": 1,
        "K": 1024,
        "KB": 1000,
        "KI": 1024,
        "KIB": 1024,
        "M": 1024 * 1024,
        "MB": 1000 * 1000,
        "MI": 1024 * 1024,
        "MIB": 1024 * 1024,
        "G": 1024 * 1024 * 1024,
        "GB": 1000 * 1000 * 1000,
        "GI": 1024 * 1024 * 1024,
        "GIB": 1024 * 1024 * 1024,
        "T": 1024 * 1024 * 1024 * 1024,
        "TB": 1000 * 1000 * 1000 * 1000,
        "TI": 1024 * 1024 * 1024 * 1024,
        "TIB": 1024 * 1024 * 1024 * 1024,
        "P": 1024 * 1024 * 1024 * 1024 * 1024,
        "PB": 1000 * 1000 * 1000 * 1000 * 1000,
        "PI": 1024 * 1024 * 1024 * 1024 * 1024,
        "PIB": 1024 * 1024 * 1024 * 1024 * 1024,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None
    return int(number * multiplier)
