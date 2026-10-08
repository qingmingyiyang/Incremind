from __future__ import annotations

from core.product_core.workbench_direct_question import (
    WorkbenchDirectQuestionResult,
)
from core.product_core.workbench_direct_question_endpoint import (
    ServeWorkbenchDirectQuestionEndpoint,
)
from core.product_core.global_project_series_router import (
    GlobalProjectRouteCandidate,
    GlobalProjectRouteDecision,
    ProjectRouteAssistTrace,
)


def _result(question: str) -> WorkbenchDirectQuestionResult:
    return WorkbenchDirectQuestionResult(
        status="answered",
        question_id="question-1",
        answer_id="answer-1",
        question=question,
        answer_preview="本地回答",
        qa_mode="direct_local_answer",
        knowledge_base_write=False,
        source_created=False,
        job_created=False,
        library_item_created=False,
        memory_publication_state="not_published",
        blocked_operations=(),
    )


def test_endpoint_passes_explicit_project_id_to_answer_use_case() -> None:
    calls: list[dict[str, str]] = []

    def answer_question(*, question: str, project_id: str):
        calls.append({"question": question, "project_id": project_id})
        return _result(question)

    response = ServeWorkbenchDirectQuestionEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/direct-question",
        body={"question": "系列问题", "project_id": "project-a"},
        answer_question=answer_question,
    )

    assert response.status_code == 200
    assert calls == [{"question": "系列问题", "project_id": "project-a"}]


def test_endpoint_defaults_project_and_rejects_empty_scope() -> None:
    calls: list[str] = []

    def answer_question(*, question: str, project_id: str):
        calls.append(project_id)
        return _result(question)

    endpoint = ServeWorkbenchDirectQuestionEndpoint()
    defaulted = endpoint.execute(
        method="POST",
        path=endpoint.endpoint_path,
        body={"question": "默认问题"},
        answer_question=answer_question,
    )
    rejected = endpoint.execute(
        method="POST",
        path=endpoint.endpoint_path,
        body={"question": "越权问题", "project_id": " "},
        answer_question=answer_question,
    )

    assert defaulted.status_code == 200
    assert calls == ["default"]
    assert rejected.status_code == 400


def test_endpoint_auto_routes_missing_project_and_preserves_explicit_scope() -> None:
    calls: list[str] = []

    def answer_question(*, question: str, project_id: str):
        calls.append(project_id)
        return _result(question)

    routed = GlobalProjectRouteDecision(
        query_fingerprint="a" * 64,
        status="routed",
        reason_code="high_confidence_project_match",
        selected_project_id="project-auto",
        candidates=(),
    )
    endpoint = ServeWorkbenchDirectQuestionEndpoint()
    automatic = endpoint.execute(
        method="POST",
        path=endpoint.endpoint_path,
        body={"question": "自动选择项目"},
        answer_question=answer_question,
        resolve_project=lambda _question: routed,
    )
    explicit = endpoint.execute(
        method="POST",
        path=endpoint.endpoint_path,
        body={"question": "显式项目", "project_id": "project-explicit"},
        answer_question=answer_question,
        resolve_project=lambda _question: routed,
    )

    assert automatic.status_code == explicit.status_code == 200
    assert calls == ["project-auto", "project-explicit"]
    assert automatic.body["project_route"]["status"] == "routed"
    assert automatic.body["project_route"]["selected_project_id"] == "project-auto"
    assert explicit.body["project_route"] == {
        "status": "explicit",
        "selected_project_id": "project-explicit",
        "ai_assist": None,
    }


def test_endpoint_returns_actionable_project_ambiguity() -> None:
    candidate = GlobalProjectRouteCandidate(
        project_id="alpha",
        rank=1,
        score=0.91,
        confidence="high",
        series_candidates=(),
    )
    decision = GlobalProjectRouteDecision(
        query_fingerprint="a" * 64,
        status="ambiguous",
        reason_code="ambiguous_project_scope",
        selected_project_id=None,
        candidates=(
            candidate,
            GlobalProjectRouteCandidate(
                project_id="beta",
                rank=2,
                score=0.90,
                confidence="high",
                series_candidates=(),
            ),
        ),
    )
    calls: list[str] = []
    response = ServeWorkbenchDirectQuestionEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/direct-question",
        body={"question": "共享研究"},
        answer_question=lambda **kwargs: calls.append(kwargs["project_id"]),
        resolve_project=lambda _question: decision,
    )

    assert response.status_code == 409
    assert calls == []
    assert response.body["reason"] == "ambiguous_project_scope"
    assert [
        item["project_id"] for item in response.body["candidates"]
    ] == ["alpha", "beta"]
    assert response.body["project_route"]["status"] == "ambiguous"


def test_endpoint_exposes_only_privacy_safe_ai_route_trace() -> None:
    candidate = GlobalProjectRouteCandidate(
        project_id="alpha",
        rank=1,
        score=0.81,
        confidence="high",
        series_candidates=(),
    )
    decision = GlobalProjectRouteDecision(
        query_fingerprint="a" * 64,
        status="routed",
        reason_code="ai_assisted_project_match",
        selected_project_id="alpha",
        candidates=(candidate,),
        assist=ProjectRouteAssistTrace(
            status="succeeded",
            reason_code="semantic_topic_match",
            provider_route_fingerprint="b" * 64,
            elapsed_ms=12.5,
            deterministic_scores=(("alpha", 0.72),),
            ai_scores=(("alpha", 0.95),),
            fused_scores=(("alpha", 0.7775),),
        ),
    )

    response = ServeWorkbenchDirectQuestionEndpoint().execute(
        method="POST",
        path="/api/rebuild/workbench/direct-question",
        body={"question": "PRIVATE-CANARY-QUERY"},
        answer_question=lambda **kwargs: _result(kwargs["question"]),
        resolve_project=lambda _question: decision,
    )

    assert response.status_code == 200
    trace = response.body["project_route"]["ai_assist"]
    assert trace["status"] == "succeeded"
    assert trace["query_recorded"] is False
    assert trace["projection_content_recorded"] is False
    assert "PRIVATE-CANARY-QUERY" not in str(response.body["project_route"])
