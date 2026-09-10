from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.updates import UpdateValue

BACKEND_KIND_PATTERN = "^(static|app|shield)$"


class BackendIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(pattern=BACKEND_KIND_PATTERN)
    port: Optional[int] = None
    static_root: Optional[str] = None
    sandbox_profile: Optional[str] = None
    handoff_port: int = Field(default=8000, ge=1, le=65535)
    healthcheck_mode: Optional[str] = None
    healthcheck_path: Optional[str] = None
    healthcheck_host_header: Optional[str] = None
    resource_mode: str = Field(default="auto", pattern="^(auto|manual)$")
    resource_size: str = Field(default="small", pattern="^(small|medium|large)$")
    memory_high_override: Optional[str] = None
    memory_max_override: Optional[str] = None
    cpu_quota_override: Optional[str] = None
    shield_enabled: bool = False
    shield_code_hash: Optional[str] = None
    shield_access_code: Optional[str] = None
    volumes_json: str = "[]"
    enabled: bool = True
    notes: Optional[str] = None
    input_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip().lower()


class BackendUpdate(BaseModel):
    name: UpdateValue[Annotated[str, Field(min_length=1, max_length=255)]] = None
    kind: UpdateValue[Annotated[str, Field(pattern=BACKEND_KIND_PATTERN)]] = None
    port: Optional[int] = None
    static_root: Optional[str] = None
    sandbox_profile: Optional[str] = None
    handoff_port: UpdateValue[Annotated[int, Field(ge=1, le=65535)]] = None
    healthcheck_mode: Optional[str] = None
    healthcheck_path: Optional[str] = None
    healthcheck_host_header: Optional[str] = None
    resource_mode: UpdateValue[Annotated[str, Field(pattern="^(auto|manual)$")]] = None
    resource_size: UpdateValue[
        Annotated[str, Field(pattern="^(small|medium|large)$")]
    ] = None
    memory_high_override: Optional[str] = None
    memory_max_override: Optional[str] = None
    cpu_quota_override: Optional[str] = None
    shield_enabled: UpdateValue[bool] = None
    shield_code_hash: Optional[str] = None
    shield_access_code: Optional[str] = None
    volumes_json: UpdateValue[str] = None
    enabled: UpdateValue[bool] = None
    notes: Optional[str] = None
    input_ids: Optional[list[int]] = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().lower()


class BackendCloneIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().lower()


class BackendStateIn(BaseModel):
    enabled: bool


class BackendOut(BaseModel):
    id: int
    name: str
    kind: str
    port: Optional[int]
    static_root: Optional[str]
    sandbox_profile: Optional[str]
    handoff_port: int
    healthcheck_mode: Optional[str]
    healthcheck_path: Optional[str]
    healthcheck_host_header: Optional[str]
    resource_mode: str
    resource_size: str
    memory_high_override: Optional[str]
    memory_max_override: Optional[str]
    cpu_quota_override: Optional[str]
    shield_enabled: bool
    shield_code_hash: Optional[str]
    placement_node_uid: Optional[str] = None
    placement_mode: str = "single"
    placement_active_node_uid: Optional[str] = None
    placement_node_uids_json: str = "[]"
    inter_app_interfaces_json: str = "[]"
    hardening_config_json: str = "{}"
    volumes_json: str
    enabled: bool
    notes: Optional[str]
    input_ids: list[int]

    model_config = {"from_attributes": True}
