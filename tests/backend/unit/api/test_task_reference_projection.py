from __future__ import annotations

import pytest

from backend.api import task_reference_projection as task_projection
from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from backend.api.task_reference_projection import (
    TaskReferenceError,
    list_task_references,
    task_reference_detail,
    task_ref_for_favorite_batch,
    task_ref_for_media_processing_job,
    task_ref_for_workbench_content_transform,
    task_ref_for_world_action,
    workbench_transform_task_ref_for_document,
)
from core.storage_provider import JsonObjectStore


def _repository(tmp_path):
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    return BilibiliFavoriteBatchRepository(store, namespace_id="default")


def _create(
    repository,
    *,
    batch_id="favorite-batch-0001",
    project_id="project-a",
    created_at="2026-09-05T00:00:00Z",
):
    return repository.create(
        batch_id=batch_id,
        project_id=project_id,
        snapshot_ref=f"crp://default/favorites/{project_id}/{batch_id}",
        snapshot_revision="r1",
        items=[], created_at=created_at,
    )


def _projection(payload):
    return {
        "revision": payload["revision"], "updated_at": payload["updated_at"],
        "admission_status": payload["admission_status"],
        "processing_status": payload["processing_status"], "counts": {},
        "total": 1, "has_failed": False, "child_outputs": [],
    }


def test_favorite_batch_task_ref_is_stable_across_same_command_replay(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    _create(repository)

    first = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
    )
    second = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
    )

    assert first["items"][0]["task_ref"] == second["items"][0]["task_ref"]
    assert first["items"][0]["status"] == "accepted"


def test_task_list_has_project_scope_and_stable_cursor_pagination(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository, batch_id="favorite-batch-0001")
    _create(repository, batch_id="favorite-batch-0002")
    _create(repository, batch_id="favorite-batch-other", project_id="project-b")

    first = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection, limit=1,
    )
    second = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        limit=1, cursor=first["next_cursor"],
    )

    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["project_id"] == second["items"][0]["project_id"] == "project-a"
    assert first["items"][0]["task_ref"] != second["items"][0]["task_ref"]
    with pytest.raises(TaskReferenceError, match="cursor project"):
        list_task_references(
            repository=repository, project_id="project-b", project_batch_projection=_projection,
            cursor=first["next_cursor"],
        )


def test_task_list_mixes_owner_families_in_stable_time_order_across_pages(tmp_path) -> None:
    repository = _repository(tmp_path)
    for index in range(51):
        _create(repository, batch_id=f"favorite-batch-{index:04}")
    _write_media_owner(repository, updated_at="2026-09-06T00:01:00Z")

    first = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=repository.object_store, limit=50,
    )
    second = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=repository.object_store, limit=50, cursor=first["next_cursor"],
    )
    replayed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=repository.object_store, limit=100,
    )

    combined = [*first["items"], *second["items"]]
    assert len(combined) == len({item["task_ref"] for item in combined}) == 52
    assert first["items"][0]["title"] == "音频转写"
    assert [item["task_ref"] for item in combined] == [item["task_ref"] for item in replayed["items"]]


def test_task_list_keeps_cursor_position_when_a_newer_owner_arrives_and_rejects_missing_cursor(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository, batch_id="favorite-batch-old-a", created_at="2026-09-05T00:00:00Z")
    _create(repository, batch_id="favorite-batch-old-b", created_at="2026-09-05T00:00:00Z")
    _create(repository, batch_id="favorite-batch-old-c", created_at="2026-09-05T00:00:00Z")
    first = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection, limit=2,
    )
    cursor = first["next_cursor"]
    _create(repository, batch_id="favorite-batch-new", created_at="2026-09-07T00:00:00Z")

    continued = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        limit=2, cursor=cursor,
    )
    assert cursor not in {item["task_ref"] for item in continued["items"]}
    assert all(item["task_ref"] not in {entry["task_ref"] for entry in first["items"]} for item in continued["items"])

    cursor_owner = next(
        batch_id for batch_id in ("favorite-batch-old-a", "favorite-batch-old-b", "favorite-batch-old-c")
        if task_ref_for_favorite_batch(project_id="project-a", batch_id=batch_id) == cursor
    )
    assert repository.object_store.delete("bilibili_favorite_batches", cursor_owner)
    with pytest.raises(TaskReferenceError, match="cursor is unavailable"):
        list_task_references(
            repository=repository, project_id="project-a", project_batch_projection=_projection,
            limit=2, cursor=cursor,
        )


