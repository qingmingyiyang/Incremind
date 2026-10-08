from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_memory_candidate_review
from core.memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore
from core.model_gateway import ObjectStoreModelRequestRepository, ObjectStoreModelResultRepository
from core.product_core import (
    CreateMemoryCandidateFromModelResult,
    MemoryCandidateReviewError,
    ReviewMemoryCandidate,
    ServeMemoryCandidateReviewEndpoint,
    serialize_memory_candidate_review_result,
)
from core.storage_provider import JsonObjectStore
from tests.rebuild.memory_candidate_saga_review_testlib import (
    review_candidate_to_staging,
    saga_promote_closures,
    saga_records,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _answer_model_request(request_id: str = "model-request-review") -> dict[str, object]:
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
        "created_at": "2026-06-30T17:00:00+08:00",
    }


def _candidate(tmp_path: Path, *, request_id: str = "model-request-review") -> tuple[
    JsonObjectStore,
    str,
]:
    object_store = _store(tmp_path)
    requests = ObjectStoreModelRequestRepository(object_store)
    results = ObjectStoreModelResultRepository(object_store)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    request = requests.save_request(_answer_model_request(request_id))
    model_result = results.create_completed_local_result(
        request_id=str(request["id"]),
        output_text="项目问答必须保留证据来源。",
        input_tokens=30,
        output_tokens=10,
        started_at="2026-06-30T17:01:00+08:00",
        completed_at="2026-06-30T17:01:01+08:00",
        elapsed_ms=1000,
    )
    handoff = CreateMemoryCandidateFromModelResult(
        model_requests=requests,
        model_results=results,
        candidates=candidates,
    )
    result = handoff.execute(str(model_result["id"]), created_at="2026-06-30T17:02:00+08:00")
    return object_store, result.candidate_id


