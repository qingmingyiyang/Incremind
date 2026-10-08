from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from backend.api.task_completion_learning import (
    CompanionRepositoryCompletedEpisodeReader,
    CompletedCompanionEpisode,
    CompletedTurn,
    CompletionLearningOutcome,
    ExplicitSkillLearningEvidence,
    ObjectStoreSkillLearningJournal,
    TaskCompletionLearningCoordinator,
)
from core.companion_core.models import CompanionMessage, ConversationEpisodeProjection
from core.product_core.memory_distillation import ObjectStoreMemoryDistillationDiaryRepository
from core.storage_provider import JsonObjectStore


@dataclass
class _EpisodeReader:
    episode: ConversationEpisodeProjection | None

    def completed_episode(self, *, project_id: str, session_id: str, episode_id: str):
        if self.episode is None:
            return None
        if (project_id, session_id, episode_id) != (
            self.episode.project_id, self.episode.session_id, self.episode.episode_id,
        ):
            return None
        return self.episode


@dataclass
class _Messages:
    values: dict[str, CompanionMessage]

    def get_message(self, message_id: str):
        return self.values.get(message_id)


@dataclass
class _Candidates:
    values: dict[str, dict[str, object]]

    def save(self, candidate):
        self.values[str(candidate["id"])] = dict(candidate)
        return dict(candidate)

    def get(self, candidate_id):
        item = self.values.get(candidate_id)
        return dict(item) if item else None

    def list_by_project(self, project_id):
        return tuple(item for item in self.values.values() if item["project_id"] == project_id)


@dataclass
class _Published:
    values: tuple[dict[str, object], ...] = ()

    def published_memory_for_project(self, _project_id):
        return self.values


@dataclass
class _SkillRuntime:
    calls: list[dict[str, object]]
    fail: bool = False

    def propose(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise ValueError("authoritative receipt rejected")
        return {"status": "pending_review", "proposal_id": "skill-proposal-alpha"}


def _message(message_id: str, role: str, content: str) -> CompanionMessage:
    return CompanionMessage(
        message_id=message_id, request_id="request-alpha", session_id="session-alpha", context_epoch=1,
        project_id="project-alpha", role=role, status="completed", content=content,
        created_at="2026-09-01T10:00:00+00:00", provider_mode="local", revision=1, memory_review={},
    )


def _episode(*, assistant: str = "保留 SQLite 作为项目唯一写入权威，并要求人工审核长期变更。") -> ConversationEpisodeProjection:
    return ConversationEpisodeProjection(
        episode_id="episode-alpha", agent_id="companion.chat", project_id="project-alpha",
        session_id="session-alpha", context_epoch=1, request_id="request-alpha",
        user_message_id="message-user", user_message_revision=1,
        assistant_message_id="message-assistant", assistant_message_revision=1,
        summary=f"用户：请记录这个长期决定\n助手：{assistant}",
        occurred_at="2026-09-01T10:00:00+00:00",
    )


def _coordinator(tmp_path: Path, *, episode: ConversationEpisodeProjection | None = None, skill_runtime=None):
    active_episode = episode or _episode()
    messages = _Messages({
        "message-user": _message("message-user", "user", "请记录这个长期决定"),
        "message-assistant": _message("message-assistant", "assistant", active_episode.summary.split("助手：", 1)[1]),
    })
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "legacy")
    candidates = _Candidates({})
    coordinator = TaskCompletionLearningCoordinator(
        episodes=_EpisodeReader(active_episode), messages=messages, candidates=candidates,
        memory_diary=ObjectStoreMemoryDistillationDiaryRepository(store), published_memory=_Published(),
        skill_runtime=skill_runtime, skill_journal=ObjectStoreSkillLearningJournal(store),
        now=lambda: "2026-09-01T12:00:00+00:00",
    )
    return coordinator, candidates, store


