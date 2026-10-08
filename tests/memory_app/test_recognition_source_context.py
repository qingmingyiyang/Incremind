import pytest

from backend.memory_app.context_adapter import ContextAdapter, ContextSelectionError, format_recognition_content
from backend.recognition import RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def evidence(kind="model_generated_artifact", occurred_at=None):
    return {"type": "experience", "id": "source-a", "revision": 1,
        "kind": kind,
        "epistemic_status": "user_asserted" if kind == "user_statement" else "unknown" if kind == "legacy_unspecified" else "unverified",
        "recorded_at": "2026-09-30T00:00:00Z", "occurred_at": occurred_at,
        "artifact_status": "committed" if kind == "model_generated_artifact" else None,
        "outcome_status": "unknown" if kind == "model_generated_artifact" else None}


def entry(source=None):
    return {"id": "recognition-a", "project_id": "project", "revision": 1, "current_revision": 1,
        "content": "部署已完成", "conditions": ["仅限演示"], "status": "active", "authorized": True,
        "source_refs": [{"type": "experience", "id": "source-a", "revision": 1}],
        "source_evidence": [source or evidence()], "source_evidence_complete": True,
        "source_evidence_reason": None}


def packet(value, **options):
    return ContextAdapter(**options).compile_selected("project", [value], [value["id"]], "部署是否完成？", 1)


@pytest.mark.parametrize("kind", ["user_statement", "workspace_confirmed_document", "model_generated_artifact", "legacy_unspecified"])
def test_exact_origin_and_limitations_reach_actual_messages_and_preview_items(kind):
    value = entry(evidence(kind))
    result = packet(value)
    actual = result["messages"][1]["content"]
    assert kind in actual
    assert value["source_evidence"][0]["epistemic_status"] in actual
    assert "仅限演示" in actual
    assert result["items"][0]["source_evidence"] == value["source_evidence"]
    assert result["items"][0]["source_evidence_complete"] is True
    assert format_recognition_content(value) in actual
    assert result["graph"]["nodes"][0]["trust"] == "user_authored"
    if kind == "model_generated_artifact":
        assert '"outcome_status":"unknown"' in actual
        assert "不证明" in actual


def test_occurrence_and_recording_are_distinct_from_validity_or_revision():
    past = packet(entry(evidence("user_statement", "2019-01-01T00:00:00Z")))
    recent = packet(entry(evidence("user_statement", "2026-09-29T00:00:00Z")))
    assert past["messages"] != recent["messages"]
    actual = past["messages"][1]["content"]
    assert "2019-01-01T00:00:00Z" in actual and "2026-09-30T00:00:00Z" in actual
    assert "不是有效期" in actual
    assert "记录时间" in actual and "发生时间" in actual


def test_existing_source_metadata_from_published_service_reaches_wire(tmp_path):
    service = RecognitionService(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"))
    scope = WorkScope("user", "project")
    source = service.stage_experience(scope=scope, content="某次陈述", provenance={
        "kind": "user_statement", "occurred_at": "2018-01-01T00:00:00Z"})
    candidate = service.propose(scope=scope, content="办公室在海淀", source_experience_ids=[source])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user")
    value = service.get_recognition(scope=scope, recognition_id=recognition.id).retrieval_projection()
    actual = packet(value)["messages"][1]["content"]
    assert "user_statement" in actual and "2018-01-01T00:00:00Z" in actual


@pytest.mark.parametrize("complete", [False, None, "true", 1])
def test_incomplete_source_evidence_cannot_be_consumed_as_ordinary_body(complete):
    value = {**entry(), "source_evidence_complete": complete, "source_evidence_reason": "source_evidence_item_limit"}
    with pytest.raises(ContextSelectionError, match="recognition_source_evidence_incomplete"):
        format_recognition_content(value)
    with pytest.raises(ContextSelectionError, match="recognition_source_evidence_incomplete"):
        packet(value)


@pytest.mark.parametrize("mutation", [
    {"epistemic_status": "verified"}, {"revision": True}, {"kind": "verified"},
    {"private_body": "not allowed"}, {"outcome_status": "achieved"},
])
def test_forged_or_unbounded_metadata_is_rejected(mutation):
    value = entry({**evidence(), **mutation})
    with pytest.raises(ContextSelectionError, match="recognition_source_evidence_invalid"):
        packet(value)


def test_source_metadata_is_counted_in_the_actual_message_budget():
    value = entry(evidence("user_statement"))
    value["source_evidence"] = [{**evidence("user_statement"), "id": f"source-{index}"} for index in range(20)]
    # The short conclusion alone fits; complete source metadata must not be silently dropped.
    with pytest.raises(ContextSelectionError, match="exceeds_input_capacity"):
        packet(value, max_input_tokens=1000)


def test_missing_legacy_metadata_is_explicitly_unknown_without_inventing_sources():
    value = {key: content for key, content in entry().items() if not key.startswith("source_evidence")}
    result = packet(value)
    actual = result["messages"][1]["content"]
    assert "来源身份未提供" in actual
    assert "核验状态未知" in actual
    assert "model_generated_artifact" not in actual
    assert "source_evidence" not in result["items"][0]


def test_unselected_incomplete_evidence_does_not_block_a_complete_selection():
    value = entry()
    incomplete = {**entry(), "id": "oversized", "source_evidence_complete": False,
        "source_evidence_reason": "source_evidence_item_limit", "source_evidence": []}
    result = ContextAdapter().compile_selected("project", [value, incomplete], [value["id"]], "问题", 1)
    assert [item["id"] for item in result["items"]] == [value["id"]]
    assert result["exclusions"] == [{"id": "oversized", "reason": "not_selected"}]
    assert "oversized" not in result["messages"][1]["content"]
