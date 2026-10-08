from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import ProjectionReadResult
from core.product_core.progressive_recall_drilldown import (
    AuthorityEvidenceCandidate,
    EvidenceSourceRef,
    ProgressiveRecallDrilldownError,
    run_progressive_recall_drilldown,
)
from core.product_core.progressive_recall_shadow import (
    SeriesRouteCandidate,
    SeriesRouteDecision,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
FINGERPRINT = "a" * 64
SOURCE_HASH = "b" * 64
CANARY_QUERY = "核对原文 PRIVATE-QUERY-CANARY"


def _projection(*, status: str = "fresh") -> ProjectionReadResult:
    payload = {
        "projection_version": PROJECTION_VERSION,
        "generator_policy_id": GENERATOR_POLICY_ID,
        "project_id": "project-1",
        "authority_fingerprint": FINGERPRINT,
        "status": "ready",
        "r1_items": [
            {
                "series_id": "series-1",
                "project_id": "project-1",
                "authority_fingerprint": FINGERPRINT,
                "status": "ready",
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:one"}
                ],
            }
        ],
    }
    return ProjectionReadResult(
        status=status,
        fallback_to_authority=status != "fresh",
        reason_code="projection_fresh" if status == "fresh" else "projection_stale",
        projection=payload if status == "fresh" else None,
        manifest={},
    )


def _route() -> SeriesRouteDecision:
    return SeriesRouteDecision(
        project_id="project-1",
        query_fingerprint="c" * 64,
        status="routed",
        confidence="high",
        reason_code="high_confidence",
        top_k=3,
        candidates=(
            SeriesRouteCandidate(
                series_id="series-1",
                series_memory_id="memory-series-1",
                projection_id="projection-1",
                rank=1,
                score=0.91,
                matched_fields=("title",),
            ),
        ),
    )


@dataclass
class _Reader:
    fail_r2: bool = False
    fail_r3: bool = False
    calls: list[str] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    def read_structured(self, **kwargs):
        assert kwargs["project_id"] == "project-1"
        assert kwargs["series_ids"] == ("series-1",)
        assert kwargs["allowed_source_refs"] == (("source-1", "section:one"),)
        self.calls.append("r2")
        if self.fail_r2:
            raise RuntimeError("PRIVATE-R2-ERROR")
        return (
            AuthorityEvidenceCandidate(
                layer="r2_structured_content",
                object_type="document_block",
                object_id="document-1#block-1",
                revision_identity="document:document-1:r1",
                content="结构化内容 " * 30,
                content_hash=hashlib.sha256(("结构化内容 " * 30).encode()).hexdigest(),
                source_refs=(
                    EvidenceSourceRef("source-1", "section:one", SOURCE_HASH),
                ),
                series_id="series-1",
            ),
        )

    def read_source_evidence(self, **kwargs):
        assert kwargs["project_id"] == "project-1"
        assert kwargs["allowed_source_refs"] == (("source-1", "section:one"),)
        self.calls.append("r3")
        if self.fail_r3:
            raise RuntimeError("PRIVATE-R3-ERROR")
        return (
            AuthorityEvidenceCandidate(
                layer="r3_source_evidence",
                object_type="source_content_read",
                object_id="read-1",
                revision_identity="read:read-1:current",
                content="原始证据 PRIVATE-BODY-CANARY",
                content_hash=hashlib.sha256("原始证据 PRIVATE-BODY-CANARY".encode()).hexdigest(),
                source_refs=(
                    EvidenceSourceRef("source-1", "section:one", SOURCE_HASH),
                ),
            ),
        )


def test_standard_does_not_read_r2_or_r3_without_escalation() -> None:
    reader = _Reader()
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="这个项目现在如何",
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
    )
    assert reader.calls == []
    assert result.bundle["items"] == []
    assert result.bundle["stages_read"] == []


def test_deep_synthesis_reads_r2_but_not_r3() -> None:
    reader = _Reader()
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="请形成完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
    )
    assert reader.calls == ["r2"]
    assert [item["layer"] for item in result.bundle["items"]] == [
        "r2_structured_content"
    ]


def test_source_verification_reads_r2_then_r3_and_validates_contracts() -> None:
    reader = _Reader()
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query=CANARY_QUERY,
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
    )
    assert reader.calls == ["r2", "r3"]
    assert [item["layer"] for item in result.bundle["items"]] == [
        "r2_structured_content",
        "r3_source_evidence",
    ]
    context_schema_path = (
        ROOT / "core-contracts/rebuild/progressive_recall_context_bundle.schema.json"
    )
    trace_schema_path = (
        ROOT / "core-contracts/rebuild/progressive_recall_drilldown_trace.schema.json"
    )
    context_schema = json.loads(context_schema_path.read_text(encoding="utf-8"))
    trace_schema = json.loads(trace_schema_path.read_text(encoding="utf-8"))
    assert validate_contract_instance(
        context_schema_path.name,
        context_schema,
        result.bundle,
    ) == []
    assert validate_contract_instance(
        trace_schema_path.name,
        trace_schema,
        result.trace,
    ) == []
    trace_text = json.dumps(result.trace, ensure_ascii=False)
    assert CANARY_QUERY not in trace_text
    assert "PRIVATE-BODY-CANARY" not in trace_text
    assert "section:one" not in trace_text


@pytest.mark.parametrize(
    ("query", "signals", "expected"),
    [
        ("普通问题", ("insufficient_evidence",), ["r2"]),
        ("普通问题", ("explicit_detail_request",), ["r2"]),
        ("普通问题", ("conflicting_evidence",), ["r2"]),
        ("普通问题", ("source_verification_required",), ["r2", "r3"]),
    ],
)
def test_escalation_signals_are_deterministic(query, signals, expected) -> None:
    reader = _Reader()
    run_progressive_recall_drilldown(
        project_id="project-1",
        query=query,
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
        escalation_signals=signals,
    )
    assert reader.calls == expected


