from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.job_runtime import build_rebuild_job_repository
from backend.memory_app.document_visibility import recognition_document_visible as _document_visible
from backend.memory_app.legacy_intake_review import LegacyIntakeReview
from backend.memory_app.workspace import install_workspace_routes
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import (
    DocumentExpectedRevisionError, DocumentRepositoryError,
    ObjectStoreDocumentRepository, SQLiteDocumentRepository,
)
from core.document_engine.ports import DocumentDraft
from core.job_runner.runtime import InMemoryJobRepository
from core.storage_provider import (
    JsonObjectStore, ObjectStoreRevisionError, SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)


def _service(tmp_path, *, with_document=True, job_status="completed"):
    root = tmp_path / "runtime"
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    store.write("sources", "source-one", {
        "id": "source-one", "project_id": "project-a", "title": "原文标题", "type": "text",
        "metadata": {"content_snapshot": "完整原文。"}, "created_at": "2026-09-24T00:00:00Z",
    }, expected_revision=0)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    docs = SQLiteDocumentRepository(records)
    if with_document:
        document = docs.create(DocumentDraft(
            title="原文标题", document_type="legacy_text", markdown="机器草稿",
            source_refs=({"source_id": "source-one", "locator": "text:0:5"},),
            project_id="project-a",
        ))
    else:
        document = None
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-one", {
            "schema_version": "1.0.0", "id": "review-source-one",
            "source_id": "source-one", "project_id": "project-a",
            "job_id": "job-capture", "state": "pending", "source_revision": 1,
        }, expected_revision=0)
        tx.commit()
    jobs = InMemoryJobRepository()
    jobs.save({"id": "job-capture", "status": job_status, "source_id": "source-one"})
    service = LegacyIntakeReview(root, records, docs, object_store=store, jobs=jobs)
    return service, docs, records, document, jobs


def _save_review(service, source_id, project_id, markdown):
    current = service.get(source_id, project_id)
    return service.save_draft(source_id, project_id, markdown,
                              expected_revision=current["revision"],
                              expected_document_basis=current["document_basis"])


def _confirm_review(service, source_id, project_id):
    current = service.get(source_id, project_id)
    return service.confirm(source_id, project_id, expected_revision=current["revision"],
                           expected_document_basis=current["document_basis"],
                           expected_markdown=current["draft_markdown"])


def _review_http(service, docs, records, jobs):
    persisted_jobs = build_rebuild_job_repository(service.runtime_root, service.object_store)
    for job in jobs.all():
        persisted_jobs.save({**job, "job_type": "capture"})
    app = FastAPI()
    install_workspace_routes(app, runtime_root=service.runtime_root, records=records,
                             models=object(), documents=docs, service=RecognitionService(records))
    return TestClient(app)


def test_http_review_requires_observed_versions_and_replays_confirmation(tmp_path):
    service, docs, records, _, jobs = _service(tmp_path)
    http = _review_http(service, docs, records, jobs)
    endpoint = "/api/workspace/v1/legacy-reviews/source-one"
    seen = http.get(endpoint, params={"project_id": "project-a"}).json()
    original = {"project_id": "project-a", "expected_revision": seen["revision"],
                "expected_document_basis": seen["document_basis"], "markdown": "窗口一修改"}
    for missing in ("expected_revision", "expected_document_basis"):
        response = http.put(endpoint + "/draft", json={k: v for k, v in original.items() if k != missing})
        assert response.status_code == 422
    for key, values in (("expected_revision", [True, 0, "1"]),
                        ("expected_document_basis", [{}, {"id": "doc", "revision": True}, "doc"])):
        for value in values:
            assert http.put(endpoint + "/draft", json={**original, key: value}).status_code == 422
    saved = http.put(endpoint + "/draft", json=original)
    assert saved.status_code == 200
    current = saved.json()
    assert current["revision"] == seen["revision"] + 1
    rejected = http.put(endpoint + "/draft", json={**original, "markdown": "窗口二过期修改"})
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {"code": "draft_revision_conflict", "current": current}
    assert service.get("source-one", "project-a")["draft_markdown"] == original["markdown"]
    wrong_project = http.put(endpoint + "/draft", json={**original, "project_id": "project-b"})
    assert wrong_project.status_code == 404
    assert "current" not in wrong_project.json()["detail"]
    confirmation = {"project_id": "project-a", "expected_revision": current["revision"],
                    "expected_document_basis": current["document_basis"]}
    assert http.post(endpoint + "/confirm", json=confirmation).status_code == 422
    confirmation["expected_markdown"] = current["draft_markdown"]
    result = http.post(endpoint + "/confirm", json=confirmation)
    assert result.status_code == 200
    assert result.json()["status"] == "confirmed"
    assert http.post(endpoint + "/confirm", json=confirmation).json() == result.json()
    assert len(docs.revisions(result.json()["document_id"])) == 2


