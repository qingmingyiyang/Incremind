from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_global_project_router_reads_only_current_lightweight_projections() -> None:
    source = (
        ROOT
        / "src"
        / "core"
        / "product_core"
        / "global_project_series_router.py"
    ).read_text(encoding="utf-8")

    assert ".manifests()" in source
    assert ".load_current_for_generation(" in source
    assert "route_progressive_memory_r0(" in source
    for forbidden in (
        ".load(project_id",
        "read_structured",
        "read_source_evidence",
        "complete_json(",
        "answer_provider",
        "team_memory",
        "httpx",
        "requests.",
    ):
        assert forbidden not in source


def test_direct_question_resolves_project_before_project_scoped_recall() -> None:
    endpoint = (
        ROOT
        / "src"
        / "core"
        / "product_core"
        / "workbench_direct_question_endpoint.py"
    ).read_text(encoding="utf-8")
    route = (ROOT / "src" / "backend" / "api" / "workbench_ai_runtime.py").read_text(encoding="utf-8")

    assert '"project_id" in body' in endpoint
    assert 'decision.status == "ambiguous"' in endpoint
    assert "409," in endpoint
    assert "project_id=project_id.strip()" in endpoint
    assert "CurrentGlobalProjectProjectionCatalog(" in route
    assert "GlobalProjectSeriesRouter(" in route
    assert 'decision.status == "ambiguous"' in route


def test_global_project_route_payload_excludes_query_and_projection_content() -> None:
    source = (
        ROOT
        / "src"
        / "core"
        / "product_core"
        / "global_project_series_router.py"
    ).read_text(encoding="utf-8")
    payload_start = source.index(
        "class GlobalProjectRouteDecision"
    )
    payload_end = source.index(
        "class CurrentGlobalProjectProjectionCatalog",
        payload_start,
    )
    payload_contract = source[payload_start:payload_end]

    for forbidden in (
        '"query"',
        '"title"',
        '"description"',
        '"keywords"',
        '"source_refs"',
        '"locator"',
    ):
        assert forbidden not in payload_contract
