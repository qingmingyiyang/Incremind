"""Job status / retry / resume 服务层。

把 JobRepositoryPort 的 get/save 包装为面向 API endpoint 的服务：
- get_job_detail：返回完整 job record 或 None
- list_jobs：按 job_type / status / source_id / limit 过滤
- mark_job_for_retry：重置失败 step，保留已完成 outputs，标记为 pending
- mark_job_for_resume：从 checkpoint 恢复，标记为 pending

设计决策：
- retry/resume 只标记 job 为 pending，不实际执行 step——实际执行需要 worker（Phase 6 后续）。
  这让 endpoint 是幂等的、可测试的、不依赖 workflow handler。
- retry 重置 failed/waiting_user/cancelled step 为 pending，保留 completed/skipped step，
  符合 spec §3.5 验收"retry 只重跑失败节点，不重复读取/下载已完成部分"。
- resume 要求 job.checkpoint 非空；无 checkpoint 时抛 JobServiceError。
- attempt 字段在 retry/resume 时 +1，让前端能看到重试次数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .status import (
    JOB_STATUS_PENDING,
    STEP_STATUS_PENDING,
    is_retryable_job_status,
    is_retryable_step_status,
)


class JobServiceError(Exception):
    """Raised when a job service operation cannot be performed."""


_LEGACY_MEDIA_JOB_TYPE = "media_hands"


def _reject_legacy_media_job_command(
    job: Mapping[str, object], *, command: str,
) -> None:
    """Keep legacy Media Jobs read-only until a Core Effect command exists.

    Retry, resume, and cancellation used to mutate the Job lifecycle directly.
    That lifecycle is no longer an execution authority for Media, so these
    commands must fail closed rather than enqueue or rewrite an old Job.
    """
    if job.get("job_type") != _LEGACY_MEDIA_JOB_TYPE:
        return
    job_id = job.get("id")
    raise JobServiceError(
        f"legacy Media Job {job_id!r} is read-only; {command} requires a Core Effect command"
    )


@dataclass(frozen=True, slots=True)
class JobStatusService:
    """Read-only job status lookup service."""

    repository: object  # JobRepositoryPort, 但避免运行时 Protocol 检查

    def get_job_detail(self, job_id: str) -> Mapping[str, object] | None:
        return self.repository.get(job_id)

    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        list_jobs = getattr(self.repository, "list_jobs", None)
        if list_jobs is None:
            # Fallback for repositories without list_jobs（向后兼容）
            jobs = self.repository.all()
            filtered = [
                job for job in jobs
                if _matches_filter(job, job_type=job_type, status=status, source_id=source_id)
            ]
            if limit is not None and limit >= 0:
                filtered = filtered[:limit]
            return tuple(filtered)
        return list_jobs(
            job_type=job_type,
            status=status,
            source_id=source_id,
            limit=limit,
        )


@dataclass(frozen=True, slots=True)
class JobRetryService:
    """Retry/resume service that marks jobs for re-execution.

    This service does NOT execute workflow steps. It only updates the job
    record so a future worker (or next intake trigger) can pick it up.
    """

    repository: object  # JobRepositoryPort
    now: str = "2026-07-04T09:00:00+08:00"

    def mark_job_for_retry(self, job_id: str) -> Mapping[str, object]:
        """Reset failed steps and mark job as pending for retry.

        - Resets steps with status in (failed, waiting_user, cancelled) to pending.
        - Preserves completed/skipped steps and staged_outputs.
        - Increments attempt, clears error, sets status to pending.
        - Returns the updated job record.

        Raises JobServiceError if job not found or not retryable.
        """
        job = self.repository.get(job_id)
        if job is None:
            raise JobServiceError(f"job not found: {job_id}")
        _reject_legacy_media_job_command(job, command="retry")
        status = job.get("status")
        if not isinstance(status, str) or not is_retryable_job_status(status):
            raise JobServiceError(
                f"job {job_id} status {status!r} is not retryable; "
                f"expected one of failed|waiting_user|cancelled"
            )
        if _has_non_retryable_error(job):
            raise JobServiceError(f"job {job_id} is blocked by a non-retryable error")
        updated_steps = []
        for step in job.get("steps", []) or []:
            step_status = step.get("status") if isinstance(step, Mapping) else None
            if isinstance(step_status, str) and is_retryable_step_status(step_status):
                updated_step = dict(step)
                updated_step["status"] = STEP_STATUS_PENDING
                updated_step["error"] = None
                updated_steps.append(updated_step)
            else:
                updated_steps.append(step)
        attempt = job.get("attempt", 0)
        if not isinstance(attempt, int):
            attempt = 0
        updated_job = dict(job)
        updated_job["status"] = JOB_STATUS_PENDING
        updated_job["attempt"] = attempt + 1
        updated_job["error"] = None
        updated_job["steps"] = updated_steps
        updated_job["updated_at"] = self.now
        # checkpoint 在 retry 后失效——resume 需要新的 checkpoint
        updated_job["checkpoint"] = None
        updated_job.pop("cancel_request", None)
        try:
            self.repository.save(updated_job)
        except (ValueError, KeyError) as exc:
            raise JobServiceError(f"job {job_id} changed while retry was requested: {exc}") from exc
        return updated_job


    def mark_job_for_resume(self, job_id: str) -> Mapping[str, object]:
        """Mark job for resume from checkpoint.

        - Requires job.checkpoint to be non-null.
        - Increments attempt, sets status to pending.
        - Does NOT reset steps——resume 从 checkpoint.resume_step 继续，
          已完成 step 保留，未完成 step 由 worker 重新执行。

        Raises JobServiceError if job not found, not resumable, or has no checkpoint.
        """
        job = self.repository.get(job_id)
        if job is None:
            raise JobServiceError(f"job not found: {job_id}")
        _reject_legacy_media_job_command(job, command="resume")
        status = job.get("status")
        if not isinstance(status, str) or not is_retryable_job_status(status):
            raise JobServiceError(
                f"job {job_id} status {status!r} is not resumable; "
                f"expected one of failed|waiting_user|cancelled"
            )
        if _has_non_retryable_error(job):
            raise JobServiceError(f"job {job_id} is blocked by a non-retryable error")
        checkpoint = job.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise JobServiceError(
                f"job {job_id} has no checkpoint; cannot resume"
            )
        attempt = job.get("attempt", 0)
        if not isinstance(attempt, int):
            attempt = 0
        updated_job = dict(job)
        updated_job["status"] = JOB_STATUS_PENDING
        updated_job["attempt"] = attempt + 1
        updated_job["error"] = None
        updated_job["updated_at"] = self.now
        updated_job.pop("cancel_request", None)
        try:
            self.repository.save(updated_job)
        except (ValueError, KeyError) as exc:
            raise JobServiceError(f"job {job_id} changed while resume was requested: {exc}") from exc
        return updated_job


@dataclass(frozen=True, slots=True)
class JobCancelService:
    repository: object
    now: str

    def request_cancel(self, job_id: str, *, request_id: str) -> Mapping[str, object]:
        clean_job_id = (job_id or "").strip()
        clean_request_id = (request_id or "").strip()
        if not clean_job_id or not clean_request_id:
            raise JobServiceError("job_id and request_id are required")
        job = self.repository.get(clean_job_id)
        if job is not None:
            _reject_legacy_media_job_command(job, command="cancel")
        request_cancel = getattr(self.repository, "request_cancel", None)
        if request_cancel is None:
            raise JobServiceError("job repository does not support durable cancellation")
        try:
            return request_cancel(clean_job_id, request_id=clean_request_id, now=self.now)
        except (ValueError, KeyError) as exc:
            raise JobServiceError(str(exc)) from exc


def _has_non_retryable_error(job: Mapping[str, object]) -> bool:
    error = job.get("error")
    return isinstance(error, Mapping) and error.get("retryable") is False


def _matches_filter(
    job: Mapping[str, object],
    *,
    job_type: str | None,
    status: str | None,
    source_id: str | None,
) -> bool:
    if job_type is not None and job.get("job_type") != job_type:
        return False
    if status is not None and job.get("status") != status:
        return False
    if source_id is not None and job.get("source_id") != source_id:
        return False
    return True
