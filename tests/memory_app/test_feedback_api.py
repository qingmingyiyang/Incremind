from fastapi import FastAPI
import pytest

from backend.memory_app.app import create_app
from backend.memory_app.erasure import ErasureService
from backend.recognition import WorkScope
from core.document_engine import DocumentDraft


@pytest.fixture
def feedback_context(tmp_path):
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI())
    service = app.state.recognition_service
    records = service.records
    scope = WorkScope("local-user", "verification")
    experience = service.stage_experience(scope=scope, content="original evidence")
    candidate = service.propose(scope=scope, content="frozen recognition", source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="local-user")
    document = app.state.recognition_documents.create(DocumentDraft(
        title="test result", document_type="agent-result", markdown="result", project_id=scope.project_id,
        source_refs=({"source_id": "task-feedback", "locator": "task://task-feedback"},),
    ))
    with records.begin() as tx:
        tx.put("recognition_context_packets", "packet-feedback", {
            "project_id": scope.project_id, "task_id": "task-feedback", "state": "consumed",
            "items": [{"id": recognition.id, "revision": recognition.revision, "content": recognition.content}],
        }, expected_revision=0)
        tx.put("recognition_tasks", "task-feedback", {
            "project_id": scope.project_id, "state": "completed", "document_id": document["id"],
            "context_packet_id": "packet-feedback", "turn_id": "turn-feedback",
        }, expected_revision=0)
        tx.commit()
    return service, scope, recognition


def test_historical_unknown_feedback_remains_in_erasure_closure(feedback_context, tmp_path):
    service, scope, recognition = feedback_context
    with service.records.begin() as tx:
        tx.put("recognition_task_feedback", "feedback-api-unknown", {
            "id": "feedback-api-unknown",
            "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
            "project_id": scope.project_id,
            "task_id": "task-feedback",
            "turn_id": "turn-feedback",
            "context_packet_id": "packet-feedback",
            "recognition_refs": [],
            "attribution": "unknown",
            "content": "uncertain cause",
            "created_at": "2026-09-16T00:00:00Z",
            "revision": 1,
        }, expected_revision=0)
        tx.commit()
    saved = service.records.read("recognition_task_feedback", "feedback-api-unknown")
    assert saved.payload["attribution"] == "unknown" and saved.payload["recognition_refs"] == []
    assert service.records.read("recognitions", recognition.id).revision == 1
    assert service.records.list("recognition_recall_preferences") == ()
    erasure = ErasureService(service.records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id=recognition.id, expected_revision=1)
    assert preview.counts["recognition_task_feedback"] == 1
    receipt = erasure.erase(scope=scope, recognition_id=recognition.id, expected_revision=1)
    assert receipt.counts["recognition_task_feedback"] == 1
    assert service.records.list("recognition_task_feedback") == ()