def test_task_list_filters_attention_active_and_delivered_on_the_server(tmp_path) -> None:
    repository = _repository(tmp_path)
    for batch_id in ("attention", "active", "delivered"):
        _create(repository, batch_id=f"favorite-batch-{batch_id}")

    def projection(payload):
        status = payload["batch_id"].removeprefix("favorite-batch-")
        if status == "attention":
            return {**_projection(payload), "processing_status": "waiting_user"}
        if status == "active":
            return {**_projection(payload), "processing_status": "running"}
        return {**_projection(payload), "processing_status": "complete", "child_outputs": [{
            "job_id": "job-delivered", "status": "completed", "openable": True,
        }]}

    by_filter = {
        task_filter: list_task_references(
            repository=repository, project_id="project-a", project_batch_projection=projection,
            filter=task_filter,
        )["items"]
        for task_filter in ("attention", "active", "delivered")
    }

    assert [item["status"] for item in by_filter["attention"]] == ["attention"]
    assert [item["status"] for item in by_filter["active"]] == ["active"]
    assert [item["status"] for item in by_filter["delivered"]] == ["delivered"]
    with pytest.raises(TaskReferenceError, match="filter"):
        list_task_references(
            repository=repository, project_id="project-a", project_batch_projection=_projection, filter="all",
        )


def test_task_detail_rejects_cross_project_without_owner_disclosure(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    task_ref = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
    )["items"][0]["task_ref"]

    assert task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a",
        project_batch_projection=_projection,
    )["project_id"] == "project-a"
    with pytest.raises(TaskReferenceError, match="project"):
        task_reference_detail(
            repository=repository, task_ref=task_ref, project_id="project-b",
            project_batch_projection=_projection,
        )


def test_detail_uses_existing_read_back_outputs_and_existing_actions(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    task_ref = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
    )["items"][0]["task_ref"]

    detail = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a",
        project_batch_projection=lambda payload: {
            **_projection(payload), "processing_status": "partial_failure", "has_failed": True,
            "child_outputs": [{"job_id": "job-1", "status": "failed", "openable": False, "outputs": []}],
        },
    )

    assert detail["attention"] == {"kind": "failed_items", "label": "有失败项需要处理"}
    assert detail["allowed_actions"] == [{"kind": "retry_failed", "label": "重试失败项"}]
    assert detail["detail"]["children"] == [{"job_id": "job-1", "status": "failed", "openable": False}]


def test_waiting_user_and_empty_owner_states_are_never_accepted(tmp_path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    task_ref = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
    )["items"][0]["task_ref"]

    waiting = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a",
        project_batch_projection=lambda payload: {
            **_projection(payload), "processing_status": "waiting_user",
            "child_outputs": [{"job_id": "job-1", "status": "waiting_user", "openable": False, "outputs": []}],
        },
    )
    empty = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a",
        project_batch_projection=lambda payload: {**_projection(payload), "processing_status": "empty", "total": 0},
    )

    assert waiting["status"] == "attention"
    assert waiting["allowed_actions"] == []
    assert empty["status"] == "empty"
    assert empty["allowed_actions"] == []


def _write_media_owner(
    repository,
    *,
    project_id: str | None = "project-a",
    source_id: str = "source-media-1",
    job_id: str = "media-job-source-media-1",
    status: str = "completed",
    updated_at: str = "2026-09-06T00:01:00Z",
) -> None:
    source = {"id": source_id, "type": "audio", "updated_at": "2026-09-06T00:00:00Z"}
    if project_id is not None:
        source["project_id"] = project_id
    repository.object_store.write("sources", source_id, source, expected_revision=None)
    repository.object_store.write("media_processing_jobs", job_id, {
        "id": job_id, "source_id": source_id, "source_type": "audio", "status": status,
        "updated_at": updated_at,
    }, expected_revision=None)


def test_media_processing_task_ref_requires_explicit_source_project_and_is_stable(tmp_path) -> None:
    repository = _repository(tmp_path)
    _write_media_owner(repository)
    _write_media_owner(
        repository, project_id=None, source_id="source-no-project", job_id="media-job-source-no-project",
    )

    listed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=repository.object_store,
    )

    assert [item["task_ref"] for item in listed["items"]] == [
        task_ref_for_media_processing_job(project_id="project-a", job_id="media-job-source-media-1")
    ]
    assert listed["items"][0]["status"] == "completed"
    assert listed["items"][0]["attention"] == {
        "kind": "outputs_unavailable", "label": "处理已结束，结果仍需核验",
    }


