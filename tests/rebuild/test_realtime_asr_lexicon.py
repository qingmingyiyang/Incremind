from pathlib import Path

import pytest

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.realtime_asr_lexicon import (
    MAX_SESSION_TERMS,
    MAX_SUPER_TERMS,
    SUPER_WEIGHT,
    RealtimeAsrLexicon,
    RealtimeAsrLexiconError,
)


def _lexicon(tmp_path: Path) -> RealtimeAsrLexicon:
    store, _settings = build_rebuild_object_store(tmp_path)
    return RealtimeAsrLexicon(store, now=lambda: "2026-09-04T00:00:00Z")


def test_manual_upsert_activates_and_advances_local_revision(tmp_path: Path) -> None:
    lexicon = _lexicon(tmp_path)

    created = lexicon.manual_upsert("Chriptmas OS", weight=4)
    updated = lexicon.manual_upsert("chriptmas os", weight=5)

    assert created.status == "active"
    assert updated.term_id == created.term_id
    assert updated.weight == 5
    assert lexicon.current_revision() == 2
    assert [item.term for item in lexicon.select_for_session().terms] == ["chriptmas os"]


def test_candidates_are_pending_until_explicit_acceptance(tmp_path: Path) -> None:
    lexicon = _lexicon(tmp_path)

    candidate = lexicon.propose_candidate("混元语音", suggested_weight=3, source_ref="crp://default/sources/a")

    assert candidate.status == "pending_review"
    assert not lexicon.select_for_session().terms
    accepted = lexicon.accept_candidate(candidate.candidate_id)

    assert accepted.status == "active"
    assert accepted.origin == "candidate"
    assert lexicon.list_candidates(status="accepted")[0].candidate_id == candidate.candidate_id
    assert [item.term for item in lexicon.select_for_session().terms] == ["混元语音"]


def test_rejection_never_activates_an_automatic_candidate(tmp_path: Path) -> None:
    lexicon = _lexicon(tmp_path)
    candidate = lexicon.propose_candidate("不应启用")

    rejected = lexicon.reject_candidate(candidate.candidate_id)

    assert rejected.status == "rejected"
    assert not lexicon.select_for_session().terms
    with pytest.raises(RealtimeAsrLexiconError, match="pending_review"):
        lexicon.accept_candidate(candidate.candidate_id)


def test_super_terms_are_exact_weight_50_and_bounded(tmp_path: Path) -> None:
    lexicon = _lexicon(tmp_path)
    for index in range(MAX_SUPER_TERMS):
        assert lexicon.manual_upsert(f"super-{index}", is_super=True).weight == SUPER_WEIGHT

    with pytest.raises(RealtimeAsrLexiconError, match="super"):
        lexicon.manual_upsert("one-too-many", is_super=True)
    assert lexicon.retire("super-0").status == "retired"
    assert lexicon.manual_upsert("replacement", is_super=True).weight == SUPER_WEIGHT


def test_selection_is_deterministic_weighted_and_hard_capped(tmp_path: Path) -> None:
    lexicon = _lexicon(tmp_path)
    lexicon.manual_upsert("zeta", weight=1)
    lexicon.manual_upsert("beta", weight=5)
    lexicon.manual_upsert("alpha", weight=5)
    lexicon.manual_upsert("priority", is_super=True)

    selected = lexicon.select_for_session(limit=99_999)

    assert [item.term for item in selected.terms[:4]] == ["priority", "alpha", "beta", "zeta"]
    assert len(selected.terms) <= MAX_SESSION_TERMS


@pytest.mark.parametrize(
    "term",
    ["", " ", "超长专有名词必须被提供商约束阻止提交", "one two three four five six seven eight", "bad\nterm"],
)
def test_term_constraints_are_enforced(tmp_path: Path, term: str) -> None:
    with pytest.raises(RealtimeAsrLexiconError, match="term"):
        _lexicon(tmp_path).manual_upsert(term)


@pytest.mark.parametrize("weight", [0, 6, 50, True])
def test_normal_weight_is_limited_to_one_through_five(tmp_path: Path, weight: object) -> None:
    with pytest.raises(RealtimeAsrLexiconError, match="weight"):
        _lexicon(tmp_path).manual_upsert("normal", weight=weight)  # type: ignore[arg-type]
