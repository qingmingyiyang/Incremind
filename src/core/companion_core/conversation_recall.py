from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import ConversationEpisodeProjection

if TYPE_CHECKING:
    from .repository import CompanionRepository


CONVERSATION_AGENT_ID = "companion.chat"
DEFAULT_EPISODE_LIMIT = 4
DEFAULT_SUMMARY_BYTE_BUDGET = 2_400
_PAIR_SUMMARY_BYTE_BUDGET = 2_000


@dataclass(frozen=True, slots=True)
class ConversationRecall:
    episodes: tuple[ConversationEpisodeProjection, ...]
    byte_count: int


class ConversationRecallService:
    """Reads only rebuildable, project-scoped historical conversation summaries."""

    def __init__(self, repository: CompanionRepository, *, project_id: str = "default") -> None:
        self.repository = repository
        self.project_id = project_id

    def rebuild(self) -> int:
        return self.repository.rebuild_conversation_episodes(project_id=self.project_id)

    def recall(
        self,
        *,
        query: str,
        current_session_id: str,
        limit: int = DEFAULT_EPISODE_LIMIT,
        byte_budget: int = DEFAULT_SUMMARY_BYTE_BUDGET,
    ) -> ConversationRecall:
        if not isinstance(query, str):
            raise ValueError("conversation recall query is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= DEFAULT_EPISODE_LIMIT:
            raise ValueError("conversation recall limit is invalid")
        if not isinstance(byte_budget, int) or isinstance(byte_budget, bool) or not 1 <= byte_budget <= DEFAULT_SUMMARY_BYTE_BUDGET:
            raise ValueError("conversation recall byte budget is invalid")
        selected: list[ConversationEpisodeProjection] = []
        used = 0
        candidates = self.repository.list_conversation_episodes(
            project_id=self.project_id, current_session_id=current_session_id, limit=12,
        )
        ranked = sorted(
            candidates,
            key=lambda episode: (_relevance_score(query, episode.summary), episode.occurred_at, episode.episode_id),
            reverse=True,
        )
        for episode in ranked:
            size = len(episode.summary.encode("utf-8"))
            if size > byte_budget - used:
                continue
            selected.append(episode)
            used += size
            if len(selected) == limit:
                break
        return ConversationRecall(tuple(selected), used)


def episode_id_for(user_message_id: str, assistant_message_id: str) -> str:
    digest = hashlib.sha256(f"{user_message_id}\x00{assistant_message_id}".encode("utf-8")).hexdigest()[:32]
    return f"episode:{digest}"


def summarize_conversation_pair(user_content: str, assistant_content: str) -> str:
    """Produce stable, bounded text without interpreting it as a formal Memory."""

    prefix = "用户："
    divider = "\n助手："
    available = _PAIR_SUMMARY_BYTE_BUDGET - len((prefix + divider).encode("utf-8"))
    user_budget = available // 2
    assistant_budget = available - user_budget
    return prefix + _utf8_prefix(user_content.strip(), user_budget) + divider + _utf8_prefix(assistant_content.strip(), assistant_budget)


def _utf8_prefix(value: str, byte_budget: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_budget:
        return value
    return encoded[:byte_budget].decode("utf-8", errors="ignore").rstrip()


def _relevance_score(query: str, summary: str) -> int:
    normalized = query.strip().lower()
    if not normalized:
        return 0
    haystack = summary.lower()
    if normalized in haystack:
        return 10_000
    terms: set[str] = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+", normalized))
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return sum(1 for term in terms if term in haystack)
