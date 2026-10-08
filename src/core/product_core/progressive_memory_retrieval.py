from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


SCHEMA_VERSION = "1.0.0"
PLAN_VERSION = "progressive-memory-retrieval-v1"
MAX_QUERY_CHARS = 4000

RETRIEVAL_STAGES = (
    "r0_series_router",
    "r1_series_digest",
    "r2_structured_content",
    "r3_source_evidence",
)

CONTEXT_LANES = (
    "k_global_profile",
    "k_atom_exact",
    "k_project_skill",
)

_SPACE_PATTERN = re.compile(r"\s+")

_SOURCE_VERIFICATION_CUES = (
    "原文",
    "原话",
    "出处",
    "来源",
    "引用",
    "证据",
    "哪一段",
    "哪一页",
    "链接",
    "追溯",
    "核对",
    "逐字",
)
_SKILL_EXECUTION_CUES = (
    "按照之前",
    "沿用之前",
    "按以前",
    "工作方法",
    "处理方法",
    "执行流程",
    "操作步骤",
    "怎么做",
    "如何做",
    "开始执行",
)
_DEEP_SYNTHESIS_CUES = (
    "完整分析",
    "详细分析",
    "深度分析",
    "完整方案",
    "详细方案",
    "技术方案",
    "研究报告",
    "完整报告",
    "面试手册",
    "白皮书",
    "系统总结",
    "全面梳理",
)
_FACT_LOOKUP_CUES = (
    "是什么",
    "什么时候",
    "哪一天",
    "哪天",
    "日期",
    "多少",
    "几个",
    "谁",
    "版本",
    "配置值",
    "具体值",
    "是否",
    "有没有",
)
_OVERVIEW_CUES = (
    "总览",
    "整体",
    "概况",
    "大概",
    "总体",
    "进展",
    "现状",
    "最近",
    "都有什么",
    "有哪些",
)
_TOPIC_ROUTING_CUES = (
    *_DEEP_SYNTHESIS_CUES,
    *_OVERVIEW_CUES,
    *_SKILL_EXECUTION_CUES,
    "核对原文",
    "原文出处",
    "原话出处",
    "逐字核对",
    "核对",
    "当前",
    "具体",
    "版本",
    "是什么",
    "是多少",
    "什么时候",
    "哪一天",
    "哪天",
    "日期",
    "多少",
    "几个",
    "谁",
    "配置值",
    "是否",
    "有没有",
    "请",
)


class ProgressiveMemoryRetrievalError(ValueError):
    """Raised when a progressive retrieval plan cannot be built safely."""


@dataclass(frozen=True, slots=True)
class RetrievalStagePlan:
    stage: str
    purpose: str
    max_items: int
    max_chars: int
    timeout_ms: int
    on_timeout: str
    initial: bool
    stop_when: tuple[str, ...]
    escalate_when: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "purpose": self.purpose,
            "max_items": self.max_items,
            "max_chars": self.max_chars,
            "timeout_ms": self.timeout_ms,
            "on_timeout": self.on_timeout,
            "initial": self.initial,
            "stop_when": list(self.stop_when),
            "escalate_when": list(self.escalate_when),
        }


@dataclass(frozen=True, slots=True)
class ProgressiveRetrievalPlan:
    query_fingerprint: str
    intent: str
    read_order: tuple[str, ...]
    context_lanes: tuple[str, ...]
    stages: tuple[RetrievalStagePlan, ...]
    max_items: int
    max_chars: int
    timeout_ms: int
    on_timeout: str
    requires_source_body: bool
    allow_cross_series_fallback: bool
    rationale_codes: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_version": PLAN_VERSION,
            "query_fingerprint": self.query_fingerprint,
            "intent": self.intent,
            "read_order": list(self.read_order),
            "context_lanes": list(self.context_lanes),
            "stages": [stage.to_payload() for stage in self.stages],
            "budget": {
                "max_items": self.max_items,
                "max_chars": self.max_chars,
                "timeout_ms": self.timeout_ms,
                "on_timeout": self.on_timeout,
            },
            "requires_source_body": self.requires_source_body,
            "allow_cross_series_fallback": self.allow_cross_series_fallback,
            "rationale_codes": list(self.rationale_codes),
            "safety": {
                "read_only": True,
                "business_writes_allowed": False,
                "memory_publication_allowed": False,
                "team_memory_body_allowed": False,
                "requires_user_confirmation_for_writes": True,
            },
        }


@dataclass(frozen=True, slots=True)
class _IntentPolicy:
    initial_stage_count: int
    context_lanes: tuple[str, ...]
    max_items: int
    max_chars: int
    requires_source_body: bool
    rationale_codes: tuple[str, ...]


