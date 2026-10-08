from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from core.product_core.project_memory_recall import (
    ProjectMemoryRecallError,
    ProjectMemoryRecallResult,
)
from core.product_core.workbench_direct_question import (
    AnswerWorkbenchDirectQuestion,
    WorkbenchDirectQuestionError,
    serialize_workbench_direct_question,
)
from core.storage_provider import JsonObjectStore


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "core-contracts" / "rebuild"


class _FakeRecall:
    """Fake recall port for controlling recall outcomes in unit tests."""

    def __init__(
        self,
        *,
        result: ProjectMemoryRecallResult | None = None,
        error: ProjectMemoryRecallError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        created_at: str | None = None,
    ) -> ProjectMemoryRecallResult:
        self.calls.append(
            {"project_id": project_id, "query": query, "created_at": created_at}
        )
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise ProjectMemoryRecallError("fake recall has no result configured")
        return self._result




def _recall_result(
    *,
    hit_count: int = 1,
    evidence_hits: tuple[dict[str, object], ...] = (),
) -> ProjectMemoryRecallResult:
    return ProjectMemoryRecallResult(
        project_id="default",
        skill_id="skill-default",
        request_id="recall-request-test",
        result_id="recall-result-test",
        hit_count=hit_count,
        evidence_hits=evidence_hits,
    )


def _deep_recall_result() -> SimpleNamespace:
    return SimpleNamespace(
        project_id="default",
        skill_id="skill-default",
        request_id="deep-recall-request",
        result_id="deep-recall-result",
        hit_count=1,
        evidence_hits=(
            {
                "hit_id": "hit-r1",
                "layer": "l3_series_memory",
                "object_id": "series-memory-default",
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:architecture"}
                ],
                "snippet": "R1 当前系列概况。",
                "explanation": "当前系列摘要。",
                "score": 0.9,
                "trust_status": "user_confirmed",
            },
        ),
        progressive_trace={
            "trace_version": "progressive-direct-question-v3",
            "safety": {"content_recorded": False},
        },
        ephemeral_context_bundle={
            "bundle_version": "progressive-recall-context-v1",
            "items": [
                {
                    "layer": "r2_structured_content",
                    "object_id": "document-1#block-1",
                    "content": "R2-PERSISTENCE-CANARY 结构化详细资料。",
                    "excerpt_hash": "a" * 64,
                    "source_refs": [
                        {
                            "source_id": "source-1",
                            "locator": "section:architecture",
                            "source_content_hash": "b" * 64,
                        }
                    ],
                },
                {
                    "layer": "r3_source_evidence",
                    "object_id": "source-read-1",
                    "content": "R3-PERSISTENCE-CANARY 原始来源正文。",
                    "excerpt_hash": "c" * 64,
                    "source_refs": [
                        {
                            "source_id": "source-1",
                            "locator": "section:architecture",
                            "source_content_hash": "b" * 64,
                        }
                    ],
                },
            ],
            "safety": {
                "ephemeral": True,
                "persistence_allowed": False,
                "logging_allowed": False,
                "provider_egress_allowed": False,
            },
        },
    )


def _use_case(
    tmp_path: Path,
    *,
    recall: _FakeRecall | None = None,
    recall_project_id: str = "default",
    provider_unavailable_reason: str = "",
) -> tuple[AnswerWorkbenchDirectQuestion, JsonObjectStore]:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    use_case = AnswerWorkbenchDirectQuestion(
        store,
        namespace_id="default",
        recall=recall,
        recall_project_id=recall_project_id,
        provider_unavailable_reason=provider_unavailable_reason,
    )
    return use_case, store


def test_direct_question_without_recall_returns_disabled_status(tmp_path: Path) -> None:
    use_case, _ = _use_case(tmp_path, recall=None)

    result = use_case.execute(question="本地问答不入库。", created_at="2026-07-04T10:00:00+08:00")

    assert result.status == "answered"
    assert result.recall_status == "disabled"
    assert result.recall_request_id is None
    assert result.recall_result_id is None
    assert result.evidence_refs == ()
    assert result.evidence_count == 0


