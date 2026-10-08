from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from pathlib import Path

import pytest

from core.composition import build_recall_repository
from core.search_and_recall import ObjectStoreRecallRepository, RecallRepositoryError
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _repository(tmp_path: Path) -> ObjectStoreRecallRepository:
    return ObjectStoreRecallRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )


def _project_default_request(repository: ObjectStoreRecallRepository) -> dict[str, object]:
    return dict(
        repository.create_project_default_request(
            project_id="project-alpha",
            query="这个项目下一版应该先改哪里？",
            project_skill_id="skill-project-alpha",
            created_at="2026-06-29T15:00:00+08:00",
        )
    )


def _evidence_result(request: dict[str, object]) -> dict[str, object]:
    layers = list(request["layers"])
    return {
        "schema_version": "1.0.0",
        "id": "recall-result-project-alpha-001",
        "request_id": request["id"],
        "project_id": request["project_id"],
        "status": "evidence_found",
        "hits": [
            {
                "hit_id": "hit-skill-alpha",
                "layer": "l3_project_skill",
                "object_id": "skill-project-alpha",
                "project_id": "project-alpha",
                "source_project_label": None,
                "trust_status": "user_confirmed",
                "score": 0.96,
                "token_estimate": 900,
                "source_refs": [
                    {
                        "source_id": "source-text-001",
                        "locator": "char:0-80",
                        "quote": "生成文档必须保留可追溯 source refs。",
                    }
                ],
                "snippet": "项目 Skill 要求后续输出沿用既有结构，并保留来源引用。",
                "explanation": "L3 Project Skill 是项目问答的首个证据层。",
            }
        ],
        "coverage": {
            "status": "sufficient",
            "requested_layers": layers,
            "covered_layers": ["l3_project_skill"],
            "missing_layers": [layer for layer in layers if layer != "l3_project_skill"],
            "low_trust": False,
            "source_ref_count": 1,
        },
        "truncation": {
            "applied": False,
            "reason": "none",
            "dropped_hit_ids": [],
            "final_hit_count": 1,
            "final_token_estimate": 900,
        },
        "explanation": {
            "summary": "先返回 Project Skill，证据足够支持回答。",
            "layer_order": layers,
            "warnings": [],
        },
        "cross_project": {
            "used": False,
            "grant_id": None,
            "project_ids": [],
        },
        "errors": [],
        "created_at": "2026-06-29T15:20:00+08:00",
    }


def test_recall_repository_creates_project_default_request_with_skill_first_context(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    request = _project_default_request(repository)
    reloaded = _repository(tmp_path)

    assert validate_contract_instance("recall_request.schema.json", _schema("recall_request.schema.json"), request) == []
    assert request["scope"] == "project"
    assert request["layers"][:2] == ["l4_persona", "l3_project_skill"]
    assert request["cross_project"] == {"allowed": False, "grant_id": None, "project_ids": []}
    assert request["required_context_refs"][0]["kind"] == "project_skill"
    assert request["required_context_refs"][0]["object_id"] == "skill-project-alpha"
    assert reloaded.get_request(str(request["id"])) == request
    assert reloaded.list_requests("project-alpha") == (request,)
    assert reloaded.list_requests("project-beta") == ()
    assert not (tmp_path / "library").exists()


def test_recall_repository_persists_project_result_with_isolation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    result = _evidence_result(request)

    saved = repository.save_result(result)
    reloaded = _repository(tmp_path)

    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), saved) == []
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.list_results("project-alpha") == (result,)
    assert reloaded.results_for_request(str(request["id"])) == (result,)
    assert reloaded.list_results("project-beta") == ()
    assert not (tmp_path / "library").exists()