_INTENT_POLICIES = {
    "overview": _IntentPolicy(
        initial_stage_count=2,
        context_lanes=("k_global_profile",),
        max_items=8,
        max_chars=5000,
        requires_source_body=False,
        rationale_codes=("series_router_first", "digest_default"),
    ),
    "standard": _IntentPolicy(
        initial_stage_count=2,
        context_lanes=("k_global_profile",),
        max_items=10,
        max_chars=8000,
        requires_source_body=False,
        rationale_codes=("series_router_first", "digest_default", "evidence_escalation"),
    ),
    "fact_lookup": _IntentPolicy(
        initial_stage_count=2,
        context_lanes=("k_global_profile", "k_atom_exact"),
        max_items=8,
        max_chars=6000,
        requires_source_body=False,
        rationale_codes=("exact_atom_shortcut", "series_fallback", "evidence_escalation"),
    ),
    "deep_synthesis": _IntentPolicy(
        initial_stage_count=3,
        context_lanes=("k_global_profile", "k_project_skill"),
        max_items=12,
        max_chars=12000,
        requires_source_body=False,
        rationale_codes=("series_router_first", "structured_content_required", "source_on_demand"),
    ),
    "source_verification": _IntentPolicy(
        initial_stage_count=4,
        context_lanes=("k_global_profile",),
        max_items=12,
        max_chars=12000,
        requires_source_body=True,
        rationale_codes=("source_evidence_required", "provenance_required"),
    ),
    "skill_execution": _IntentPolicy(
        initial_stage_count=2,
        context_lanes=("k_global_profile", "k_project_skill"),
        max_items=10,
        max_chars=10000,
        requires_source_body=False,
        rationale_codes=("project_skill_shortcut", "series_scope_required", "evidence_escalation"),
    ),
}

_STAGE_BUDGETS = {
    "r0_series_router": (12, 2400, "route_relevant_series"),
    "r1_series_digest": (8, 6000, "supply_series_context"),
    "r2_structured_content": (6, 8000, "supply_structured_detail"),
    "r3_source_evidence": (4, 6000, "verify_against_authorized_source"),
}


def plan_progressive_memory_retrieval(query: str) -> ProgressiveRetrievalPlan:
    normalized = _normalize_query(query)
    intent = _classify_intent(normalized)
    policy = _INTENT_POLICIES[intent]
    stages = tuple(
        _stage_plan(
            stage,
            initial=index < policy.initial_stage_count,
            requires_source_body=policy.requires_source_body,
        )
        for index, stage in enumerate(RETRIEVAL_STAGES)
    )
    return ProgressiveRetrievalPlan(
        query_fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        intent=intent,
        read_order=RETRIEVAL_STAGES,
        context_lanes=policy.context_lanes,
        stages=stages,
        max_items=policy.max_items,
        max_chars=policy.max_chars,
        timeout_ms=300,
        on_timeout="partial",
        requires_source_body=policy.requires_source_body,
        allow_cross_series_fallback=True,
        rationale_codes=policy.rationale_codes,
    )


def serialize_progressive_retrieval_plan(plan: ProgressiveRetrievalPlan) -> dict[str, object]:
    return plan.to_payload()


def topic_routing_query(query: str) -> str:
    """Keep retrieval instructions from diluting a lightweight topic match."""

    topic = query
    for cue in _TOPIC_ROUTING_CUES:
        topic = topic.replace(cue, " ")
    normalized = " ".join(topic.split()).strip("，。！？?：:；; ")
    return normalized or query


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ProgressiveMemoryRetrievalError("query must be a string")
    normalized = _SPACE_PATTERN.sub(" ", query).strip().casefold()
    if not normalized:
        raise ProgressiveMemoryRetrievalError("query is required")
    if len(normalized) > MAX_QUERY_CHARS:
        raise ProgressiveMemoryRetrievalError("query exceeds the progressive retrieval limit")
    return normalized


def _classify_intent(query: str) -> str:
    if _contains_any(query, _SOURCE_VERIFICATION_CUES):
        return "source_verification"
    if _contains_any(query, _SKILL_EXECUTION_CUES):
        return "skill_execution"
    if _contains_any(query, _DEEP_SYNTHESIS_CUES):
        return "deep_synthesis"
    if _contains_any(query, _FACT_LOOKUP_CUES):
        return "fact_lookup"
    if _contains_any(query, _OVERVIEW_CUES):
        return "overview"
    return "standard"


def _contains_any(query: str, cues: tuple[str, ...]) -> bool:
    return any(cue in query for cue in cues)


def _stage_plan(
    stage: str,
    *,
    initial: bool,
    requires_source_body: bool,
) -> RetrievalStagePlan:
    max_items, max_chars, purpose = _STAGE_BUDGETS[stage]
    stop_when = ("answer_supported", "context_budget_exhausted")
    if stage == "r3_source_evidence":
        stop_when = ("source_verified", "authorized_source_unavailable", "context_budget_exhausted")
    escalate_when = ()
    if stage != "r3_source_evidence":
        escalate_when = (
            "insufficient_evidence",
            "low_confidence",
            "conflicting_evidence",
            "explicit_detail_request",
        )
    if requires_source_body and stage != "r3_source_evidence":
        escalate_when = (*escalate_when, "source_verification_required")
    return RetrievalStagePlan(
        stage=stage,
        purpose=purpose,
        max_items=max_items,
        max_chars=max_chars,
        timeout_ms=300,
        on_timeout="partial",
        initial=initial,
        stop_when=stop_when,
        escalate_when=escalate_when,
    )
