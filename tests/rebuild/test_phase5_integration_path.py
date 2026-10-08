from __future__ import annotations

import json
from pathlib import Path

from core.document_engine import ObjectStoreDocumentRepository
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import (
    CreateAnswerModelRequestFromRecallResult,
    CreateDocumentFromModelResult,
    CreateMemoryCandidateFromModelResult,
)
from core.search_and_recall import (
    ObjectStoreRecallIndex,
    ObjectStoreRecallRepository,
    RecallIndexEntry,
    RecallQuery,
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


def test_phase5_persistent_index_to_reviewed_draft_atom_path(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    recall_index = ObjectStoreRecallIndex(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)

    manifest = recall_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-project-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="项目回答必须先使用项目 Skill 和可追溯证据。",
                source_refs=("source-alpha#char:0-30",),
                trust_status="user_confirmed",
                base_score=0.72,
            ),
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="Phase 5 需要保留 Recall 到 Memory Candidate 的来源链路。",
                source_refs=("source-alpha#char:31-90",),
                trust_status="system_generated",
                base_score=0.64,
            ),
        ),
        source="phase5-integration-test",
        rebuilt_at="2026-06-30T21:00:00+08:00",
    )
    recall_request = recalls.create_project_default_request(
        project_id="project-alpha",
        query="Phase 5 的下一步应该如何保证可追溯？",
        project_skill_id="skill-project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        created_at="2026-06-30T21:01:00+08:00",
    )
    hits = recall_index.recall(
        RecallQuery(
            text=str(recall_request["query"]),
            project_id=str(recall_request["project_id"]),
            layers=tuple(recall_request["layers"]),
            allowed_trust_statuses=tuple(recall_request["trust_filter"]["include"]),
            limit=12,
        )
    )
    recall_result = recalls.create_result_from_hits(
        request_id=str(recall_request["id"]),
        hits=hits,
        created_at="2026-06-30T21:02:00+08:00",
    )
    answer_request = CreateAnswerModelRequestFromRecallResult(
        recalls=recalls,
        model_requests=model_requests,
    ).execute(
        str(recall_result["id"]),
        created_at="2026-06-30T21:03:00+08:00",
    )
    model_result = model_results.create_completed_local_result(
        request_id=answer_request.model_request_id,
        output_text="Phase 5 的下一步是保持 Recall、文档和记忆候选的同一组 source_refs。",
        input_tokens=80,
        output_tokens=20,
        started_at="2026-06-30T21:04:00+08:00",
        completed_at="2026-06-30T21:04:01+08:00",
        elapsed_ms=1000,
    )
    document_result = CreateDocumentFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        documents=documents,
    ).execute(
        str(model_result["id"]),
        title="Phase 5 集成回答草稿",
    )
    candidate_result = CreateMemoryCandidateFromModelResult(
        model_requests=model_requests,
        model_results=model_results,
        candidates=candidates,
    ).execute(
        str(model_result["id"]),
        document_id=document_result.document_id,
        document_revision=document_result.document_revision,
        created_at="2026-06-30T21:05:00+08:00",
    )
    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_result.candidate_id,
        review_reason="用户确认 Phase 5 集成路径输出可作为草稿事实。",
        reviewed_at="2026-06-30T21:06:00+08:00",
        tags=("phase5", "integration", "traceable"),
    )

    model_request = model_requests.get_request(answer_request.model_request_id)
    document = documents.read(document_result.document_id)
    document_revision = documents.revision(document_result.document_id, document_result.document_revision)
    candidate = candidates.get(candidate_result.candidate_id)
    staged = saga_records(tmp_path).read("staging_atoms", operation.evidence.draft_id)
    draft_atom = staged.payload if staged is not None else None

    assert manifest["backend_kind"] == "object_store_lexical"
    assert manifest["vector"]["enabled"] is False
    assert [hit.object_id for hit in hits] == ["skill-project-alpha", "atom-alpha"]
    assert recall_result["status"] == "evidence_found"
    assert model_request is not None
    assert document is not None
    assert document_revision is not None
    assert candidate is not None
    assert draft_atom is not None
    assert answer_request.source_ref_count == 2
    assert model_request["provider_preference"]["allow_remote"] is False
    assert model_request["payload"]["recall_result_id"] == recall_result["id"]
    assert model_request["payload"]["source_refs"] == [
        {"source_id": "source-alpha", "locator": "char:0-30"},
        {"source_id": "source-alpha", "locator": "char:31-90"},
    ]
    assert document["source_snapshot"]["source_refs"] == model_request["payload"]["source_refs"]
    assert candidate["provenance"]["recall_result_id"] == recall_result["id"]
    assert candidate["provenance"]["model_request_id"] == model_request["id"]
    assert candidate["provenance"]["model_result_id"] == model_result["id"]
    assert candidate["provenance"]["document_id"] == document_result.document_id
    assert candidate["status"] == "promoted"
    assert candidate["review"]["reviewed_by"] == "user"
    assert draft_atom["source_refs"] == candidate["source_refs"]
    assert draft_atom["trust_status"] == "system_generated"
    assert draft_atom["tags"] == ["phase5", "integration", "traceable"]
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    assert validate_contract_instance("model_request.schema.json", _schema("model_request.schema.json"), model_request) == []
    assert validate_contract_instance("model_result.schema.json", _schema("model_result.schema.json"), model_result) == []
    assert validate_contract_instance("document.schema.json", _schema("document.schema.json"), document) == []
    assert (
        validate_contract_instance(
            "document_revision.schema.json",
            _schema("document_revision.schema.json"),
            document_revision,
        )
        == []
    )
    assert validate_contract_instance("memory_candidate.schema.json", _schema("memory_candidate.schema.json"), candidate) == []
    assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), draft_atom) == []
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()
