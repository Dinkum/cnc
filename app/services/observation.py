from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar, Union


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Present(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class Absent:
    pass


ABSENT = Absent()


@dataclass(frozen=True, slots=True)
class Unknown:
    reason: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


Observation = Union[Present[T], Absent, Unknown]


class ObservationUnavailable(RuntimeError):
    def __init__(self, subject: str, unknown: Unknown) -> None:
        super().__init__(f"cannot observe {subject} ({unknown.describe()})")
        self.subject = subject
        self.unknown = unknown


def known(observation: Observation[T], subject: str) -> T | None:
    """Return the observed value, or None only when the subject is confirmed absent."""
    if isinstance(observation, Present):
        return observation.value
    if isinstance(observation, Absent):
        return None
    raise ObservationUnavailable(subject, observation)


def present_value(observation: Observation[T]) -> T | None:
    """Return the value only when present; for display paths that must not act on absence."""
    return observation.value if isinstance(observation, Present) else None
