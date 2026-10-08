from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_workbench_document_qa_recall
from core.document_engine import ObjectStoreDocumentRepository
from core.model_gateway import ObjectStoreModelRequestRepository
from core.product_core import (
    CreateAnswerModelRequestFromRecallResult,
    CreateDocumentDraftFromWorkbenchSelection,
    CreateWorkbenchDocumentQaRecall,
    WorkbenchDocumentDraftSelection,
    WorkbenchDocumentQaRecallError,
    serialize_workbench_document_qa_recall,
)
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _selection() -> WorkbenchDocumentDraftSelection:
    return WorkbenchDocumentDraftSelection(
        selection_id="selection-audio-qa-001",
        source_id="source-audio-qa-001",
        source_title="Audio metadata-only QA source",
        source_uri="crp://default/sources/source-audio-qa-001.json",
        capture_job_id="job-audio-capture-001",
        media_type="audio",
        selected_evidence_refs=(
            "crp://default/sources/source-audio-qa-001.json",
            "crp://default/jobs/job-audio-capture-001/trace.json",
        ),
        project_id="project-alpha",
    )


def _draft(documents: ObjectStoreDocumentRepository) -> tuple[str, int]:
    result = CreateDocumentDraftFromWorkbenchSelection(documents=documents).execute(
        _selection(),
        summary="Audio Source has metadata-only capture trace. No transcription, waveform, or content read.",
    )
    return result.document_id, result.document_revision


def _parts(
    tmp_path: Path,
) -> tuple[
    CreateWorkbenchDocumentQaRecall,
    ObjectStoreDocumentRepository,
    ObjectStoreRecallRepository,
    ObjectStoreModelRequestRepository,
]:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    answer_requests = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
    )
    return (
        CreateWorkbenchDocumentQaRecall(
            documents=documents,
            recalls=recalls,
            answer_requests=answer_requests,
        ),
        documents,
        recalls,
        model_requests,
    )


def test_workbench_document_qa_recall_creates_source_backed_local_model_request(
    tmp_path: Path,
) -> None:
    use_case, documents, recalls, model_requests = _parts(tmp_path)
    document_id, revision = _draft(documents)

    result = use_case.execute(
        document_id,
        expected_revision=revision,
        question="这个音频 Source 当前可以进入 QA 吗？",
        project_skill_id="skill-project-alpha",
        created_at="2026-07-01T10:00:00+08:00",
    )
    recall_request = recalls.get_request(result.recall_request_id)
    recall_result = recalls.get_result(result.recall_result_id)
    model_request = model_requests.get_request(str(result.model_request_id))

    assert result.status == "model_request_created"
    assert result.qa_answer_state == "model_request_ready_no_answer_generated"
    assert result.memory_publication_state == "not_published"
    assert result.source_refs_display == (
        "source-audio-qa-001#source:metadata",
        "source-audio-qa-001#job:job-audio-capture-001",
    )
    assert "model_provider_execution" in result.blocked_operations
    assert "long_term_memory_publication" in result.blocked_operations
    assert recall_request is not None
    assert recall_result is not None
    assert model_request is not None
    assert validate_contract_instance(
        "recall_request.schema.json",
        _schema("recall_request.schema.json"),
        recall_request,
    ) == []
    assert validate_contract_instance(
        "recall_result.schema.json",
        _schema("recall_result.schema.json"),
        recall_result,
    ) == []
    assert validate_contract_instance(
        "model_request.schema.json",
        _schema("model_request.schema.json"),
        model_request,
    ) == []
    assert recall_request["required_context_refs"][0]["kind"] == "project_skill"
    assert {ref["kind"] for ref in recall_request["required_context_refs"]} == {
        "project_skill",
        "document",
        "source",
    }
    assert recall_result["status"] == "partial"
    assert recall_result["hits"][0]["layer"] == "l0_source"
    assert model_request["payload"]["kind"] == "answer"
    assert model_request["payload"]["input_refs"][0]["kind"] == "recall_result"
    assert model_request["payload"]["input_refs"][1]["kind"] == "source"
    assert model_request["provider_preference"]["allow_remote"] is False
    assert "output" not in model_request
    assert not (tmp_path / "library").exists()
    assert "model_request_id" in serialize_workbench_document_qa_recall(result)


def test_workbench_document_qa_recall_blocks_when_index_unavailable(
    tmp_path: Path,
) -> None:
    use_case, documents, recalls, model_requests = _parts(tmp_path)
    document_id, revision = _draft(documents)

    result = use_case.execute(
        document_id,
        expected_revision=revision,
        question="索引不可用时是否允许编造回答？",
        project_skill_id="skill-project-alpha",
        index_available=False,
        created_at="2026-07-01T10:05:00+08:00",
    )
    recall_result = recalls.get_result(result.recall_result_id)

    assert result.status == "index_unavailable"
    assert result.model_request_id is None
    assert result.qa_answer_state == "blocked_index_unavailable"
    assert "model_request_creation" in result.blocked_operations
    assert recall_result is not None
    assert recall_result["status"] == "index_unavailable"
    assert validate_contract_instance(
        "recall_result.schema.json",
        _schema("recall_result.schema.json"),
        recall_result,
    ) == []
    assert model_requests.list_requests("project-alpha") == ()


def test_workbench_document_qa_recall_rejects_stale_document_revision(
    tmp_path: Path,
) -> None:
    use_case, documents, _recalls, model_requests = _parts(tmp_path)
    document_id, revision = _draft(documents)
    documents.save_user_edit(
        document_id,
        markdown="# User edit\n\n用户已经编辑该 Document。",
        expected_revision=revision,
    )

    with pytest.raises(WorkbenchDocumentQaRecallError, match="expected revision"):
        use_case.execute(
            document_id,
            expected_revision=revision,
            question="旧 revision 可以继续 QA 吗？",
            project_skill_id="skill-project-alpha",
        )

    assert model_requests.list_requests("project-alpha") == ()


def test_workbench_document_qa_recall_composition_uses_temp_storage(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(object_store)
    document_id, revision = _draft(documents)
    use_case = build_workbench_document_qa_recall(ROOT, runtime_root=tmp_path)

    result = use_case.execute(
        document_id,
        expected_revision=revision,
        question="composition 是否只使用临时 rebuild storage？",
        project_skill_id="skill-project-alpha",
        created_at="2026-07-01T10:10:00+08:00",
    )

    assert result.status == "model_request_created"
    assert result.model_request_id is not None
    assert not (tmp_path / "library").exists()