def test_media_processing_task_outputs_require_exact_job_and_source_read_back(tmp_path) -> None:
    repository = _repository(tmp_path)
    _write_media_owner(repository)
    store = repository.object_store
    store.write("media_processing_outputs", "output-mismatch-job", {
        "id": "output-mismatch-job", "job_id": "another-job", "source_id": "source-media-1",
        "output_kind": "transcript", "status": "completed",
    }, expected_revision=None)
    store.write("media_processing_outputs", "output-mismatch-source", {
        "id": "output-mismatch-source", "job_id": "media-job-source-media-1", "source_id": "another-source",
        "output_kind": "transcript", "status": "completed",
    }, expected_revision=None)
    store.write("media_processing_outputs", "output-unreadable", {
        "id": "output-unreadable", "job_id": "media-job-source-media-1", "source_id": "source-media-1",
        "output_kind": "transcript", "status": "running",
    }, expected_revision=None)
    store.write("media_processing_outputs", "output-transcript", {
        "id": "output-transcript", "job_id": "media-job-source-media-1", "source_id": "source-media-1",
        "output_kind": "transcript", "status": "completed",
    }, expected_revision=None)
    task_ref = task_ref_for_media_processing_job(project_id="project-a", job_id="media-job-source-media-1")

    detail = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a", project_batch_projection=_projection,
        object_store=store,
    )

    assert detail["status"] == "delivered"
    assert detail["attention"] is None
    assert detail["detail"]["outputs"] == [{
        "artifact_id": "output-transcript", "kind": "transcript", "title": "转写文本",
        "status": "completed", "href": "#view=rebuild-library-overview&source_id=source-media-1",
    }]
@pytest.mark.parametrize(("job_status", "attention_kind"), [
    ("failed", "media_processing_failed"),
    ("skipped", "media_processing_skipped"),
])
def test_media_processing_failed_and_skipped_tasks_need_attention(tmp_path, job_status, attention_kind) -> None:
    repository = _repository(tmp_path)
    _write_media_owner(repository, status=job_status)
    detail = task_reference_detail(
        repository=repository,
        task_ref=task_ref_for_media_processing_job(project_id="project-a", job_id="media-job-source-media-1"),
        project_id="project-a",
        project_batch_projection=_projection,
        object_store=repository.object_store,
    )

    assert detail["status"] == "attention"
    assert detail["attention"]["kind"] == attention_kind
    assert detail["allowed_actions"] == []


def test_media_processing_detail_rejects_cross_project_without_owner_disclosure(tmp_path) -> None:
    repository = _repository(tmp_path)
    _write_media_owner(repository, project_id="project-b")
    task_ref = task_ref_for_media_processing_job(project_id="project-b", job_id="media-job-source-media-1")

    with pytest.raises(TaskReferenceError, match="project"):
        task_reference_detail(
            repository=repository, task_ref=task_ref, project_id="project-a", project_batch_projection=_projection,
            object_store=repository.object_store,
        )