def test_global_budget_counts_existing_context_and_truncates_unicode() -> None:
    reader = _Reader()
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
        consumed_items=11,
        consumed_chars=11990,
    )
    assert result.bundle["budget"]["used_items"] == 12
    assert result.bundle["budget"]["used_chars"] == 12000
    assert result.bundle["items"][0]["char_count"] == 10
    assert result.bundle["items"][0]["truncated"] is True


def test_reader_failures_are_anonymous_and_do_not_raise() -> None:
    reader = _Reader(fail_r2=True, fail_r3=True)
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query=CANARY_QUERY,
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
    )
    assert result.bundle["items"] == []
    assert result.trace["error_codes"] == [
        "r2_reader_unavailable",
        "r3_reader_unavailable",
    ]
    assert "PRIVATE-R2-ERROR" not in json.dumps(result.trace)


def test_slow_reader_times_out_to_anonymous_partial_result() -> None:
    class SlowReader(_Reader):
        def read_structured(self, **kwargs):
            self.calls.append("r2")
            time.sleep(2)
            return ()

    reader = SlowReader()
    started = time.monotonic()
    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="请形成完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=reader,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert result.bundle["items"] == []
    assert result.trace["error_codes"] == ["r2_reader_timeout_partial"]


def test_stale_projection_and_cross_project_route_are_rejected() -> None:
    with pytest.raises(ProgressiveRecallDrilldownError):
        run_progressive_recall_drilldown(
            project_id="project-1",
            query="完整技术方案",
            projection_result=_projection(status="stale"),
            route=_route(),
            authority_reader=_Reader(),
        )
    route = _route()
    wrong = SeriesRouteDecision(
        project_id="project-2",
        query_fingerprint=route.query_fingerprint,
        status=route.status,
        confidence=route.confidence,
        reason_code=route.reason_code,
        top_k=route.top_k,
        candidates=route.candidates,
    )
    with pytest.raises(ProgressiveRecallDrilldownError):
        run_progressive_recall_drilldown(
            project_id="project-1",
            query="完整技术方案",
            projection_result=_projection(),
            route=wrong,
            authority_reader=_Reader(),
        )


def test_invalid_signal_and_budget_are_rejected() -> None:
    with pytest.raises(ProgressiveRecallDrilldownError):
        run_progressive_recall_drilldown(
            project_id="project-1",
            query="普通问题",
            projection_result=_projection(),
            route=_route(),
            authority_reader=_Reader(),
            escalation_signals=("unknown",),
        )


def test_reader_cannot_escape_fresh_r1_scope_or_forge_content_hash() -> None:
    class MaliciousReader(_Reader):
        def read_structured(self, **kwargs):
            return (
                AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="document_block",
                    object_id="foreign",
                    revision_identity="foreign:r1",
                    content="PRIVATE-FOREIGN",
                    content_hash=hashlib.sha256(b"PRIVATE-FOREIGN").hexdigest(),
                    source_refs=(
                        EvidenceSourceRef("source-2", "section:two", "f" * 64),
                    ),
                    relevance_score=1.0,
                ),
                AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="document_block",
                    object_id="forged",
                    revision_identity="forged:r1",
                    content="PRIVATE-FORGED",
                    content_hash="0" * 64,
                    source_refs=(
                        EvidenceSourceRef("source-1", "section:one", SOURCE_HASH),
                    ),
                    relevance_score=0.9,
                ),
            )

    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=MaliciousReader(),
    )
    assert result.bundle["items"] == []
    assert set(result.bundle["drop_codes"]) == {
        "candidate_content_hash_drift",
        "candidate_missing_evidence",
    }


def test_relevance_score_precedes_stable_identity_tie_break() -> None:
    class RankedReader(_Reader):
        def read_structured(self, **kwargs):
            def item(object_id: str, content: str, score: float):
                return AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="document_block",
                    object_id=object_id,
                    revision_identity=f"{object_id}:r1",
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    source_refs=(
                        EvidenceSourceRef("source-1", "section:one", SOURCE_HASH),
                    ),
                    relevance_score=score,
                )

            return (
                item("a-low", "低相关", 0.1),
                item("z-high", "高相关", 0.9),
                item("b-tie", "同分乙", 0.5),
                item("a-tie", "同分甲", 0.5),
            )

    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=RankedReader(),
    )
    assert [item["object_id"] for item in result.bundle["items"]] == [
        "z-high",
        "a-tie",
        "b-tie",
        "a-low",
    ]


def test_content_identity_preserves_leading_and_trailing_whitespace() -> None:
    class WhitespaceReader(_Reader):
        def read_structured(self, **kwargs):
            content = "\n  有意义的原始排版  \n"
            return (
                AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="document_block",
                    object_id="whitespace",
                    revision_identity="whitespace:r1",
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    source_refs=(
                        EvidenceSourceRef("source-1", "section:one", SOURCE_HASH),
                    ),
                ),
            )

    result = run_progressive_recall_drilldown(
        project_id="project-1",
        query="完整技术方案",
        projection_result=_projection(),
        route=_route(),
        authority_reader=WhitespaceReader(),
    )
    assert result.bundle["items"][0]["content"] == "\n  有意义的原始排版  \n"
    with pytest.raises(ProgressiveRecallDrilldownError):
        run_progressive_recall_drilldown(
            project_id="project-1",
            query="普通问题",
            projection_result=_projection(),
            route=_route(),
            authority_reader=_Reader(),
            consumed_items=True,
        )