def test_recall_repository_replays_identical_request_and_result_without_new_records(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    replayed_request = repository.create_project_default_request(
        project_id="project-alpha",
        query="这个项目下一版应该先改哪里？",
        project_skill_id="skill-project-alpha",
        created_at="2026-07-15T18:00:00+08:00",
    )
    result = repository.save_result(_evidence_result(request))
    replay_payload = dict(_evidence_result(request))
    replay_payload["created_at"] = "2026-07-15T18:01:00+08:00"
    replayed_result = repository.save_result(replay_payload)

    assert replayed_request == request
    assert replayed_result == result
    assert repository.list_requests("project-alpha") == (request,)
    assert repository.list_results("project-alpha") == (result,)


def test_recall_repository_replay_rejects_payload_drift_for_same_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    drifted_request = dict(request)
    drifted_request["query"] = "同一ID不能代表另一条问题。"
    with pytest.raises(RecallRepositoryError, match="request conflicts with existing payload"):
        repository.save_request(drifted_request)

    result = dict(repository.save_result(_evidence_result(request)))
    drifted_result = json.loads(json.dumps(result, ensure_ascii=False))
    drifted_result["hits"][0]["snippet"] = "同一结果ID不能覆盖新的证据正文。"
    with pytest.raises(RecallRepositoryError, match="result conflicts with existing payload"):
        repository.save_result(drifted_result)


def test_recall_repository_concurrent_identical_creates_converge(tmp_path: Path) -> None:
    def create_request(_index: int) -> Mapping[str, object]:
        return _repository(tmp_path).create_project_default_request(
            project_id="project-alpha",
            query="并发相同问题应收敛。",
            project_skill_id="skill-project-alpha",
            created_at="2026-07-15T18:02:00+08:00",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        requests = tuple(pool.map(create_request, range(16)))

    assert len({str(item["id"]) for item in requests}) == 1
    repository = _repository(tmp_path)
    assert len(repository.list_requests("project-alpha")) == 1
    request = dict(requests[0])
    result_payload = _evidence_result(request)

    def create_result(_index: int) -> Mapping[str, object]:
        return _repository(tmp_path).save_result(result_payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(create_result, range(16)))

    assert len({str(item["id"]) for item in results}) == 1
    assert len(repository.list_results("project-alpha")) == 1


def test_recall_repository_creates_insufficient_evidence_result_without_answer(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)

    result = dict(
        repository.create_insufficient_evidence_result(
            request_id=str(request["id"]),
            message="没有找到足够证据。",
            created_at="2026-06-29T15:25:00+08:00",
        )
    )
    reloaded = _repository(tmp_path)

    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), result) == []
    assert result["status"] == "insufficient_evidence"
    assert result["hits"] == []
    assert result["coverage"] == {
        "status": "insufficient",
        "requested_layers": request["layers"],
        "covered_layers": [],
        "missing_layers": request["layers"],
        "low_trust": False,
        "source_ref_count": 0,
    }
    assert result["truncation"]["final_hit_count"] == 0
    assert result["truncation"]["final_token_estimate"] == 0
    assert result["errors"] == [{"code": "insufficient_evidence", "message": "没有找到足够证据。"}]
    assert result["cross_project"] == {"used": False, "grant_id": None, "project_ids": []}
    assert "answer" not in result
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.results_for_request(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_recall_repository_creates_index_unavailable_result_without_answer(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)

    result = dict(
        repository.create_index_unavailable_result(
            request_id=str(request["id"]),
            message="召回索引暂不可用。",
            created_at="2026-06-30T11:00:00+08:00",
        )
    )
    reloaded = _repository(tmp_path)

    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), result) == []
    assert result["status"] == "index_unavailable"
    assert result["hits"] == []
    assert result["coverage"] == {
        "status": "insufficient",
        "requested_layers": request["layers"],
        "covered_layers": [],
        "missing_layers": request["layers"],
        "low_trust": False,
        "source_ref_count": 0,
    }
    assert result["truncation"]["final_hit_count"] == 0
    assert result["truncation"]["final_token_estimate"] == 0
    assert result["errors"] == [{"code": "index_unavailable", "message": "召回索引暂不可用。"}]
    assert result["cross_project"] == {"used": False, "grant_id": None, "project_ids": []}
    assert "answer" not in result
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.results_for_request(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_recall_repository_rejects_cross_project_without_grant_or_foreign_hit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    invalid_request = dict(request)
    invalid_request["id"] = "recall-request-invalid-cross-project"
    invalid_request["scope"] = "cross_project"

    with pytest.raises(RecallRepositoryError, match="explicit grant"):
        repository.save_request(invalid_request)

    result = _evidence_result(request)
    result["id"] = "recall-result-foreign-hit"
    result["hits"][0]["project_id"] = "project-beta"
    with pytest.raises(RecallRepositoryError, match="foreign recall hit"):
        repository.save_result(result)


def test_recall_repository_rejects_insufficient_evidence_with_hits_or_missing_request(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    invalid = _evidence_result(request)
    invalid["id"] = "recall-result-invalid-insufficient-with-hit"
    invalid["status"] = "insufficient_evidence"
    invalid["errors"] = [{"code": "insufficient_evidence", "message": "没有找到足够证据。"}]

    with pytest.raises(RecallRepositoryError, match="must not include hits"):
        repository.save_result(invalid)
    with pytest.raises(RecallRepositoryError, match="not found"):
        repository.create_insufficient_evidence_result(request_id="recall-request-missing")


def test_recall_repository_rejects_index_unavailable_with_hits_or_missing_request(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    request = _project_default_request(repository)
    invalid = _evidence_result(request)
    invalid["id"] = "recall-result-invalid-index-unavailable-with-hit"
    invalid["status"] = "index_unavailable"
    invalid["errors"] = [{"code": "index_unavailable", "message": "召回索引暂不可用。"}]

    with pytest.raises(RecallRepositoryError, match="must not include hits"):
        repository.save_result(invalid)
    with pytest.raises(RecallRepositoryError, match="not found"):
        repository.create_index_unavailable_result(request_id="recall-request-missing")


def test_recall_repository_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    repository = build_recall_repository(ROOT, runtime_root=tmp_path)

    request = repository.create_project_default_request(
        project_id="project-alpha",
        query="这个项目下一版应该先改哪里？",
        project_skill_id="skill-project-alpha",
        created_at="2026-06-29T15:00:00+08:00",
    )

    assert repository.get_request(str(request["id"])) == request
    result = repository.create_insufficient_evidence_result(
        request_id=str(request["id"]),
        created_at="2026-06-29T15:25:00+08:00",
    )
    assert result["status"] == "insufficient_evidence"
    index_unavailable = repository.create_index_unavailable_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T11:05:00+08:00",
    )
    assert index_unavailable["status"] == "index_unavailable"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "recall_requests").exists()
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "recall_results").exists()
    assert not (tmp_path / "library").exists()
