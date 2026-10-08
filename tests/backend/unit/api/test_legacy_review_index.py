from collections import Counter
from types import SimpleNamespace

import pytest

from backend.memory_app.legacy_intake_review import LegacyIntakeReview


def _reviews(tmp_path, count=20, *, inline=False):
    calls = Counter()
    sources, intents, documents, jobs, reads = {}, {}, [], [], []
    for index in range(count):
        source = f"source-{index:03}"
        sources[source] = {"id": source, "project_id": "qa", "type": "text",
                           "metadata": {"content_snapshot": f"inline-{index}"} if inline else {}}
        intents["review-" + source] = SimpleNamespace(object_id="review-" + source, revision=1, payload={
            "id": "review-" + source, "source_id": source, "project_id": "qa",
            "source_revision": 1, "state": "pending", "job_id": "job-" + source,
        })
        documents.append({"id": "document-" + source, "revision": 1, "project_id": "qa",
                          "status": "active", "source_refs": [{"source_id": source}, {"source_id": source}]})
        jobs.append({"id": "job-" + source, "job_type": "capture", "status": "completed"})
        reads.append({"source_id": source, "status": "completed", "text": f"text-{index}"})

    def listing(key, values):
        calls[key] += 1
        return tuple(values)

    def capture(job_id):
        calls["capture"] += 1
        return next((job for job in jobs if job["id"] == job_id), None)

    records = SimpleNamespace(
        list_matching=lambda _collection, **fields: tuple(row for row in intents.values() if row.payload["project_id"] == fields["project_id"]),
        read=lambda _collection, key: intents.get(key),
    )
    doc_store = SimpleNamespace(list=lambda **_: listing("documents", documents), markdown=lambda key, **_: "draft-" + key)
    job_store = SimpleNamespace(all=lambda: listing("jobs", jobs), get=capture)
    source_store = SimpleNamespace(read=lambda _collection, key: sources.get(key), revision=lambda *_: 1,
                                   list=lambda _collection: listing("reads", reads))
    service = LegacyIntakeReview(tmp_path, records, doc_store, jobs=job_store, object_store=source_store)
    return service, calls, sources, intents, documents, jobs, reads


@pytest.mark.parametrize("inline", [False, True])
def test_list_reuses_scans_and_preserves_individual_projections(tmp_path, inline):
    service, calls, sources, _, _, _, _ = _reviews(tmp_path, count=100, inline=inline)
    individual = tuple(service.get(source, "qa") for source in sources)
    assert calls["documents"] == calls["jobs"] == 100
    calls.clear()
    assert service.list("qa") == individual
    assert calls == {"documents": 1, "jobs": 1, "capture": 100, **({} if inline else {"reads": 1})}


def test_list_keeps_tie_order_and_refreshes_between_requests(tmp_path):
    service, calls, _, _, _, jobs, reads = _reviews(tmp_path, count=2)
    transform = {"id": "transform-first", "job_type": "workbench_content_transform", "project_id": "qa",
                 "updated_at": "2026-09-29", "status": "failed", "error": "first-error",
                 "transform_items": [{"source_id": "source-000"}, {"source_id": "source-000"}]}
    jobs.extend([transform, {**transform, "id": "transform-tied", "error": "tied-error"},
                 {**transform, "id": "transform-other-project", "project_id": "other", "updated_at": "2099"}])
    reads.extend([{"source_id": "source-000", "status": "completed", "text": "last-in-store", "created_at": "1900"},
                  {"source_id": "source-000", "status": "failed", "text": "must-not-be-used"}])
    listed = service.list("qa")
    assert listed[0] == service.get("source-000", "qa")
    assert listed[0]["error"] == "first-error"
    assert listed[0]["source_text"] == "last-in-store"
    calls.clear()
    jobs.append({**transform, "id": "transform-new", "updated_at": "2026-09-30", "status": "completed", "error": None})
    reads.append({"source_id": "source-000", "status": "completed", "text": "next-request"})
    refreshed = service.list("qa")
    assert refreshed[0]["status"] == "ready"
    assert refreshed[0]["source_text"] == "next-request"
    assert calls["documents"] == calls["jobs"] == calls["reads"] == 1


def test_list_does_not_scan_dependencies_for_empty_or_broken_sources(tmp_path):
    service, calls, sources, _, _, _, _ = _reviews(tmp_path, count=2)
    assert service.list("other") == ()
    assert not calls
    for source in sources.values():
        source["project_id"] = "other"
    result = service.list("qa")
    assert all(item["error"] == "review_source_binding_changed" for item in result)
    assert not calls


def test_list_retains_cross_project_ambiguity_and_archive_checks(tmp_path):
    service, calls, _, intents, documents, _, _ = _reviews(tmp_path, count=3)
    documents.append({**documents[0], "id": "foreign-document", "project_id": "other", "status": "archived"})
    documents[1]["status"] = "archived"
    documents[2]["status"] = "archived"
    intents["review-source-002"].payload["state"] = "confirmed"
    listed = service.list("qa")
    assert listed[0]["error"] == "review_multiple_documents"
    assert listed[1]["error"] == "review_document_archived"
    assert listed[2]["status"] == "confirmed"
    assert listed[2]["document_status"] == "archived"
    assert all(not row["source_text"] and row["document_id"] is None for row in listed[:2])
    assert calls["documents"] == 1


def test_list_keeps_capture_authority_errors_even_when_transform_exists(tmp_path):
    service, _, _, _, _, jobs, _ = _reviews(tmp_path, count=1)
    jobs.append({"id": "transform", "job_type": "workbench_content_transform", "project_id": "qa",
                 "status": "completed", "transform_items": [{"source_id": "source-000"}]})
    def conflict(_job_id):
        raise ValueError("duplicate legacy job identity")
    service.jobs.get = conflict
    with pytest.raises(ValueError, match="duplicate legacy job identity"):
        service.list("qa")
