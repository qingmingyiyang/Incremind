import json

import pytest

from backend.memory_app.restructure_generation import build_messages, parse_proposal
from backend.recognition import RecognitionError, RecognitionService, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def source(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("local-user", "project-a")
    eid = service.stage_experience(scope=scope, content="Unverified model plan, not a completed action")
    candidate = service.propose(scope=scope, content="Before", source_experience_ids=[eid])
    parent = service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer="local-user")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    return records, scope, eid, parent, snapshot


def output(eid):
    return {"content": "After", "conditions": ["Needs verification"],
            "source_experience_ids": [eid], "source_recognition_ids": []}


def test_generated_proposal_is_validated_without_publishing(source):
    records, scope, eid, parent, snapshot = source
    messages = build_messages(scope=scope, snapshot=snapshot, operation="revise", instruction="Clarify uncertainty")
    sent = json.loads(messages[1]["content"])
    assert sent["target_recognition_ids"] == [parent.id]
    assert sent["experiences"][0]["revision"] == 1
    parsed = parse_proposal(scope=scope, snapshot=snapshot, requested_operation="revise",
        response=json.dumps({"operation": "revise", "outputs": [output(eid)], "reason": "Clarify"}))
    assert parsed["outputs"][0]["content"] == "After"
    assert records.read("recognitions", parent.id).revision == parent.revision
    assert records.list("recognition_restructure_proposals") == ()


@pytest.mark.parametrize("case", ["wrong-operation", "extra-key", "foreign-source", "parent-source", "empty-source", "null-array", "chosen-id"])
def test_model_cannot_expand_requested_operation_or_evidence(source, case):
    _, scope, eid, parent, snapshot = source
    row = output(eid)
    body = {"operation": "revise", "outputs": [row], "reason": "Clarify"}
    if case == "wrong-operation": body["operation"] = "supersede"
    if case == "extra-key": body["approved"] = True
    if case == "foreign-source": row["source_experience_ids"] = ["foreign"]
    if case == "parent-source": row["source_recognition_ids"] = [parent.id]
    if case == "empty-source": row["source_experience_ids"] = []
    if case == "null-array": row["source_recognition_ids"] = None
    if case == "chosen-id": row["recognition_id"] = "model-chosen"
    with pytest.raises(RecognitionError):
        parse_proposal(scope=scope, snapshot=snapshot, requested_operation="revise", response=json.dumps(body))


@pytest.mark.parametrize("response", ['{"operation":"noop","operation":"revise","outputs":[],"reason":"x"}',
    '{"operation":"noop","outputs":[],"reason":NaN}', 'prefix {"operation":"noop"}', '[]'])
def test_ambiguous_or_non_json_responses_are_rejected(source, response):
    _, scope, _, _, snapshot = source
    with pytest.raises(RecognitionError):
        parse_proposal(scope=scope, snapshot=snapshot, requested_operation="revise", response=response)


def test_noop_and_json_fence_are_allowed(source):
    _, scope, _, _, snapshot = source
    result = parse_proposal(scope=scope, snapshot=snapshot, requested_operation="revise",
        response='```json\n{"operation":"noop","outputs":[],"reason":"Already accurate"}\n```')
    assert result == {"operation": "noop", "outputs": [], "reason": "Already accurate"}


def test_merge_requires_multiple_targets_before_sending(source):
    _, scope, _, _, snapshot = source
    with pytest.raises(RecognitionError, match="target count"):
        build_messages(scope=scope, snapshot=snapshot, operation="merge", instruction="Merge")
