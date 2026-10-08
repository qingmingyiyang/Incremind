"""Bounded, post-terminal proposal intake for memory and Skill learning.

The coordinator deliberately runs *after* an existing terminal authority has
committed.  It never changes the Turn or Companion terminal state, installs no
recovery worker, and treats every exception as an observable no-op.  Memory
and Skill paths keep their existing authorities: verified Companion episodes
flow through ``DistillConversationEpisodeToMemoryProposal`` and authoritative
AI Turns flow through ``ApplicationSkillLearningRuntime``.

There is intentionally no inference over a Turn body here.  Skill learning
only happens when a caller supplies an explicit, already-classified reusable
signal and proposed content.  The Workshop remains the final safety gate and
always creates a pending-review proposal.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from backend.api.application_skill_learning_runtime import ApplicationSkillLearningRuntime
from backend.companion_runtime_layout import build_companion_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.companion_core import CompanionMessage, CompanionRepository
from core.companion_core.conversation_recall import episode_id_for
from core.companion_core.models import ConversationEpisodeProjection
from core.memory_core import (
    ObjectStoreMemoryCandidateRepository,
    ObjectStoreMemoryStore,
    SQLiteMemoryReader,
)
from core.product_core.memory_distillation import (
    ConversationMessageReaderPort,
    DistillConversationEpisodeToMemoryProposal,
    MemoryCandidateRepositoryPort,
    MemoryDistillationDiaryRepositoryPort,
    ObjectStoreMemoryDistillationDiaryRepository,
)
from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_~-]{0,95}$")


class CompletedConversationEpisodeReader(Protocol):
    """Resolve one already-persisted private Companion episode by its scope."""

    def completed_episode(
        self, *, project_id: str, session_id: str, episode_id: str,
    ) -> ConversationEpisodeProjection | None: ...


@dataclass(frozen=True, slots=True)
class CompanionRepositoryCompletedEpisodeReader:
    """Narrow adapter over the existing private Companion repository.

    The repository keeps the user boundary in its local data root.  This
    adapter additionally requires the event session to still belong to the
    same project, and accepts only an episode produced in that exact session.
    It does not expose message content or permit a cross-project history scan.
    """

    repository: object

    def completed_episode(
        self, *, project_id: str, session_id: str, episode_id: str,
    ) -> ConversationEpisodeProjection | None:
        get_session = getattr(self.repository, "get_session", None)
        get_episode = getattr(self.repository, "get_conversation_episode", None)
        if not callable(get_session) or not callable(get_episode):
            return None
        session = get_session(session_id)
        if getattr(session, "project_id", None) != project_id:
            return None
        episode = get_episode(
            project_id=project_id, session_id=session_id, episode_id=episode_id,
        )
        return episode if isinstance(episode, ConversationEpisodeProjection) else None


class PublishedMemoryReader(Protocol):
    def published_memory_for_project(self, project_id: str) -> Iterable[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class CompletedCompanionEpisode:
    """A bounded completion notification, with no message body or credentials."""

    completion_id: str
    project_id: str
    session_id: str
    episode_id: str


@dataclass(frozen=True, slots=True)
class ExplicitSkillLearningEvidence:
    """A trusted, explicit learning decision; never synthesized from Turn text."""

    resolution_id: str
    skill_id: str
    expected_fingerprint: str
    reusable_signal: Mapping[str, object]
    proposed_content: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CompletedTurn:
    """A bounded AI Turn terminal notification, scoped by persisted project ID."""

    completion_id: str
    project_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class CompletionLearningOutcome:
    kind: str
    status: str
    proposal_id: str | None = None
    candidate_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    replayed: bool = False


class ObjectStoreSkillLearningJournal:
    """Idempotency metadata for automatic Skill proposals, without proposal text."""

    collection = "task_completion_skill_learning"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def get(self, completion_id: str) -> Mapping[str, object] | None:
        item = self._object_store.read(self.collection, f"skill-learning-{completion_id}")
        return dict(item) if item is not None else None

    def record(
        self, *, completion: CompletedTurn, evidence: ExplicitSkillLearningEvidence,
        result: Mapping[str, object], created_at: str,
    ) -> Mapping[str, object]:
        proposal_id = result.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("Skill learning proposal identity is unavailable")
        payload = {
            "schema_version": "1.0.0",
            "id": f"skill-learning-{completion.completion_id}",
            "kind": "task_completion_skill_learning",
            "completion_id": completion.completion_id,
            "project_id": completion.project_id,
            "turn_id": completion.turn_id,
            "resolution_id": evidence.resolution_id,
            "skill_id": evidence.skill_id,
            "skill_fingerprint": evidence.expected_fingerprint,
            "proposal_id": proposal_id,
            "proposal_status": result.get("status"),
            "write_effect": "proposal_only",
            "skill_file_modified": False,
            "created_at": created_at,
        }
        existing = self._object_store.read(self.collection, str(payload["id"]))
        if existing is not None:
            if dict(existing) != payload:
                raise ValueError("Skill learning completion identity conflicts")
            return dict(existing)
        try:
            self._object_store.write(self.collection, str(payload["id"]), payload, expected_revision=0)
        except ObjectStoreRevisionError as error:
            raced = self._object_store.read(self.collection, str(payload["id"]))
            if raced is not None and dict(raced) == payload:
                return dict(raced)
            raise ValueError("Skill learning completion identity conflicts") from error
        return payload


@dataclass(slots=True)
class TaskCompletionLearningCoordinator:
    """Runs bounded proposal intake after authoritative completion only.

    This class has no background scheduler and no terminal-state dependency in
    the reverse direction.  Call it in a best-effort ``finally`` block after a
    completion has been durably committed.
    """

    episodes: CompletedConversationEpisodeReader
    messages: ConversationMessageReaderPort
    candidates: MemoryCandidateRepositoryPort
    memory_diary: MemoryDistillationDiaryRepositoryPort
    published_memory: PublishedMemoryReader
    skill_runtime: ApplicationSkillLearningRuntime | None = None
    skill_journal: ObjectStoreSkillLearningJournal | None = None
    now: Callable[[], str] | None = None

    def on_completed_companion_episode(self, completion: CompletedCompanionEpisode) -> CompletionLearningOutcome:
        """Attempt one verified private episode distillation without publication."""
        try:
            _validate_completion(completion.completion_id, completion.project_id, completion.session_id, completion.episode_id)
            episode = self.episodes.completed_episode(
                project_id=completion.project_id,
                session_id=completion.session_id,
                episode_id=completion.episode_id,
            )
            if episode is None:
                return CompletionLearningOutcome("memory", "skipped", reason_codes=("episode_unavailable",))
            if (
                episode.agent_id != "companion.chat"
                or episode.project_id != completion.project_id
                or episode.session_id != completion.session_id
                or episode.episode_id != completion.episode_id
            ):
                return CompletionLearningOutcome("memory", "skipped", reason_codes=("scope_or_authority_drift",))

            command_id = f"distill-auto-{completion.completion_id}"
            existing = _get_diary(self.memory_diary, command_id)
            if existing is not None:
                if (
                    existing.get("project_id") != completion.project_id
                    or existing.get("episode_id") != completion.episode_id
                    or existing.get("requested_from_session_id") != completion.session_id
                ):
                    return CompletionLearningOutcome("memory", "failed", reason_codes=("completion_identity_conflict",))
                return CompletionLearningOutcome(
                    "memory", str(existing.get("disposition") or "skipped"),
                    candidate_id=_optional_id(existing.get("candidate_id")),
                    reason_codes=_reason_codes(existing.get("reason_codes")), replayed=True,
                )

            result = DistillConversationEpisodeToMemoryProposal(
                messages=self.messages, candidates=self.candidates, now=self._now(),
            ).execute(
                episode,
                published_memory=self.published_memory.published_memory_for_project(completion.project_id),
            )
            self.memory_diary.record(
                command_id=command_id,
                current_session_id=completion.session_id,
                episode=episode,
                result=result,
                created_at=self._now(),
            )
            return CompletionLearningOutcome(
                "memory", result.status, candidate_id=result.candidate_id,
                reason_codes=result.diary.reason_codes,
            )
        except Exception:
            # Learning is expressly non-critical: terminal state and user data
            # must remain valid even if a derived proposal cannot be assessed.
            return CompletionLearningOutcome("memory", "failed", reason_codes=("intake_failed",))

    def on_completed_turn(
        self, completion: CompletedTurn, evidence: ExplicitSkillLearningEvidence | None = None,
    ) -> CompletionLearningOutcome:
        """Propose a Skill change only for an explicit trusted learning signal."""
        try:
            _validate_completion(completion.completion_id, completion.project_id, completion.turn_id)
            if evidence is None:
                return CompletionLearningOutcome("skill", "skipped", reason_codes=("no_explicit_signal",))
            if self.skill_runtime is None or self.skill_journal is None:
                return CompletionLearningOutcome("skill", "skipped", reason_codes=("learning_runtime_unavailable",))
            existing = self.skill_journal.get(completion.completion_id)
            if existing is not None:
                if (
                    existing.get("project_id") != completion.project_id
                    or existing.get("turn_id") != completion.turn_id
                    or existing.get("resolution_id") != evidence.resolution_id
                    or existing.get("skill_id") != evidence.skill_id
                    or existing.get("skill_fingerprint") != evidence.expected_fingerprint
                ):
                    return CompletionLearningOutcome("skill", "failed", reason_codes=("completion_identity_conflict",))
                return CompletionLearningOutcome(
                    "skill", str(existing.get("proposal_status") or "pending_review"),
                    proposal_id=_optional_id(existing.get("proposal_id")),
                    reason_codes=("replayed_completion",), replayed=True,
                )
            result = self.skill_runtime.propose(
                turn_id=completion.turn_id,
                resolution_id=evidence.resolution_id,
                skill_id=evidence.skill_id,
                expected_fingerprint=evidence.expected_fingerprint,
                reusable_signal=evidence.reusable_signal,
                proposed_content=evidence.proposed_content,
            )
            if result.get("status") != "pending_review":
                return CompletionLearningOutcome("skill", "failed", reason_codes=("proposal_state_invalid",))
            # The authoritative runtime independently resolves project scope.
            self.skill_journal.record(
                completion=completion, evidence=evidence, result=result, created_at=self._now(),
            )
            return CompletionLearningOutcome(
                "skill", "pending_review", proposal_id=_optional_id(result.get("proposal_id")),
                reason_codes=("explicit_signal", "requires_user_review"),
            )
        except Exception:
            return CompletionLearningOutcome("skill", "failed", reason_codes=("intake_failed",))

    def _now(self) -> str:
        if self.now is not None:
            return self.now()
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_completion(*values: str) -> None:
    if any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in values):
        raise ValueError("completion scope is invalid")


def _get_diary(repository: MemoryDistillationDiaryRepositoryPort, command_id: str) -> Mapping[str, object] | None:
    getter = getattr(repository, "get", None)
    if not callable(getter):
        return None
    result = getter(command_id)
    return dict(result) if isinstance(result, Mapping) else None


def _optional_id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) is not None else None


def _reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return ("invalid_recorded_outcome",)
    return tuple(value)


def process_completed_companion_episode(
    container: object, *, user_message: CompanionMessage, assistant_message: CompanionMessage,
) -> CompletionLearningOutcome:
    """Build existing authorities and assess one committed Companion pair.

    This function is safe for a response background task: every domain failure
    is converted to a learning outcome and never changes the completed reply.
    """

    if (
        user_message.role != "user"
        or assistant_message.role != "assistant"
        or user_message.status != "completed"
        or assistant_message.status != "completed"
        or user_message.project_id != assistant_message.project_id
        or user_message.session_id != assistant_message.session_id
        or user_message.request_id != assistant_message.request_id
    ):
        return CompletionLearningOutcome(
            "memory", "skipped", reason_codes=("scope_or_authority_drift",),
        )
    try:
        root_dir = getattr(container, "root_dir")
        repository = CompanionRepository.at_data_root(root_dir)
        store = build_companion_object_store(root_dir)
        factory = AggregateRepositoryFactory(
            runtime_root=root_dir,
            namespace_id=store.namespace_id,
            json_store=store,
        )
        resolution = factory.memory_publication_authority_resolution()
        published_memory = (
            SQLiteMemoryReader(resolution.records)
            if resolution.records is not None
            else ObjectStoreMemoryStore(store)
        )
        coordinator = TaskCompletionLearningCoordinator(
            episodes=CompanionRepositoryCompletedEpisodeReader(repository),
            messages=repository,
            candidates=ObjectStoreMemoryCandidateRepository(store),
            memory_diary=ObjectStoreMemoryDistillationDiaryRepository(store),
            published_memory=published_memory,
        )
        return coordinator.on_completed_companion_episode(CompletedCompanionEpisode(
            completion_id=assistant_message.request_id,
            project_id=assistant_message.project_id,
            session_id=assistant_message.session_id,
            episode_id=episode_id_for(user_message.message_id, assistant_message.message_id),
        ))
    except Exception:
        return CompletionLearningOutcome(
            "memory", "failed", reason_codes=("intake_failed",),
        )
