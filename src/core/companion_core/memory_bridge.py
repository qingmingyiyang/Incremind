from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
import math
import re
from typing import Protocol

from core.product_core.companion_memory_boundary import (
    LibrarySearchError,
    LibrarySearchService,
    MemoryCandidateRepositoryError,
    memory_candidate_id,
)

from .errors import CompanionConflict, CompanionRepositoryError
from .models import CompanionMessage
from .repository import CompanionRepository


_ALLOWED_ROLES = {"user", "assistant"}
_ALLOWED_LAYERS = ("l1_atom", "l2_scenario", "l3_series_memory")
_ALLOWED_TRUST = ("user_confirmed", "trusted")
_MAX_CANDIDATE_CHARS = 4_000
_MAX_HITS = 4
_MAX_CONTEXT_CHARS = 2_400
_MIN_SCORE = 0.5
_MEMORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,191}$")


class CompanionMemoryBridgeError(CompanionRepositoryError):
    pass


class CandidateStorePort(Protocol):
    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]: ...
    def get(self, candidate_id: str) -> Mapping[str, object] | None: ...


class PublishedMemoryLookup(Protocol):
    def __call__(self, layer: str, object_id: str) -> Mapping[str, object] | None: ...


class PublishedMemoryList(Protocol):
    def __call__(self, layer: str) -> tuple[Mapping[str, object], ...]: ...


