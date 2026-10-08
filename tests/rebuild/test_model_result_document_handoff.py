from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_model_result_document_handoff
from core.document_engine import ObjectStoreDocumentRepository
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import CreateDocumentFromModelResult, ModelResultDocumentHandoffError
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _answer_model_request(request_id: str = "model-request-document-handoff") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": request_id,
        "project_id": "project-alpha",
        "capability": "text_generation",
        "provider_preference": {
            "mode": "local_only",
            "provider": None,
            "model": None,
            "allow_remote": False,
            "config_version": 1,
        },
        "payload": {
            "kind": "answer",
            "content": "请只根据证据回答。",
            "input_refs": [
                {
                    "kind": "recall_result",
                    "object_id": "recall-result-alpha",
                    "uri": "crp://default/recall-results/recall-result-alpha.json",
                }
            ],
            "source_refs": [
                {
                    "source_id": "source-alpha",
                    "locator": "char:0-20",
                    "quote": "Alpha evidence",
                }
            ],
            "recall_result_id": "recall-result-alpha",
        },
        "privacy": {
            "scope": "local_only",
            "pii": "possible",
            "allow_remote": False,
            "redaction": {
                "applied": False,
                "strategy": "none",
            },
            "retention": "none",
        },
        "timeout": {
            "request_timeout_ms": 60000,
            "idle_timeout_ms": 10000,
            "deadline_at": None,
        },
        "cancel": {
            "cancellable": True,
            "cancel_token": f"cancel-{request_id}",
            "requested": False,
        },
        "budget": {
            "max_input_tokens": 12000,
            "max_output_tokens": 2048,
            "max_total_tokens": 14048,
            "max_cost_usd": 0,
        },
        "response_schema": {
            "type": "text",
            "json_schema_uri": None,
            "strict": False,
        },
        "created_at": "2026-06-30T12:00:00+08:00",
    }


def test_model_result_document_handoff_creates_source_backed_document(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store)
    request = requests.save_request(_answer_model_request())
    model_result = results.create_completed_local_result(
        request_id=str(request["id"]),
        output_text="根据当前证据，下一步应生成可编辑文档草稿。",
        input_tokens=40,
        output_tokens=12,
        started_at="2026-06-30T14:00:00+08:00",
        completed_at="2026-06-30T14:00:01+08:00",
        elapsed_ms=1000,
    )
    handoff = CreateDocumentFromModelResult(
        model_requests=requests,
        model_results=results,
        documents=documents,
    )

    result = handoff.execute(str(model_result["id"]), title="模型回答文档")
    document = documents.read(result.document_id)
    revision = documents.revision(result.document_id, result.document_revision)
    markdown = documents.markdown(result.document_id)

    assert result.project_id == "project-alpha"
    assert result.model_request_id == request["id"]
    assert result.model_result_id == model_result["id"]
    assert result.document_revision == 1
    assert document is not None
    assert revision is not None
    assert document["type"] == "qa_answer"
    assert document["project_id"] == "project-alpha"
    assert "根据当前证据，下一步应生成可编辑文档草稿。" in (markdown or "")
    assert f"来源模型结果：{model_result['id']}" in (markdown or "")
    assert document["source_snapshot"]["source_refs"] == [
        {"source_id": "source-alpha", "locator": "char:0-20", "quote": "Alpha evidence"}
    ]
    assert revision["source_snapshot"] == document["source_snapshot"]
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


def test_model_result_document_handoff_rejects_non_completed_result(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store)
    request = requests.save_request(_answer_model_request())
    blocked = results.create_privacy_blocked_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T14:01:00+08:00",
    )
    handoff = CreateDocumentFromModelResult(
        model_requests=requests,
        model_results=results,
        documents=documents,
    )

    with pytest.raises(ModelResultDocumentHandoffError, match="completed"):
        handoff.execute(str(blocked["id"]))
    assert documents.revisions("missing") == ()
    assert not (tmp_path / "library").exists()


def test_model_result_document_handoff_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    request = ObjectStoreModelRequestRepository(object_store).save_request(
        _answer_model_request("model-request-composed-document")
    )
    model_result = ObjectStoreModelResultRepository(object_store).create_completed_local_result(
        request_id=str(request["id"]),
        output_text="组合入口生成文档草稿。",
        input_tokens=20,
        output_tokens=8,
        started_at="2026-06-30T14:02:00+08:00",
        completed_at="2026-06-30T14:02:01+08:00",
        elapsed_ms=1000,
    )
    handoff = build_model_result_document_handoff(ROOT, runtime_root=tmp_path)

    result = handoff.execute(str(model_result["id"]), title="Composed Model Result Document")
    documents = ObjectStoreDocumentRepository(object_store)
    document = documents.read(result.document_id)

    assert document is not None
    assert document["type"] == "qa_answer"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / "library").exists()