def _layer_candidate(
    tmp_path: Path,
    *,
    target_layer: str,
    candidate_id: str | None = None,
    series_id: str | None = None,
) -> tuple[
    JsonObjectStore,
    str,
]:
    object_store = _store(tmp_path)
    clean_candidate_id = candidate_id or f"memory-candidate-{target_layer}"
    candidate = {
        "schema_version": "1.0.0",
        "id": clean_candidate_id,
        "project_id": "project-alpha",
        **({"series_id": series_id} if series_id else {}),
        "target_layer": target_layer,
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": f"{target_layer} 候选应先进入 staging，不能直接发布长期记忆。",
        "source_refs": [
            {
                "source_id": "source-alpha",
                "locator": "char:0-80",
                "quote": "Alpha evidence for layered memory",
            }
        ],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": "source-content-read-alpha",
            "input_refs": [
                {
                    "kind": "source",
                    "object_id": "source-alpha",
                    "uri": "crp://default/sources/source-alpha.json",
                },
                {
                    "kind": "source_content_read",
                    "object_id": "source-content-read-alpha",
                    "uri": "crp://default/source-content-reads/source-content-read-alpha.json",
                },
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "候选需要用户确认。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": "2026-07-02T10:00:00+08:00",
        "updated_at": "2026-07-02T10:00:00+08:00",
    }
    ObjectStoreMemoryCandidateRepository(object_store).save(candidate)
    return object_store, clean_candidate_id


def test_memory_candidate_reject_updates_review_without_memory_side_effects(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(
        candidates=candidates,
        memory=ObjectStoreMemoryStore(object_store),
    )

    result = reviewer.reject(
        candidate_id,
        reason="该回答不应沉淀为长期记忆。",
        reviewed_at="2026-06-30T17:03:00+08:00",
    )
    candidate = candidates.get(candidate_id)

    assert candidate is not None
    assert result.status == "rejected"
    assert result.promoted_object_id is None
    assert candidate["status"] == "rejected"
    assert candidate["review"] == {
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "reason": "该回答不应沉淀为长期记忆。",
        "reviewed_by": "user",
        "reviewed_at": "2026-06-30T17:03:00+08:00",
    }
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_memory_candidate_promote_writes_draft_atom_with_source_refs(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-promote")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_id,
        review_reason="用户确认该回答可作为项目事实候选。",
        reviewed_at="2026-06-30T17:04:00+08:00",
        tags=("project", "evidence", "project"),
    )
    candidate = candidates.get(candidate_id)
    staged = saga_records(tmp_path).read("staging_atoms", operation.evidence.draft_id)
    atom = staged.payload if staged is not None else None

    assert candidate is not None
    assert atom is not None
    assert operation.state == "finalized"
    assert operation.evidence.layer == "atom"
    assert candidate["status"] == "promoted"
    assert candidate["review"]["reviewed_by"] == "user"
    assert candidate["review"]["reviewed_at"] == "2026-06-30T17:04:00+08:00"
    assert atom["content"] == candidate["proposed_content"]
    assert atom["atom_type"] == "fact"
    assert atom["tags"] == ["project", "evidence"]
    assert atom["confidence"] == 0.7
    assert atom["trust_status"] == "system_generated"
    assert atom["source_id"] == "source-alpha"
    assert atom["source_refs"] == candidate["source_refs"]
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), atom) == []
    assert saga_records(tmp_path).list("memory_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_memory_candidate_withdraw_updates_review_without_memory_side_effects(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-withdraw")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))

    result = reviewer.withdraw(
        candidate_id,
        reason="用户撤回该候选，暂不进入记忆流程。",
        reviewed_at="2026-06-30T17:04:30+08:00",
    )
    candidate = candidates.get(candidate_id)

    assert candidate is not None
    assert result.status == "withdrawn"
    assert result.promoted_object_id is None
    assert candidate["status"] == "withdrawn"
    assert candidate["review"]["reason"] == "用户撤回该候选，暂不进入记忆流程。"
    assert candidate["review"]["reviewed_by"] == "user"
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_memory_candidate_promotion_requires_user_and_pending_status(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-pending-guard")
    reviewer = ReviewMemoryCandidate(
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
        memory=ObjectStoreMemoryStore(object_store),
    )
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)

    with pytest.raises(MemoryCandidateReviewError, match="requires user"):
        promote_to_atom(
            candidate_id,
            reason="系统不能代替用户确认。",
            reviewed_by="system",
            reviewed_at="2026-06-30T17:05:00+08:00",
        )

    reviewer.reject(
        candidate_id,
        reason="先拒绝该候选。",
        reviewed_at="2026-06-30T17:06:00+08:00",
    )
    with pytest.raises(MemoryCandidateReviewError, match="pending_review"):
        promote_to_atom(
            candidate_id,
            reason="已拒绝候选不能再次提升。",
            reviewed_at="2026-06-30T17:07:00+08:00",
        )
    assert saga_records(tmp_path).list("staging_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_memory_candidate_review_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-composed")
    reviewer = build_memory_candidate_review(ROOT, runtime_root=tmp_path)

    with pytest.raises(
        MemoryCandidateReviewError,
        match="requires the durable Memory publication review staging saga",
    ):
        reviewer.promote_to_layer(
            candidate_id,
            target_layer="atom",
            reason="域类不再直接晋升，必须走 durable saga。",
        )

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_id,
        review_reason="组合入口提升为草稿 Atom。",
        reviewed_at="2026-06-30T17:08:00+08:00",
    )
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)
    staged = saga_records(tmp_path).read("staging_atoms", operation.evidence.draft_id)

    assert candidate is not None
    assert staged is not None
    assert candidate["status"] == "promoted"
    assert staged.payload["source_refs"] == candidate["source_refs"]
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_candidates").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_memory_candidate_review_endpoint_reads_candidate_detail(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-endpoint-read")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="GET",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body=None,
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["candidate_id"] == candidate_id
    assert response.body["candidate_status"] == "pending_review"
    assert response.body["available_actions"] == ["reject", "promote_to_atom", "withdraw"]
    assert response.body["memory_publication_state"] == "not_published"
    assert response.body["source_refs"] == [
        {"source_id": "source-alpha", "locator": "char:0-20", "quote": "Alpha evidence"}
    ]
    assert response.body["review"]["requires_user_confirmation"] is True


