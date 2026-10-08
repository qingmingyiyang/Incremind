from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_answer_model_request_from_recall, build_project_memory_recall
from core.model_gateway import ObjectStoreModelRequestRepository
from core.product_core import AnswerModelRequestError, CreateAnswerModelRequestFromRecallResult
from core.product_core import ReviewStrictQuestionAnswerAcceptance, serialize_strict_question_answer_acceptance
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _project_skill_fixture() -> dict[str, object]:
    return json.loads(
        (CONTRACT_ROOT / "fixtures" / "project_skill" / "valid-active-skill.json").read_text(
            encoding="utf-8"
        )
    )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _project_default_request(repository: ObjectStoreRecallRepository) -> dict[str, object]:
    return dict(
        repository.create_project_default_request(
            project_id="project-alpha",
            query="这个项目下一版应该先改哪里？",
            project_skill_id="skill-project-alpha",
            created_at="2026-06-30T10:00:00+08:00",
        )
    )


def _evidence_result(request: dict[str, object]) -> dict[str, object]:
    layers = list(request["layers"])
    return {
        "schema_version": "1.0.0",
        "id": "recall-result-project-alpha-answer-001",
        "request_id": request["id"],
        "project_id": request["project_id"],
        "status": "evidence_found",
        "hits": [
            {
                "hit_id": "hit-skill-alpha-answer",
                "layer": "l3_project_skill",
                "object_id": "skill-project-alpha",
                "project_id": "project-alpha",
                "source_project_label": None,
                "trust_status": "user_confirmed",
                "score": 0.98,
                "token_estimate": 700,
                "source_refs": [
                    {
                        "source_id": "source-text-001",
                        "locator": "char:0-80",
                        "quote": "回答必须先基于当前项目 Skill 和可追溯证据。",
                    }
                ],
                "snippet": "当前项目要求回答必须沿用项目 Skill，并保留来源引用。",
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
            "final_token_estimate": 700,
        },
        "explanation": {
            "summary": "Project Skill 证据足够创建回答请求。",
            "layer_order": layers,
            "warnings": [],
        },
        "cross_project": {
            "used": False,
            "grant_id": None,
            "project_ids": [],
        },
        "errors": [],
        "created_at": "2026-06-30T10:01:00+08:00",
    }


def _layered_evidence_result(request: dict[str, object]) -> dict[str, object]:
    result = _evidence_result(request)
    result["id"] = "recall-result-project-alpha-strict-qa-001"
    result["hits"] = [
        *result["hits"],
        {
            "hit_id": "hit-scenario-alpha-answer",
            "layer": "l2_scenario",
            "object_id": "scenario-project-alpha",
            "project_id": "project-alpha",
            "source_project_label": None,
            "trust_status": "system_generated",
            "score": 0.78,
            "token_estimate": 200,
            "source_refs": [{"source_id": "source-text-002", "locator": "char:0-90"}],
            "snippet": "项目下一版先完成严格问答验收。",
            "explanation": "L2 Scenario 补充当前项目阶段性摘要。",
        },
        {
            "hit_id": "hit-atom-alpha-answer",
            "layer": "l1_atom",
            "object_id": "atom-project-alpha",
            "project_id": "project-alpha",
            "source_project_label": None,
            "trust_status": "system_generated",
            "score": 0.7,
            "token_estimate": 120,
            "source_refs": [{"source_id": "source-text-003", "locator": "char:10-70"}],
            "snippet": "严格问答必须带可点击来源。",
            "explanation": "L1 Atom 补充当前项目可追溯事实。",
        },
    ]
    result["coverage"]["covered_layers"] = ["l3_project_skill", "l2_scenario", "l1_atom"]
    result["coverage"]["missing_layers"] = [
        layer for layer in request["layers"] if layer not in result["coverage"]["covered_layers"]
    ]
    result["coverage"]["source_ref_count"] = 3
    result["truncation"]["final_hit_count"] = 3
    result["truncation"]["final_token_estimate"] = 1020
    return result


def _parts(
    tmp_path: Path,
) -> tuple[
    CreateAnswerModelRequestFromRecallResult,
    ObjectStoreRecallRepository,
    ObjectStoreModelRequestRepository,
]:
    object_store = _store(tmp_path)
    recalls = ObjectStoreRecallRepository(object_store)
    model_requests = ObjectStoreModelRequestRepository(object_store)
    return (
        CreateAnswerModelRequestFromRecallResult(recalls=recalls, model_requests=model_requests),
        recalls,
        model_requests,
    )


def test_answer_model_request_created_only_from_evidence_bearing_recall_result(
    tmp_path: Path,
) -> None:
    use_case, recalls, model_requests = _parts(tmp_path)
    request = _project_default_request(recalls)
    recall_result = dict(recalls.save_result(_evidence_result(request)))

    result = use_case.execute(
        str(recall_result["id"]),
        created_at="2026-06-30T10:05:00+08:00",
    )
    model_request = model_requests.get_request(result.model_request_id)

    assert model_request is not None
    assert validate_contract_instance(
        "model_request.schema.json",
        _schema("model_request.schema.json"),
        model_request,
    ) == []
    assert result.project_id == "project-alpha"
    assert result.source_ref_count == 1
    assert model_request["capability"] == "text_generation"
    assert model_request["provider_preference"] == {
        "mode": "local_only",
        "provider": None,
        "model": None,
        "allow_remote": False,
        "config_version": 1,
    }
    assert model_request["privacy"]["allow_remote"] is False
    assert model_request["payload"]["kind"] == "answer"
    assert model_request["payload"]["recall_result_id"] == recall_result["id"]
    assert model_request["payload"]["source_refs"] == recall_result["hits"][0]["source_refs"]
    assert "记忆读取顺序固定为 L4 稳定画像 → L3 系列与项目方法" in model_request["payload"]["content"]
    assert "不得把未召回层当作已知内容" in model_request["payload"]["content"]
    assert "[source-text-001#char:0-80](crp://default/sources/source-text-001.json#char:0-80)" in model_request["payload"]["content"]
    assert model_request["payload"]["input_refs"][0] == {
        "kind": "recall_result",
        "object_id": recall_result["id"],
        "uri": f"crp://default/recall-results/{recall_result['id']}.json",
    }
    assert not model_requests.list_requests("project-beta")
    assert "output" not in model_request
    assert not (tmp_path / "library").exists()


def test_answer_prompt_normalizes_shuffled_hits_to_five_layer_order(
    tmp_path: Path,
) -> None:
    use_case, recalls, model_requests = _parts(tmp_path)
    request = _project_default_request(recalls)
    payload = _layered_evidence_result(request)
    payload["hits"] = list(reversed(payload["hits"]))
    recall_result = dict(recalls.save_result(payload))

    result = use_case.execute(
        str(recall_result["id"]),
        created_at="2026-06-30T10:05:30+08:00",
    )
    model_request = model_requests.get_request(result.model_request_id)

    assert model_request is not None
    content = model_request["payload"]["content"]
    assert content.index("layer=l3_project_skill") < content.index("layer=l2_scenario")
    assert content.index("layer=l2_scenario") < content.index("layer=l1_atom")
    assert [item["kind"] for item in model_request["payload"]["input_refs"][:4]] == [
        "recall_result",
        "project_skill",
        "scenario",
        "atom",
    ]


def test_strict_question_answer_acceptance_requires_layer_order_and_clickable_sources(
    tmp_path: Path,
) -> None:
    use_case, recalls, model_requests = _parts(tmp_path)
    request = _project_default_request(recalls)
    recall_result = dict(recalls.save_result(_layered_evidence_result(request)))
    model_request_result = use_case.execute(
        str(recall_result["id"]),
        created_at="2026-06-30T10:06:00+08:00",
    )
    model_request = model_requests.get_request(model_request_result.model_request_id)
    assert model_request is not None

    acceptance = ReviewStrictQuestionAnswerAcceptance().execute(
        recall_result=recall_result,
        model_request=model_request,
    )
    body = serialize_strict_question_answer_acceptance(acceptance)

    assert body["status"] == "ready"
    assert body["checks"]["starts_from_l3_project_skill"] is True
    assert body["checks"]["high_layers_before_low_layers"] is True
    assert body["checks"]["lower_layers_have_source_refs"] is True
    assert body["checks"]["answer_prompt_requires_clickable_sources"] is True
    assert body["source_links"] == [
        "crp://default/sources/source-text-001.json#char:0-80",
        "crp://default/sources/source-text-002.json#char:0-90",
        "crp://default/sources/source-text-003.json#char:10-70",
    ]


def test_answer_model_request_rejects_insufficient_evidence_result(
    tmp_path: Path,
) -> None:
    use_case, recalls, model_requests = _parts(tmp_path)
    request = _project_default_request(recalls)
    insufficient = recalls.create_insufficient_evidence_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T10:03:00+08:00",
    )

    with pytest.raises(AnswerModelRequestError, match="not evidence-bearing"):
        use_case.execute(str(insufficient["id"]))

    assert model_requests.list_requests("project-alpha") == ()
    assert not (tmp_path / "library").exists()


