"""统一 Job 状态与 blocked reason 常量。

这些常量是 Phase 6 §3.5 任务队列规范化的一部分。
现有 workflow（video_auto_workflow / audio_auto_workflow / workbench_auto_intake）
仍可继续写入各自的 step status 字符串；本模块为新代码提供标准引用点，
让 API endpoint / 前端展示 / 后续 worker 能按统一语义分支处理。

设计决策：
- 用字符串常量而非 Enum，避免与 job.schema.json 的字符串枚举不一致。
- Job status 对齐 schema 的 6 个状态（pending/running/waiting_user/completed/failed/cancelled）。
- Step status 对齐 schema 的 7 个状态（pending/running/waiting_user/completed/failed/cancelled/skipped）。
  现有 workflow 中出现的 "blocked" / "queued" 是历史遗留，新代码应使用 waiting_user / pending。
- BlockedReason 是 jobError.code 字段的标准取值集合，让前端能按类型分支处理。
"""

from __future__ import annotations

# ── Job 顶层状态 ──
# 对齐 job.schema.json status 枚举
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_WAITING_USER = "waiting_user"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

JOB_STATUSES: frozenset[str] = frozenset({
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_USER,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
})

# ── Job step 状态 ──
# 对齐 job.schema.json jobStep.status 枚举
STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_WAITING_USER = "waiting_user"
STEP_STATUS_COMPLETED = "completed"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_CANCELLED = "cancelled"
STEP_STATUS_SKIPPED = "skipped"

STEP_STATUSES: frozenset[str] = frozenset({
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_WAITING_USER,
    STEP_STATUS_COMPLETED,
    STEP_STATUS_FAILED,
    STEP_STATUS_CANCELLED,
    STEP_STATUS_SKIPPED,
})

# 可重试的 step 终态：retry 时这些 step 会被重置为 pending
STEP_RETRYABLE_STATUSES: frozenset[str] = frozenset({
    STEP_STATUS_FAILED,
    STEP_STATUS_WAITING_USER,
    STEP_STATUS_CANCELLED,
})

# 已完成的 step 终态：retry 时保留，不重置
STEP_TERMINAL_OK_STATUSES: frozenset[str] = frozenset({
    STEP_STATUS_COMPLETED,
    STEP_STATUS_SKIPPED,
})

# ── Blocked reason 标准取值 ──
# 这些值写入 jobError.code 字段，让前端能按类型分支处理。
# 现有 workflow 的自由文本 reason 不强制迁移；新 workflow 应使用这些标准值。
BLOCKED_REASON_PROVIDER_MISSING = "provider_missing"
BLOCKED_REASON_AUTHORIZATION_REQUIRED = "authorization_required"
BLOCKED_REASON_PATH_UNAVAILABLE = "path_unavailable"
BLOCKED_REASON_SCHEMA_REJECTED = "schema_rejected"
BLOCKED_REASON_NETWORK_RETRYABLE = "network_retryable"
BLOCKED_REASON_USER_CONFIRMATION_REQUIRED = "user_confirmation_required"

BLOCKED_REASONS: frozenset[str] = frozenset({
    BLOCKED_REASON_PROVIDER_MISSING,
    BLOCKED_REASON_AUTHORIZATION_REQUIRED,
    BLOCKED_REASON_PATH_UNAVAILABLE,
    BLOCKED_REASON_SCHEMA_REJECTED,
    BLOCKED_REASON_NETWORK_RETRYABLE,
    BLOCKED_REASON_USER_CONFIRMATION_REQUIRED,
})

# ── 可重试的 job 状态 ──
# retry endpoint 只接受这些状态的 job
JOB_RETRYABLE_STATUSES: frozenset[str] = frozenset({
    JOB_STATUS_FAILED,
    JOB_STATUS_WAITING_USER,
    JOB_STATUS_CANCELLED,
})


def is_retryable_step_status(status: str) -> bool:
    """Return True if a step with this status should be reset on retry."""
    return status in STEP_RETRYABLE_STATUSES


def is_retryable_job_status(status: str) -> bool:
    """Return True if a job with this status can be retried."""
    return status in JOB_RETRYABLE_STATUSES
