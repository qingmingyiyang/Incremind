from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_workbench_document_draft_handoff, build_workbench_document_memory_candidate_handoff
from core.document_engine import ObjectStoreDocumentRepository
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    CreateDocumentDraftFromWorkbenchSelection,
    CreateMemoryCandidateFromWorkbenchDocument,
    ServeWorkbenchDocumentDraftEndpoint,
    WorkbenchDocumentDraftError,
    WorkbenchDocumentDraftSelection,
    WorkbenchDocumentMemoryCandidateError,
    serialize_workbench_document_memory_candidate,
    serialize_workbench_document_draft,
)
from core.storage_provider import JsonObjectStore
from tests.rebuild.memory_candidate_saga_review_testlib import (
    review_candidate_to_staging,
    saga_records,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _selection() -> WorkbenchDocumentDraftSelection:
    return WorkbenchDocumentDraftSelection(
        selection_id="library-selection-source-text-001",
        source_id="source-text-001",
        source_title="Workbench selected note",
        source_uri="crp://default/sources/source-text-001",
        capture_job_id="job-capture-source-text-001",
        media_type="text/plain",
        selected_evidence_refs=(
            "crp://default/sources/source-text-001",
            "crp://default/logs/jobs/job-capture-source-text-001/persist_source.jsonl",
        ),
        project_id="project-alpha",
    )


def test_workbench_selection_creates_source_backed_document_draft(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    use_case = CreateDocumentDraftFromWorkbenchSelection(documents=documents)

    result = use_case.execute(
        _selection(),
        title="Workbench selected Source document",
        summary="根据 Workbench 已选 Source / Job 证据创建可编辑文档草稿。",
    )
    document = documents.read(result.document_id)
    revision = documents.revision(result.document_id, result.document_revision)
    markdown = documents.markdown(result.document_id)

    assert result.status == "draft_created"
    assert result.phase == "Phase 11 Workbench Memory / Document / QA Vertical Slice"
    assert result.document_type == "summary"
    assert result.source_refs_display == (
        "source-text-001#source:metadata",
        "source-text-001#job:job-capture-source-text-001",
    )
    assert result.memory_publication_state == "not_published"
    assert result.qa_answer_state == "not_started"
    assert result.user_edit_protection == "document_revision_protects_user_edited_blocks"
    assert "long_term_memory_publication" in result.blocked_operations
    assert "user_edit_overwrite" in result.blocked_operations
    assert document is not None
    assert revision is not None
    assert document["type"] == "summary"
    assert document["project_id"] == "project-alpha"
    assert document["source_snapshot"]["source_refs"] == [
        {
            "source_id": "source-text-001",
            "locator": "source:metadata",
            "quote": "Workbench selected note",
        },
        {
            "source_id": "source-text-001",
            "locator": "job:job-capture-source-text-001",
            "quote": "Workbench capture Job trace",
        },
    ]
    assert revision["source_snapshot"] == document["source_snapshot"]
    assert markdown is not None
    assert "根据 Workbench 已选 Source / Job 证据创建可编辑文档草稿。" in markdown
    assert "未发布长期 Memory" in markdown
    assert validate_contract_instance("document.schema.json", _schema("document.schema.json"), document) == []
    assert (
        validate_contract_instance(
            "document_revision.schema.json",
            _schema("document_revision.schema.json"),
            revision,
        )
        == []
    )
    assert not (tmp_path / "library").exists()


def test_workbench_selection_rejects_missing_source_evidence(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    use_case = CreateDocumentDraftFromWorkbenchSelection(
        documents=ObjectStoreDocumentRepository(object_store),
    )
    selection = WorkbenchDocumentDraftSelection(
        selection_id="library-selection-source-text-001",
        source_id="source-text-001",
        source_title="Missing Source evidence",
        source_uri="crp://default/sources/source-text-001",
        capture_job_id="job-capture-source-text-001",
        media_type="text/plain",
        selected_evidence_refs=(
            "crp://default/logs/jobs/job-capture-source-text-001/persist_source.jsonl",
        ),
    )

    with pytest.raises(WorkbenchDocumentDraftError, match="Source URI"):
        use_case.execute(selection)
    assert not (tmp_path / "library").exists()


def test_workbench_selection_rejects_missing_capture_job_trace(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    use_case = CreateDocumentDraftFromWorkbenchSelection(
        documents=ObjectStoreDocumentRepository(object_store),
    )
    selection = WorkbenchDocumentDraftSelection(
        selection_id="library-selection-source-text-001",
        source_id="source-text-001",
        source_title="Missing Job evidence",
        source_uri="crp://default/sources/source-text-001",
        capture_job_id="job-capture-source-text-001",
        media_type="text/plain",
        selected_evidence_refs=("crp://default/sources/source-text-001",),
    )

    with pytest.raises(WorkbenchDocumentDraftError, match="capture Job trace"):
        use_case.execute(selection)
    assert not (tmp_path / "library").exists()


def test_workbench_document_draft_serializer_is_display_ready(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    use_case = CreateDocumentDraftFromWorkbenchSelection(
        documents=ObjectStoreDocumentRepository(object_store),
    )

    result = use_case.execute(_selection())
    payload = serialize_workbench_document_draft(result)

    assert payload["status"] == "draft_created"
    assert payload["source_id"] == "source-text-001"
    assert payload["source_refs_display"] == [
        "source-text-001#source:metadata",
        "source-text-001#job:job-capture-source-text-001",
    ]
    assert payload["memory_publication_state"] == "not_published"
    assert payload["qa_answer_state"] == "not_started"
    assert payload["no_silent_overwrite_boundary"] == "creates_new_draft_revision_1_only_no_existing_document_overwrite"
    assert "source_content_read" in payload["blocked_operations"]


def test_workbench_document_draft_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    handoff = build_workbench_document_draft_handoff(ROOT, runtime_root=tmp_path)

    result = handoff.execute(_selection(), title="Composed Workbench Document")
    documents = ObjectStoreDocumentRepository(_store(tmp_path))
    document = documents.read(result.document_id)

    assert document is not None
    assert document["type"] == "summary"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / "library").exists()


def test_workbench_document_draft_endpoint_serves_narrow_post(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    use_case = CreateDocumentDraftFromWorkbenchSelection(
        documents=ObjectStoreDocumentRepository(object_store),
    )
    endpoint = ServeWorkbenchDocumentDraftEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/document-draft",
        body={
            "selection_id": "library-selection-source-text-001",
            "source_id": "source-text-001",
            "source_title": "Endpoint Source",
            "source_uri": "crp://default/sources/source-text-001",
            "capture_job_id": "job-capture-source-text-001",
            "media_type": "text/plain",
            "selected_evidence_refs": [
                "crp://default/sources/source-text-001",
                "crp://default/logs/jobs/job-capture-source-text-001/persist_source.jsonl",
            ],
            "project_id": "project-alpha",
            "title": "Endpoint Document",
            "summary": "Endpoint creates a display-ready source-backed draft.",
        },
        create_document_draft=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "draft_created"
    assert response.body["document_type"] == "summary"
    assert response.body["source_refs_display"] == [
        "source-text-001#source:metadata",
        "source-text-001#job:job-capture-source-text-001",
    ]
    assert response.body["memory_publication_state"] == "not_published"
    assert response.body["qa_answer_state"] == "not_started"
    assert "user_edit_overwrite" in response.body["blocked_operations"]
    assert not (tmp_path / "library").exists()


def test_workbench_document_draft_endpoint_rejects_wrong_method(tmp_path: Path) -> None:
    endpoint = ServeWorkbenchDocumentDraftEndpoint()

    response = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/document-draft",
        body={},
        create_document_draft=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
    )

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"


def test_workbench_document_draft_endpoint_rejects_missing_evidence_refs(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    use_case = CreateDocumentDraftFromWorkbenchSelection(
        documents=ObjectStoreDocumentRepository(object_store),
    )
    endpoint = ServeWorkbenchDocumentDraftEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/document-draft",
        body={
            "selection_id": "library-selection-source-text-001",
            "source_id": "source-text-001",
            "source_title": "Endpoint Source",
            "source_uri": "crp://default/sources/source-text-001",
            "capture_job_id": "job-capture-source-text-001",
            "media_type": "text/plain",
            "selected_evidence_refs": [],
        },
        create_document_draft=use_case.execute,
    )

    assert response.status_code == 400
    assert response.body["detail"] == "selected_evidence_refs must be a non-empty string array"
    assert not (tmp_path / "library").exists()


def test_workbench_document_creates_reviewable_memory_candidate_without_publication(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    draft = CreateDocumentDraftFromWorkbenchSelection(documents=documents).execute(
        _selection(),
        title="Document to candidate",
        summary="Workbench Document 可进入待审记忆候选，但不能自动发布长期 Memory。",
    )
    use_case = CreateMemoryCandidateFromWorkbenchDocument(documents=documents, candidates=candidates)

    result = use_case.execute(
        draft.document_id,
        expected_revision=draft.document_revision,
        proposed_content="Workbench Document 的结论必须带 source_refs 并经过用户审核。",
        created_at="2026-07-01T10:00:00+08:00",
    )
    candidate = candidates.get(result.candidate_id)
    payload = serialize_workbench_document_memory_candidate(result)

    assert candidate is not None
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert result.status == "candidate_created"
    assert result.candidate_status == "pending_review"
    assert result.memory_publication_state == "not_published"
    assert result.review_state == "pending_user_review"
    assert result.source_refs_display == (
        "source-text-001#source:metadata",
        "source-text-001#job:job-capture-source-text-001",
    )
    assert payload["candidate_id"] == result.candidate_id
    assert candidate["candidate_type"] == "document_takeaway"
    assert candidate["proposed_content"] == "Workbench Document 的结论必须带 source_refs 并经过用户审核。"
    assert candidate["source_refs"] == [
        {
            "source_id": "source-text-001",
            "locator": "source:metadata",
            "quote": "Workbench selected note",
        },
        {
            "source_id": "source-text-001",
            "locator": "job:job-capture-source-text-001",
            "quote": "Workbench capture Job trace",
        },
    ]
    assert candidate["provenance"] == {
        "model_result_id": None,
        "model_request_id": None,
        "recall_result_id": None,
        "document_id": draft.document_id,
        "document_revision": draft.document_revision,
        "input_refs": [
            {
                "kind": "document",
                "object_id": draft.document_id,
                "uri": f"crp://default/documents/{draft.document_id}.json",
            }
        ],
    }
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_workbench_document_memory_candidate_rejects_conflicted_document(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    draft = CreateDocumentDraftFromWorkbenchSelection(documents=documents).execute(_selection())
    user_edit = documents.save_user_edit(
        draft.document_id,
        markdown="# User edited\n\n用户编辑优先。",
        expected_revision=draft.document_revision,
    )
    documents.apply_ai_patch(
        draft.document_id,
        blocks=(
            {
                "id": "block-001",
                "block_type": "heading",
                "origin": "ai",
                "content": "AI overwrite attempt",
                "source_refs": list(draft.source_refs),
                "edited_by_user": False,
                "lock_policy": "source_required",
            },
        ),
        expected_revision=int(user_edit["revision"]),
    )
    use_case = CreateMemoryCandidateFromWorkbenchDocument(documents=documents, candidates=candidates)

    with pytest.raises(WorkbenchDocumentMemoryCandidateError, match="draft without unresolved conflict"):
        use_case.execute(draft.document_id, expected_revision=3)

    assert candidates.list_by_project("project-alpha") == ()
    assert not (tmp_path / "library").exists()


def test_workbench_document_memory_candidate_rejects_stale_revision(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    draft = CreateDocumentDraftFromWorkbenchSelection(documents=documents).execute(_selection())
    documents.save_user_edit(
        draft.document_id,
        markdown="# User edited\n\n用户编辑后必须用最新 revision。",
        expected_revision=draft.document_revision,
    )
    use_case = CreateMemoryCandidateFromWorkbenchDocument(documents=documents, candidates=candidates)

    with pytest.raises(WorkbenchDocumentMemoryCandidateError, match="expected revision 1, found 2"):
        use_case.execute(draft.document_id, expected_revision=1)

    assert candidates.list_by_project("project-alpha") == ()
    assert not (tmp_path / "library").exists()


def test_workbench_document_memory_candidate_composition_and_review_path_use_temp_storage_only(
    tmp_path: Path,
) -> None:
    draft_handoff = build_workbench_document_draft_handoff(ROOT, runtime_root=tmp_path)
    draft = draft_handoff.execute(_selection(), title="Composed Document to Candidate")
    candidate_handoff = build_workbench_document_memory_candidate_handoff(ROOT, runtime_root=tmp_path)

    result = candidate_handoff.execute(
        draft.document_id,
        expected_revision=draft.document_revision,
        proposed_content="组合入口生成待审 Document Memory Candidate。",
        created_at="2026-07-01T10:10:00+08:00",
    )
    object_store = _store(tmp_path)
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(result.candidate_id)
    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        result.candidate_id,
        review_reason="用户确认从 Document 提取为草稿 Atom。",
        reviewed_at="2026-07-01T10:11:00+08:00",
    )
    staged = saga_records(tmp_path).read("staging_atoms", operation.evidence.draft_id)

    assert candidate is not None
    assert staged is not None
    assert candidate["status"] == "pending_review"
    assert staged.payload["content"] == "组合入口生成待审 Document Memory Candidate。"
    assert staged.payload["source_refs"] == candidate["source_refs"]
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_candidates").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()