def test_answer_model_request_rejects_index_unavailable_result(
    tmp_path: Path,
) -> None:
    use_case, recalls, model_requests = _parts(tmp_path)
    request = _project_default_request(recalls)
    index_unavailable = recalls.create_index_unavailable_result(
        request_id=str(request["id"]),
        created_at="2026-06-30T11:03:00+08:00",
    )

    with pytest.raises(AnswerModelRequestError, match="not evidence-bearing"):
        use_case.execute(str(index_unavailable["id"]))

    assert model_requests.list_requests("project-alpha") == ()
    assert not (tmp_path / "library").exists()


def test_answer_model_request_composition_uses_project_memory_recall_result(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    skills = ObjectStoreProjectSkillRepository(object_store)
    skill = _project_skill_fixture()
    skills.save(
        ProjectSkillUpdate(
            project_id=str(skill["project_id"]),
            markdown="# Alpha 项目 Skill\n\n回答必须基于项目 Skill。",
            structured=skill,
            expected_revision=0,
            reason="test answer model request composition",
        )
    )
    recall = build_project_memory_recall(ROOT, runtime_root=tmp_path)
    recall_result = recall.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-30T10:10:00+08:00",
    )
    answer_request = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path)

    result = answer_request.execute(
        recall_result.result_id,
        created_at="2026-06-30T10:12:00+08:00",
    )
    model_requests = ObjectStoreModelRequestRepository(object_store)
    model_request = model_requests.get_request(result.model_request_id)

    assert model_request is not None
    assert validate_contract_instance(
        "model_request.schema.json",
        _schema("model_request.schema.json"),
        model_request,
    ) == []
    assert model_request["payload"]["recall_result_id"] == recall_result.result_id
    assert model_request["payload"]["input_refs"][0]["kind"] == "recall_result"
    assert model_request["payload"]["source_refs"]
    assert model_request["provider_preference"]["allow_remote"] is False
    assert not (tmp_path / "library").exists()