def test_existing_document_keeps_identity_and_adds_reviewed_revision(tmp_path):
    service, docs, _, original, _ = _service(tmp_path)
    assert service.get("source-one", "project-a")["source_text"] == "完整原文。"
    _save_review(service, "source-one", "project-a", "人工审核稿")
    confirmed = _confirm_review(service, "source-one", "project-a")
    assert confirmed["status"] == "confirmed"
    assert confirmed["document_id"] == original["id"]
    assert confirmed["document_revision"] == 2
    assert docs.markdown(original["id"]) == "人工审核稿"
    assert _confirm_review(service, "source-one", "project-a")["document_revision"] == 2


def test_completed_job_without_document_creates_one_in_confirm_transaction(tmp_path):
    service, docs, _, _, _ = _service(tmp_path, with_document=False)
    confirmed = _confirm_review(service, "source-one", "project-a")
    assert confirmed["status"] == "confirmed"
    assert confirmed["document_revision"] == 1
    assert docs.markdown(confirmed["document_id"]) == "完整原文。"
    assert docs.read(confirmed["document_id"])["source_refs"][0]["source_id"] == "source-one"


@pytest.mark.parametrize("operation", ["save", "confirm"])
def test_review_rejects_projection_changed_before_write(tmp_path, monkeypatch, operation):
    service, docs, records, document, _ = _service(tmp_path)
    original_begin = records.begin

    def concurrent_begin():
        monkeypatch.setattr(records, "begin", original_begin)
        _save_review(service, "source-one", "project-a", "另一窗口刚保存的新稿")
        return original_begin()

    monkeypatch.setattr(records, "begin", concurrent_begin)
    with pytest.raises(ValueError, match="review_revision_conflict"):
        if operation == "save":
            _save_review(service, "source-one", "project-a", "过期窗口的稿件")
        else:
            _confirm_review(service, "source-one", "project-a")
    assert service.get("source-one", "project-a")["draft_markdown"] == "另一窗口刚保存的新稿"
    assert records.read("workspace_review_intents", "review-source-one").payload["state"] == "pending"
    assert docs.markdown(document["id"]) == "机器草稿"


@pytest.mark.parametrize("operation", ["save", "confirm"])
def test_review_rejects_another_window_revision(tmp_path, operation):
    service, docs, records, document, _ = _service(tmp_path)
    seen = service.get("source-one", "project-a")
    saved = _save_review(service, "source-one", "project-a", "另一窗口稿件")
    before = records.list("workspace_review_intents")
    version = {"expected_revision": seen["revision"], "expected_document_basis": seen["document_basis"]}
    with pytest.raises(ValueError, match="review_revision_conflict") as caught:
        if operation == "save":
            service.save_draft("source-one", "project-a", "本机旧稿", **version)
        else:
            service.confirm("source-one", "project-a", expected_markdown=seen["draft_markdown"], **version)
    assert caught.value.current == saved
    assert records.list("workspace_review_intents") == before
    assert docs.markdown(document["id"]) == "机器草稿"


@pytest.mark.parametrize("operation", ["save", "confirm"])
@pytest.mark.parametrize("saved_draft", [False, True])
def test_review_rejects_changed_document_even_with_saved_draft(tmp_path, operation, saved_draft):
    service, docs, records, document, _ = _service(tmp_path)
    if saved_draft:
        _save_review(service, "source-one", "project-a", "之前保存的审核稿")
    seen = service.get("source-one", "project-a")
    docs.save_user_edit(document["id"], markdown="资料库的更新", expected_revision=1)
    before = records.list("workspace_review_intents")
    version = {"expected_revision": seen["revision"], "expected_document_basis": seen["document_basis"]}
    with pytest.raises(ValueError, match="review_revision_conflict"):
        if operation == "save":
            service.save_draft("source-one", "project-a", "本机旧稿", **version)
        else:
            service.confirm("source-one", "project-a", expected_markdown=seen["draft_markdown"], **version)
    assert records.list("workspace_review_intents") == before
    assert docs.markdown(document["id"]) == "资料库的更新"