def test_completed_private_episode_creates_only_pending_memory_candidate_and_replays(tmp_path: Path) -> None:
    coordinator, candidates, store = _coordinator(tmp_path)
    completion = CompletedCompanionEpisode(
        completion_id="completion-alpha", project_id="project-alpha",
        session_id="session-alpha", episode_id="episode-alpha",
    )

    first = coordinator.on_completed_companion_episode(completion)
    replay = coordinator.on_completed_companion_episode(completion)

    assert first.status == "pending_review"
    assert first.candidate_id and candidates.values[first.candidate_id]["review"]["auto_promote_allowed"] is False
    assert replay == CompletionLearningOutcome(
        "memory", "pending_review", candidate_id=first.candidate_id,
        reason_codes=("eligible_grounded_episode", "requires_user_review"), replayed=True,
    )
    diary = store.read("memory_distillation_diary", "memory-distillation-diary-distill-auto-completion-alpha")
    assert diary is not None and diary["content_included"] is False and "proposed_content" not in str(diary)


def test_memory_intake_skips_scope_drift_and_sensitive_material_without_publishing(tmp_path: Path) -> None:
    coordinator, candidates, _store = _coordinator(tmp_path)
    wrong_scope = coordinator.on_completed_companion_episode(CompletedCompanionEpisode(
        completion_id="completion-wrong", project_id="project-alpha", session_id="session-other", episode_id="episode-alpha",
    ))
    assert wrong_scope.status == "skipped"
    assert candidates.values == {}

    sensitive = _episode(assistant="api_key=super-secret-value 应长期保存")
    coordinator, candidates, _store = _coordinator(tmp_path / "sensitive", episode=sensitive)
    result = coordinator.on_completed_companion_episode(CompletedCompanionEpisode(
        completion_id="completion-sensitive", project_id="project-alpha", session_id="session-alpha", episode_id="episode-alpha",
    ))
    assert result.status == "skipped" and result.reason_codes == ("redaction_required",)
    assert candidates.values == {}


def test_companion_adapter_only_reads_the_current_private_project_session() -> None:
    episode = _episode()
    repository = SimpleNamespace(
        get_session=lambda session_id: SimpleNamespace(project_id="project-alpha") if session_id == "session-alpha" else None,
        get_conversation_episode=lambda **kwargs: episode if kwargs == {
            "project_id": "project-alpha", "session_id": "session-alpha",
            "episode_id": "episode-alpha",
        } else None,
    )
    reader = CompanionRepositoryCompletedEpisodeReader(repository)

    assert reader.completed_episode(
        project_id="project-alpha", session_id="session-alpha", episode_id="episode-alpha",
    ) == episode
    assert reader.completed_episode(
        project_id="project-beta", session_id="session-alpha", episode_id="episode-alpha",
    ) is None


def _evidence() -> ExplicitSkillLearningEvidence:
    return ExplicitSkillLearningEvidence(
        resolution_id="resolution-alpha", skill_id="review-method", expected_fingerprint="a" * 64,
        reusable_signal={"kind": "explicit_correction", "evidence": "审查已明确纠正了重复出现的排序错误。"},
        proposed_content={"summary": "先检查排序证据。", "instructions": "在提交前检查权威来源与排序合同。"},
    )


def test_turn_intake_requires_explicit_signal_and_persists_no_body_idempotency_record(tmp_path: Path) -> None:
    runtime = _SkillRuntime([])
    coordinator, _candidates, store = _coordinator(tmp_path, skill_runtime=runtime)
    completion = CompletedTurn("completion-skill", "project-alpha", "turn-alpha")

    assert coordinator.on_completed_turn(completion).reason_codes == ("no_explicit_signal",)
    first = coordinator.on_completed_turn(completion, _evidence())
    replay = coordinator.on_completed_turn(completion, _evidence())

    assert first.status == "pending_review" and first.proposal_id == "skill-proposal-alpha"
    assert replay.replayed is True and len(runtime.calls) == 1
    journal = store.read("task_completion_skill_learning", "skill-learning-completion-skill")
    assert journal is not None and journal["skill_file_modified"] is False
    assert "审查已明确" not in str(journal) and "排序合同" not in str(journal)


def test_turn_intake_never_propagates_learning_failure_into_terminal_path(tmp_path: Path) -> None:
    runtime = _SkillRuntime([], fail=True)
    coordinator, _candidates, _store = _coordinator(tmp_path, skill_runtime=runtime)

    result = coordinator.on_completed_turn(CompletedTurn("completion-failed", "project-alpha", "turn-alpha"), _evidence())

    assert result == CompletionLearningOutcome("skill", "failed", reason_codes=("intake_failed",))
    assert len(runtime.calls) == 1