def test_direct_question_with_recall_returns_recalled_status_and_evidence_refs(
    tmp_path: Path,
) -> None:
    hits = (
        {
            "hit_id": "hit-skill",
            "layer": "l3_project_skill",
            "object_id": "skill-default",
            "source_refs": [{"source_id": "source-1", "locator": "char:0-40"}],
            "snippet": "默认项目 Skill 概述。",
            "explanation": "当前项目 Skill 是项目问答的首个证据层。",
            "score": 1.0,
            "token_estimate": 20,
            "trust_status": "system_generated",
        },
        {
            "hit_id": "hit-atom",
            "layer": "l1_atom",
            "object_id": "atom-default",
            "source_refs": [{"source_id": "source-1", "locator": "char:0-60"}],
            "snippet": "默认项目已发布的事实记忆。",
            "explanation": "L1 Atom 补充当前项目可追溯事实。",
            "score": 0.7,
            "token_estimate": 18,
            "trust_status": "system_generated",
        },
    )
    fake = _FakeRecall(result=_recall_result(hit_count=2, evidence_hits=hits))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="默认项目下一版应该先改哪里？", created_at="2026-07-04T10:00:00+08:00")

    assert result.recall_status == "recalled"
    assert result.recall_request_id == "recall-request-test"
    assert result.recall_result_id == "recall-result-test"
    assert result.evidence_count == 2
    assert len(result.evidence_refs) == 2
    assert result.evidence_refs[0]["layer"] == "l3_project_skill"
    assert result.evidence_refs[0]["object_id"] == "skill-default"
    assert result.evidence_refs[0]["source_refs"] == [
        {"source_id": "source-1", "locator": "char:0-40"}
    ]
    assert result.evidence_refs[1]["layer"] == "l1_atom"
    assert result.evidence_refs[1]["snippet"] == "默认项目已发布的事实记忆。"
    assert "L3 Project Skill：默认项目 Skill 概述。" in result.answer_preview
    assert "L1 Atom：默认项目已发布的事实记忆。" in result.answer_preview
    assert "只归纳当前已发布证据" in result.answer_preview
    assert fake.calls == [
        {
            "project_id": "default",
            "query": "默认项目下一版应该先改哪里？",
            "created_at": "2026-07-04T10:00:00+08:00",
        }
    ]




def test_direct_question_no_evidence_never_calls_provider(tmp_path: Path) -> None:
    use_case, _ = _use_case(
        tmp_path,
        recall=_FakeRecall(result=_recall_result(hit_count=0, evidence_hits=())),
    )

    result = use_case.execute(question="没有证据时不要猜。")

    assert result.provider_call_performed is False
    assert result.provider_status == "not_attempted_no_evidence"
    assert result.qa_mode == "direct_local_answer"






def test_deep_evidence_is_not_sent_without_explicit_egress_authorization(
    tmp_path: Path,
) -> None:
    store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    use_case = AnswerWorkbenchDirectQuestion(
        store,
        recall=_FakeRecall(result=_deep_recall_result()),
        recall_project_id="default",
    )

    result = use_case.execute(question="请核对原文并给出详细分析。")
    payload = serialize_workbench_direct_question(result)
    record = store.read("workbench_direct_questions", result.question_id)

    assert result.provider_status == "fallback_not_configured"
    assert result.provider_call_performed is False
    assert result.provider_egress_authorized is False
    assert "R2-PERSISTENCE-CANARY" not in result.answer_preview
    assert "R3-PERSISTENCE-CANARY" not in result.answer_preview
    assert payload["deep_evidence"]["count"] == 2
    assert payload["privacy"]["mode"] == "local_only"
    persisted = json.dumps(record, ensure_ascii=False, sort_keys=True)
    assert "R2-PERSISTENCE-CANARY" not in persisted
    assert "R3-PERSISTENCE-CANARY" not in persisted