@pytest.mark.parametrize("fallback", ["source", "extraction"])
def test_confirmation_matches_seen_fallback_text(tmp_path, fallback):
    service, docs, records, _, _ = _service(tmp_path, with_document=False)
    seen = service.get("source-one", "project-a")
    source = service.object_store.read("sources", "source-one")
    service.object_store.write("sources", "source-one", {
        **source, "metadata": {"content_snapshot": "变化后的原文"} if fallback == "source" else {},
    }, expected_revision=1)
    if fallback == "extraction":
        service.object_store.write("source_content_reads", "read-one", {
            "id": "read-one", "source_id": "source-one", "status": "completed", "text": "新提取正文",
        }, expected_revision=0)
    before = records.list("workspace_review_intents")
    with pytest.raises(ValueError, match="review_revision_conflict"):
        service.confirm("source-one", "project-a", expected_revision=seen["revision"],
                        expected_document_basis=None, expected_markdown=seen["draft_markdown"])
    assert records.list("workspace_review_intents") == before
    assert docs.list() == ()


def test_confirmation_replays_only_the_reviewed_version(tmp_path):
    service, docs, _, _, _ = _service(tmp_path)
    seen = _save_review(service, "source-one", "project-a", "审核后的内容")
    request = {"expected_revision": seen["revision"], "expected_document_basis": seen["document_basis"],
               "expected_markdown": seen["draft_markdown"]}
    first = service.confirm("source-one", "project-a", **request)
    assert service.confirm("source-one", "project-a", **request) == first
    for change in ({"expected_revision": 1}, {"expected_markdown": "并未审核过"}):
        with pytest.raises(ValueError, match="review_revision_conflict"):
            service.confirm("source-one", "project-a", **{**request, **change})
    assert len(docs.revisions(first["document_id"])) == 2


@pytest.mark.parametrize("change", ["document", "source", "extraction"])
def test_confirmation_rechecks_dependencies_after_pre_read(tmp_path, monkeypatch, change):
    service, docs, records, document, _ = _service(tmp_path, with_document=change == "document")
    if change == "extraction":
        source = service.object_store.read("sources", "source-one")
        service.object_store.write("sources", "source-one", {**source, "metadata": {}}, expected_revision=1)
        service.object_store.write("source_content_reads", "read-1", {
            "source_id": "source-one", "status": "completed", "text": "旧提取正文",
        }, expected_revision=0)
    original_begin = records.begin

    def concurrent_begin():
        monkeypatch.setattr(records, "begin", original_begin)
        if change == "document":
            docs.save_user_edit(document["id"], markdown="其他入口更新", expected_revision=1)
        elif change == "source":
            source = service.object_store.read("sources", "source-one")
            service.object_store.write("sources", "source-one", {
                **source, "metadata": {"content_snapshot": "来源刚变更"},
            }, expected_revision=1)
        else:
            service.object_store.write("source_content_reads", "read-2", {
                "source_id": "source-one", "status": "completed", "text": "提取结果刚更新",
            }, expected_revision=0)
        return original_begin()

    monkeypatch.setattr(records, "begin", concurrent_begin)
    with pytest.raises(ValueError, match="review_revision_conflict"):
        _confirm_review(service, "source-one", "project-a")
    assert records.read("workspace_review_intents", "review-source-one").payload["state"] == "pending"
    assert docs.markdown(document["id"]) == "其他入口更新" if document else not docs.list()


def test_save_receipt_does_not_substitute_a_later_windows_write(tmp_path, monkeypatch):
    service, _, records, _, _ = _service(tmp_path)
    original_begin = records.begin

    def begin_with_later_writer():
        monkeypatch.setattr(records, "begin", original_begin)
        tx = original_begin()
        commit = tx.commit
        def commit_then_other_window():
            commit()
            _save_review(service, "source-one", "project-a", "后来窗口的草稿")
        monkeypatch.setattr(tx, "commit", commit_then_other_window)
        return tx

    monkeypatch.setattr(records, "begin", begin_with_later_writer)
    receipt = _save_review(service, "source-one", "project-a", "当前窗口的草稿")
    assert receipt["draft_markdown"] == "当前窗口的草稿"
    assert receipt["revision"] == 2
    later = service.get("source-one", "project-a")
    assert later["draft_markdown"] == "后来窗口的草稿"
    assert later["revision"] == 3


