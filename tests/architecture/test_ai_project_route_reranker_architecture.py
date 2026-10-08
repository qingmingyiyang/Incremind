from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_ai_project_route_is_a_bounded_optional_second_opinion() -> None:
    reranker = _read(
        "src/core/product_core/ai_project_route_reranker.py"
    )
    router = _read(
        "src/core/product_core/global_project_series_router.py"
    )

    assert "DETERMINISTIC_WEIGHT = 0.75" in reranker
    assert "AI_WEIGHT = 0.25" in reranker
    assert "MIN_PROVIDER_CONFIDENCE = 0.65" in reranker
    assert "MIN_FUSED_SCORE = 0.40" in reranker
    assert "MIN_FUSED_MARGIN = 0.10" in reranker
    assert "candidate project_id values" in reranker
    assert "provider_or_contract_failed" in reranker
    assert '"query_recorded": False' in router
    assert '"projection_content_recorded": False' in router
    assert "len(inputs) < 2 or self._reranker_factory is None" in router
    assert "reranker = self._reranker_factory()" in router


def test_production_workbench_project_tie_requires_user_selection() -> None:
    routes = _read("src/backend/api/routes/product/workbench_compat.py")
    runtime = _read("src/backend/api/workbench_ai_runtime.py")

    assert "_resolve_workbench_project_reranker" not in routes
    assert "GlobalProjectSeriesRouter(" in runtime
    assert 'if decision.status == "ambiguous":' in runtime
    assert "_ambiguous_presentation" in runtime
    assert 'route.get("status") == "ambiguous"' in runtime
    assert "memory.project_routing" not in runtime


def test_explicit_project_scope_bypasses_global_and_ai_routing() -> None:
    endpoint = _read(
        "src/core/product_core/workbench_direct_question_endpoint.py"
    )

    assert "if not explicit_project and resolve_project is not None:" in endpoint
    assert '"status": "explicit"' in endpoint
    assert '"ai_assist": None' in endpoint
