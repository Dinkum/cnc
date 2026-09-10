from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.updates import UpdateValue


INPUT_KIND_PATTERN = "^(domain|shield|tailnet_path|tailnet_service)$"


class InputIn(BaseModel):
    kind: str = Field(pattern=INPUT_KIND_PATTERN, default="domain")
    value: str = Field(min_length=1, max_length=255)
    backend_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    shield_enabled: bool = False
    shield_code_hash: Optional[str] = None
    shield_access_code: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return value.strip().lower()


class InputUpdate(BaseModel):
    kind: UpdateValue[Annotated[str, Field(pattern=INPUT_KIND_PATTERN)]] = None
    value: UpdateValue[Annotated[str, Field(min_length=1, max_length=255)]] = None
    backend_ids: Optional[list[int]] = None
    enabled: UpdateValue[bool] = None
    shield_enabled: UpdateValue[bool] = None
    shield_code_hash: Optional[str] = None
    shield_access_code: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().lower()

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().lower()


class InputStateIn(BaseModel):
    enabled: bool


class InputOut(BaseModel):
    id: int
    kind: str
    value: str
    backend_ids: list[int]
    enabled: bool
    shield_enabled: bool
    shield_code_hash: Optional[str]

    model_config = {"from_attributes": True}