def test_local_answer_does_not_copy_deep_evidence_into_answer_or_record(
    tmp_path: Path,
) -> None:
    use_case, store = _use_case(
        tmp_path,
        recall=_FakeRecall(result=_deep_recall_result()),
    )

    result = use_case.execute(question="请核对原文并给出详细分析。")
    persisted = json.dumps(
        store.read("workbench_direct_questions", result.question_id),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert result.provider_status == "fallback_not_configured"
    assert "R2-PERSISTENCE-CANARY" not in result.answer_preview
    assert "R3-PERSISTENCE-CANARY" not in result.answer_preview
    assert "R2-PERSISTENCE-CANARY" not in persisted
    assert "R3-PERSISTENCE-CANARY" not in persisted


def test_deep_evidence_replay_revalidates_current_ephemeral_bundle(
    tmp_path: Path,
) -> None:
    first_result = _deep_recall_result()
    recall = _FakeRecall(result=first_result)
    use_case, store = _use_case(tmp_path, recall=recall)

    first = use_case.execute(question="请核对原文并给出详细分析。")
    first_record = store.read("workbench_direct_questions", first.question_id)
    first_result.ephemeral_context_bundle["items"][0]["content"] = (
        "R2-UPDATED-CANARY 当前权威已更新。"
    )
    first_result.ephemeral_context_bundle["items"][0]["excerpt_hash"] = "d" * 64
    second = use_case.execute(question="请核对原文并给出详细分析。")
    second_payload = serialize_workbench_direct_question(second)
    second_record = store.read("workbench_direct_questions", second.question_id)

    assert second.replayed is False
    assert any(
        "R2-UPDATED-CANARY" in item["snippet"]
        for item in second_payload["evidence_items"]
    )
    assert first_record != second_record
    persisted = json.dumps(second_record, ensure_ascii=False, sort_keys=True)
    assert "R2-UPDATED-CANARY" not in persisted


def test_direct_question_reports_unavailable_route_without_attempting_provider(tmp_path: Path) -> None:
    hit = {
        "hit_id": "hit-provider", "layer": "l1_atom", "object_id": "atom-default",
        "source_refs": [], "snippet": "已发布事实。", "explanation": "事实证据。",
        "score": 0.8, "token_estimate": 6, "trust_status": "user_confirmed",
    }
    use_case, _ = _use_case(
        tmp_path,
        recall=_FakeRecall(result=_recall_result(hit_count=1, evidence_hits=(hit,))),
        provider_unavailable_reason="ModelRouteRuntimeError",
    )

    result = use_case.execute(question="路由不可用时回落。")

    assert result.provider_call_performed is False
    assert result.provider_status == "fallback_provider_unavailable"
    assert "remote_model_provider_execution" in result.blocked_operations


def test_direct_question_with_recall_returning_no_hits_returns_insufficient_evidence(
    tmp_path: Path,
) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=0, evidence_hits=()))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="没有任何已发布记忆可召回。", created_at="2026-07-04T10:00:00+08:00")

    assert result.recall_status == "insufficient_evidence"
    assert result.recall_request_id == "recall-request-test"
    assert result.recall_result_id == "recall-result-test"
    assert result.evidence_refs == ()
    assert result.evidence_count == 0
    assert "没有足够的已发布本地证据" in result.answer_preview
    assert "请先审核并发布相关项目记忆" in result.answer_preview


def test_direct_question_bounds_evidence_answer_snippets(tmp_path: Path) -> None:
    hits = tuple(
        {
            "hit_id": f"hit-{index}",
            "layer": "l1_atom",
            "object_id": f"atom-{index}",
            "source_refs": [{"source_id": "source-1", "locator": f"char:{index}-{index + 1}"}],
            "snippet": f"证据 {index} " + ("很长 " * 100),
            "explanation": "L1 Atom。",
            "score": 0.7,
            "token_estimate": 100,
            "trust_status": "user_confirmed",
        }
        for index in range(8)
    )
    use_case, _ = _use_case(
        tmp_path,
        recall=_FakeRecall(result=_recall_result(hit_count=len(hits), evidence_hits=hits)),
    )

    result = use_case.execute(question="有界回答。", created_at="2026-07-04T10:00:00+08:00")

    assert len(result.answer_preview) <= 800
    assert result.answer_preview.count("- L1 Atom：") == 6
    assert "证据 5" in result.answer_preview
    assert "证据 6" not in result.answer_preview


