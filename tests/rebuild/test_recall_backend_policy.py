from __future__ import annotations

from core.search_and_recall import (
    RecallBackendCandidate,
    SelectRecallBackendPolicy,
    select_default_recall_backend_policy,
    sqlite_fts5_candidate,
)


def test_default_recall_backend_policy_selects_sqlite_fts5_and_defers_vector() -> None:
    selection = select_default_recall_backend_policy()
    decisions = {decision.candidate.name: decision for decision in selection.decisions}

    assert selection.status == "ready"
    assert selection.selected_backend == "sqlite_fts5"
    assert selection.fallback_backend == "object_store_lexical"
    assert selection.vector_status == "deferred"
    assert decisions["sqlite_fts5"].status == "selected"
    assert decisions["object_store_lexical"].status == "fallback"
    assert decisions["sqlite_vec_or_external_vector"].status == "deferred"
    assert "dependency_not_audited" in decisions["sqlite_vec_or_external_vector"].reasons


def test_sqlite_fts5_probe_is_available_in_runtime() -> None:
    candidate = sqlite_fts5_candidate()

    assert candidate.name == "sqlite_fts5"
    assert candidate.available is True
    assert candidate.supports_bm25 is True
    assert candidate.max_target_atoms == 100_000


def test_backend_policy_rejects_non_local_or_untraceable_candidate() -> None:
    selection = SelectRecallBackendPolicy(
        (
            RecallBackendCandidate(
                name="remote_untraceable_vector",
                kind="vector",
                local_first=False,
                dependency_audited=False,
                preserves_source_refs=False,
                supports_project_filter=True,
                supports_layer_filter=False,
                supports_trust_filter=True,
                supports_bm25=False,
                supports_vector=True,
                vector_enabled_by_default=True,
                max_target_atoms=100_000,
                available=True,
            ),
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
        )
    ).execute()
    decision = selection.decisions[0]

    assert selection.status == "degraded"
    assert selection.selected_backend is None
    assert selection.fallback_backend == "object_store_lexical"
    assert decision.status == "deferred"
    assert set(decision.reasons) >= {
        "not_local_first",
        "dependency_not_audited",
        "source_refs_not_preserved",
        "project_layer_trust_filters_missing",
        "vector_enabled_without_gate",
    }


def test_backend_policy_does_not_select_object_store_lexical_as_production_target() -> None:
    selection = SelectRecallBackendPolicy(
        (
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
        )
    ).execute()

    assert selection.status == "degraded"
    assert selection.selected_backend is None
    assert selection.fallback_backend == "object_store_lexical"
    assert selection.decisions[0].status == "fallback"