def test_confirmed_projection_keeps_the_frozen_review_after_document_edits(tmp_path):
    service, docs, _, document, _ = _service(tmp_path)
    confirmed = _confirm_review(service, "source-one", "project-a")
    docs.save_user_edit(document["id"], markdown="入库后的正式文档编辑", expected_revision=2)
    later = service.get("source-one", "project-a")
    assert later["draft_markdown"] == confirmed["draft_markdown"] == "机器草稿"
    assert later["document_revision"] == 2
    assert later["document_basis"] == {"id": document["id"], "revision": 3}
    assert docs.markdown(document["id"]) == "入库后的正式文档编辑"


@pytest.mark.parametrize("with_document", [False, True])
def test_concurrent_confirmation_returns_the_same_committed_result(tmp_path, monkeypatch, with_document):
    service, docs, records, _, _ = _service(tmp_path, with_document=with_document)
    finish = service._finish_pending
    completed = []

    def another_request_finishes_first(intent, source_id, project_id):
        monkeypatch.setattr(service, "_finish_pending", finish)
        completed.append(finish(intent, source_id, project_id))
        # The outer request already read this same frozen intent before the
        # other request committed; its write must replay that exact outcome.
        return finish(intent, source_id, project_id)

    monkeypatch.setattr(service, "_finish_pending", another_request_finishes_first)
    result = _confirm_review(service, "source-one", "project-a")
    assert result == completed[0]
    assert result["status"] == "confirmed"
    assert len(docs.list()) == 1
    assert len(docs.revisions(result["document_id"])) == (2 if with_document else 1)
    assert records.read("workspace_review_intents", "review-source-one").payload["confirmed_markdown"] == result["draft_markdown"]


def test_confirmation_replays_document_compare_and_swap_lost_to_same_confirmation(tmp_path, monkeypatch):
    service, docs, records, _, _ = _service(tmp_path)
    original = SQLiteDocumentRepository.save_user_edit

    def competing_confirmation(self, *args, **kwargs):
        monkeypatch.setattr(SQLiteDocumentRepository, "save_user_edit", original)
        frozen = records.read("workspace_review_intents", "review-source-one").payload
        service._finish_pending(frozen, "source-one", "project-a")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteDocumentRepository, "save_user_edit", competing_confirmation)
    result = _confirm_review(service, "source-one", "project-a")
    assert result["status"] == "confirmed"
    assert len(docs.revisions(result["document_id"])) == 2


@pytest.mark.parametrize("failure", ["unknown", "different_frozen_intent"])
def test_confirmation_replay_does_not_hide_unrelated_errors(tmp_path, monkeypatch, failure):
    service, _, records, _, _ = _service(tmp_path)
    original = service._finish_pending

    def completed_then_error(intent, source_id, project_id):
        original(intent, source_id, project_id)
        if failure == "unknown":
            raise RuntimeError("unrelated repository failure")
        with records.begin() as tx:
            row = tx.read("workspace_review_intents", "review-source-one")
            tx.put("workspace_review_intents", row.object_id,
                   {**row.payload, "confirmed_markdown": "不同的冻结审核"}, expected_revision=row.revision)
            tx.commit()
        raise ValueError("review_intent_changed")

    monkeypatch.setattr(service, "_finish_pending", completed_then_error)
    with pytest.raises(RuntimeError if failure == "unknown" else ValueError,
                       match="unrelated repository failure" if failure == "unknown" else "review_intent_changed"):
        _confirm_review(service, "source-one", "project-a")


def test_pending_failed_and_project_scope_do_not_publish(tmp_path):
    service, docs, _, original, jobs = _service(tmp_path, job_status="pending")
    assert service.get("source-one", "project-b") is None
    assert service.list("project-b") == ()
    assert service.get("source-one", "project-a")["status"] == "processing"
    with pytest.raises(ValueError, match="review_not_ready"):
        _confirm_review(service, "source-one", "project-a")
    jobs.save({"id": "job-capture", "status": "failed", "source_id": "source-one", "error": "bad input"})
    assert service.get("source-one", "project-a")["status"] == "failed"
    with pytest.raises(ValueError, match="review_not_ready"):
        _save_review(service, "source-one", "project-a", "wrong")
    assert docs.read(original["id"])["revision"] == 1