@pytest.mark.parametrize(
    "target_layer",
    ["atom", "scenario", "series_memory", "project_skill"],
)
def test_memory_candidate_promote_to_layer_requires_durable_saga(
    tmp_path: Path,
    target_layer: str,
) -> None:
    object_store, candidate_id = _layer_candidate(tmp_path, target_layer=target_layer)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))

    with pytest.raises(MemoryCandidateReviewError, match="requires the durable"):
        reviewer.promote_to_layer(
            candidate_id,
            target_layer=target_layer,
            reason=f"域类不允许直接晋升 {target_layer}。",
            reviewed_at="2026-07-02T10:05:00+08:00",
            series_id="series-alpha",
            scenario_ids=("scenario-alpha",),
            atom_ids=("atom-alpha",),
            tags=("four-layer", "four-layer", target_layer),
        )

    candidate = candidates.get(candidate_id)
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert saga_records(tmp_path).list("staging_atoms") == ()
    assert saga_records(tmp_path).list("staging_scenarios") == ()
    assert saga_records(tmp_path).list("staging_series_memory") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_scenarios").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_series_memory").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_publications").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_transitions").exists()


@pytest.mark.parametrize(
    ("target_layer", "expected_action"),
    [
        ("scenario", "promote_to_scenario"),
        ("series_memory", "promote_to_series_memory"),
        ("project_skill", "promote_to_project_skill"),
    ],
)
def test_memory_candidate_review_endpoint_exposes_layered_promote_actions(
    tmp_path: Path,
    target_layer: str,
    expected_action: str,
) -> None:
    object_store, candidate_id = _layer_candidate(tmp_path, target_layer=target_layer)
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="GET",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body=None,
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
        promote_to_layer=promote_to_layer,
    )

    assert response.status_code == 200
    assert response.body["target_layer"] == target_layer
    assert response.body["available_actions"] == ["reject", expected_action, "withdraw"]


def test_memory_candidate_review_endpoint_promotes_scenario_to_staging_only(tmp_path: Path) -> None:
    object_store, candidate_id = _layer_candidate(tmp_path, target_layer="scenario")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={
            "action": "promote_to_scenario",
            "reason": "用户确认场景候选进入草稿。",
            "series_id": "series-alpha",
            "atom_ids": ["atom-alpha"],
            "tags": ["scenario"],
        },
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
        promote_to_layer=promote_to_layer,
    )
    staged = saga_records(tmp_path).read("staging_scenarios", str(response.body["promoted_object_id"]))

    assert response.status_code == 200
    assert response.body["promoted_layer"] == "scenario"
    assert response.body["memory_publication_state"] == "staging_scenario_created_not_published"
    assert staged is not None
    assert staged.payload["series_id"] == "series-alpha"
    assert staged.payload["atom_ids"] == ["atom-alpha"]
    assert saga_records(tmp_path).list("memory_scenarios") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_scenarios").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_scenarios").exists()


def test_series_memory_review_preserves_candidate_series_identity(tmp_path: Path) -> None:
    object_store, candidate_id = _layer_candidate(
        tmp_path,
        target_layer="series_memory",
        series_id="series-from-confirmed-assignment",
    )
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    candidate = candidates.get(candidate_id)
    assert candidate is not None
    series_id = str(candidate.get("series_id"))

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_id,
        review_reason="用户确认系列候选进入草稿。",
        reviewed_at="2026-07-02T10:07:00+08:00",
        series_id=series_id,
    )
    staged = saga_records(tmp_path).read("staging_series_memory", operation.evidence.draft_id)

    assert operation.state == "finalized"
    assert staged is not None
    assert staged.payload["series_id"] == "series-from-confirmed-assignment"
    assert saga_records(tmp_path).list("memory_series_memory") == ()


