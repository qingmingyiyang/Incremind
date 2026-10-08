"""Grounded, proposal-only consolidation of private conversation episodes.

This module intentionally has no publication dependency.  It produces the
existing ``MemoryCandidate`` envelope in ``pending_review`` state only; a
separate, explicit review flow remains the sole path to formal Memory.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.companion_core.conversation_recall import summarize_conversation_pair
from core.companion_core.models import CompanionMessage, ConversationEpisodeProjection
from core.memory_core import memory_candidate_id
from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


class MemoryDistillationError(ValueError):
    """Raised when a conversation episode cannot be safely distilled."""


class ConversationMessageReaderPort(Protocol):
    def get_message(self, message_id: str) -> CompanionMessage | None:
        """Read an authoritative Companion message by immutable identifier."""


class MemoryCandidateRepositoryPort(Protocol):
    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """Persist one reviewable candidate without publishing it."""

    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        """Read one candidate for idempotent replay."""

    def list_by_project(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """Read candidates for deterministic duplicate and conflict checks."""


class MemoryDistillationDiaryRepositoryPort(Protocol):
    """Persists privacy-safe evidence of an explicitly requested distillation."""

    def record(
        self,
        *,
        command_id: str,
        current_session_id: str,
        episode: ConversationEpisodeProjection,
        result: "MemoryDistillationResult",
        created_at: str,
    ) -> Mapping[str, object]:
        """Record one idempotent Dream Diary entry without proposal body text."""


@dataclass(frozen=True, slots=True)
class MemoryDistillationDiaryEntry:
    """Privacy-safe Dream Diary metadata.  It never contains proposal text."""

    episode_id: str
    source_refs: tuple[dict[str, object], ...]
    disposition: str
    reason_codes: tuple[str, ...]
    confidence: float
    conflicts: tuple[str, ...]
    estimated_cost_tokens: int


@dataclass(frozen=True, slots=True)
class MemoryDistillationResult:
    episode_id: str
    candidate_id: str | None
    status: str
    diary: MemoryDistillationDiaryEntry


class ObjectStoreMemoryDistillationDiaryRepository:
    """Append-only, body-free Dream Diary backed by the existing ObjectStore."""

    collection = "memory_distillation_diary"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def record(
        self,
        *,
        command_id: str,
        current_session_id: str,
        episode: ConversationEpisodeProjection,
        result: MemoryDistillationResult,
        created_at: str,
    ) -> Mapping[str, object]:
        if not isinstance(command_id, str) or not command_id:
            raise MemoryDistillationError("Dream Diary command_id is required")
        entry_id = f"memory-distillation-diary-{command_id}"
        payload = {
            "schema_version": "1.0.0",
            "id": entry_id,
            "kind": "conversation_memory_distillation",
            "command_id": command_id,
            "project_id": episode.project_id,
            "episode_id": episode.episode_id,
            "agent_id": episode.agent_id,
            "session_id": episode.session_id,
            "requested_from_session_id": current_session_id,
            "request_id": episode.request_id,
            "source_refs": [dict(item) for item in result.diary.source_refs],
            "candidate_id": result.candidate_id,
            "disposition": result.diary.disposition,
            "reason_codes": list(result.diary.reason_codes),
            "confidence": result.diary.confidence,
            "conflicts": list(result.diary.conflicts),
            "estimated_cost_tokens": result.diary.estimated_cost_tokens,
            "memory_publication_state": "not_published",
            "auto_publish_allowed": False,
            "content_included": False,
            "created_at": created_at,
        }
        existing = self._object_store.read(self.collection, entry_id)
        if existing is not None:
            if dict(existing) != payload:
                raise MemoryDistillationError("Dream Diary command identity conflicts with existing entry")
            return dict(existing)
        try:
            self._object_store.write(self.collection, entry_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as error:
            raced = self._object_store.read(self.collection, entry_id)
            if raced is not None and dict(raced) == payload:
                return dict(raced)
            raise MemoryDistillationError("Dream Diary command identity conflicts with existing entry") from error
        return payload

    def get(self, command_id: str) -> Mapping[str, object] | None:
        if not isinstance(command_id, str) or not command_id:
            raise MemoryDistillationError("Dream Diary command_id is required")
        item = self._object_store.read(
            self.collection, f"memory-distillation-diary-{command_id}",
        )
        return None if item is None else dict(item)


class DistillConversationEpisodeToMemoryProposal:
    """Turn one verified Episode into an explicitly reviewable Memory Candidate.

    The deterministic gate is deliberately conservative.  It treats any
    sensitive-looking material, stale projection source, weak value signal,
    duplicate, or conflict as a non-publication outcome.  No model call,
    Effect, Receipt, or automatic promotion is performed here.
    """

    def __init__(
        self,
        *,
        messages: ConversationMessageReaderPort,
        candidates: MemoryCandidateRepositoryPort,
        now: str | None = None,
        max_age_days: int = 90,
    ) -> None:
        if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or max_age_days < 1:
            raise MemoryDistillationError("max_age_days must be positive")
        self._messages = messages
        self._candidates = candidates
        self._now = _parse_utc(now) if now is not None else datetime.now(timezone.utc)
        self._max_age_days = max_age_days

    def execute(
        self,
        episode: ConversationEpisodeProjection,
        *,
        published_memory: Iterable[Mapping[str, object]] = (),
    ) -> MemoryDistillationResult:
        _validate_episode(episode)
        source_refs = _source_refs(episode)
        source_state = _verify_sources(self._messages, episode)
        if source_state is not None:
            return self._skip(episode, source_refs, source_state)
        if _is_stale(episode.occurred_at, now=self._now, max_age_days=self._max_age_days):
            return self._skip(episode, source_refs, "stale_source")

        proposed_content = _proposal_content(episode.summary)
        if _requires_redaction(proposed_content):
            return self._skip(episode, source_refs, "redaction_required")
        if not _has_memory_value(proposed_content):
            return self._skip(episode, source_refs, "insufficient_value")

        candidate_id = memory_candidate_id(episode.episode_id, "atom", "other", proposed_content)
        existing = self._candidates.get(candidate_id)
        if existing is not None:
            return self._result(
                episode, candidate_id, "pending_review", source_refs,
                reason_codes=("duplicate_candidate",), confidence=_confidence(proposed_content),
            )

        conflict_ids = _conflict_ids(
            proposed_content,
            (*self._candidates.list_by_project(episode.project_id), *tuple(published_memory)),
        )
        if conflict_ids:
            return self._skip(episode, source_refs, "conflict_current", conflicts=conflict_ids)

        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": episode.project_id,
            "target_layer": "atom",
            "candidate_type": "other",
            "status": "pending_review",
            "proposed_content": proposed_content,
            "source_refs": list(source_refs),
            "provenance": {
                "companion_message_id": episode.assistant_message_id,
                "conversation_episode_id": episode.episode_id,
                "conversation_agent_id": episode.agent_id,
                "source_revisions": {
                    "user": episode.user_message_revision,
                    "assistant": episode.assistant_message_revision,
                },
                "input_refs": [
                    {
                        "kind": "companion_message",
                        "object_id": episode.assistant_message_id,
                        "uri": f"companion://messages/{episode.assistant_message_id}",
                    },
                    {
                        "kind": "conversation_episode",
                        "object_id": episode.episode_id,
                        "uri": f"companion://episodes/{episode.episode_id}",
                    },
                ],
            },
            "distillation": {
                "policy": "grounded_memory_distillation_v1",
                "confidence": _confidence(proposed_content),
                "reason_codes": ["eligible_grounded_episode", "requires_user_review"],
                "conflicts": [],
                "estimated_cost_tokens": _estimated_cost(proposed_content),
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "基于已验证会话事实生成的候选记忆，需用户审核后才能进入长期记忆。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": self._now.isoformat(timespec="seconds"),
            "updated_at": self._now.isoformat(timespec="seconds"),
        }
        self._candidates.save(candidate)
        return self._result(
            episode, candidate_id, "pending_review", source_refs,
            reason_codes=("eligible_grounded_episode", "requires_user_review"),
            confidence=_confidence(proposed_content),
        )

    def _skip(
        self,
        episode: ConversationEpisodeProjection,
        source_refs: tuple[dict[str, object], ...],
        reason_code: str,
        *,
        conflicts: tuple[str, ...] = (),
    ) -> MemoryDistillationResult:
        return self._result(
            episode, None, "skipped", source_refs, reason_codes=(reason_code,),
            confidence=0.0, conflicts=conflicts,
        )

    @staticmethod
    def _result(
        episode: ConversationEpisodeProjection,
        candidate_id: str | None,
        status: str,
        source_refs: tuple[dict[str, object], ...],
        *,
        reason_codes: tuple[str, ...],
        confidence: float,
        conflicts: tuple[str, ...] = (),
    ) -> MemoryDistillationResult:
        diary = MemoryDistillationDiaryEntry(
            episode_id=episode.episode_id,
            source_refs=source_refs,
            disposition=status,
            reason_codes=reason_codes,
            confidence=confidence,
            conflicts=conflicts,
            estimated_cost_tokens=_estimated_cost(episode.summary),
        )
        return MemoryDistillationResult(episode.episode_id, candidate_id, status, diary)


def serialize_memory_distillation_result(result: MemoryDistillationResult) -> dict[str, object]:
    """Serialize auditable metadata only; proposal content is deliberately absent."""

    return {
        "episode_id": result.episode_id,
        "candidate_id": result.candidate_id,
        "status": result.status,
        "diary": {
            "source_refs": list(result.diary.source_refs),
            "disposition": result.diary.disposition,
            "reason_codes": list(result.diary.reason_codes),
            "confidence": result.diary.confidence,
            "conflicts": list(result.diary.conflicts),
            "estimated_cost_tokens": result.diary.estimated_cost_tokens,
        },
        "memory_publication_state": "candidate_created_not_published" if result.candidate_id else "not_published",
    }


def _validate_episode(episode: ConversationEpisodeProjection) -> None:
    if episode.agent_id != "companion.chat" or not episode.project_id or not episode.episode_id:
        raise MemoryDistillationError("conversation episode authority is invalid")
    if not episode.summary.strip():
        raise MemoryDistillationError("conversation episode summary is required")


def _verify_sources(reader: ConversationMessageReaderPort, episode: ConversationEpisodeProjection) -> str | None:
    expected = (
        (episode.user_message_id, episode.user_message_revision, "user"),
        (episode.assistant_message_id, episode.assistant_message_revision, "assistant"),
    )
    resolved: list[CompanionMessage] = []
    for message_id, revision, role in expected:
        message = reader.get_message(message_id)
        if message is None:
            return "stale_source"
        if (
            message.revision != revision or message.role != role or message.status != "completed"
            or message.project_id != episode.project_id or message.session_id != episode.session_id
            or message.context_epoch != episode.context_epoch or message.request_id != episode.request_id
        ):
            return "stale_source"
        resolved.append(message)
    if summarize_conversation_pair(resolved[0].content, resolved[1].content) != episode.summary:
        return "stale_source"
    return None


def _source_refs(episode: ConversationEpisodeProjection) -> tuple[dict[str, object], ...]:
    return (
        {"source_id": episode.user_message_id, "locator": f"companion://messages/{episode.user_message_id}"},
        {"source_id": episode.assistant_message_id, "locator": f"companion://messages/{episode.assistant_message_id}"},
    )


def _proposal_content(summary: str) -> str:
    assistant_marker = "\n助手："
    content = summary.split(assistant_marker, 1)[-1].strip()
    return re.sub(r"\s+", " ", content)


_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:api[_-]?key|secret|token|password|cookie)\b\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)1\d{10}(?!\d)"),
)


def _requires_redaction(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SENSITIVE_PATTERNS)


def _has_memory_value(content: str) -> bool:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", content)
    if len(normalized) < 16:
        return False
    generic = {"好的", "谢谢", "收到", "不知道", "继续", "可以", "ok", "okay"}
    return normalized.lower() not in generic


def _content_key(content: str) -> str:
    return re.sub(r"\W+", "", content.lower())[:72]


def _conflict_ids(content: str, existing: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    key = _content_key(content)
    conflicts: list[str] = []
    for record in existing:
        existing_content = record.get("proposed_content") or record.get("content")
        existing_id = record.get("id")
        if not isinstance(existing_content, str) or not isinstance(existing_id, str):
            continue
        other_key = _content_key(existing_content)
        if not other_key:
            continue
        if other_key == key:
            return (existing_id,)
        if min(len(other_key), len(key)) >= 24 and other_key[:24] == key[:24]:
            conflicts.append(existing_id)
    return tuple(sorted(set(conflicts)))


def _confidence(content: str) -> float:
    return min(0.9, round(0.55 + min(len(content), 280) / 800, 2))


def _estimated_cost(content: str) -> int:
    return max(1, (len(content) + 3) // 4)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryDistillationError("now must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryDistillationError("now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _is_stale(occurred_at: str, *, now: datetime, max_age_days: int) -> bool:
    occurred = _parse_utc(occurred_at)
    return occurred > now or (now - occurred).days > max_age_days
