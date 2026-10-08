from __future__ import annotations

from collections.abc import Mapping

from core.job_runner import JobStepResult

from .source_job_memory_loop import SourceJobMemoryLoop


class SourceJobMemoryPublishHandler:
    job_type = "extract_memory"

    def __init__(self, loop: SourceJobMemoryLoop) -> None:
        self._loop = loop

    def run_step(self, step_name: str, job: Mapping[str, object]) -> JobStepResult:
        if step_name != "publish_atom":
            raise ValueError(f"extract_memory handler does not support step: {step_name}")
        outputs = self._loop.publish_staged_job(job)
        return JobStepResult(published_outputs=tuple(outputs), consume_staged_outputs=True)
