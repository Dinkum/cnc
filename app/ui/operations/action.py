
from __future__ import annotations

from dataclasses import dataclass

from app.services.error_reporting import ErrorCode


@dataclass(frozen=True)
class Action:
    kind: str
    success: str
    label: str
    error_code: ErrorCode
    detail: str
    noun: str = "output"
    subject: str = "backend"
    pipeline: str = ""
    failure_prefix_override: str = ""

    @property
    def log_event(self) -> str:
        return f"{self.kind}.worker_failed"

    @property
    def failure_prefix(self) -> str:
        return self.failure_prefix_override or self.success.rstrip(".")

    @property
    def failure_message(self) -> str:
        return f"{self.label} failed."

    @property
    def id_key(self) -> str:
        return f"{self.subject}_id"

    @property
    def name_key(self) -> str:
        return f"{self.subject}_name"

    @property
    def validate_phase(self) -> str:
        return f"Validate {self.noun}"

    @property
    def validating(self) -> str:
        return f"Validating {self.noun}"

    @property
    def save_phase(self) -> str:
        return f"Save {self.noun}"

    @property
    def refreshing(self) -> str:
        return f"Refreshing {self.noun}"
