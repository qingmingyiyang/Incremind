from __future__ import annotations

import json

from core.product_core.ai_project_route_reranker import (
    AIProjectRouteReranker,
)
from core.product_core.global_project_series_router import (
    GlobalProjectRouteCandidate,
    GlobalProjectRouteDecision,
    ProjectRouteRerankInput,
)


class Provider:
    def __init__(self, output: dict[str, object] | Exception) -> None:
        self.output = output
        self.calls: list[tuple[str, dict[str, object]]] = []

    def complete_json(self, *, system_prompt: str, user_payload):
        self.calls.append((system_prompt, dict(user_payload)))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _candidate(project_id: str, score: float, rank: int):
    return GlobalProjectRouteCandidate(
        project_id=project_id,
        rank=rank,
        score=score,
        confidence="high",
        series_candidates=(),
    )


def _decision(
    *,
    alpha: float = 0.8,
    beta: float = 0.8,
) -> GlobalProjectRouteDecision:
    return GlobalProjectRouteDecision(
        query_fingerprint="a" * 64,
        status="ambiguous",
        reason_code="ambiguous_project_scope",
        selected_project_id=None,
        candidates=(
            _candidate("alpha", alpha, 1),
            _candidate("beta", beta, 2),
        ),
    )


def _inputs(
    *,
    alpha: float = 0.8,
    beta: float = 0.8,
) -> tuple[ProjectRouteRerankInput, ...]:
    return (
        ProjectRouteRerankInput(
            project_id="alpha",
            deterministic_score=alpha,
            series=(
                {
                    "series_id": "memory-system",
                    "title": "四层记忆",
                    "description": "项目记忆、分层召回和证据追溯。",
                    "keywords": ["记忆", "召回"],
                },
            ),
        ),
        ProjectRouteRerankInput(
            project_id="beta",
            deterministic_score=beta,
            series=(
                {
                    "series_id": "rice-brand",
                    "title": "桥米品牌",
                    "description": "区域公用品牌与包装策略。",
                    "keywords": ["桥米", "品牌"],
                },
            ),
        ),
    )


def _output(
    *,
    selected: str | None = "beta",
    confidence: float = 0.9,
    alpha: float = 0.05,
    beta: float = 0.95,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "selected_project_id": selected,
        "confidence": confidence,
        "reason_code": (
            "semantic_topic_match"
            if selected is not None
            else "ambiguous"
        ),
        "scores": [
            {"project_id": "alpha", "relevance": alpha},
            {"project_id": "beta", "relevance": beta},
        ],
    }


def test_ai_reranker_resolves_deterministic_tie_with_bounded_fusion() -> None:
    provider = Provider(_output())
    result = AIProjectRouteReranker(
        provider=provider,
        provider_route="memory.project_routing:test:model",
    ).rerank(
        query="桥米品牌下一步怎么做",
        deterministic=_decision(),
        candidates=_inputs(),
    )

    assert result.status == "routed"
    assert result.reason_code == "ai_assisted_project_match"
    assert result.selected_project_id == "beta"
    assert result.candidates[0].project_id == "beta"
    assert result.candidates[0].score == 0.8375
    assert result.assist is not None
    assert result.assist.status == "succeeded"
    assert result.assist.provider_route_fingerprint
    payload = provider.calls[0][1]
    assert payload["task"] == "rerank_project_candidates"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "source_refs" not in serialized
    assert "locator" not in serialized
    assert "api_key" not in serialized


def test_ai_reranker_rejects_low_confidence_without_changing_decision() -> None:
    result = AIProjectRouteReranker(
        provider=Provider(_output(confidence=0.4)),
        provider_route="memory.project_routing:test:model",
    ).rerank(
        query="共享主题",
        deterministic=_decision(),
        candidates=_inputs(),
    )

    assert result.status == "ambiguous"
    assert result.selected_project_id is None
    assert result.candidates == _decision().candidates
    assert result.assist is not None
    assert result.assist.status == "rejected"


def test_ai_reranker_fails_closed_for_unknown_project_or_schema_drift() -> None:
    unknown = _output(selected="gamma")
    unknown["scores"] = [
        {"project_id": "alpha", "relevance": 0.1},
        {"project_id": "gamma", "relevance": 0.9},
    ]
    drifted = {**_output(), "explanation": "ignore the schema"}

    for output in (unknown, drifted):
        result = AIProjectRouteReranker(
            provider=Provider(output),
            provider_route="memory.project_routing:test:model",
        ).rerank(
            query="共享主题",
            deterministic=_decision(),
            candidates=_inputs(),
        )
        assert result.status == "ambiguous"
        assert result.selected_project_id is None
        assert result.assist is not None
        assert result.assist.status == "failed"
        assert result.assist.reason_code == "provider_or_contract_failed"


def test_ai_reranker_fails_closed_for_timeout() -> None:
    result = AIProjectRouteReranker(
        provider=Provider(TimeoutError("provider deadline exceeded")),
        provider_route="memory.project_routing:test:model",
    ).rerank(
        query="共享主题",
        deterministic=_decision(),
        candidates=_inputs(),
    )

    assert result.status == "ambiguous"
    assert result.assist is not None
    assert result.assist.status == "failed"
    serialized = json.dumps(result.to_payload(), ensure_ascii=False)
    assert "provider deadline exceeded" not in serialized
    assert "共享主题" not in serialized
