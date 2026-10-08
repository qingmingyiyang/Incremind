"""A full document draft must remain editable through the user-facing API."""

import json

import pytest

from backend.memory_app.document_recognition import extract_document_candidate
from core.document_engine.ports import DocumentDraft
from tests.memory_app.test_api import _client


def _request(client, method, path, body, *, escaped=False):
    return client.request(method, path, content=json.dumps(body, ensure_ascii=escaped),
                          headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("character,length,escaped", [("a", 20_001, False), ("文", 50_000, False), ("🙂", 100_000, True)])
def test_full_document_candidate_can_be_edited_published_and_revised(tmp_path, character, length, escaped):
    client, models = _client(tmp_path)
    with client:
        documents = client.app.state.recognition_documents
        service = client.app.state.recognition_service
        document = documents.create(DocumentDraft(title="Long source", document_type="legacy-material",
            markdown=character * length, project_id="project-a",
            source_refs=({"source_id": "source-1", "locator": f"text:0:{length}"},)))
        extracted = extract_document_candidate(documents, service, "project-a", document["id"])
        candidate_id = extracted["candidate_id"]
        edited = character * (length - 1) + "X"
        saved = _request(client, "PATCH", f"/api/recognition/candidates/{candidate_id}/draft", {
            "project_id": "project-a", "expected_revision": 1, "content": edited,
            "conditions": ["Only an isolated demonstration"]}, escaped=escaped)
        assert saved.status_code == 200, saved.text
        assert saved.json()["content"] == edited

        approved = _request(client, "PATCH", f"/api/recognition/candidates/{candidate_id}", {
            "project_id": "project-a", "expected_revision": saved.json()["revision"],
            "decision": "approve", "content": edited}, escaped=escaped)
        assert approved.status_code == 200, approved.text
        recognition = approved.json()
        path = f"/api/recognition/recognitions/{recognition['id']}/markdown"
        exported = client.get(path, params={"project_id": "project-a"})
        assert exported.status_code == 200
        markdown = exported.text.replace(edited, edited[:-1] + "Y")
        body = {"project_id": "project-a", "expected_revision": recognition["revision"],
                "markdown": markdown, "mode": "preview"}
        preview = _request(client, "POST", path, body, escaped=escaped)
        assert preview.status_code == 200, preview.text
        assert preview.json()["changed"] is True
        committed = _request(client, "POST", path, {**body, "mode": "commit"}, escaped=escaped)
        assert committed.status_code == 200, committed.text
        assert committed.json()["content"] == edited[:-1] + "Y"
        assert models.calls == []


def test_edit_still_rejects_content_over_the_domain_limit_without_mutation(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        documents = client.app.state.recognition_documents
        service = client.app.state.recognition_service
        document = documents.create(DocumentDraft(title="Source", document_type="legacy-material",
            markdown="Evidence", project_id="project-a",
            source_refs=({"source_id": "source-1", "locator": "text:0:8"},)))
        extracted = extract_document_candidate(documents, service, "project-a", document["id"])
        candidate_id = extracted["candidate_id"]
        response = client.patch(f"/api/recognition/candidates/{candidate_id}/draft", json={
            "project_id": "project-a", "expected_revision": 1, "content": "x" * 100_001})
        assert response.status_code == 422, response.text
        candidate = service.records.read("recognition_candidates", candidate_id)
        assert candidate.revision == 1
        assert candidate.payload["content"] == "Evidence"


@pytest.mark.parametrize("chunked", [False, True])
def test_edit_request_budget_remains_bounded_for_headers_and_streams(tmp_path, chunked):
    from backend.memory_app.app import _MAX_RECOGNITION_EDIT_BYTES

    client, models = _client(tmp_path)
    with client:
        payload = json.dumps({"content": "x" * (_MAX_RECOGNITION_EDIT_BYTES + 1)}).encode()
        response = client.patch("/api/recognition/candidates/missing/draft",
            content=iter([payload]) if chunked else payload,
            headers={"Content-Type": "application/json"})
        assert response.status_code == (422 if chunked else 413), response.text
        assert response.json()["detail"] == "request_too_large"
        assert client.app.state.recognition_records.list("recognition_candidates") == ()
        assert models.calls == []


def test_other_endpoints_keep_the_small_request_budget(tmp_path):
    client, models = _client(tmp_path)
    with client:
        response = client.post("/api/recognition/experiences", json={
            "project_id": "project-a", "content": "文" * 50_000})
        assert response.status_code == 413, response.text
        assert models.calls == []
