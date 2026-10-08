from __future__ import annotations

from core.job_runner import (
    InMemoryJobRepository,
    JobCancelService,
    JobRetryService,
    JobServiceError,
    JobStatusService,
    is_retryable_job_status,
    is_retryable_step_status,
)


class _CommandRecordingRepository(InMemoryJobRepository):
    """Records lifecycle writes so legacy Media command guards stay central."""

    def __init__(self) -> None:
        super().__init__()
        self.save_calls = 0
        self.request_cancel_calls = 0

    def save(self, job) -> None:
        self.save_calls += 1
        super().save(job)

    def request_cancel(self, job_id: str, *, request_id: str, now: str):
        self.request_cancel_calls += 1
        return {"id": job_id, "request_id": request_id, "updated_at": now}


# ── 测试用 job 工厂 ──

def _job(
    job_id: str = "job-1",
    *,
    job_type: str = "workbench_auto_intake",
    status: str = "failed",
    source_id: str = "source-1",
    attempt: int = 1,
    steps=None,
    checkpoint=None,
    error=None,
) -> dict[str, object]:
    if steps is None:
        steps = [
            {"name": "step_a", "status": "completed", "error": None},
            {"name": "step_b", "status": "failed", "error": {"code": "provider_missing", "message": "x"}},
        ]
    return {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": source_id,
        "job_type": job_type,
        "idempotency_key": f"intake-{job_id}",
        "status": status,
        "attempt": attempt,
        "max_attempts": 3,
        "lease": None,
        "progress": {"current": 1, "total": 2, "percent": 50, "message": None},
        "steps": steps,
        "error": error,
        "checkpoint": checkpoint,
        "staged_outputs": [{"kind": "source", "uri": "crp://ns/sources/source-1", "object_id": "source-1", "published": True}],
        "published_outputs": [],
        "log_refs": ["crp://ns/logs/jobs/job-1/orchestrate.jsonl"],
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }


# ── status 常量 ──

def test_is_retryable_job_status() -> None:
    assert is_retryable_job_status("failed") is True
    assert is_retryable_job_status("waiting_user") is True
    assert is_retryable_job_status("cancelled") is True
    assert is_retryable_job_status("pending") is False
    assert is_retryable_job_status("running") is False
    assert is_retryable_job_status("completed") is False


def test_is_retryable_step_status() -> None:
    assert is_retryable_step_status("failed") is True
    assert is_retryable_step_status("waiting_user") is True
    assert is_retryable_step_status("cancelled") is True
    assert is_retryable_step_status("pending") is False
    assert is_retryable_step_status("running") is False
    assert is_retryable_step_status("completed") is False
    assert is_retryable_step_status("skipped") is False


# ── JobStatusService.list_jobs 过滤 ──

def _seed_jobs(repo: InMemoryJobRepository) -> None:
    repo.save(_job("job-1", job_type="workbench_auto_intake", status="failed", source_id="source-a"))
    repo.save(_job("job-2", job_type="workbench_auto_intake", status="completed", source_id="source-b"))
    repo.save(_job("job-3", job_type="capture", status="failed", source_id="source-a"))
    repo.save(_job("job-4", job_type="capture", status="running", source_id="source-c"))


def test_list_jobs_no_filter_returns_all() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs()
    assert len(jobs) == 4


def test_list_jobs_filter_by_job_type() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(job_type="capture")
    assert len(jobs) == 2
    assert all(job["job_type"] == "capture" for job in jobs)


def test_list_jobs_filter_by_status() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(status="failed")
    assert len(jobs) == 2
    assert all(job["status"] == "failed" for job in jobs)


def test_list_jobs_filter_by_source_id() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(source_id="source-a")
    assert len(jobs) == 2
    assert all(job["source_id"] == "source-a" for job in jobs)


def test_list_jobs_combined_filter() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(job_type="workbench_auto_intake", status="failed", source_id="source-a")
    assert len(jobs) == 1
    assert jobs[0]["id"] == "job-1"


def test_list_jobs_limit() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(limit=2)
    assert len(jobs) == 2


def test_list_jobs_limit_zero_returns_empty() -> None:
    repo = InMemoryJobRepository()
    _seed_jobs(repo)
    service = JobStatusService(repo)
    jobs = service.list_jobs(limit=0)
    assert len(jobs) == 0


# ── JobStatusService.get_job_detail ──

def test_get_job_detail_returns_record() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1"))
    service = JobStatusService(repo)
    job = service.get_job_detail("job-1")
    assert job is not None
    assert job["id"] == "job-1"


def test_get_job_detail_returns_none_when_missing() -> None:
    repo = InMemoryJobRepository()
    service = JobStatusService(repo)
    assert service.get_job_detail("missing") is None


# ── JobRetryService.mark_job_for_retry ──

def test_retry_resets_failed_steps_to_pending() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1"))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    updated = service.mark_job_for_retry("job-1")
    assert updated["status"] == "pending"
    assert updated["attempt"] == 2
    assert updated["error"] is None
    assert updated["checkpoint"] is None
    steps = updated["steps"]
    assert steps[0]["status"] == "completed"  # 保留已完成 step
    assert steps[1]["status"] == "pending"  # 重置 failed step
    assert steps[1]["error"] is None


