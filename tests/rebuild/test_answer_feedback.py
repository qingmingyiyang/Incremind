from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_answer_feedback_from_reviewed_candidate
from core.document_engine import ObjectStoreDocumentRepository
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import (
    AnswerFeedbackError,
    CreateAnswerFeedbackFromReviewedCandidate,
    CreateAnswerModelRequestFromRecallResult,
    CreateDocumentFromModelResult,
    CreateMemoryCandidateFromModelResult,
)
from core.search_and_recall import (
    ObjectStoreRecallIndex,
    ObjectStoreRecallRepository,
    RecallIndexEntry,
    RecallQuery,
)
from core.storage_provider import JsonObjectStore, ObjectStoreAnswerFeedbackRepository
from tests.rebuild.memory_candidate_saga_review_testlib import review_candidate_to_staging
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _reviewed_candidate_path(tmp_path: Path) -> tuple[JsonObjectStore, str]:
    object_store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    recall_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-project-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="项目回答必须先使用项目 Skill 和可追溯证据。",
                source_refs=("source-alpha#char:0-30",),
                trust_status="user_confirmed",
                base_score=0.72,
            ),
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="反馈必须连接 Recall、回答、文档和记忆候选。",
                source_refs=("source-alpha#char:31-90",),
                trust_status="system_generated",
                base_score=0.64,
            ),
        ),
        source="answer-feedback-test",
        rebuilt_at="2026-06-30T23:00:00+08:00",
    )
    recall_request = recalls.create_project_default_request(
        project_id="project-alpha",
        query="如何记录这次回答反馈？",
        project_skill_id="skill-project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        created_at="2026-06-30T23:01:00+08:00",
    )
    hits = recall_index.recall(
        RecallQuery(
            text=str(recall_request["query"]),
            project_id=str(recall_request["project_id"]),
            layers=tuple(recall_request["layers"]),
            allowed_trust_statuses=tuple(recall_request["trust_filter"]["include"]),
            limit=12,
        )
    )
    recall_result = recalls.create_result_from_hits(
        request_id=str(recall_request["id"]),
        hits=hits,
        created_at="2026-06-30T23:02:00+08:00",
    )
    answer_request = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
    ).execute(
        str(recall_result["id"]),
        created_at="2026-06-30T23:03:00+08:00",
    )
    model_result = model_results.create_completed_local_result(
        request_id=answer_request.model_request_id,
        output_text="反馈应记录候选审核结果并保留同一组 source_refs。",
        input_tokens=80,
        output_tokens=20,
        started_at="2026-06-30T23:04:00+08:00",
        completed_at="2026-06-30T23:04:01+08:00",
        elapsed_ms=1000,
    )
    document_result = CreateDocumentFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        documents=documents,
    ).execute(
        str(model_result["id"]),
        title="Answer Feedback Smoke Document",
    )
    candidate_result = CreateMemoryCandidateFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        candidates=candidates,
    ).execute(
        str(model_result["id"]),
        document_id=document_result.document_id,
        document_revision=document_result.document_revision,
        created_at="2026-06-30T23:05:00+08:00",
    )
    review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_result.candidate_id,
        review_reason="用户确认该反馈路径可以形成草稿事实。",
        reviewed_at="2026-06-30T23:06:00+08:00",
        tags=("feedback", "recall", "candidate"),
    )
    return object_store, candidate_result.candidate_id


def test_answer_feedback_links_reviewed_candidate_to_recall_answer_chain(tmp_path: Path) -> None:
    object_store, candidate_id = _reviewed_candidate_path(tmp_path)
    feedback_repo = ObjectStoreAnswerFeedbackRepository(object_store)
    feedback_use_case = CreateAnswerFeedbackFromReviewedCandidate(
        recalls=ObjectStoreRecallRepository(object_store),
        model_requests=ObjectStoreModelRequestRepository(object_store),
        model_results=ObjectStoreModelResultRepository(object_store),
        documents=ObjectStoreDocumentRepository(object_store),
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
        feedback=feedback_repo,
    )

    result = feedback_use_case.execute(
        candidate_id,
        created_at="2026-06-30T23:07:00+08:00",
    )
    feedback = feedback_repo.get(result.feedback_id)
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)

    assert feedback is not None
    assert candidate is not None
    assert result.project_id == "project-alpha"
    assert result.candidate_status == "promoted"
    assert result.feedback_type == "candidate_promoted_to_draft"
    assert result.source_ref_count == 2
    assert feedback["memory_candidate_id"] == candidate_id
    assert feedback["recall_result_id"] == candidate["provenance"]["recall_result_id"]
    assert feedback["model_request_id"] == candidate["provenance"]["model_request_id"]
    assert feedback["model_result_id"] == candidate["provenance"]["model_result_id"]
    assert feedback["document_id"] == candidate["provenance"]["document_id"]
    assert feedback["candidate_status"] == "promoted"
    assert feedback["review"] == {
        "reviewed_by": "user",
        "reviewed_at": "2026-06-30T23:06:00+08:00",
        "reason": "用户确认该反馈路径可以形成草稿事实。",
    }
    assert [ref["kind"] for ref in feedback["input_refs"]] == [
        "recall_result",
        "model_request",
        "model_result",
        "document",
        "memory_candidate",
    ]
    assert feedback["source_refs"] == candidate["source_refs"]
    assert validate_contract_instance("answer_feedback.schema.json", _schema("answer_feedback.schema.json"), feedback) == []
    assert feedback_repo.list_by_recall_result(str(feedback["recall_result_id"])) == (feedback,)
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_answer_feedback_rejects_pending_candidate(tmp_path: Path) -> None:
    object_store, candidate_id = _reviewed_candidate_path(tmp_path)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    candidate = dict(candidates.get(candidate_id) or {})
    candidate["status"] = "pending_review"
    candidate["review"] = {
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "reason": "reset for guard",
        "reviewed_by": None,
        "reviewed_at": None,
    }

    with pytest.raises(AnswerFeedbackError, match="reviewed"):
        CreateAnswerFeedbackFromReviewedCandidate(
            recalls=ObjectStoreRecallRepository(object_store),
            model_requests=ObjectStoreModelRequestRepository(object_store),
            model_results=ObjectStoreModelResultRepository(object_store),
            documents=ObjectStoreDocumentRepository(object_store),
            candidates=_CandidateStub(candidate),
            feedback=ObjectStoreAnswerFeedbackRepository(object_store),
        ).execute(candidate_id)


def test_answer_feedback_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store, candidate_id = _reviewed_candidate_path(tmp_path)
    feedback_use_case = build_answer_feedback_from_reviewed_candidate(ROOT, runtime_root=tmp_path)

    result = feedback_use_case.execute(
        candidate_id,
        created_at="2026-06-30T23:08:00+08:00",
    )
    feedback = ObjectStoreAnswerFeedbackRepository(object_store).get(result.feedback_id)

    assert feedback is not None
    assert feedback["candidate_status"] == "promoted"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "answer_feedback").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


class _CandidateStub:
    def __init__(self, candidate: dict[str, object]) -> None:
        self._candidate = candidate

    def get(self, candidate_id: str) -> dict[str, object]:
        return dict(self._candidate)
