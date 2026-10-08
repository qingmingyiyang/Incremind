from __future__ import annotations

from collections.abc import Mapping

from core.job_runner import JobStepBlockedError, JobStepResult


class LegacyExtractMemoryQuarantineHandler:
    """Block deprecated direct memory publication without mutating its payload."""

    job_type = "extract_memory"

    def run_step(self, step_name: str, job: Mapping[str, object]) -> JobStepResult:
        del step_name, job
        raise JobStepBlockedError(
            code="legacy_direct_memory_publish_disabled",
            message=(
                "Legacy extract_memory direct publication is disabled; "
                "create a Memory Candidate and complete user review instead."
            ),
        )
