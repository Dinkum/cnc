from __future__ import annotations

from datetime import datetime
import json
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


input_backend_links = Table(
    "input_backend_links",
    Base.metadata,
    Column(
        "input_id",
        Integer,
        ForeignKey("inputs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "backend_id",
        Integer,
        ForeignKey("backends.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Backend(Base):
    __tablename__ = "backends"
    _LEGACY_SANDBOX_PROFILE = "ubuntu-24.04-systemd"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    static_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sandbox_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    handoff_port: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    healthcheck_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    healthcheck_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    healthcheck_host_header: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resource_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto"
    )
    resource_size: Mapped[str] = mapped_column(
        String(16), nullable=False, default="small"
    )
    resource_size_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    memory_high_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    memory_max_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cpu_quota_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shield_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    shield_code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shield_access_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    placement_node_uid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    placement_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default="single"
    )
    placement_active_node_uid: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    placement_node_uids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    inter_app_interfaces_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    ssh_public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ssh_private_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    volumes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    inputs: Mapped[list[Input]] = relationship(
        "Input",
        secondary=input_backend_links,
        back_populates="backends",
    )

    def __init__(self, **kwargs: object) -> None:
        if "base_image" in kwargs and "sandbox_profile" not in kwargs:
            kwargs["sandbox_profile"] = self._LEGACY_SANDBOX_PROFILE
        kwargs.pop("base_image", None)
        if "sandbox_image" in kwargs and "sandbox_profile" not in kwargs:
            kwargs["sandbox_profile"] = self._LEGACY_SANDBOX_PROFILE
        kwargs.pop("sandbox_image", None)
        if "internal_port" in kwargs and "handoff_port" not in kwargs:
            kwargs["handoff_port"] = kwargs.pop("internal_port")
        for key in (
            "workdir",
            "install_command",
            "update_command",
            "start_command",
            "env_json",
            "no_new_privileges",
            "drop_capabilities",
        ):
            kwargs.pop(key, None)
        super().__init__(**kwargs)

    @property
    def input_ids(self) -> list[int]:
        return sorted(item.id for item in self.inputs)


class Input(Base):
    __tablename__ = "inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="domain")
    hostname: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    shield_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    shield_code_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shield_access_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    backends: Mapped[list[Backend]] = relationship(
        "Backend",
        secondary=input_backend_links,
        back_populates="inputs",
    )

    @property
    def backend_ids(self) -> list[int]:
        return sorted(backend.id for backend in self.backends)

    @property
    def value(self) -> str:
        return self.hostname

    @property
    def kind_label(self) -> str:
        if self.kind == "domain":
            return "domain"
        if self.kind == "shield":
            return "shield"
        if self.kind == "tailnet_service":
            return "tailscale subdomain"
        return "tailnet path"


class ApplyRun(Base):
    __tablename__ = "apply_runs"
    __table_args__ = (Index("ix_apply_runs_status_id", "status", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    config_revision: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    desired_state_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    desired_state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_nginx_files_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    runtime_graph_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    route_contracts_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    backend_contracts_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resource_profile_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    phase: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    backend_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("backends.id", ondelete="SET NULL"),
        nullable=True,
    )
    config_revision: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    desired_state_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class HostApplyState(Base):
    __tablename__ = "host_apply_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_applied_state_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    last_successful_operation_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_successful_apply_run_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("apply_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ClusterNode(Base):
    __tablename__ = "cluster_nodes"
    __table_args__ = (
        UniqueConstraint("node_uid", name="uq_cluster_nodes_node_uid"),
        Index("ix_cluster_nodes_state_last_seen", "state", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="follower", server_default="follower"
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="joining", server_default="joining"
    )
    wireguard_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    wireguard_public_key: Mapped[str] = mapped_column(Text, nullable=False)
    public_endpoint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tailnet_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ram_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disk_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ClusterJoinToken(Base):
    __tablename__ = "cluster_join_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_cluster_join_tokens_token_hash"),
        Index("ix_cluster_join_tokens_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    node_uid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClusterNodeLatencySample(Base):
    __tablename__ = "cluster_node_latency_samples"
    __table_args__ = (
        Index(
            "ix_cluster_node_latency_samples_node_recorded", "node_uid", "recorded_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BackendReplicaReadiness(Base):
    __tablename__ = "backend_replica_readiness"
    __table_args__ = (
        UniqueConstraint(
            "backend_id",
            "node_uid",
            name="uq_backend_replica_readiness_backend_node",
        ),
        Index("ix_backend_replica_readiness_backend_id", "backend_id"),
        Index("ix_backend_replica_readiness_node_uid", "node_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("backends.id", ondelete="CASCADE"), nullable=False
    )
    node_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    setup_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_contract_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    healthcheck_result: Mapped[str] = mapped_column(Text, nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class UpdateRun(Base):
    __tablename__ = "update_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UpdateCheck(Base):
    __tablename__ = "update_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_version: Mapped[str] = mapped_column(String(64), nullable=False)
    available_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    has_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BackendBackup(Base):
    __tablename__ = "backend_backups"
    __table_args__ = (
        Index("ix_backend_backups_backend_id_id", "backend_id", "id"),
        Index("ix_backend_backups_backend_id_status_id", "backend_id", "status", "id"),
        Index("ix_backend_backups_operation_id", "operation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("backends.id", ondelete="SET NULL"),
        nullable=True,
    )
    operation_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="metadata_only"
    )
    bundle_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bundle_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BackendResourceSample(Base):
    __tablename__ = "backend_resource_samples"
    __table_args__ = (
        UniqueConstraint(
            "backend_id", "bucket_start", name="uq_backend_resource_sample_bucket"
        ),
        Index("ix_backend_resource_samples_bucket_start", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("backends.id", ondelete="CASCADE"), nullable=False
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sampled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cpu_percent_of_host: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_percent_of_entitlement: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    cpu_entitlement_percent_of_host: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_limit_percent_of_host: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_current_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_max_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_peak_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_events_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_events_oom_kill: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    disk_usage_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disk_usage_complete: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    disk_usage_skipped_paths: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    network_rx_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_tx_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_rx_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_tx_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_total_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_percent_of_host_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cpu_percent_of_host_sum: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_host_min: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_host_max: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_host_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_host_last: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_entitlement_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cpu_percent_of_entitlement_sum: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_entitlement_min: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_entitlement_max: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_entitlement_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cpu_percent_of_entitlement_last: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_percent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memory_percent_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_current_bytes_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memory_current_bytes_sum: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_current_bytes_min: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_current_bytes_max: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_current_bytes_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    memory_current_bytes_last: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    disk_usage_bytes_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    disk_usage_bytes_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_usage_bytes_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_usage_bytes_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_usage_bytes_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    disk_usage_bytes_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_rx_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_tx_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_total_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    network_total_bps_last: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HostResourceSample(Base):
    __tablename__ = "host_resource_samples"
    __table_args__ = (
        UniqueConstraint("bucket_start", name="uq_host_resource_sample_bucket"),
        Index("ix_host_resource_samples_bucket_start", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sampled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cpu_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    memory_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disk_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_rx_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_tx_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_rx_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_tx_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    network_total_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_total_jiffies: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_idle_jiffies: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_percent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cpu_percent_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_percent_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_percent_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_percent_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_percent_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    memory_percent_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_percent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    disk_percent_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_percent_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_percent_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_percent_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_percent_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_rx_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_rx_bps_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_tx_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_first: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_tx_bps_last: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    network_total_bps_sum: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    network_total_bps_first: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    network_total_bps_last: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BackendHardeningRun(Base):
    __tablename__ = "backend_hardening_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backend_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("backends.id", ondelete="SET NULL"),
        nullable=True,
    )
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    evidence_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ratings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ControlEvent(Base):
    __tablename__ = "control_events"
    __table_args__ = (
        Index("ix_control_events_created_at", "created_at"),
        Index("ix_control_events_kind_created_at", "kind", "created_at"),
        Index("ix_control_events_backend_name_id", "backend_name", "id"),
        Index("ix_control_events_scope_affects_all_id", "scope", "affects_all", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="host")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    backend_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    affects_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    related_backends_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    subevents_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def related_backends(self) -> list[str]:
        try:
            payload = json.loads(self.related_backends_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    @property
    def subevents(self) -> list[dict[str, str]]:
        try:
            payload = json.loads(self.subevents_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        rows: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if not label or not value:
                continue
            rows.append({"label": label, "value": value})
        return rows