def test_direct_question_with_recall_error_returns_skipped_status(tmp_path: Path) -> None:
    fake = _FakeRecall(error=ProjectMemoryRecallError("Project Skill not found: default"))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="项目还没有 Skill。", created_at="2026-07-04T10:00:00+08:00")

    assert result.recall_status == "skipped"
    assert result.recall_request_id is None
    assert result.recall_result_id is None
    assert result.evidence_refs == ()
    assert result.evidence_count == 0


def test_direct_question_uses_configured_recall_project_id(tmp_path: Path) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=1, evidence_hits=()))
    use_case, _ = _use_case(tmp_path, recall=fake, recall_project_id="project-alpha")

    use_case.execute(question="项目 alpha 的上下文。", created_at="2026-07-04T10:00:00+08:00")

    assert fake.calls[0]["project_id"] == "project-alpha"


def test_direct_question_question_identity_is_project_scoped(tmp_path: Path) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=0))
    first, _ = _use_case(
        tmp_path,
        recall=fake,
        recall_project_id="project-alpha",
    )
    second, _ = _use_case(
        tmp_path,
        recall=fake,
        recall_project_id="project-beta",
    )

    alpha = first.execute(question="同一个问题")
    beta = second.execute(question="同一个问题")

    assert alpha.question_id != beta.question_id


def test_default_project_question_identity_remains_upgrade_compatible(
    tmp_path: Path,
) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=0))
    use_case, _ = _use_case(tmp_path, recall=fake)
    question = "同一个默认问题"
    expected = hashlib.sha256(f"default\n{question}".encode("utf-8")).hexdigest()[:16]

    result = use_case.execute(question=question)

    assert result.question_id == f"workbench-direct-question-{expected}"


def test_direct_question_never_creates_source_or_library_item(tmp_path: Path) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=1, evidence_hits=()))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="确保不写入资料库。", created_at="2026-07-04T10:00:00+08:00")

    assert result.knowledge_base_write is False
    assert result.source_created is False
    assert result.job_created is False
    assert result.library_item_created is False
    assert result.memory_publication_state == "not_published"
    assert "source_creation" in result.blocked_operations
    assert "long_term_memory_publication" in result.blocked_operations
    assert "library_item_creation" in result.blocked_operations


def test_direct_question_record_persists_recall_fields(tmp_path: Path) -> None:
    hits = (
        {
            "hit_id": "hit-skill",
            "layer": "l3_project_skill",
            "object_id": "skill-default",
            "source_refs": [{"source_id": "source-1", "locator": "char:0-40"}],
            "snippet": "默认项目 Skill 概述。",
            "explanation": "当前项目 Skill 是项目问答的首个证据层。",
            "score": 1.0,
            "token_estimate": 20,
            "trust_status": "system_generated",
        },
    )
    fake = _FakeRecall(result=_recall_result(hit_count=1, evidence_hits=hits))
    use_case, store = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="持久化召回字段。", created_at="2026-07-04T10:00:00+08:00")

    record = store.read("workbench_direct_questions", result.question_id)
    assert record is not None
    assert record["recall_status"] == "recalled"
    assert record["recall_request_id"] == "recall-request-test"
    assert record["recall_result_id"] == "recall-result-test"
    assert record["evidence_count"] == 1
    persisted_refs = record["evidence_refs"]
    assert isinstance(persisted_refs, list)
    assert persisted_refs[0]["layer"] == "l3_project_skill"
    assert record["source_created"] is False
    assert record["knowledge_base_write"] is False