def test_memory_candidate_review_endpoint_rejects_without_memory_side_effects(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-endpoint-reject")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={"action": "reject", "reason": "用户确认该候选不进入记忆。"},
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    candidate = candidates.get(candidate_id)

    assert response.status_code == 200
    assert response.body["status"] == "rejected"
    assert response.body["memory_publication_state"] == "candidate_rejected_not_published"
    assert candidate is not None
    assert candidate["status"] == "rejected"
    assert candidate["review"]["reviewed_by"] == "user"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_memory_candidate_review_endpoint_withdraws_without_memory_side_effects(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-endpoint-withdraw")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={"action": "withdraw", "reason": "用户撤回该候选。"},
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
        withdraw_candidate=reviewer.withdraw,
    )
    candidate = candidates.get(candidate_id)

    assert response.status_code == 200
    assert response.body["status"] == "withdrawn"
    assert response.body["memory_publication_state"] == "candidate_withdrawn_not_published"
    assert candidate is not None
    assert candidate["status"] == "withdrawn"
    assert candidate["review"]["reviewed_by"] == "user"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_memory_candidate_review_endpoint_promotes_to_staging_atom_only(tmp_path: Path) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-endpoint-promote")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)

    response = ServeMemoryCandidateReviewEndpoint().execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={
            "action": "promote_to_atom",
            "reason": "用户确认该候选进入草稿记忆。",
            "tags": ["review", "review"],
            "confidence": 0.8,
        },
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    candidate = candidates.get(candidate_id)
    staged = saga_records(tmp_path).read("staging_atoms", str(response.body["promoted_object_id"]))

    assert response.status_code == 200
    assert response.body["status"] == "promoted"
    assert response.body["promoted_layer"] == "atom"
    assert response.body["memory_publication_state"] == "staging_atom_created_not_published"
    assert candidate is not None
    assert candidate["status"] == "promoted"
    assert staged is not None
    assert staged.payload["tags"] == ["review"]
    assert staged.payload["confidence"] == 0.8
    assert saga_records(tmp_path).list("memory_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_memory_candidate_review_endpoint_rejects_wrong_method_action_and_repeated_review(
    tmp_path: Path,
) -> None:
    object_store, candidate_id = _candidate(tmp_path, request_id="model-request-review-endpoint-errors")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    reviewer = ReviewMemoryCandidate(candidates=candidates, memory=ObjectStoreMemoryStore(object_store))
    promote_to_atom, _promote_to_layer = saga_promote_closures(object_store, tmp_path)
    endpoint = ServeMemoryCandidateReviewEndpoint()

    wrong_method = endpoint.execute(
        method="PUT",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body=None,
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    wrong_action = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={"action": "publish", "reason": "不允许直接发布。"},
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    missing = endpoint.execute(
        method="GET",
        path="/api/rebuild/memory-candidates/memory-candidate-missing/review",
        body=None,
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={"action": "reject", "reason": "先拒绝。"},
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )
    repeated = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/memory-candidates/{candidate_id}/review",
        body={"action": "promote_to_atom", "reason": "已拒绝不能确认。"},
        get_candidate=candidates.get,
        reject_candidate=reviewer.reject,
        promote_to_atom=promote_to_atom,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "GET, POST"
    assert wrong_action.status_code == 400
    assert wrong_action.body["reason"] == (
        "review action must be reject, promote_to_atom, promote_to_scenario, "
        "promote_to_series_memory, promote_to_project_skill, or withdraw"
    )
    assert missing.status_code == 404
    assert missing.body["actionable"] is False
    assert repeated.status_code == 400
    assert repeated.body["reason"] == "candidate must be pending_review"
    assert saga_records(tmp_path).list("staging_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
