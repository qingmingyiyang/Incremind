import pytest

from core.memory_core import (
    ManualPublicationContractError,
    build_manual_publication_context,
    build_manual_publication_record,
    validate_manual_publication_context,
)


def _context() -> dict[str, object]:
    refs = [{"source_id": "source-alpha", "locator": "char:0-40"}]
    return build_manual_publication_context(
        namespace_id="default", layer="atom", draft_id="atom-draft-alpha",
        candidate_id="candidate-alpha", reviewed_at="2026-07-12T12:00:00+08:00",
        review_reason="用户确认候选进入草稿。", source_refs=refs, evidence_refs=refs,
    )


def test_manual_publication_record_preserves_candidate_review_policy_and_revision() -> None:
    record = build_manual_publication_record(
        context=_context(), namespace_id="default", layer="atom", draft_id="atom-draft-alpha",
        revision=1, published_at="2026-07-12T12:05:00+08:00",
    )
    assert record["source_candidate_id"] == "candidate-alpha"
    assert record["reviewer"] == "user"
    assert record["policy_id"] == "local-manual-v1"
    assert record["published_revision"] == 1
    assert record["published_at"] == "2026-07-12T12:05:00+08:00"


def test_manual_publication_context_rejects_review_ref_drift() -> None:
    context = _context()
    context["review_ref"] = "crp://default/memory-candidates/other.json"
    with pytest.raises(ManualPublicationContractError, match="context is invalid"):
        validate_manual_publication_context(context, namespace_id="default", layer="atom", draft_id="atom-draft-alpha")