def test_workbench_transform_task_requires_source_scope_and_readable_published_document(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = repository.object_store
    store.write("sources", "source-doc-1", {
        "id": "source-doc-1", "project_id": "project-a", "title": "受控 DOCX",
    }, expected_revision=None)
    completed_job = {
        "id": "workbench-transform-doc-1", "job_type": "workbench_content_transform",
        "execution_version": "effect-v2", "status": "completed", "updated_at": "2026-09-06T04:00:00Z",
        "transform_items": [{"source_id": "source-doc-1", "pipeline": "document_extract"}],
        "published_outputs": [{"kind": "document", "object_id": "document-doc-1", "status": "published"}],
    }
    foreign_job = {
        **completed_job, "id": "workbench-transform-foreign", "transform_items": [{"source_id": "source-foreign"}],
    }
    store.write("sources", "source-foreign", {
        "id": "source-foreign", "project_id": "project-b",
    }, expected_revision=None)
    current_document = {
        "id": "document-doc-1", "project_id": "project-a", "revision": 2,
        "source_refs": [{"source_id": "source-doc-1"}],
    }
    published_revision = {
        "id": "document-revision-document-doc-1-r1", "document_id": "document-doc-1", "revision": 1,
        "source_snapshot": {"source_refs": [{"source_id": "source-doc-1"}]},
    }
    receipt = {"outputs": [{
        "source_id": "source-doc-1", "document_id": "document-doc-1", "document_revision": 1,
    }]}

    listed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection, object_store=store,
        workbench_transform_jobs=lambda: (completed_job, foreign_job),
        workbench_transform_receipt=lambda job_id: receipt if job_id == completed_job["id"] else None,
        document_reader=lambda document_id: current_document if document_id == "document-doc-1" else None,
        document_revision_reader=lambda document_id, revision: published_revision if (document_id, revision) == ("document-doc-1", 1) else None,
        document_markdown_reader=lambda _document_id, revision: "正文" if revision in {1, 2} else None,
    )
    task_ref = task_ref_for_workbench_content_transform(project_id="project-a", job_id=completed_job["id"])
    assert [item["task_ref"] for item in listed["items"]] == [task_ref]
    assert listed["items"][0]["status"] == "delivered"

    detail = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a", project_batch_projection=_projection,
        object_store=store, workbench_transform_jobs=lambda: (completed_job,),
        workbench_transform_receipt=lambda _job_id: receipt,
        document_reader=lambda _document_id: current_document,
        document_revision_reader=lambda _document_id, _revision: published_revision,
        document_markdown_reader=lambda _document_id, _revision: "正文",
    )
    assert detail["detail"]["outputs"] == [{
        "artifact_id": "document-doc-1", "kind": "document", "title": "已整理文档",
        "status": "completed", "href": "#view=rebuild-library-overview&document_id=document-doc-1",
    }]
    assert workbench_transform_task_ref_for_document(
        document=current_document, object_store=store, jobs=(completed_job,),
        receipt_reader=lambda _job_id: receipt,
        document_revision_reader=lambda _document_id, _revision: published_revision,
        document_markdown_reader=lambda _document_id, revision: "正文" if revision in {1, 2} else None,
    ) == task_ref
    assert workbench_transform_task_ref_for_document(
        document=current_document, object_store=store, jobs=(completed_job,),
        receipt_reader=lambda _job_id: receipt,
        document_revision_reader=lambda _document_id, _revision: published_revision,
        document_markdown_reader=lambda _document_id, revision: "" if revision == 2 else "历史正文",
    ) is None
    def unreadable_markdown(_document_id, _revision):
        raise OSError("document body is unavailable")
    unreadable = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection, object_store=store,
        workbench_transform_jobs=lambda: (completed_job,), workbench_transform_receipt=lambda _job_id: receipt,
        document_reader=lambda _document_id: current_document,
        document_revision_reader=lambda _document_id, _revision: published_revision,
        document_markdown_reader=unreadable_markdown,
    )
    assert unreadable["items"][0]["status"] == "completed"
    assert unreadable["items"][0]["attention"]["kind"] == "outputs_unavailable"
    assert workbench_transform_task_ref_for_document(
        document=current_document, object_store=store, jobs=(completed_job,),
        receipt_reader=lambda _job_id: receipt,
        document_revision_reader=lambda _document_id, _revision: published_revision,
        document_markdown_reader=lambda _document_id, revision: "当前正文" if revision == 2 else None,
    ) is None

    with pytest.raises(TaskReferenceError, match="project"):
        task_reference_detail(
            repository=repository, task_ref=task_ref, project_id="project-b", project_batch_projection=_projection,
            object_store=store,
        )


def test_workbench_transform_completed_without_verified_receipt_or_document_needs_attention(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = repository.object_store
    store.write("sources", "source-doc-1", {"id": "source-doc-1", "project_id": "project-a"}, expected_revision=None)
    job = {
        "id": "workbench-transform-doc-1", "job_type": "workbench_content_transform",
        "execution_version": "effect-v2", "status": "completed", "updated_at": "2026-09-06T04:00:00Z",
        "transform_items": [{"source_id": "source-doc-1"}], "published_outputs": [],
    }
    listed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection, object_store=store,
        workbench_transform_jobs=lambda: (job,), workbench_transform_receipt=lambda _job_id: None,
        document_reader=lambda _document_id: None, document_revision_reader=lambda _document_id, _revision: None,
        document_markdown_reader=lambda _document_id, _revision: None,
    )

    assert listed["items"][0]["status"] == "completed"
    assert listed["items"][0]["attention"]["kind"] == "outputs_unavailable"