def test_legacy_media_job_retry_fails_closed_without_repository_write() -> None:
    repo = _CommandRecordingRepository()
    InMemoryJobRepository.save(repo, _job("media-legacy", job_type="media_hands"))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")

    try:
        service.mark_job_for_retry("media-legacy")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "read-only" in str(exc)
        assert "retry" in str(exc)
    assert repo.save_calls == 0


def test_retry_preserves_staged_outputs() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1"))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    updated = service.mark_job_for_retry("job-1")
    assert len(updated["staged_outputs"]) == 1
    assert updated["staged_outputs"][0]["object_id"] == "source-1"


def test_retry_increments_attempt() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1", attempt=3))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    updated = service.mark_job_for_retry("job-1")
    assert updated["attempt"] == 4


def test_retry_rejects_not_found() -> None:
    repo = InMemoryJobRepository()
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.mark_job_for_retry("missing")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "not found" in str(exc)


def test_retry_rejects_non_retryable_status() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1", status="completed"))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.mark_job_for_retry("job-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "not retryable" in str(exc)


def test_retry_maps_concurrent_repository_conflict_to_stable_service_error() -> None:
    class _ConflictingRepository(InMemoryJobRepository):
        def save(self, job) -> None:
            if self.get(str(job["id"])) is not None:
                raise ValueError("stale job revision")
            super().save(job)

    repo = _ConflictingRepository()
    InMemoryJobRepository.save(repo, _job("job-1"))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")

    try:
        service.mark_job_for_retry("job-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "changed while retry was requested" in str(exc)


def test_retry_resets_waiting_user_and_cancelled_steps() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job(
        "job-1",
        status="waiting_user",
        steps=[
            {"name": "a", "status": "completed", "error": None},
            {"name": "b", "status": "waiting_user", "error": None},
            {"name": "c", "status": "cancelled", "error": None},
            {"name": "d", "status": "skipped", "error": None},
        ],
    ))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    updated = service.mark_job_for_retry("job-1")
    statuses = [s["status"] for s in updated["steps"]]
    assert statuses == ["completed", "pending", "pending", "skipped"]


# ── JobRetryService.mark_job_for_resume ──

def _checkpoint() -> dict[str, object]:
    return {
        "resume_step": "step_b",
        "checkpoint_uri": "crp://ns/jobs/job-1/checkpoint",
        "state_hash": "sha256:" + "a" * 64,
        "updated_at": "2026-07-04T09:30:00+08:00",
    }


def test_resume_marks_pending_with_checkpoint() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1", status="failed", checkpoint=_checkpoint()))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    updated = service.mark_job_for_resume("job-1")
    assert updated["status"] == "pending"
    assert updated["attempt"] == 2
    assert updated["error"] is None
    # resume 不重置 steps——worker 从 checkpoint.resume_step 继续
    assert updated["steps"][1]["status"] == "failed"
    # checkpoint 保留——resume 后 worker 仍需读取
    assert updated["checkpoint"] is not None


def test_legacy_media_job_resume_fails_closed_without_repository_write() -> None:
    repo = _CommandRecordingRepository()
    InMemoryJobRepository.save(
        repo,
        _job(
            "media-legacy",
            job_type="media_hands",
            checkpoint=_checkpoint(),
        ),
    )
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")

    try:
        service.mark_job_for_resume("media-legacy")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "read-only" in str(exc)
        assert "resume" in str(exc)
    assert repo.save_calls == 0


def test_legacy_media_job_cancel_fails_closed_without_repository_write() -> None:
    repo = _CommandRecordingRepository()
    InMemoryJobRepository.save(
        repo,
        _job("media-legacy", job_type="media_hands", status="running"),
    )
    service = JobCancelService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.request_cancel("media-legacy", request_id="cancel-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "read-only" in str(exc)
        assert "cancel" in str(exc)
    assert repo.save_calls == 0
    assert repo.request_cancel_calls == 0


def test_resume_maps_concurrent_repository_conflict_to_stable_service_error() -> None:
    class _ConflictingRepository(InMemoryJobRepository):
        def save(self, job) -> None:
            if self.get(str(job["id"])) is not None:
                raise ValueError("stale job revision")
            super().save(job)

    repo = _ConflictingRepository()
    InMemoryJobRepository.save(repo, _job("job-1", checkpoint=_checkpoint()))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")

    try:
        service.mark_job_for_resume("job-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "changed while resume was requested" in str(exc)


def test_resume_rejects_when_no_checkpoint() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1", status="failed", checkpoint=None))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.mark_job_for_resume("job-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "no checkpoint" in str(exc)


def test_resume_rejects_not_found() -> None:
    repo = InMemoryJobRepository()
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.mark_job_for_resume("missing")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "not found" in str(exc)


def test_resume_rejects_non_retryable_status() -> None:
    repo = InMemoryJobRepository()
    repo.save(_job("job-1", status="completed", checkpoint=_checkpoint()))
    service = JobRetryService(repo, now="2026-07-04T10:00:00+08:00")
    try:
        service.mark_job_for_resume("job-1")
        assert False, "expected JobServiceError"
    except JobServiceError as exc:
        assert "not resumable" in str(exc)
