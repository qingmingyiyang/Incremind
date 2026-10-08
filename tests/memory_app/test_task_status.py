import pytest

from backend.memory_app.task_status import get_task_status
from backend.recognition import RecognitionConflict, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


def _records(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")


def _document(records, project_id: str):
    return SQLiteDocumentRepository(records, namespace_id="recognition").create(
        DocumentDraft(
            title=f"result-{project_id}",
            document_type="agent-result",
            markdown="private generated content",
            project_id=project_id,
            source_refs=({"source_id": "task-source", "locator": "task://task-source"},),
        )
    )


def _task(records, *, task_id="task-a", project_id="project-a", document_id=None):
    payload = {
        "id": task_id,
        "project_id": project_id,
        "state": "completed",
        "title": "Visible task title",
        "context_packet_id": "context-a",
        "document_id": document_id,
        "created_at": "2026-09-15T12:00:00+00:00",
        "finished_at": "2026-09-15T12:01:00+00:00",
        "input": "must never be projected",
        "messages": [{"role": "user", "content": "must never be projected"}],
        "api_key": "must never be projected",
    }
    with records.begin() as tx:
        tx.put("recognition_tasks", task_id, payload, expected_revision=0)
        tx.commit()


def test_task_status_returns_a_fixed_content_free_projection(tmp_path):
    records = _records(tmp_path)
    document = _document(records, "project-a")
    _task(records, document_id=document["id"])

    result = get_task_status(records, WorkScope("local-user", "project-a"), "task-a")

    assert result == {
        "task_id": "task-a",
        "status": "completed",
        "title": "Visible task title",
        "context_packet_id": "context-a",
        "document_id": document["id"],
        "document_revision": document["revision"],
        "created_at": "2026-09-15T12:00:00+00:00",
        "finished_at": "2026-09-15T12:01:00+00:00",
        "revision": 1,
    }


@pytest.mark.parametrize("task_project,document_project", [("project-b", "project-b"), ("project-a", "project-b")])
def test_task_status_hides_cross_project_tasks_and_documents(tmp_path, task_project, document_project):
    records = _records(tmp_path)
    document = _document(records, document_project)
    _task(records, project_id=task_project, document_id=document["id"])

    with pytest.raises(RecognitionConflict, match="task is unavailable in this project"):
        get_task_status(records, WorkScope("local-user", "project-a"), "task-a")


def test_task_status_hides_missing_tasks_and_missing_linked_documents(tmp_path):
    records = _records(tmp_path)
    with pytest.raises(RecognitionConflict, match="task is unavailable in this project"):
        get_task_status(records, WorkScope("local-user", "project-a"), "missing-task")

    _task(records, document_id="document-missing")
    with pytest.raises(RecognitionConflict, match="task is unavailable in this project"):
        get_task_status(records, WorkScope("local-user", "project-a"), "task-a")