def test_transform_task_page_reads_only_page_and_one_lookahead_artifact_chain(tmp_path) -> None:
    """A 500-owner page must not read every Source/receipt before slicing."""
    repository = _repository(tmp_path)
    store = repository.object_store
    jobs = []
    for index in range(500):
        source_id = f"source-{index:04}"
        document_id = f"document-{index:04}"
        store.write("sources", source_id, {"id": source_id, "project_id": "project-a"}, expected_revision=None)
        jobs.append({
            "id": f"transform-{index:04}", "job_type": "workbench_content_transform",
            "execution_version": "effect-v2", "status": "completed",
            "updated_at": f"2026-09-06T{index // 60:02}:{index % 60:02}:00Z",
            "transform_items": [{"source_id": source_id}],
            "published_outputs": [{"kind": "document", "object_id": document_id, "status": "published"}],
        })

    calls = {"source": 0, "receipt": 0, "document": 0, "revision": 0, "markdown": 0}

    class CountingStore:
        def read(self, collection, object_id):
            if collection == "sources":
                calls["source"] += 1
            return store.read(collection, object_id)

        def list(self, collection):
            return store.list(collection)

    def receipt(job_id):
        calls["receipt"] += 1
        index = int(job_id.rsplit("-", 1)[1])
        return {"outputs": [{
            "source_id": f"source-{index:04}", "document_id": f"document-{index:04}", "document_revision": 1,
        }]}

    def document(document_id):
        calls["document"] += 1
        return {"id": document_id, "project_id": "project-a", "revision": 1,
                "source_refs": [{"source_id": document_id.replace("document", "source")}]}

    def revision(document_id, revision_number):
        calls["revision"] += 1
        return {"document_id": document_id, "revision": revision_number,
                "source_snapshot": {"source_refs": [{"source_id": document_id.replace("document", "source")} ]}}

    def markdown(_document_id, _revision):
        calls["markdown"] += 1
        return "正文"

    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=CountingStore(), workbench_transform_jobs=lambda: tuple(jobs),
        workbench_transform_receipt=receipt, document_reader=document,
        document_revision_reader=revision, document_markdown_reader=markdown, limit=1,
    )

    assert [item["task_ref"] for item in result["items"]] == [
        task_ref_for_workbench_content_transform(project_id="project-a", job_id="transform-0499"),
    ]
    assert result["next_cursor"] == result["items"][0]["task_ref"]
    # The second verified owner is a deliberate exact-next-page lookahead.
    assert calls == {"source": 2, "receipt": 2, "document": 2, "revision": 2, "markdown": 4}


def test_pending_transform_task_page_reads_only_page_and_lookahead_sources(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = repository.object_store
    jobs = []
    for index in range(500):
        source_id = f"source-pending-{index:04}"
        store.write("sources", source_id, {"id": source_id, "project_id": "project-a"}, expected_revision=None)
        jobs.append({
            "id": f"pending-{index:04}", "job_type": "workbench_content_transform",
            "execution_version": "effect-v2", "status": "pending",
            "updated_at": f"2026-09-06T{index // 60:02}:{index % 60:02}:00Z",
            "transform_items": [{"source_id": source_id}],
        })
    calls = {"source": 0, "receipt": 0}

    class CountingStore:
        def read(self, collection, object_id):
            if collection == "sources":
                calls["source"] += 1
            return store.read(collection, object_id)

        def list(self, collection):
            return store.list(collection)

    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=CountingStore(), workbench_transform_jobs=lambda: tuple(jobs),
        workbench_transform_receipt=lambda _job_id: calls.__setitem__("receipt", calls["receipt"] + 1),
        document_reader=lambda _id: None, document_revision_reader=lambda _id, _revision: None,
        document_markdown_reader=lambda _id, _revision: None, limit=1,
    )

    assert result["items"][0]["task_ref"] == task_ref_for_workbench_content_transform(
        project_id="project-a", job_id="pending-0499",
    )
    assert result["next_cursor"] == result["items"][0]["task_ref"]
    assert calls == {"source": 2, "receipt": 0}
    continued = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=CountingStore(), workbench_transform_jobs=lambda: tuple(jobs),
        workbench_transform_receipt=lambda _job_id: calls.__setitem__("receipt", calls["receipt"] + 1),
        document_reader=lambda _id: None, document_revision_reader=lambda _id, _revision: None,
        document_markdown_reader=lambda _id, _revision: None, limit=1, cursor=result["next_cursor"],
    )
    assert continued["items"][0]["task_ref"] == task_ref_for_workbench_content_transform(
        project_id="project-a", job_id="pending-0498",
    )
    # Cursor validation rechecks the prior exposed owner, then page+lookahead.
    assert calls == {"source": 5, "receipt": 0}