class PublishedDependencyId(Protocol):
    def __call__(self, layer: str, object_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CompanionMemoryCandidateResult:
    candidate_id: str
    message_id: str
    status: str
    target_layer: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompanionMemoryRecall:
    status: str
    backend: str
    context: tuple[Mapping[str, object], ...]
    selected: tuple[Mapping[str, object], ...]
    dropped_reasons: tuple[str, ...]
    review_scope: str | None = None


class CompanionMemoryBridge:
    """Narrow adapter from Companion messages to the existing Memory authorities."""

    def __init__(
        self,
        repository: CompanionRepository,
        *,
        candidates: CandidateStorePort,
        search: LibrarySearchService,
        project_id: str = "default",
        published_lookup: PublishedMemoryLookup | None = None,
        published_list: PublishedMemoryList | None = None,
        candidate_delete: Callable[[str], bool] | None = None,
        published_dependency_id: PublishedDependencyId | None = None,
        published_withdraw: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
        local_timezone: Callable[[], tzinfo] | None = None,
    ) -> None:
        self.repository = repository
        self.candidates = candidates
        self.search = search
        self.project_id = project_id
        self.published_lookup = published_lookup
        self.published_list = published_list
        self.candidate_delete = candidate_delete
        self.published_dependency_id = published_dependency_id
        self.published_withdraw = published_withdraw
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.today = today or (lambda: datetime.now().astimezone().date())
        self.local_timezone = local_timezone or (lambda: datetime.now().astimezone().tzinfo or timezone.utc)

    def propose(self, message_id: str) -> CompanionMemoryCandidateResult:
        message = self.repository.get_message(message_id)
        if message is None:
            raise CompanionMemoryBridgeError("companion message was not found")
        if message.project_id != self.project_id:
            raise CompanionConflict("companion message project conflicts with memory candidate scope")
        if message.role not in _ALLOWED_ROLES or message.status != "completed":
            raise CompanionMemoryBridgeError("companion message is not eligible for memory review")
        content = message.content.strip()
        if not content or len(content) > _MAX_CANDIDATE_CHARS:
            raise CompanionMemoryBridgeError("companion message is not eligible for memory review")
        candidate_id = memory_candidate_id("companion-message", message.message_id)
        existing = self.candidates.get(candidate_id)
        if existing is not None:
            self._assert_same_candidate(existing, message)
            self._ensure_dependency(message.message_id, candidate_id, message.created_at)
            return _candidate_result(existing, message.message_id, replayed=True)
        timestamp = _utc(self.now())
        source_ref = {
            "source_id": message.message_id,
            "locator": "companion:message",
        }
        payload = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": self.project_id,
            "target_layer": "atom",
            "candidate_type": "answer_fact" if message.role == "user" else "answer_summary",
            "status": "pending_review",
            "proposed_content": content,
            "source_refs": [source_ref],
            "evidence_refs": [source_ref],
            "provenance": {
                "companion_message_id": message.message_id,
                "input_refs": [
                    {
                        "kind": "companion_message",
                        "object_id": message.message_id,
                        "uri": f"crp://default/companion/messages/{message.message_id}",
                    }
                ],
            },
            "review_prompt": "请确认这条陪伴对话是否应成为长期记忆。",
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "用户从陪伴历史显式提议，等待人工审阅。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        created_here = True
        try:
            saved = self.candidates.save(payload)
        except MemoryCandidateRepositoryError as exc:
            replay = self.candidates.get(candidate_id)
            if replay is None:
                raise CompanionMemoryBridgeError("memory candidate could not be saved") from exc
            self._assert_same_candidate(replay, message)
            saved = replay
            created_here = False
        try:
            self._ensure_dependency(message.message_id, candidate_id, message.created_at)
        except Exception as exc:
            if created_here and self._compensate_candidate(candidate_id):
                raise CompanionMemoryBridgeError(
                    "memory candidate dependency failed and candidate was compensated"
                ) from exc
            raise CompanionMemoryBridgeError(
                "memory candidate dependency failed; retry is required"
            ) from exc
        return _candidate_result(saved, message.message_id, replayed=False)

    def candidate_status(self, message_id: str) -> Mapping[str, object] | None:
        candidate = self.candidates.get(memory_candidate_id("companion-message", message_id))
        if candidate is None:
            return None
        message = self.repository.get_message(message_id)
        if message is None:
            return {"candidate_id": "", "status": "unavailable", "target_layer": "atom"}
        try:
            self._assert_same_candidate(candidate, message)
        except CompanionConflict:
            return {"candidate_id": "", "status": "unavailable", "target_layer": "atom"}
        return {
            "candidate_id": str(candidate.get("id") or ""),
            "status": str(candidate.get("status") or "pending_review"),
            "target_layer": str(candidate.get("target_layer") or "atom"),
        }

    def recall(
        self, query: str, *, review_scope: str | None = None, target_memory_id: str | None = None,
    ) -> CompanionMemoryRecall:
        if not isinstance(query, str) or not query.strip():
            return CompanionMemoryRecall("empty", "none", (), (), ("empty_query",))
        if review_scope in {"today_memory_review", "weekly_memory_review"}:
            return self._recall_review(review_scope)
        if target_memory_id is not None and review_scope != "memory_topic_discussion":
            return CompanionMemoryRecall("empty", "none", (), (), ("invalid_anchor_scope",))
        selected: list[Mapping[str, object]] = []
        context: list[Mapping[str, object]] = []
        dropped: list[str] = []
        used_chars = 0
        if target_memory_id is not None:
            anchor, reason = self._topic_anchor(target_memory_id)
            if anchor is None:
                return CompanionMemoryRecall(
                    "empty", "publication_authority", (), (), (reason,), "memory_topic_discussion",
                )
            object_id, source_id, text = anchor
            bounded = text[: min(800, _MAX_CONTEXT_CHARS)]
            context.append({"memory_id": object_id, "text": bounded})
            selected.append({"memory_id": object_id, "source_id": source_id, "score": 1.0, "rank": 1})
            used_chars = len(bounded)
        try:
            result = self.search.search(
                query=query.strip(), project_id=self.project_id, layers=_ALLOWED_LAYERS,
                trust_statuses=_ALLOWED_TRUST, limit=12,
            )
        except (LibrarySearchError, OSError, ValueError):
            resolved_scope = review_scope if review_scope == "memory_topic_discussion" else None
            if selected:
                return CompanionMemoryRecall(
                    "recalled", "publication_authority", tuple(context), tuple(selected),
                    ("index_unavailable",), resolved_scope,
                )
            return CompanionMemoryRecall(
                "degraded", "unavailable", (), (), ("index_unavailable",), resolved_scope,
            )
        for hit in result.hits:
            if any(item.get("memory_id") == hit.object_id for item in selected):
                continue
            current = self.published_lookup(hit.layer, hit.object_id) if self.published_lookup else None
            if current is None:
                dropped.append("not_published")
                continue
            conflict = current.get("conflict")
            if isinstance(conflict, Mapping) and conflict.get("status") not in {None, "none", "resolved"}:
                dropped.append("relationship_conflict")
                continue
            confidence = current.get("confidence")
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.7:
                dropped.append("low_confidence")
                continue
            if current.get("status") in {"withdrawn", "rolled_back", "deleted"} or current.get("stale") is True:
                dropped.append("withdrawn")
                continue
            if not math.isfinite(hit.score) or hit.score < _MIN_SCORE:
                dropped.append("low_score")
                continue
            if not hit.source_refs:
                dropped.append("missing_source")
                continue
            text = hit.content.strip()
            if not text:
                dropped.append("empty_content")
                continue
            remaining = _MAX_CONTEXT_CHARS - used_chars
            if remaining <= 0 or len(selected) >= _MAX_HITS:
                dropped.append("budget_exceeded")
                continue
            bounded = text[: min(800, remaining)]
            used_chars += len(bounded)
            context.append({"memory_id": hit.object_id, "text": bounded})
            selected.append({
                "memory_id": hit.object_id,
                "source_id": hit.source_refs[0].split("#", 1)[0],
                "score": round(hit.score, 6),
                "rank": len(selected) + 1,
            })
        status = "recalled" if selected else ("degraded" if result.reason else "empty")
        resolved_scope = review_scope if review_scope == "memory_topic_discussion" else None
        return CompanionMemoryRecall(
            status, "publication_authority" if target_memory_id else result.backend,
            tuple(context), tuple(selected), tuple(dict.fromkeys(dropped)), resolved_scope,
        )

    def _topic_anchor(self, target_memory_id: str) -> tuple[tuple[str, str, str] | None, str]:
        if _MEMORY_ID.fullmatch(target_memory_id) is None:
            return None, "invalid_memory_id"
        if self.published_lookup is None:
            return None, "publication_authority_unavailable"
        for layer in _ALLOWED_LAYERS:
            item = self.published_lookup(layer, target_memory_id)
            if item is None:
                continue
            if _memory_project_id(item) != self.project_id:
                return None, "outside_project"
            reason = _review_drop_reason(item)
            if reason:
                return None, reason
            source_id = _first_source_id(item.get("source_refs") or item.get("evidence_refs"))
            if not source_id:
                return None, "missing_source"
            text = _memory_text(item)
            if not text:
                return None, "empty_content"
            return (target_memory_id, source_id, text), ""
        return None, "not_published"

    def _recall_review(self, review_scope: str) -> CompanionMemoryRecall:
        if self.published_list is None:
            return CompanionMemoryRecall(
                "degraded", "unavailable", (), (), ("publication_authority_unavailable",), review_scope,
            )
        end = self.today()
        start = end if review_scope == "today_memory_review" else end - timedelta(days=6)
        eligible: list[tuple[datetime, str, Mapping[str, object], str, str]] = []
        dropped: list[str] = []
        for layer in _ALLOWED_LAYERS:
            for item in self.published_list(layer):
                if _memory_project_id(item) != self.project_id:
                    dropped.append("outside_project")
                    continue
                reason = _review_drop_reason(item)
                if reason:
                    dropped.append(reason)
                    continue
                published_at = _published_datetime(item, local_timezone=self.local_timezone())
                if published_at is None:
                    dropped.append("missing_publication_date")
                    continue
                if not start <= published_at.date() <= end:
                    dropped.append("outside_review_window")
                    continue
                source_id = _first_source_id(item.get("source_refs") or item.get("evidence_refs"))
                if not source_id:
                    dropped.append("missing_source")
                    continue
                text = _memory_text(item)
                if not text:
                    dropped.append("empty_content")
                    continue
                object_id = str(item.get("id") or item.get("skill_id") or "").strip()
                if not object_id:
                    dropped.append("missing_id")
                    continue
                eligible.append((published_at, object_id, item, source_id, text))
        eligible.sort(key=lambda row: (row[0], row[1]), reverse=True)
        context: list[Mapping[str, object]] = []
        selected: list[Mapping[str, object]] = []
        used_chars = 0
        for published_at, object_id, _item, source_id, text in eligible:
            remaining = _MAX_CONTEXT_CHARS - used_chars
            if remaining <= 0 or len(selected) >= _MAX_HITS:
                dropped.append("budget_exceeded")
                continue
            bounded = text[: min(800, remaining)]
            used_chars += len(bounded)
            context.append({
                "memory_id": object_id,
                "published_date": published_at.date().isoformat(),
                "text": bounded,
            })
            selected.append({
                "memory_id": object_id,
                "source_id": source_id,
                "score": 1.0,
                "rank": len(selected) + 1,
            })
        return CompanionMemoryRecall(
            "recalled" if selected else "empty",
            "publication_authority",
            tuple(context),
            tuple(selected),
            tuple(dict.fromkeys(dropped)),
            review_scope,
        )

    def sync_published_dependencies(self, message_id: str) -> None:
        message = self.repository.get_message(message_id)
        if message is None or self.published_list is None:
            return
        for layer in _ALLOWED_LAYERS:
            for item in self.published_list(layer):
                refs = item.get("source_refs")
                if not isinstance(refs, list) or not any(
                    isinstance(ref, Mapping) and ref.get("source_id") == message_id for ref in refs
                ):
                    continue
                object_id = item.get("id")
                if isinstance(object_id, str) and object_id:
                    dependent_id = (
                        self.published_dependency_id(layer, object_id)
                        if self.published_dependency_id else None
                    )
                    if not dependent_id:
                        raise CompanionMemoryBridgeError("published memory publication evidence is unavailable")
                    self._ensure_dependency(
                        message_id, dependent_id, message.created_at,
                        dependent_kind="published_memory",
                    )


    def erase_candidate(self, candidate_id: str) -> None:
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            return
        if self.candidate_delete is None or not self.candidate_delete(candidate_id):
            raise CompanionMemoryBridgeError("memory candidate could not be deleted")

    def withdraw_published(self, publication_id: str) -> None:
        if self.published_withdraw is None:
            raise CompanionMemoryBridgeError("published memory withdrawal adapter is unavailable")
        self.published_withdraw(publication_id)

    def _ensure_dependency(
        self, message_id: str, candidate_id: str, created_at: str, *, dependent_kind: str = "candidate",
    ) -> None:
        try:
            self.repository.register_message_dependency(
                message_id=message_id, dependent_kind=dependent_kind,
                dependent_id=candidate_id, created_at=created_at,
            )
        except CompanionConflict:
            return

    def _compensate_candidate(self, candidate_id: str) -> bool:
        if self.candidate_delete is None:
            return False
        try:
            return self.candidate_delete(candidate_id) is True
        except Exception:
            return False

    @staticmethod
    def _assert_same_candidate(candidate: Mapping[str, object], message: CompanionMessage) -> None:
        provenance = candidate.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("companion_message_id") != message.message_id
            or candidate.get("project_id") != message.project_id
        ):
            raise CompanionConflict("memory candidate identity conflicts with stored provenance")


