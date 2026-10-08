from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_model_result_memory_candidate_handoff
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import CreateMemoryCandidateFromModelResult, ModelResultMemoryCandidateError
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _answer_model_request(request_id: str = "model-request-memory-candidate") -> dict[str, object]:
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
                },
                {
                    "kind": "project_skill",
                    "object_id": "skill-project-alpha",
                    "uri": "crp://default/projects/project-alpha/project-skill.json",
                },
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
        "created_at": "2026-06-30T16:00:00+08:00",
    }


def test_model_result_memory_candidate_handoff_creates_reviewable_candidate(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    request = requests.save_request(_answer_model_request())
    model_result = results.create_completed_local_result(
        request_id=str(request["id"]),
        output_text="项目问答必须先使用当前项目 Skill 和可追溯证据。",
        input_tokens=40,
        output_tokens=12,
        started_at="2026-06-30T16:01:00+08:00",
        completed_at="2026-06-30T16:01:01+08:00",
        elapsed_ms=1000,
    )
    handoff = CreateMemoryCandidateFromModelResult(
        model_requests=requests,
        model_results=results,
        candidates=candidates,
    )

    result = handoff.execute(
        str(model_result["id"]),
        document_id="document-alpha",
        document_revision=1,
        created_at="2026-06-30T16:02:00+08:00",
    )
    candidate = candidates.get(result.candidate_id)

    assert candidate is not None
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert result.project_id == "project-alpha"
    assert result.model_request_id == request["id"]
    assert result.model_result_id == model_result["id"]
    assert result.status == "pending_review"
    assert result.target_layer == "atom"
    assert candidate["candidate_type"] == "answer_fact"
    assert candidate["proposed_content"] == "项目问答必须先使用当前项目 Skill 和可追溯证据。"
    assert candidate["source_refs"] == [
        {"source_id": "source-alpha", "locator": "char:0-20", "quote": "Alpha evidence"}
    ]
    assert candidate["provenance"]["model_result_id"] == model_result["id"]
    assert candidate["provenance"]["model_request_id"] == request["id"]
    assert candidate["provenance"]["recall_result_id"] == "recall-result-alpha"
    assert candidate["provenance"]["document_id"] == "document-alpha"
    assert candidate["provenance"]["document_revision"] == 1
    assert [ref["kind"] for ref in candidate["provenance"]["input_refs"]] == [
        "recall_result",
        "project_skill",
        "model_request",
        "model_result",
        "document",
    ]
    assert candidate["review"] == {
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "reason": "模型回答只能进入待审候选，不能自动写入长期记忆。",
        "reviewed_by": None,
        "reviewed_at": None,
    }
    assert candidates.list_by_project("project-alpha") == (candidate,)
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_scenarios").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_series_memory").exists()
    assert not (tmp_path / "library").exists()


def test_model_result_memory_candidate_handoff_rejects_blocked_results(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    request = requests.save_request(_answer_model_request())
    blocked = results.create_safety_blocked_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T16:03:00+08:00",
    )
    handoff = CreateMemoryCandidateFromModelResult(
        model_requests=requests,
        model_results=results,
        candidates=candidates,
    )

    with pytest.raises(ModelResultMemoryCandidateError, match="completed"):
        handoff.execute(str(blocked["id"]))

    assert candidates.list_by_project("project-alpha") == ()
    assert not (tmp_path / "library").exists()


def test_model_result_memory_candidate_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    request = ObjectStoreModelRequestRepository(object_store).save_request(
        _answer_model_request("model-request-composed-memory-candidate")
    )
    model_result = ObjectStoreModelResultRepository(object_store).create_completed_local_result(
        request_id=str(request["id"]),
        output_text="组合入口可以生成待审记忆候选。",
        input_tokens=20,
        output_tokens=8,
        started_at="2026-06-30T16:04:00+08:00",
        completed_at="2026-06-30T16:04:01+08:00",
        elapsed_ms=1000,
    )
    handoff = build_model_result_memory_candidate_handoff(ROOT, runtime_root=tmp_path)

    result = handoff.execute(str(model_result["id"]), created_at="2026-06-30T16:05:00+08:00")
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(result.candidate_id)

    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["provenance"]["document_id"] is None
    assert candidate["provenance"]["document_revision"] is None
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_candidates").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()
