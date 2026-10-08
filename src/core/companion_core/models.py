from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CompanionSchemaStatus:
    schema_version: int
    applied_migrations: tuple[int, ...]
    journal_mode: str
    foreign_keys_enabled: bool
    synchronous_mode: str
    busy_timeout_ms: int


@dataclass(frozen=True, slots=True)
class CompanionSetting:
    setting_id: str
    payload: dict[str, object]
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionReminder:
    reminder_id: str
    schedule: dict[str, object]
    advance_minutes: int
    next_fire_at: str | None
    ack_state: str
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionReminderOccurrence:
    occurrence_id: str
    reminder_id: str
    scheduled_for: str
    fire_at: str
    phase: str
    state: str
    snooze_until: str | None
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionSession:
    session_id: str
    context_epoch: int
    project_id: str
    prompt_revision: int
    profile_revision: int
    started_at: str
    closed_at: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class CompanionMasterProfile:
    profile_id: str
    nickname: str
    birthday: str | None
    oc_address: str
    relationship: str
    custom_notes: str
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionProfileMutation:
    profile: CompanionMasterProfile
    sessions_rebased: int


@dataclass(frozen=True, slots=True)
class CompanionPromptBinding:
    active_prompt_id: str
    activation_revision: int
    unit_revision: int
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionPromptBindingMutation:
    binding: CompanionPromptBinding
    sessions_rebased: int


@dataclass(frozen=True, slots=True)
class CompanionMessage:
    message_id: str
    request_id: str
    session_id: str
    context_epoch: int
    project_id: str
    role: str
    status: str
    content: str
    created_at: str
    provider_mode: str
    revision: int
    memory_review: dict[str, object]


@dataclass(frozen=True, slots=True)
class CompanionMessagePage:
    items: tuple[CompanionMessage, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationEpisodeProjection:
    """Rebuildable, source-bound summary of one completed chat exchange."""

    episode_id: str
    agent_id: str
    project_id: str
    session_id: str
    context_epoch: int
    request_id: str
    user_message_id: str
    user_message_revision: int
    assistant_message_id: str
    assistant_message_revision: int
    summary: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class CompanionHistoryItem:
    message: CompanionMessage
    dependency_count: int
    dependency_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompanionHistoryPage:
    items: tuple[CompanionHistoryItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CompanionMessageDependency:
    message_id: str
    dependent_kind: str
    dependent_id: str
    state: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CompanionForgetReceipt:
    receipt_id: str
    message_id: str
    session_id: str
    status: str
    failed_step: str | None
    affected: dict[str, int]
    attempts: int
    started_at: str
    updated_at: str
    completed_at: str | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class CompanionChatResult:
    session: CompanionSession
    user_message: CompanionMessage
    assistant_message: CompanionMessage
    source: str
    reason: str | None
    trace: dict[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompanionWalletMutation:
    transaction_id: str
    idempotency_key: str
    reason: str
    delta: int
    balance_after: int
    created_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompanionWalletIntegrity:
    snapshot_balance: int
    ledger_balance: int
    consistent: bool


@dataclass(frozen=True, slots=True)
class CompanionWalletEntry:
    transaction_id: str
    reason: str
    delta: int
    balance_after: int
    created_at: str


@dataclass(frozen=True, slots=True)
class CompanionStateSnapshot:
    affinity: int
    affinity_level: int
    mood_score: int
    mood: str
    coins: int
    outfit_id: str
    background_id: str
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionStateActionResult:
    action_id: str
    idempotency_key: str
    command: str
    subject_id: str | None
    local_day: str
    rule_version: int
    snapshot: CompanionStateSnapshot
    affinity_delta: int
    mood_delta: int
    coin_delta: int
    unlocks: tuple[int, ...]
    created_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompanionStateProjection:
    snapshot: CompanionStateSnapshot
    effective_mood_score: int
    effective_mood: str
    transient_modifier: int
    transient_expires_at: str | None


@dataclass(frozen=True, slots=True)
class CompanionInteractionEvent:
    event_id: str
    kind: str
    occurred_at: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompanionInventoryItem:
    item_id: str
    quantity: int
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class CompanionBackupReceipt:
    backup_path: Path
    manifest_path: Path
    fingerprint: str
    size_bytes: int
    database_schema_version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class CompanionRestorePreflight:
    backup_path: Path
    fingerprint: str
    size_bytes: int
    source_schema_version: int
    target_schema_version: int | None
    requires_migration: bool


@dataclass(frozen=True, slots=True)
class CompanionRestoreReceipt:
    restored_fingerprint: str
    restored_schema_version: int
    rollback_backup: CompanionBackupReceipt | None
    completed_at: str