def test_delivered_filter_skips_noncompleted_transforms_without_owner_or_artifact_reads(tmp_path) -> None:
    repository = _repository(tmp_path)

    def forbidden_read(*_args):
        raise AssertionError("noncompleted candidates cannot produce delivered tasks")

    class UnreadableStore:
        read = staticmethod(forbidden_read)

        def list(self, collection):
            return []

    jobs = tuple({
        "id": f"unfinished-{index}", "job_type": "workbench_content_transform",
        "execution_version": "effect-v2",
        "status": ("pending", "running", "failed", "cancelled", "waiting_user")[index % 5],
        "transform_items": [{"source_id": f"source-{index}"}],
    } for index in range(500))
    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=UnreadableStore(), workbench_transform_jobs=lambda: jobs,
        workbench_transform_receipt=forbidden_read, document_reader=forbidden_read,
        document_revision_reader=forbidden_read, document_markdown_reader=forbidden_read,
        filter="delivered", limit=1,
    )
    assert result == {"items": [], "next_cursor": None}


def test_active_filter_skips_completed_transform_candidates_without_artifact_reads(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = repository.object_store
    jobs = []
    for index in range(500):
        source_id = f"source-completed-{index:04}"
        store.write("sources", source_id, {"id": source_id, "project_id": "project-a"}, expected_revision=None)
        jobs.append({
            "id": f"completed-{index:04}", "job_type": "workbench_content_transform",
            "execution_version": "effect-v2", "status": "completed",
            "updated_at": f"2026-09-06T{index // 60 + 1:02}:{index % 60:02}:00Z",
            "transform_items": [{"source_id": source_id}],
        })
    store.write("sources", "source-running", {"id": "source-running", "project_id": "project-a"}, expected_revision=None)
    jobs.append({
        "id": "running", "job_type": "workbench_content_transform", "execution_version": "effect-v2",
        "status": "running", "updated_at": "2026-09-06T00:00:00Z", "transform_items": [{"source_id": "source-running"}],
    })
    calls = {"source": 0, "receipt": 0}

    class CountingStore:
        def read(self, collection, object_id):
            if collection == "sources":
                calls["source"] += 1
            return store.read(collection, object_id)

        def list(self, collection):
            return store.list(collection)

    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=CountingStore(), workbench_transform_jobs=lambda: tuple(jobs),
        workbench_transform_receipt=lambda _job_id: calls.__setitem__("receipt", calls["receipt"] + 1),
        document_reader=lambda _id: None, document_revision_reader=lambda _id, _revision: None,
        document_markdown_reader=lambda _id, _revision: None, filter="active", limit=1,
    )

    assert result["items"][0]["task_ref"] == task_ref_for_workbench_content_transform(
        project_id="project-a", job_id="running",
    )
    assert result["next_cursor"] is None
    assert calls == {"source": 1, "receipt": 0}


