from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


JobOutput = Mapping[str, object] | str


class JobStepBlockedError(ValueError):
    """A durable, non-retryable policy block raised by a Job handler."""

    def __init__(self, *, code: str, message: str) -> None:
        if not code:
            raise ValueError("blocked Job error code is required")
        if not message:
            raise ValueError("blocked Job error message is required")
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class JobStepResult:
    staged_outputs: tuple[JobOutput, ...] = ()
    published_outputs: tuple[Mapping[str, object], ...] = ()
    consume_staged_outputs: bool = False
    checkpoint: Mapping[str, object] | None = None
    staged_output_refs: tuple[str, ...] = ()
    log_refs: tuple[str, ...] = ()
    resource_consumed: Mapping[str, int] | None = None


class JobRepositoryPort(Protocol):
    """Persists state before and after every processing step."""

    def get(self, job_id: str) -> Mapping[str, object] | None:
        """Read a Job by identifier."""

    def save(self, job: Mapping[str, object]) -> None:
        """Persist the current Job state."""


class JobHandlerPort(Protocol):
    """Runs one named step and returns references to staged outputs."""

    @property
    def job_type(self) -> str:
        """Return the supported job type."""

    def run_step(self, step_name: str, job: Mapping[str, object]) -> JobStepResult:
        """Run one step and classify its durable staged/published outputs."""
