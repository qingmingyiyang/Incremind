from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.product_core.progressive_memory_retrieval import (
    ProgressiveMemoryRetrievalError,
    plan_progressive_memory_retrieval,
    serialize_progressive_retrieval_plan,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "core-contracts" / "rebuild" / "progressive_memory_retrieval_plan.schema.json"


@pytest.mark.parametrize(
    ("query", "expected_intent"),
    [
        ("这个项目整体进展怎么样？", "overview"),
        ("Chriptmas OS 的记忆系统怎么运行？", "standard"),
        ("我们具体使用的是哪个版本？", "fact_lookup"),
        ("启动目标和恢复规则分别是什么？", "fact_lookup"),
        ("请形成一份完整的技术方案", "deep_synthesis"),
        ("这句话的原文和出处是什么？", "source_verification"),
        ("按照之前的工作方法开始执行", "skill_execution"),
    ],
)
def test_progressive_retrieval_classifies_supported_intents(
    query: str,
    expected_intent: str,
) -> None:
    plan = plan_progressive_memory_retrieval(query)

    assert plan.intent == expected_intent
    assert plan.read_order == (
        "r0_series_router",
        "r1_series_digest",
        "r2_structured_content",
        "r3_source_evidence",
    )


def test_source_verification_starts_all_stages_and_requires_source_body() -> None:
    plan = plan_progressive_memory_retrieval("请核对原文、来源和证据")
    payload = serialize_progressive_retrieval_plan(plan)

    assert plan.intent == "source_verification"
    assert plan.requires_source_body is True
    assert all(stage.initial for stage in plan.stages)
    assert payload["safety"] == {
        "read_only": True,
        "business_writes_allowed": False,
        "memory_publication_allowed": False,
        "team_memory_body_allowed": False,
        "requires_user_confirmation_for_writes": True,
    }


def test_fact_lookup_uses_exact_atom_lane_without_skipping_series_fallback() -> None:
    plan = plan_progressive_memory_retrieval("我们使用的具体版本是多少？")

    assert plan.intent == "fact_lookup"
    assert plan.context_lanes == ("k_global_profile", "k_atom_exact")
    assert plan.read_order[0] == "r0_series_router"
    assert plan.allow_cross_series_fallback is True
    assert "exact_atom_shortcut" in plan.rationale_codes


def test_skill_execution_uses_project_skill_as_independent_authority_lane() -> None:
    plan = plan_progressive_memory_retrieval("沿用之前的工作方法继续处理")

    assert plan.intent == "skill_execution"
    assert "k_project_skill" in plan.context_lanes
    assert "project_skill_shortcut" in plan.rationale_codes
    assert plan.stages[0].initial is True
    assert plan.stages[1].initial is True
    assert plan.stages[2].initial is False


def test_deep_synthesis_reads_structured_content_before_source_on_demand() -> None:
    plan = plan_progressive_memory_retrieval("给我一份详细的技术方案")

    assert plan.intent == "deep_synthesis"
    assert [stage.stage for stage in plan.stages if stage.initial] == [
        "r0_series_router",
        "r1_series_digest",
        "r2_structured_content",
    ]
    assert plan.stages[-1].initial is False
    assert plan.max_chars == 12000


def test_plan_is_deterministic_after_safe_query_normalization() -> None:
    first = plan_progressive_memory_retrieval("  这个项目   进展如何？ ")
    second = plan_progressive_memory_retrieval("这个项目 进展如何？")

    assert first == second
    assert len(first.query_fingerprint) == 64


def test_serialized_plan_does_not_persist_raw_query() -> None:
    sensitive_canary = "CANARY-PROGRESSIVE-QUERY-7429"
    payload = serialize_progressive_retrieval_plan(
        plan_progressive_memory_retrieval(f"请总结 {sensitive_canary}")
    )

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert sensitive_canary not in serialized
    assert "query" not in payload


def test_retrieval_plan_freezes_bounded_partial_timeout_policy() -> None:
    plan = plan_progressive_memory_retrieval("请形成一份完整的技术方案")
    payload = serialize_progressive_retrieval_plan(plan)

    assert plan.timeout_ms == 300
    assert plan.on_timeout == "partial"
    assert payload["budget"]["timeout_ms"] == 300
    assert payload["budget"]["on_timeout"] == "partial"
    assert all(stage.timeout_ms == 300 for stage in plan.stages)
    assert all(stage.on_timeout == "partial" for stage in plan.stages)


def test_serialized_plan_matches_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    for query in (
        "整体概况",
        "普通问题",
        "具体值是多少",
        "完整分析",
        "核对原文",
        "按照之前的方法执行",
    ):
        payload = serialize_progressive_retrieval_plan(
            plan_progressive_memory_retrieval(query)
        )
        assert validate_contract_instance(
            SCHEMA_PATH.name,
            schema,
            payload,
        ) == []


@pytest.mark.parametrize("query", ["", "  \n\t  ", None, 42])
def test_invalid_query_fails_closed(query: object) -> None:
    with pytest.raises(ProgressiveMemoryRetrievalError):
        plan_progressive_memory_retrieval(query)  # type: ignore[arg-type]


def test_oversized_query_fails_closed() -> None:
    with pytest.raises(ProgressiveMemoryRetrievalError):
        plan_progressive_memory_retrieval("问" * 4001)


def test_source_verification_wins_over_other_cues() -> None:
    plan = plan_progressive_memory_retrieval("按照之前的方法写完整报告，并核对原文出处")

    assert plan.intent == "source_verification"
    assert plan.requires_source_body is True
