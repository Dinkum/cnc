import ipaddress
from pathlib import Path
from functools import lru_cache
from typing import Literal
import re

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_TRUSTED_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
DEFAULT_HOST_STATE_PATH = Path("/var/lib/cnc/host-state.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "cnc"
    log_app_id: str = "cnc.admin"
    log_env: str = "local"
    log_version: str = ""
    admin_host: str = "127.0.0.1"
    admin_port: int = Field(default=9090, ge=9090, le=9090)
    admin_unsafe_allow_remote: bool = False
    admin_allowed_hosts: str = ""
    access_key_hash: str = ""
    access_session_ttl_sec: int = Field(
        default=60 * 60 * 24 * 30, ge=300, le=60 * 60 * 24 * 365
    )
    csrf_token: str | None = None
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    log_path: Path = Path("data/app.log")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=1, le=100)
    log_compress_rotated: bool = True
    managed_env_file_path: Path = Path("/etc/cnc.env")

    nginx_generated_dir: Path = Path("/etc/nginx/generated")
    nginx_cloudflare_only: bool = True
    nginx_cloudflare_ips: str = (
        "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,"
        "141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,"
        "197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,"
        "104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,2400:cb00::/32,"
        "2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,"
        "2a06:98c0::/29,2c0f:f248::/32"
    )
    cloudflare_ips_v4_url: str = "https://www.cloudflare.com/ips-v4"
    cloudflare_ips_v6_url: str = "https://www.cloudflare.com/ips-v6"
    cloudflare_sync_timeout_sec: int = Field(default=15, ge=3, le=300)
    cloudflare_sync_fetch_retries: int = Field(default=2, ge=0, le=10)
    cloudflare_sync_fetch_retry_backoff_sec: float = Field(default=1.0, ge=0.0, le=60.0)
    external_http_retry_attempts: int = Field(default=2, ge=0, le=10)
    external_http_retry_backoff_sec: float = Field(default=0.5, ge=0.0, le=60.0)
    host_state_path: Path = DEFAULT_HOST_STATE_PATH
    cloudflare_sync_state_path: Path | None = None
    backend_alerts_state_path: Path | None = None
    backend_alert_grace_period_sec: int = Field(default=300, ge=0, le=86400)
    backend_alert_down_retry_sec: int = Field(default=300, ge=0, le=86400)
    backend_alert_repeat_sec: int = Field(
        default=60 * 60 * 6, ge=0, le=60 * 60 * 24 * 30
    )
    backend_auto_fix_enabled: bool = True
    backend_auto_fix_cooldown_sec: int = Field(default=900, ge=0, le=60 * 60 * 24 * 30)
    beta_routing: bool = False
    beta_hardening: bool = False
    multi_node_enabled: bool = False
    cluster_join_tailnet_base_url: str = ""
    cluster_join_token_ttl_sec: int = Field(default=900, ge=60, le=3600)
    systemd_generated_dir: Path = Path("/etc/systemd/system")
    apply_backup_dir: Path = Path("/var/lib/cnc/backups")
    backend_backup_dir: Path = Path("/var/lib/cnc/backend-backups")
    backend_backup_archive_format: Literal["tar.gz", "tar"] = "tar.gz"
    backend_backup_gzip_compresslevel: int = Field(default=6, ge=0, le=9)
    backend_backup_retention_per_backend: int = Field(default=10, ge=0, le=1000)
    backend_backup_upload_max_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024, ge=1024 * 1024
    )
    app_control_dir: Path = Path("/var/lib/cnc/app-control")
    app_sandbox_dir: Path = Path("/var/lib/cnc/sandboxes")
    app_quadlet_dir: Path = Path("/etc/containers/systemd")
    tailscale_serve_state_path: Path = Path("/var/lib/cnc/tailscale-serve-state.json")
    tailscale_tailnet_dns_name: str = ""
    app_container_dns_servers: str = ""
    app_container_resolv_conf_path: Path = Path("/etc/resolv.conf")
    ssh_backend_group: str = "cnc-backends"
    ssh_advertise_host: str = ""
    ssh_backend_home_root: Path = Path("/var/lib/cnc/ssh-users")
    ssh_backend_root_wrapper_path: Path = Path("/usr/local/bin/cnc-ssh-backend-root")
    ssh_backend_sshd_config_path: Path = Path(
        "/etc/ssh/sshd_config.d/cnc-backend-users.conf"
    )
    ssh_backend_sudoers_path: Path = Path("/etc/sudoers.d/cnc-backend-users")
    ssh_backend_authorized_keys_path: Path = Path(
        "/etc/ssh/cnc-backend-authorized_keys"
    )
    ssh_backend_authorized_keys_source_path: Path = Path("/root/.ssh/authorized_keys")
    port_range_start: int = 12000
    port_range_end: int = 12999

    default_memory_high: str = "256M"
    default_memory_max: str = "384M"
    default_cpu_quota: str = "50%"
    auto_resource_limits: bool = True
    auto_memory_reserve_percent: int = Field(default=30, ge=0, le=90)
    auto_cpu_reserve_percent: int = Field(default=20, ge=0, le=90)
    auto_min_memory_high_mb: int = Field(default=128, ge=32, le=32768)
    auto_min_cpu_quota_percent: int = Field(default=25, ge=5, le=100)
    auto_cpu_burst_factor: float = Field(default=2.0, ge=1.0, le=8.0)
    auto_size_nightly_hour_utc: int = Field(default=3, ge=0, le=23)
    hardening_phase1_default_sec: int = Field(
        default=60 * 60 * 24, ge=5, le=60 * 60 * 24 * 7
    )
    hardening_phase2_test_sec: int = Field(default=45, ge=5, le=60 * 30)
    shield_enabled: bool = False
    shield_port: int = Field(default=1026, ge=1, le=65535)
    shield_container_image: str = "localhost/cnc-shield:current"
    shield_state_dir: Path = Path("/var/lib/cnc/shield")
    shield_db_path: Path = Path("/var/lib/cnc/shield/shield.db")
    shield_env_file_path: Path = Path("/var/lib/cnc/shield/shield.env")
    shield_config_path: Path = Path("/var/lib/cnc/shield/shield-config.json")
    shield_secret_key: str = ""
    shield_access_code_hash: str = ""
    shield_session_ttl_sec: int = Field(
        default=60 * 60 * 24 * 7, ge=300, le=60 * 60 * 24 * 30
    )
    netdata_enabled: bool = False
    netdata_port: int = Field(default=19999, ge=1, le=65535)
    netdata_config_path: Path = Path("/etc/netdata/netdata.conf")
    apply_lock_path: Path = Path("/var/lib/cnc/apply.lock")
    app_bootstrap_stale_after_sec: int = Field(default=900, ge=60, le=86400)
    startup_db_timeout_sec: int = Field(default=30, ge=1, le=600)
    startup_observe_timeout_sec: int = Field(default=30, ge=1, le=600)

    command_timeout_apply_sec: int = 300
    command_timeout_status_sec: int = 5
    command_timeout_update_sec: int = 600
    transient_command_retry_attempts: int = Field(default=1, ge=0, le=10)
    transient_command_retry_backoff_sec: float = Field(default=1.0, ge=0.0, le=60.0)
    status_cache_ttl_sec: int = 60
    update_log_dir: Path = Path("/var/log/cnc/updates")
    update_output_max_chars: int = 16000
    notification_state_path: Path | None = None

    updater_script_path: Path = Path(
        "/var/lib/cnc/current/scripts/update_from_github.sh"
    )
    github_repo: str = ""
    github_ref: str = "main"
    github_readonly_pat: str = ""
    pushover_app_token: str = ""
    pushover_user_key: str = ""

    static_files_dir: Path = Path("app/static")
    templates_dir: Path = Path("app/templates")

    @property
    def sqlite_path(self) -> str:
        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            msg = f"database_url must start with {prefix!r}, got {self.database_url!r}"
            raise ValueError(msg)
        return self.database_url[len(prefix) :]

    @property
    def cloudflare_ip_list(self) -> tuple[str, ...]:
        values = self.nginx_cloudflare_ips.replace("\n", ",").split(",")
        return tuple(entry.strip() for entry in values if entry.strip())

    @property
    def trusted_host_list(self) -> tuple[str, ...]:
        hosts = {"127.0.0.1", "localhost", "::1"}
        candidates = [self.admin_host]
        candidates.extend(self.admin_allowed_hosts.replace("\n", ",").split(","))
        for raw_value in candidates:
            normalized = _normalize_trusted_host(raw_value)
            if normalized:
                hosts.add(normalized)
        return tuple(sorted(hosts))

    @model_validator(mode="after")
    def validate_admin_bind(self) -> "Settings":
        if self.admin_unsafe_allow_remote:
            return self

        host = self.admin_host.strip().lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return self
        try:
            if ipaddress.ip_address(host).is_loopback:
                return self
        except ValueError:
            pass
        msg = (
            f"admin_host={self.admin_host!r} is not loopback. "
            "Set ADMIN_UNSAFE_ALLOW_REMOTE=true to override."
        )
        raise ValueError(msg)

    @model_validator(mode="after")
    def validate_cloudflare_acl(self) -> "Settings":
        if self.nginx_cloudflare_only and not self.cloudflare_ip_list:
            raise ValueError(
                "nginx_cloudflare_only requires at least one CIDR in nginx_cloudflare_ips"
            )
        return self

    @model_validator(mode="after")
    def validate_admin_allowed_hosts(self) -> "Settings":
        for raw_value in self.admin_allowed_hosts.replace("\n", ",").split(","):
            _normalize_trusted_host(raw_value)
        return self

    @model_validator(mode="after")
    def normalize_host_state_paths(self) -> "Settings":
        legacy_paths = [
            path
            for path in (
                self.cloudflare_sync_state_path,
                self.backend_alerts_state_path,
                self.notification_state_path,
            )
            if path is not None
        ]
        resolved_host_state_path = self.host_state_path
        if legacy_paths:
            first_legacy_path = legacy_paths[0]
            if any(path != first_legacy_path for path in legacy_paths[1:]):
                raise ValueError(
                    "legacy host-state path overrides must all point to the same file"
                )
            if (
                resolved_host_state_path != DEFAULT_HOST_STATE_PATH
                and resolved_host_state_path != first_legacy_path
            ):
                raise ValueError(
                    "host_state_path and legacy host-state path overrides must match"
                )
            resolved_host_state_path = first_legacy_path
        self.host_state_path = resolved_host_state_path
        self.cloudflare_sync_state_path = resolved_host_state_path
        self.backend_alerts_state_path = resolved_host_state_path
        self.notification_state_path = resolved_host_state_path
        return self


def _normalize_trusted_host(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower().rstrip(".")
    if not normalized:
        return None
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return normalized
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        pass
    if not _TRUSTED_HOST_RE.fullmatch(normalized):
        raise ValueError(f"invalid trusted host entry: {value!r}")
    return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