def _review_drop_reason(item: Mapping[str, object]) -> str | None:
    if item.get("status") in {"withdrawn", "rolled_back", "deleted"} or item.get("stale") is True:
        return "withdrawn"
    if item.get("trust_status") not in _ALLOWED_TRUST:
        return "untrusted"
    conflict = item.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("status") not in {None, "none", "resolved"}:
        return "relationship_conflict"
    confidence = item.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.7:
        return "low_confidence"
    return None


def _published_datetime(item: Mapping[str, object], *, local_timezone: tzinfo) -> datetime | None:
    for key in ("published_at", "updated_at", "created_at"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_timezone)
        return parsed.astimezone(local_timezone)
    return None


def _first_source_id(value: object) -> str:
    if not isinstance(value, (list, tuple)):
        return ""
    for ref in value:
        if isinstance(ref, str):
            source_id = ref.split("#", 1)[0].strip()
        elif isinstance(ref, Mapping):
            source_id = str(ref.get("source_id") or "").strip()
        else:
            source_id = ""
        if source_id:
            return source_id
    return ""


def _memory_text(item: Mapping[str, object]) -> str:
    values: list[str] = []
    for key in ("title", "name", "content", "summary", "overview", "purpose"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    return "\n".join(values)


def _memory_project_id(item: Mapping[str, object]) -> str:
    metadata = item.get("metadata")
    project_id = item.get("project_id")
    if not isinstance(project_id, str) and isinstance(metadata, Mapping):
        project_id = metadata.get("project_id")
    return project_id.strip() if isinstance(project_id, str) and project_id.strip() else "default"


def _candidate_result(
    candidate: Mapping[str, object], message_id: str, *, replayed: bool,
) -> CompanionMemoryCandidateResult:
    return CompanionMemoryCandidateResult(
        candidate_id=str(candidate["id"]), message_id=message_id,
        status=str(candidate["status"]), target_layer=str(candidate["target_layer"]),
        replayed=replayed,
    )


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompanionMemoryBridgeError("memory bridge clock is invalid")
    return value.astimezone(timezone.utc).isoformat()
