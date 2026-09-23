"""One user action, described once.

Every config change the user can make is the same operation: change some rows,
converge the host, tell them how it went. What differs between "delete output"
and "attach input" is which rows change and what to call it.

Before this, calling the shared save path meant hand-writing eight strings, six
of which were the same fact restated. That is why five of the nine runners did
not bother and grew their own copy of the flow instead. Describe the action
once and the rest follows.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.error_reporting import ErrorCode


@dataclass(frozen=True)
class Action:
    """What the user asked for, and what to call it when reporting back.

    ``kind`` is the operation identity, e.g. ``ui.backend.update``. ``success``
    is what the user reads when it works. ``label`` names the action in failure
    text. Everything else is derived, so there is one place a string can be
    wrong instead of eight.
    """

    kind: str
    success: str
    label: str
    error_code: ErrorCode
    detail: str
    #: What the user calls the thing. Drives every phase and substate string,
    #: which the progress card matches against its step headlines, so these
    #: must keep deriving byte-identical text.
    noun: str = "output"
    #: What the database calls it. Drives the detail key, ``backend_id`` for an
    #: output. The two differ because the schema says Backend and the UI says
    #: Output, and pretending otherwise would silently rename operation
    #: payload fields the front end reads.
    subject: str = "backend"
    #: The progress pipeline this action narrates through, when it has one.
    #: Inputs run planned pipelines with named steps; outputs do not.
    pipeline: str = ""
    #: Only when the failure wording is not the success wording. "Output saved."
    #: fails as "Output saved: ...", but "Interface confirmed." fails as
    #: "Interface saved: ...", so that one says so explicitly.
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