def test_serialize_workbench_direct_question_includes_recall_fields(tmp_path: Path) -> None:
    fake = _FakeRecall(result=_recall_result(hit_count=1, evidence_hits=()))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="序列化字段。", created_at="2026-07-04T10:00:00+08:00")
    payload = serialize_workbench_direct_question(result)

    assert payload["status"] == "answered"
    assert payload["recall_status"] == "recalled"
    assert payload["recall_request_id"] == "recall-request-test"
    assert payload["recall_result_id"] == "recall-result-test"
    assert payload["evidence_count"] == 1
    assert isinstance(payload["evidence_refs"], list)
    assert payload["memory_publication_state"] == "not_published"
    assert "source_creation" in payload["blocked_operations"]


def test_question_response_contract_validates_real_serializer_and_fixtures(tmp_path: Path) -> None:
    schema = json.loads((CONTRACT_ROOT / "question_response.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    fixture_root = CONTRACT_ROOT / "fixtures" / "question_response"
    for path in sorted(fixture_root.glob("valid-*.json")):
        assert validator.is_valid(json.loads(path.read_text(encoding="utf-8"))), path.name
    assert not validator.is_valid(json.loads((fixture_root / "invalid-path-leak.json").read_text(encoding="utf-8")))

    hit = {
        "hit_id": "hit-contract", "layer": "l1_atom", "object_id": "atom-contract",
        "source_refs": [{"source_id": "source-contract", "locator": "char:0-20"}],
        "snippet": "可验证证据", "explanation": "合同回归", "score": 0.8,
        "token_estimate": 8, "trust_status": "system_generated",
    }
    use_case, _ = _use_case(tmp_path, recall=_FakeRecall(result=_recall_result(hit_count=1, evidence_hits=(hit,))))
    assert validator.is_valid(serialize_workbench_direct_question(use_case.execute(question="合同是否真实？")))


def test_direct_question_evidence_refs_strip_secrets(tmp_path: Path) -> None:
    """Recall hits may carry technical metadata, but evidence_refs must not leak secrets."""
    hits = (
        {
            "hit_id": "hit-atom",
            "layer": "l1_atom",
            "object_id": "atom-default",
            "source_refs": [{"source_id": "source-1", "locator": "char:0-60"}],
            "snippet": "正常片段。",
            "explanation": "L1 Atom。",
            "score": 0.7,
            "token_estimate": 18,
            "trust_status": "system_generated",
            # Hypothetical secret-like fields that should NOT appear in evidence_refs:
            "api_key": "sk-leaked-1234567890",
            "authorization": "Bearer leaked-token",
            "cookie": "session=leaked",
        },
    )
    fake = _FakeRecall(result=_recall_result(hit_count=1, evidence_hits=hits))
    use_case, _ = _use_case(tmp_path, recall=fake)

    result = use_case.execute(question="隐私检查。", created_at="2026-07-04T10:00:00+08:00")

    payload = serialize_workbench_direct_question(result)
    body = repr(payload)
    assert "sk-leaked" not in body
    assert "Bearer leaked-token" not in body
    assert "session=leaked" not in body
    # evidence_ref keeps only contract evidence fields, never provider secrets.
    ref = result.evidence_refs[0]
    assert set(ref.keys()) <= {"layer", "object_id", "explanation", "source_refs", "snippet", "score", "quality"}


def test_direct_question_rejects_empty_question(tmp_path: Path) -> None:
    use_case, _ = _use_case(tmp_path, recall=None)

    try:
        use_case.execute(question="   ")
    except WorkbenchDirectQuestionError as error:
        assert "question is required" in str(error)
    else:
        raise AssertionError("empty question should be rejected")


def test_direct_question_rejects_non_string_question(tmp_path: Path) -> None:
    use_case, _ = _use_case(tmp_path, recall=None)

    try:
        use_case.execute(question=123)  # type: ignore[arg-type]
    except WorkbenchDirectQuestionError as error:
        assert "question must be a string" in str(error)
    else:
        raise AssertionError("non-string question should be rejected")


def test_direct_question_rejects_overlong_question(tmp_path: Path) -> None:
    use_case, _ = _use_case(tmp_path, recall=None)

    try:
        use_case.execute(question="x" * 4001)
    except WorkbenchDirectQuestionError as error:
        assert "too long" in str(error)
    else:
        raise AssertionError("overlong question should be rejected")
