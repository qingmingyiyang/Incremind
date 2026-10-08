"""The tray maps persisted checkpoints without changing domain records."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.v2 import install_v2_routes
from backend.memory_app.v2.jobs import install_job_routes
from backend.recognition import RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


FIELDS = {"id", "kind", "title", "state", "progress", "pending_count", "error", "target", "updated_at"}
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def tray(tmp_path, monkeypatch):
    from backend.memory_app.v2 import jobs
    monkeypatch.setattr(jobs, "_now", lambda: NOW)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    service = RecognitionService(records)
    app = FastAPI()
    install_job_routes(app, records=records, service=service)
    with TestClient(app) as client:
        yield records, service, client


def put(records, collection, identity, **payload):
    with records.begin() as tx:
        tx.put(collection, identity, {"id": identity, "project_id": "default",
               "title": "标题", "created_at": NOW.isoformat(), **payload}, expected_revision=0)
        tx.commit()


def get(client, project="default"):
    response = client.get("/api/v2/jobs", params={"project_id": project})
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"items", "counts"}
    assert all(set(row) == FIELDS for row in result["items"])
    assert result["counts"] == {state: sum(row["state"] == state for row in result["items"])
                                for state in ("processing", "pending", "failed")}
    return result["items"]


@pytest.mark.parametrize("status,state", [("staged", "processing"), ("processing", "processing"),
                                           ("ready", "failed"), ("failed", "failed")])
def test_intake_state_table(tray, status, state):
    records, _, client = tray
    put(records, "workspace_items", "item", status=status, source_text="正文", error="processing_failed")
    row, = get(client)
    assert row["id"] == "item" and row["kind"] == "intake" and row["state"] == state
    assert row["target"] == {"type": "item", "id": "item"}
    assert row["pending_count"] == 0


@pytest.mark.parametrize("checkpoint,done", [({"input_kind": "audio", "original_path": "fixture.wav"}, 1),
        ({"source_text": "正文"}, 2), ({"source_text": "正文", "draft": {"title": "整理稿"}}, 3),
        ({"source_text": "正文", "draft": {"title": "整理稿"}, "document_id": "doc"}, 3)])
def test_intake_progress_uses_persisted_checkpoints(tray, checkpoint, done):
    records, _, client = tray
    put(records, "workspace_items", "item", status="processing", **checkpoint)
    row, = get(client)
    assert row["progress"] == {"done": done, "total": 4}
    assert row["updated_at"] == NOW.isoformat()


def test_failed_intake_never_exposes_provider_exception(tray):
    records, _, client = tray
    put(records, "workspace_items", "item", status="failed", error="PRIVATE PROVIDER ERROR")
    row, = get(client)
    assert row["error"] == "processing_failed"


@pytest.mark.parametrize("state,expected", [("queued", "processing"), ("running", "processing"),
        ("result_ready", "processing"), ("waiting_approval", "pending"), ("failed", "failed"),
        ("interrupted", "failed"), ("completed", "done")])
def test_task_state_table(tray, state, expected):
    records, _, client = tray
    put(records, "recognition_tasks", "task", state=state, finished_at=NOW.isoformat(),
        input="PRIVATE PROMPT", error="PRIVATE PROVIDER ERROR")
    row, = get(client)
    assert row["state"] == expected and row["kind"] == "task"
    assert row["target"] == {"type": "task", "id": "task"}
    assert row["progress"] is None
    assert row["pending_count"] == (1 if state == "waiting_approval" else 0)
    assert "PRIVATE" not in str(row)


@pytest.mark.parametrize("hours,visible", [(23.99, True), (24, False), (25, False)])
def test_completed_tasks_expire_after_24_hours(tray, hours, visible):
    records, _, client = tray
    put(records, "recognition_tasks", "task", state="completed",
        finished_at=(NOW - timedelta(hours=hours)).isoformat())
    assert bool(get(client)) is visible


def test_confirmed_pending_sources_are_scoped_deduplicated_and_read_only(tray):
    records, service, client = tray
    put(records, "workspace_items", "item", status="confirmed", document_id="doc", confirmed_at=NOW.isoformat())
    scope = WorkScope("local-user", "default")
    for identity in ("exp-one", "exp-two"):
        service.stage_experience(scope=scope, experience_id=identity, content="正文",
            provenance={"kind": "workspace_confirmed_document", "actor": "local-user",
                        "source_refs": [{"type": "document", "id": "doc", "revision": 1}]})
    service.propose(scope=scope, candidate_id="candidate", content="认识",
                    source_experience_ids=["exp-one", "exp-two"])
    service.propose(scope=scope, candidate_id="candidate-two", content="另一认识",
                    source_experience_ids=["exp-one"])
    other = WorkScope("other-user", "default")
    service.stage_experience(scope=other, experience_id="foreign", content="私密",
        provenance={"kind": "workspace_confirmed_document", "actor": "other-user",
                    "source_refs": [{"type": "document", "id": "doc", "revision": 1}]})
    service.propose(scope=other, candidate_id="foreign-candidate", content="私密", source_experience_ids=["foreign"])
    put(records, "workspace_items", "other-item", status="confirmed", document_id="unrelated")
    put(records, "workspace_items", "other-project", status="processing", project_id="elsewhere")
    before = {name: deepcopy(records.list(name)) for name in
              ("workspace_items", "recognition_candidates", "recognition_experiences", "recognition_tasks")}
    row, = get(client)
    assert row["id"] == "item" and row["state"] == "pending" and row["pending_count"] == 2
    assert row["progress"] == {"done": 4, "total": 4}
    assert get(client) == [row]
    assert all(records.list(name) == value for name, value in before.items())
    assert len(get(client, "elsewhere")) == 1
    for candidate in service.list_candidates(scope=scope):
        service.reject_candidate(scope=scope, candidate_id=candidate.id,
                                 expected_revision=candidate.revision, reviewer="local-user")
    assert get(client) == []


def test_unlisted_states_and_invalid_completion_dates_do_not_appear(tray):
    records, _, client = tray
    for state in ("cancelled", "unknown"):
        put(records, "recognition_tasks", state, state=state)
    put(records, "recognition_tasks", "bad-date", state="completed", finished_at="invalid")
    put(records, "recognition_tasks", "future", state="completed", finished_at=(NOW + timedelta(hours=1)).isoformat())
    assert get(client) == []
    assert client.get("/api/v2/jobs", params={"project_id": "../private"}).status_code == 400


def test_direct_candidate_link_and_task_project_scope(tray):
    records, service, client = tray
    scope = WorkScope("local-user", "default")
    service.stage_experience(scope=scope, experience_id="exp", content="正文",
        provenance={"kind": "workspace_confirmed_document", "actor": "local-user",
                    "source_refs": [{"type": "document", "id": "doc", "revision": 1}]})
    service.propose(scope=scope, candidate_id="candidate", content="认识", source_experience_ids=["exp"])
    put(records, "workspace_items", "item", status="confirmed", document_id="doc", candidate_id="candidate")
    put(records, "recognition_tasks", "foreign-task", project_id="elsewhere", state="running")
    row, = get(client)
    assert row["pending_count"] == 1
    foreign, = get(client, "elsewhere")
    assert foreign["id"] == "foreign-task"


def test_nondefault_namespace_does_not_install_jobs(tmp_path):
    app = FastAPI()
    from backend.memory_app.v2.devices import DeviceRegistry
    app.state.device_registry = DeviceRegistry(tmp_path)
    install_v2_routes(app, runtime_root=tmp_path, records=None, models=None,
                      documents=SimpleNamespace(namespace_id="other"), service=None, workspace=None)
    with TestClient(app) as client:
        assert client.get("/api/v2/jobs").status_code == 404


def test_invalid_completed_results_do_not_break_other_jobs(tray):
    records, _, client = tray
    put(records, "recognition_tasks", "missing-proposal", state="completed", kind="restructure",
        finished_at=NOW.isoformat())
    put(records, "recognition_tasks", "invalid-document", state="completed", document_id="absent",
        finished_at=NOW.isoformat())
    put(records, "recognition_tasks", "valid", state="running")
    row, = get(client)
    assert row["id"] == "valid"


def linked_turn(records, identity="turn", *, project="default", intent="remember", item="item", **receipt):
    thread = "thread-" + identity
    put(records, "v2_threads", thread, project_id=project)
    put(records, "v2_turns", identity, project_id=project, thread_id=thread,
        intent=intent, item_id=item, updated_at=NOW.isoformat(), receipt={intent: receipt})
    return {"type": "turn", "id": identity, "thread_id": thread, "turn_id": identity}


def test_remember_job_targets_valid_thread_even_when_pending(tray):
    records, service, client = tray
    put(records, "workspace_items", "item", status="confirmed", document_id="doc")
    scope = WorkScope("local-user", "default")
    service.stage_experience(scope=scope, experience_id="exp", content="正文",
        provenance={"kind": "workspace_confirmed_document", "actor": "local-user",
                    "source_refs": [{"type": "document", "id": "doc", "revision": 1}]})
    service.propose(scope=scope, candidate_id="candidate", content="认识", source_experience_ids=["exp"])
    target = linked_turn(records, item_id="item", state="done")
    put(records, "v2_workbench_item_states", "turn", item_id="item", turn_id="turn",
        thread_id="thread-turn", state="done", updated_at=NOW.isoformat())
    row, = get(client)
    assert row["state"] == "pending" and row["target"] == target


@pytest.mark.parametrize("drift", ["thread", "item", "receipt", "intent"])
def test_remember_target_rejects_mismatched_links(tray, drift):
    records, _, client = tray
    put(records, "workspace_items", "item", status="processing")
    linked_turn(records, item_id="item", state="processing")
    put(records, "v2_workbench_item_states", "turn", item_id="item", turn_id="turn",
        thread_id="thread-turn", state="processing", updated_at=NOW.isoformat())
    collection, identity = ("v2_threads", "thread-turn") if drift == "thread" else ("v2_turns", "turn")
    row = records.read(collection, identity)
    changes = {"project_id": "other"} if drift == "thread" else {"item_id": "other"} if drift == "item" else {"receipt": {"remember": {"item_id": "other"}}} if drift == "receipt" else {"intent": "do"}
    with records.begin() as tx:
        tx.put(collection, identity, {**row.payload, **changes}, expected_revision=row.revision)
        tx.commit()
    row, = get(client)
    assert row["target"] == {"type": "item", "id": "item"}


@pytest.mark.parametrize("state,expected", [("researching", "processing"), ("preparing", "processing"),
    ("running", "processing"), ("waiting_approval", "pending"), ("done", "done"), ("failed", "failed")])
def test_do_receipts_appear_in_tray_without_execution_or_writes(tray, state, expected):
    records, _, client = tray
    target = linked_turn(records, intent="do", item=None, title="干活标题", state=state,
        task_id=None, progress={"done": 2, "total": 4}, error="PRIVATE PROVIDER ERROR",
        experts=[{"text": "PRIVATE PROMPT"}])
    before = deepcopy(records.list("v2_turns"))
    row, = get(client)
    assert row["kind"] == "task" and row["state"] == expected
    assert row["target"] == target and row["progress"] == {"done": 2, "total": 4}
    assert row["pending_count"] == (1 if state == "waiting_approval" else 0)
    assert "PRIVATE" not in str(row)
    assert records.list("v2_turns") == before
    assert get(client, "other") == []


@pytest.mark.parametrize("hours,visible", [(23.99, True), (24, False), (25, False), (-1, False)])
def test_done_do_receipt_window(tray, hours, visible):
    records, _, client = tray
    linked_turn(records, intent="do", item=None, state="done")
    row = records.read("v2_turns", "turn")
    with records.begin() as tx:
        tx.put("v2_turns", "turn", {**row.payload, "updated_at": (NOW-timedelta(hours=hours)).isoformat()}, expected_revision=row.revision)
        tx.commit()
    assert bool(get(client)) is visible


def test_do_task_deduplication_and_invalid_thread_does_not_hide_task(tray):
    records, _, client = tray
    put(records, "recognition_tasks", "task", state="running")
    target = linked_turn(records, intent="do", item=None, state="running", task_id="task")
    row, = get(client)
    assert row["target"] == target
    thread = records.read("v2_threads", "thread-turn")
    with records.begin() as tx:
        tx.put("v2_threads", thread.object_id, {**thread.payload, "project_id": "other"}, expected_revision=thread.revision)
        tx.commit()
    row, = get(client)
    assert row["id"] == "task" and row["target"] == {"type": "task", "id": "task"}


def test_legacy_intake_and_completed_task_use_real_document(tray):
    from core.document_engine import SQLiteDocumentRepository, DocumentDraft
    records, _, client = tray
    document = SQLiteDocumentRepository(records, namespace_id="default").create(
        DocumentDraft("整理稿", "note", "正文", ({"source_id": "source", "locator": "fixture"},), project_id="default"))
    put(records, "workspace_items", "item", status="failed", document_id=document["id"])
    put(records, "recognition_tasks", "task", state="completed", document_id=document["id"], finished_at=NOW.isoformat())
    rows = get(client)
    assert len(rows) == 2
    assert all(row["target"] == {"type": "document", "id": document["id"]} for row in rows)


def test_do_foreign_task_is_never_exposed_or_deduplicated(tray):
    records, _, client = tray
    put(records, "recognition_tasks", "task", project_id="other", state="running")
    linked_turn(records, intent="do", item=None, state="running", task_id="task")
    assert get(client) == []
    row, = get(client, "other")
    assert row["id"] == "task" and row["target"] == {"type": "task", "id": "task"}


def test_do_saved_running_receipt_projects_actual_task_completion_without_writes(tray):
    from core.document_engine import SQLiteDocumentRepository, DocumentDraft
    records, _, client = tray
    document = SQLiteDocumentRepository(records, namespace_id="default").create(
        DocumentDraft("成果", "note", "正文", ({"source_id": "source", "locator": "fixture"},), project_id="default"))
    put(records, "recognition_tasks", "task", state="completed", document_id=document["id"], finished_at=NOW.isoformat())
    target = linked_turn(records, intent="do", item=None, state="running", task_id="task", progress={"done": 2, "total": 3})
    before = deepcopy(records.list("v2_turns")), deepcopy(records.list("recognition_tasks"))
    row, = get(client)
    assert row["state"] == "done" and row["target"] == target
    assert row["progress"] == {"done": 3, "total": 3}
    assert (records.list("v2_turns"), records.list("recognition_tasks")) == before