def test_transform_sql_candidate_batches_keep_utc_and_opaque_reference_order_without_full_history(tmp_path) -> None:
    repository = _repository(tmp_path)
    jobs = []
    sources: dict[str, dict[str, object]] = {}
    for index in range(500):
        job_id = f"tie-job-{index:04}"
        source_id = f"tie-source-{index:04}"
        # These two offsets are the same UTC instant. The opaque TaskReference
        # must therefore decide the order, never raw ISO text or job id.
        updated_at = "2026-09-06T08:00:00+08:00" if index % 2 else "2026-09-06T00:00:00Z"
        sources[source_id] = {"id": source_id, "project_id": "project-a"}
        jobs.append({
            "id": job_id, "job_type": "workbench_content_transform", "execution_version": "effect-v2",
            "status": "pending", "updated_at": updated_at, "transform_items": [{"source_id": source_id}],
        })
    references = {job["id"]: task_ref_for_workbench_content_transform(project_id="project-a", job_id=job["id"]) for job in jobs}
    ordered = sorted(
        jobs,
        key=lambda job: (task_projection._timestamp_sort_key(job["updated_at"]), references[job["id"]]),
        reverse=True,
    )
    calls: list[tuple[tuple[str, str] | None, int]] = []

    def page(after, size):
        calls.append((after, size))
        visible = ordered if after is None else [
            job for job in ordered
            if (task_projection._task_timestamp_cursor(job["updated_at"]), references[job["id"]]) < after
        ]
        return tuple(visible[:size])

    class Store:
        def read(self, collection, object_id):
            return sources.get(object_id) if collection == "sources" else None

        def list(self, _collection):
            return ()

    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=Store(), workbench_transform_job_page=page,
        workbench_transform_job_reader=lambda job_id: next((job for job in jobs if job["id"] == job_id), None),
        workbench_transform_receipt=lambda _id: None, document_reader=lambda _id: None,
        document_revision_reader=lambda _id, _revision: None, document_markdown_reader=lambda _id, _revision: None,
        limit=1,
    )
    continued = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=Store(), workbench_transform_job_page=page,
        workbench_transform_job_reader=lambda job_id: next((job for job in jobs if job["id"] == job_id), None),
        workbench_transform_receipt=lambda _id: None, document_reader=lambda _id: None,
        document_revision_reader=lambda _id, _revision: None, document_markdown_reader=lambda _id, _revision: None,
        limit=1, cursor=result["next_cursor"],
    )

    assert [item["task_ref"] for item in result["items"] + continued["items"]] == [
        references[ordered[0]["id"]], references[ordered[1]["id"]],
    ]
    assert calls[0] == (None, 32)
    # The first request only exposed a bounded 32-job candidate batch despite
    # all 500 records sharing one normalized timestamp.
    assert len(calls) == 2


def test_transform_task_page_skips_foreign_owner_before_materializing_the_first_visible_owner(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = repository.object_store
    for source_id, project_id in (("source-foreign", "project-b"), ("source-local", "project-a")):
        store.write("sources", source_id, {"id": source_id, "project_id": project_id}, expected_revision=None)
    jobs = (
        {"id": "foreign", "job_type": "workbench_content_transform", "execution_version": "effect-v2",
         "status": "running", "updated_at": "2026-09-06T02:00:00Z", "transform_items": [{"source_id": "source-foreign"}]},
        {"id": "local", "job_type": "workbench_content_transform", "execution_version": "effect-v2",
         "status": "running", "updated_at": "2026-09-06T01:00:00Z", "transform_items": [{"source_id": "source-local"}]},
    )
    calls = {"source": 0, "receipt": 0}

    class CountingStore:
        def read(self, collection, object_id):
            if collection == "sources":
                calls["source"] += 1
            return store.read(collection, object_id)

        def list(self, collection):
            return store.list(collection)

    result = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        object_store=CountingStore(), workbench_transform_jobs=lambda: jobs,
        workbench_transform_receipt=lambda _job_id: calls.__setitem__("receipt", calls["receipt"] + 1),
        document_reader=lambda _id: None, document_revision_reader=lambda _id, _revision: None,
        document_markdown_reader=lambda _id, _revision: None, limit=1,
    )

    assert [item["task_ref"] for item in result["items"]] == [
        task_ref_for_workbench_content_transform(project_id="project-a", job_id="local"),
    ]
    assert calls == {"source": 2, "receipt": 0}


def test_world_action_task_ref_uses_planned_action_and_fixed_turn_without_private_turn_content(tmp_path) -> None:
    repository = _repository(tmp_path)
    action_id = "world-action-12345678"
    turn_id = "world-turn-12345678"
    calls: list[tuple[str, str]] = []

    def overview(*, project_id: str):
        assert project_id == "project-a"
        return {"state": {"project_id": project_id, "planned_actions": [
            {"action_id": action_id, "title": "整理项目资料", "expected_outcome": "private input", "evidence_refs": ["crp://private"]},
            {"action_id": "generic-turn-action", "title": "不能成为任务", "answer": "private"},
        ]}}

    def action_status(*, project_id: str, turn_id: str):
        calls.append((project_id, turn_id))
        return {
            "turn_id": turn_id, "action_id": action_id, "status": "completed", "terminal": True,
            "ready_for_feedback": False, "answer": "private answer", "context": {"raw": "private"},
            "governance": {"receipt": "terminal", "input_ref": "crp://private"},
        }

    listed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        world_action_overview=overview, world_action_status=action_status,
    )
    task_ref = task_ref_for_world_action(project_id="project-a", action_id=action_id)
    detail = task_reference_detail(
        repository=repository, task_ref=task_ref, project_id="project-a", project_batch_projection=_projection,
        world_action_overview=overview, world_action_status=action_status,
    )

    assert calls == [("project-a", turn_id), ("project-a", turn_id)]
    assert listed["items"] == [{
        "task_ref": task_ref, "project_id": "project-a", "title": "整理项目资料",
        "status": "completed", "attention": {"kind": "outputs_unavailable", "label": "处理已结束，结果仍需核验"},
        "next_action": None, "updated_at": None, "delivered_at": None, "allowed_actions": [],
    }]
    assert detail["detail"] == {
        "owner_kind": "world_action", "processing_status": "completed", "outputs": [],
        "children": [], "allowed_actions": [],
    }
    rendered = str({"summary": listed, "detail": detail}).lower()
    for forbidden in ("world-action-12345678", "world-turn-12345678", "private", "crp://", "answer", "context", "input_ref", "raw"):
        assert forbidden not in rendered