def test_structured_job_failure_projects_a_renderable_error(tmp_path):
    service, _, _, _, jobs = _service(tmp_path, job_status="pending")
    jobs.save({"id": "job-capture", "status": "failed", "source_id": "source-one",
               "error": {"code": "local_video_transform_failed", "detail": "private provider output"}})
    review = service.get("source-one", "project-a")
    assert review["status"] == "failed"
    assert review["error"] == "local_video_transform_failed"


def test_confirmed_document_archive_does_not_break_review_list(tmp_path):
    service, docs, _, original, _ = _service(tmp_path)
    _confirm_review(service, "source-one", "project-a")
    docs.archive(original["id"], expected_revision=2)
    review = service.list("project-a")[0]
    assert review["status"] == "confirmed"
    assert review["document_status"] == "archived"
    assert review["document_id"] == original["id"]


@pytest.mark.parametrize("fault,reason", [
    ("source_project", "review_source_binding_changed"),
    ("document_project", "review_document_project_mismatch"),
    ("archived", "review_document_archived"),
    ("multiple", "review_multiple_documents"),
])
def test_review_list_isolates_broken_binding_without_exposing_content(tmp_path, fault, reason):
    service, docs, records, original, jobs = _service(tmp_path)
    service.object_store.write("sources", "source-two", {
        "id": "source-two", "project_id": "project-a", "title": "健康资料",
        "type": "text", "metadata": {"content_snapshot": "健康原文"},
    }, expected_revision=0)
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-two", {
            "id": "review-source-two", "source_id": "source-two", "project_id": "project-a",
            "job_id": "job-two", "state": "pending", "source_revision": 1,
        }, expected_revision=0)
        tx.commit()
    jobs.save({"id": "job-two", "status": "completed", "source_id": "source-two"})
    if fault == "source_project":
        service.object_store.write("sources", "source-one", {
            "id": "source-one", "project_id": "project-b", "title": "private-title",
            "type": "text", "metadata": {"content_snapshot": "private-content"},
            "original_url": "https://private.example.test",
        }, expected_revision=1)
    elif fault == "document_project":
        with records.begin() as tx:
            row = tx.read("documents", original["id"])
            tx.put("documents", row.object_id, {**row.payload, "project_id": "project-b"},
                   expected_revision=row.revision)
            tx.commit()
    elif fault == "archived":
        docs.archive(original["id"], expected_revision=1)
    else:
        docs.create(DocumentDraft(title="重复", document_type="legacy_text", markdown="private-content",
                                  source_refs=({"source_id": "source-one", "locator": "text:0:4"},),
                                  project_id="project-a"))
    before = records.list("workspace_review_intents")
    broken, healthy = service.list("project-a")
    assert broken["id"] == "review-source-one"
    assert broken["status"] == "failed"
    assert broken["projection_error"] is True
    assert broken["error"] == reason
    assert not broken.get("source_text") and not broken.get("draft_markdown")
    assert not broken.get("document_id") and not broken.get("original_url")
    assert "private" not in str(broken)
    assert healthy["id"] == "review-source-two" and healthy["status"] == "ready"
    assert healthy["source_text"] == "健康原文"
    assert records.list("workspace_review_intents") == before
    http = _review_http(service, docs, records, jobs)
    endpoint = "/api/workspace/v1/legacy-reviews"
    response = http.get(endpoint, params={"project_id": "project-a"})
    assert response.status_code == 200
    assert response.json()["items"] == [broken, healthy]
    assert http.get(endpoint, params={"project_id": "project-b"}).json() == {"items": []}
    detail = http.get(endpoint + "/source-one", params={"project_id": "project-a"})
    assert detail.status_code == 409
    assert detail.json()["detail"] == reason
    recognition = http.post(endpoint + "/source-one/recognition", json={"project_id": "project-a"})
    assert recognition.status_code == 409
    assert recognition.json()["detail"] == reason
    for suffix, method, body in (
        ("/draft", http.put, {"markdown": "不得覆盖"}),
        ("/confirm", http.post, {}),
    ):
        rejected = method(endpoint + "/source-one" + suffix, json={"project_id": "project-a", "expected_revision": 1, "expected_document_basis": None, "expected_markdown": "", **body})
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == reason
    assert records.list("workspace_review_intents") == before
    with pytest.raises(ValueError, match=reason):
        _confirm_review(service, "source-one", "project-a")
    assert _confirm_review(service, "source-two", "project-a")["status"] == "confirmed"


