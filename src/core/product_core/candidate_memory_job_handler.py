from __future__ import annotations

from collections.abc import Mapping

from core.job_runner import JobStepResult

from .source_output_memory_candidate import CreateMemoryCandidateFromSourceOutput


class CandidateMemoryJobHandler:
    job_type = "extract_memory_candidate"

    def __init__(self, create_candidate: CreateMemoryCandidateFromSourceOutput, *, namespace_id: str = "default") -> None:
        self._create_candidate = create_candidate
        self._namespace_id = namespace_id

    def run_step(self, step_name: str, job: Mapping[str, object]) -> JobStepResult:
        del step_name, job
        raise RuntimeError(
            "candidate Job worker execution is retired; use the Core effect-v2 handler"
        )
