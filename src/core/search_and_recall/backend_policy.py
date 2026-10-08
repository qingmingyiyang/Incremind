from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Literal


BackendKind = Literal["object_store_lexical", "sqlite_fts5", "vector", "hybrid"]
BackendDecisionStatus = Literal["selected", "fallback", "deferred", "rejected"]


@dataclass(frozen=True, slots=True)
class RecallBackendCandidate:
    name: str
    kind: BackendKind
    local_first: bool
    dependency_audited: bool
    preserves_source_refs: bool
    supports_project_filter: bool
    supports_layer_filter: bool
    supports_trust_filter: bool
    supports_bm25: bool
    supports_vector: bool
    vector_enabled_by_default: bool
    max_target_atoms: int
    available: bool = True


@dataclass(frozen=True, slots=True)
class RecallBackendDecision:
    candidate: RecallBackendCandidate
    status: BackendDecisionStatus
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallBackendSelection:
    status: Literal["ready", "degraded"]
    selected_backend: str | None
    fallback_backend: str | None
    vector_status: Literal["disabled", "deferred", "ready"]
    decisions: tuple[RecallBackendDecision, ...]
    required_gates: tuple[str, ...]


REQUIRED_BACKEND_GATES = (
    "local_first",
    "dependency_audited",
    "source_ref_traceability",
    "project_layer_trust_filters",
    "bm25_or_fts",
    "100k_atom_target",
    "vector_disabled_until_audit",
)


class SelectRecallBackendPolicy:
    """Selects the next Recall backend without weakening vNext evidence guards."""

    def __init__(self, candidates: tuple[RecallBackendCandidate, ...]) -> None:
        self._candidates = candidates

    def execute(self) -> RecallBackendSelection:
        decisions = tuple(_decision(candidate) for candidate in self._candidates)
        selected = _first(decisions, status="selected")
        fallback = _first(decisions, status="fallback")
        vector_ready = any(
            decision.candidate.supports_vector and decision.status == "selected"
            for decision in decisions
        )
        vector_deferred = any(
            decision.candidate.supports_vector and decision.status in {"deferred", "rejected"}
            for decision in decisions
        )
        return RecallBackendSelection(
            status="ready" if selected is not None and fallback is not None else "degraded",
            selected_backend=selected.candidate.name if selected is not None else None,
            fallback_backend=fallback.candidate.name if fallback is not None else None,
            vector_status="ready" if vector_ready else "deferred" if vector_deferred else "disabled",
            decisions=decisions,
            required_gates=REQUIRED_BACKEND_GATES,
        )


def default_recall_backend_candidates() -> tuple[RecallBackendCandidate, ...]:
    """Return the R045 staged backend candidates in priority order."""

    return (
        sqlite_fts5_candidate(),
        RecallBackendCandidate(
            name="object_store_lexical",
            kind="object_store_lexical",
            local_first=True,
            dependency_audited=True,
            preserves_source_refs=True,
            supports_project_filter=True,
            supports_layer_filter=True,
            supports_trust_filter=True,
            supports_bm25=False,
            supports_vector=False,
            vector_enabled_by_default=False,
            max_target_atoms=10_000,
            available=True,
        ),
        RecallBackendCandidate(
            name="sqlite_vec_or_external_vector",
            kind="vector",
            local_first=True,
            dependency_audited=False,
            preserves_source_refs=True,
            supports_project_filter=True,
            supports_layer_filter=True,
            supports_trust_filter=True,
            supports_bm25=False,
            supports_vector=True,
            vector_enabled_by_default=False,
            max_target_atoms=100_000,
            available=False,
        ),
    )


def sqlite_fts5_candidate() -> RecallBackendCandidate:
    return RecallBackendCandidate(
        name="sqlite_fts5",
        kind="sqlite_fts5",
        local_first=True,
        dependency_audited=True,
        preserves_source_refs=True,
        supports_project_filter=True,
        supports_layer_filter=True,
        supports_trust_filter=True,
        supports_bm25=True,
        supports_vector=False,
        vector_enabled_by_default=False,
        max_target_atoms=100_000,
        available=_sqlite_fts5_available(),
    )


def select_default_recall_backend_policy() -> RecallBackendSelection:
    return SelectRecallBackendPolicy(default_recall_backend_candidates()).execute()


def _decision(candidate: RecallBackendCandidate) -> RecallBackendDecision:
    reasons = _candidate_rejection_reasons(candidate)
    if candidate.kind == "object_store_lexical":
        fallback_reasons = tuple(
            reason
            for reason in reasons
            if reason not in {"bm25_or_fts_missing", "100k_atom_target_missing"}
        )
        if not fallback_reasons:
            return RecallBackendDecision(
                candidate=candidate,
                status="fallback",
                reasons=("safe_traceable_fallback_not_100k_target",),
            )
    if candidate.supports_vector and reasons:
        return RecallBackendDecision(candidate=candidate, status="deferred", reasons=reasons)
    if reasons:
        return RecallBackendDecision(candidate=candidate, status="rejected", reasons=reasons)
    if candidate.supports_vector:
        return RecallBackendDecision(
            candidate=candidate,
            status="deferred",
            reasons=("vector_requires_separate_phase_gate",),
        )
    return RecallBackendDecision(candidate=candidate, status="selected", reasons=("meets_phase6_fts_policy",))


def _candidate_rejection_reasons(candidate: RecallBackendCandidate) -> tuple[str, ...]:
    reasons: list[str] = []
    if not candidate.available:
        reasons.append("backend_unavailable")
    if not candidate.local_first:
        reasons.append("not_local_first")
    if not candidate.dependency_audited:
        reasons.append("dependency_not_audited")
    if not candidate.preserves_source_refs:
        reasons.append("source_refs_not_preserved")
    if not (candidate.supports_project_filter and candidate.supports_layer_filter and candidate.supports_trust_filter):
        reasons.append("project_layer_trust_filters_missing")
    if not candidate.supports_bm25 and not candidate.supports_vector:
        reasons.append("bm25_or_fts_missing")
    if candidate.max_target_atoms < 100_000:
        reasons.append("100k_atom_target_missing")
    if candidate.vector_enabled_by_default:
        reasons.append("vector_enabled_without_gate")
    return tuple(reasons)


def _first(
    decisions: tuple[RecallBackendDecision, ...],
    *,
    status: BackendDecisionStatus,
) -> RecallBackendDecision | None:
    for decision in decisions:
        if decision.status == status:
            return decision
    return None


def _sqlite_fts5_available() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE recall_probe USING fts5(content)")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True