@pytest.mark.parametrize("error", [RuntimeError("repository unavailable"), ValueError("unknown problem")])
def test_review_list_does_not_hide_unexpected_failures(tmp_path, monkeypatch, error):
    service, _, _, _, _ = _service(tmp_path)
    def broken(_intent, **_options):
        raise error
    monkeypatch.setattr(service, "_project", broken)
    with pytest.raises(type(error), match=str(error)):
        service.list("project-a")


def test_recover_after_document_write_and_reject_concurrent_edit(tmp_path, monkeypatch):
    service, docs, records, original, _ = _service(tmp_path)
    _save_review(service, "source-one", "project-a", "审核结果")
    original_save = SQLiteDocumentRepository.save_user_edit
    calls = 0

    def after_write(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_save(self, *args, **kwargs)
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(SQLiteDocumentRepository, "save_user_edit", after_write)
    with pytest.raises(RuntimeError, match="interruption"):
        _confirm_review(service, "source-one", "project-a")
    monkeypatch.setattr(SQLiteDocumentRepository, "save_user_edit", original_save)
    assert records.read("workspace_review_intents", "review-source-one").payload["state"] == "confirming"
    assert service.recover_confirming()[0]["status"] == "confirmed"
    assert calls == 1
    assert docs.read(original["id"])["revision"] == 2

    another, docs2, _, existing, _ = _service(tmp_path / "another")
    _save_review(another, "source-one", "project-a", "审核结果")
    with docs2.records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-one")
        tx.put("workspace_review_intents", "review-source-one", {
            **row.payload, "state": "confirming", "confirmed_markdown": "审核结果",
            "expected_document_id": existing["id"], "expected_document_revision": 1,
        }, expected_revision=row.revision)
        tx.commit()
    docs2.save_user_edit(existing["id"], markdown="他人修改", expected_revision=1)
    with pytest.raises(ValueError, match="review_document_concurrent_edit"):
        another.recover_confirming()


def test_startup_recovery_continues_after_first_conflict_and_is_idempotent(tmp_path):
    service, docs, records, first_document, _ = _service(tmp_path)
    document_ids = [first_document["id"]]
    for index in (2, 3):
        source_id = f"source-x{index}"
        project_id = f"project-{index}"
        service.object_store.write("sources", source_id, {
            "id": source_id, "project_id": project_id, "title": f"资料 {index}",
            "type": "text", "metadata": {"content_snapshot": f"原文 {index}"},
        }, expected_revision=0)
        document = docs.create(DocumentDraft(
            title=f"资料 {index}", document_type="legacy_text", markdown="机器草稿",
            source_refs=({"source_id": source_id, "locator": "text:0:4"},),
            project_id=project_id,
        ))
        document_ids.append(document["id"])
        with records.begin() as tx:
            tx.put("workspace_review_intents", "review-" + source_id, {
                "id": "review-" + source_id, "source_id": source_id,
                "project_id": project_id, "job_id": f"job-{index}",
                "state": "pending", "source_revision": 1,
            }, expected_revision=0)
            tx.commit()
    for index, source_id in enumerate(("source-one", "source-x2", "source-x3")):
        intent_id = "review-" + source_id
        with records.begin() as tx:
            row = tx.read("workspace_review_intents", intent_id)
            tx.put("workspace_review_intents", intent_id, {
                **row.payload, "state": "confirming", "confirmed_markdown": f"冻结审核稿 {index}",
                "confirmed_source_revision": 1,
                "expected_document_id": document_ids[index],
                "expected_document_revision": 1,
            }, expected_revision=row.revision)
            tx.commit()
    docs.save_user_edit(document_ids[0], markdown="他人修改", expected_revision=1)
    frozen = records.read("workspace_review_intents", "review-source-one")
    with pytest.raises(ValueError, match="review_document_concurrent_edit"):
        service.recover_confirming()

    app = FastAPI()
    install_workspace_routes(app, runtime_root=service.runtime_root, records=records,
                             models=object(), documents=docs, service=RecognitionService(records))
    report = app.state.legacy_review_recovery_report
    assert report["failures"] == ({
        "intent_id": "review-source-one", "source_id": "source-one",
        "project_id": "project-a", "reason": "review_document_concurrent_edit",
    },)
    assert app.state.legacy_review_recovery_failures == report["failures"]
    assert [item["source_id"] for item in report["recovered"]] == ["source-x2", "source-x3"]
    assert records.read("workspace_review_intents", "review-source-one") == frozen
    assert docs.markdown(document_ids[0]) == "他人修改"
    assert [docs.read(document_id)["revision"] for document_id in document_ids] == [2, 2, 2]

    restarted = FastAPI()
    install_workspace_routes(restarted, runtime_root=service.runtime_root, records=records,
                             models=object(), documents=docs, service=RecognitionService(records))
    assert restarted.state.legacy_review_recovery_report == {
        "recovered": (), "failures": report["failures"],
    }
    assert records.read("workspace_review_intents", "review-source-one") == frozen
    assert [docs.read(document_id)["revision"] for document_id in document_ids] == [2, 2, 2]


@pytest.mark.parametrize("error, reason", [
    (ValueError("review_source_binding_changed"), "review_source_binding_changed"),
    (DocumentExpectedRevisionError("private draft"), "review_revision_conflict"),
    (SQLiteUnitOfWorkConflict("private data"), "review_revision_conflict"),
    (ObjectStoreRevisionError("private data"), "review_revision_conflict"),
])
def test_startup_report_redacts_known_conflicts(tmp_path, monkeypatch, error, reason):
    service, _, records, _, _ = _service(tmp_path)
    with records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-one")
        tx.put("workspace_review_intents", row.object_id,
               {**row.payload, "state": "confirming"}, expected_revision=row.revision)
        tx.commit()
    def fail(*_args):
        raise error
    monkeypatch.setattr(service, "_finish", fail)
    assert service.recover_confirming_report()["failures"] == ({
        "intent_id": "review-source-one", "source_id": "source-one",
        "project_id": "project-a", "reason": reason,
    },)


def test_startup_report_handles_wrapped_revision_conflict_and_rejects_unknown(tmp_path, monkeypatch):
    service, _, records, _, _ = _service(tmp_path)
    with records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-one")
        tx.put("workspace_review_intents", row.object_id,
               {**row.payload, "state": "confirming"}, expected_revision=row.revision)
        tx.commit()

    def wrapped(*_args):
        try:
            raise ObjectStoreRevisionError("private data")
        except ObjectStoreRevisionError as exc:
            raise DocumentRepositoryError("private data") from exc
    monkeypatch.setattr(service, "_finish", wrapped)
    assert service.recover_confirming_report()["failures"][0]["reason"] == "review_revision_conflict"
    for error in (ValueError("unknown private value"), KeyError("private key"), OSError("disk broken"),
                  DocumentRepositoryError("unknown private failure")):
        def unknown(*_args):
            raise error
        monkeypatch.setattr(service, "_finish", unknown)
        with pytest.raises(type(error)):
            service.recover_confirming_report()


def test_startup_report_skips_invalid_identity_without_touching_frozen_content(tmp_path):
    service, _, records, _, _ = _service(tmp_path)
    with records.begin() as tx:
        row = tx.read("workspace_review_intents", "review-source-one")
        tx.put("workspace_review_intents", row.object_id, {
            **row.payload, "id": "review-another", "state": "confirming",
            "confirmed_markdown": "冻结的审核正文",
        }, expected_revision=row.revision)
        tx.commit()
    frozen = records.read("workspace_review_intents", "review-source-one")
    assert service.recover_confirming_report()["failures"] == ({
        "intent_id": "review-source-one", "source_id": "source-one",
        "project_id": "project-a", "reason": "review_intent_invalid",
    },)
    assert records.read("workspace_review_intents", "review-source-one") == frozen


def test_rejects_cross_project_and_archived_document(tmp_path):
    service, docs, _, original, _ = _service(tmp_path)
    with docs.records.begin() as tx:
        row = tx.read("documents", original["id"])
        tx.put("documents", original["id"], {**row.payload, "project_id": "project-b"},
               expected_revision=row.revision)
        tx.commit()
    with pytest.raises(ValueError, match="review_document_project_mismatch"):
        service.get("source-one", "project-a")

    service2, docs2, _, original2, _ = _service(tmp_path / "archive")
    docs2.archive(original2["id"], expected_revision=1)
    with pytest.raises(ValueError, match="review_document_archived"):
        _confirm_review(service2, "source-one", "project-a")


def test_json_document_mode_recovers_new_document_after_interruption(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    store.write("sources", "source-one", {
        "id": "source-one", "project_id": "project-a", "title": "原文标题", "type": "text",
        "metadata": {"content_snapshot": "完整原文。"},
    }, expected_revision=0)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-one", {
            "id": "review-source-one", "source_id": "source-one", "project_id": "project-a",
            "job_id": "job-capture", "state": "pending", "source_revision": 1,
        }, expected_revision=0)
        tx.commit()
    jobs = InMemoryJobRepository()
    jobs.save({"id": "job-capture", "source_id": "source-one", "status": "completed"})
    documents = ObjectStoreDocumentRepository(store)
    service = LegacyIntakeReview(root, records, documents, object_store=store, jobs=jobs)
    original = ObjectStoreDocumentRepository.create_or_replay_generated

    def interrupted(self, draft):
        original(self, draft)
        raise RuntimeError("interrupted after Document write")

    monkeypatch.setattr(ObjectStoreDocumentRepository, "create_or_replay_generated", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _confirm_review(service, "source-one", "project-a")
    monkeypatch.setattr(ObjectStoreDocumentRepository, "create_or_replay_generated", original)
    recovered = service.recover_confirming()
    assert len(recovered) == 1
    assert recovered[0]["status"] == "confirmed"
    assert documents.markdown(recovered[0]["document_id"]) == "完整原文。"
    assert len(documents.list()) == 1


def test_confirmed_legacy_document_enters_new_recognition_once_per_revision(tmp_path):
    root = tmp_path / "runtime"
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    store.write("sources", "source-one", {
        "id": "source-one", "project_id": "project-a", "title": "原文", "type": "text",
        "metadata": {"content_snapshot": "原始证据"},
    }, expected_revision=0)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    document = documents.create(DocumentDraft(
        title="原文", document_type="legacy_text", markdown="机器草稿",
        source_refs=({"source_id": "source-one", "locator": "text:0:4"},), project_id="project-a"))
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-one", {
            "id": "review-source-one", "source_id": "source-one", "project_id": "project-a",
            "job_id": "job-capture", "state": "pending", "source_revision": 1,
        }, expected_revision=0)
        tx.commit()
    jobs = build_rebuild_job_repository(root, store)
    jobs.save({"id": "job-capture", "job_type": "capture", "status": "completed", "source_id": "source-one"})
    app = FastAPI()
    install_workspace_routes(app, runtime_root=root, records=records, models=object(),
                             documents=documents, service=RecognitionService(records))
    http = TestClient(app)
    endpoint = "/api/workspace/v1/legacy-reviews/source-one"
    assert not _document_visible(records, WorkScope("local-user", "project-a"), document["id"])
    assert http.post(endpoint + "/recognition", json={"project_id": "project-a"}).status_code == 409
    seen = http.get(endpoint, params={"project_id": "project-a"}).json()
    confirmed = http.post(endpoint + "/confirm", json={
        "project_id": "project-a", "expected_revision": seen["revision"],
        "expected_document_basis": seen["document_basis"], "expected_markdown": seen["draft_markdown"],
    }).json()
    assert confirmed["document_revision"] == 2
    assert _document_visible(records, WorkScope("local-user", "project-a"), document["id"])
    response = http.post(endpoint + "/recognition", json={"project_id": "project-a"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert http.post(endpoint + "/recognition", json={"project_id": "project-a"}).json() == result
    experience = records.read("recognition_experiences", result["experience_id"]).payload
    assert experience["content"] == documents.markdown(document["id"], revision=2)
    assert experience["provenance"]["source_refs"] == [{"type": "document", "id": document["id"], "revision": 2}]
    assert records.read("recognition_candidates", result["candidate_id"]).payload["state"] == "pending"
    assert http.post(endpoint + "/recognition", json={"project_id": "project-b"}).status_code == 404
    documents.save_user_edit(document["id"], markdown="后续人工修订", expected_revision=2)
    newer = http.post(endpoint + "/recognition", json={"project_id": "project-a"}).json()
    assert newer["document_revision"] == 3
    assert records.read("recognition_experiences", newer["experience_id"]).payload["content"] == "后续人工修订"