@pytest.mark.parametrize(("turn_status", "ready_for_feedback", "expected_attention"), [
    ("completed", True, "ready_for_feedback"),
    ("failed", False, "world_action_failed"),
    ("cancelled", False, "world_action_cancelled"),
])
def test_world_action_terminal_attention_states_are_not_delivered(tmp_path, turn_status, ready_for_feedback, expected_attention) -> None:
    repository = _repository(tmp_path)
    action_id = "world-action-terminal-12345678"

    def overview(*, project_id: str):
        assert project_id == "project-a"
        return {"state": {"project_id": "project-a", "planned_actions": [{"action_id": action_id, "title": "项目行动"}]}}

    def action_status(*, project_id: str, turn_id: str):
        assert project_id == "project-a"
        return {"turn_id": turn_id, "action_id": action_id, "status": turn_status, "terminal": True, "ready_for_feedback": ready_for_feedback}

    item = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        world_action_overview=overview, world_action_status=action_status,
    )["items"][0]

    assert item["status"] == "attention"
    assert item["attention"]["kind"] == expected_attention
    assert item["delivered_at"] is None


@pytest.mark.parametrize(("turn_status", "task_filter"), [
    ("accepted", "active"), ("waiting_approval", "attention"),
])
def test_admitted_world_action_remains_visible_in_its_actionable_filter(tmp_path, turn_status, task_filter) -> None:
    action_id = "world-action-pending-12345678"
    result = list_task_references(
        repository=_repository(tmp_path), project_id="project-a", project_batch_projection=_projection,
        filter=task_filter,
        world_action_overview=lambda **_: {"state": {"project_id": "project-a", "planned_actions": [
            {"action_id": action_id, "title": "项目行动"},
        ]}},
        world_action_status=lambda **scope: {
            "turn_id": scope["turn_id"], "action_id": action_id,
            "status": turn_status, "terminal": False, "ready_for_feedback": False,
        },
    )
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["status"] == task_filter
    assert item["delivered_at"] is None
    if turn_status == "waiting_approval":
        assert item["attention"]["kind"] == "waiting_approval"


def test_world_action_rejects_status_that_does_not_reconfirm_fixed_turn_and_owner(tmp_path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(TaskReferenceError, match="verification"):
        list_task_references(
            repository=repository, project_id="project-a", project_batch_projection=_projection,
            world_action_overview=lambda *, project_id: {"state": {"project_id": project_id, "planned_actions": [{
                "action_id": "world-action-12345678", "title": "项目行动",
            }]}},
            world_action_status=lambda *, project_id, turn_id: {
                "turn_id": turn_id, "action_id": "world-action-other", "status": "completed",
            },
        )


def test_world_action_without_admitted_turn_is_not_exposed_as_a_task_owner(tmp_path) -> None:
    repository = _repository(tmp_path)
    action_id = "world-action-12345678"

    listed = list_task_references(
        repository=repository, project_id="project-a", project_batch_projection=_projection,
        world_action_overview=lambda *, project_id: {"state": {"project_id": project_id, "planned_actions": [{
            "action_id": action_id, "title": "项目行动",
        }]}},
        world_action_status=lambda *, project_id, turn_id: {
            "turn_id": turn_id, "action_id": action_id, "status": "not_admitted",
        },
    )

    assert listed["items"] == []
