from __future__ import annotations

import json
from pathlib import Path

from core.composition import build_answer_model_request_from_recall, build_recall_repository
from core.model_gateway import ObjectStoreModelRequestRepository
from core.search_and_recall import InMemoryRecallIndexAdapter, RecallIndexEntry, RecallQuery
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def test_recall_adapter_result_drives_local_answer_model_request(tmp_path: Path) -> None:
    recalls = build_recall_repository(ROOT, runtime_root=tmp_path)
    request = recalls.create_project_default_request(
        project_id="project-alpha",
        query="recall project evidence",
        project_skill_id="skill-project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        created_at="2026-06-30T14:00:00+08:00",
    )
    adapter = InMemoryRecallIndexAdapter(
        (
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project recall evidence atom",
                source_refs=("source-alpha#char:20-60",),
                trust_status="system_generated",
                base_score=0.58,
            ),
            RecallIndexEntry(
                object_id="skill-project-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="project skill recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.72,
            ),
            RecallIndexEntry(
                object_id="atom-beta",
                project_id="project-beta",
                layer="l1_atom",
                content="project skill recall evidence",
                source_refs=("source-beta#char:0-20",),
                trust_status="trusted",
                base_score=1.0,
            ),
        )
    )
    hits = adapter.recall(
        RecallQuery(
            text=str(request["query"]),
            project_id=str(request["project_id"]),
            layers=tuple(request["layers"]),
            allowed_trust_statuses=tuple(request["trust_filter"]["include"]),
            limit=12,
        )
    )
    recall_result = recalls.create_result_from_hits(
        request_id=str(request["id"]),
        hits=hits,
        created_at="2026-06-30T14:01:00+08:00",
    )

    answer_request = build_answer_model_request_from_recall(ROOT, runtime_root=tmp_path)
    result = answer_request.execute(
        str(recall_result["id"]),
        created_at="2026-06-30T14:02:00+08:00",
    )
    model_requests = ObjectStoreModelRequestRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    model_request = model_requests.get_request(result.model_request_id)

    assert model_request is not None
    assert validate_contract_instance(
        "model_request.schema.json",
        _schema("model_request.schema.json"),
        model_request,
    ) == []
    assert result.project_id == "project-alpha"
    assert result.recall_result_id == recall_result["id"]
    assert result.source_ref_count == 2
    assert model_request["payload"]["kind"] == "answer"
    assert model_request["payload"]["recall_result_id"] == recall_result["id"]
    assert model_request["payload"]["source_refs"] == [
        {"source_id": "source-alpha", "locator": "char:0-20"},
        {"source_id": "source-alpha", "locator": "char:20-60"},
    ]
    assert model_request["payload"]["input_refs"] == [
        {
            "kind": "recall_result",
            "object_id": recall_result["id"],
            "uri": f"crp://default/recall-results/{recall_result['id']}.json",
        },
        {
            "kind": "project_skill",
            "object_id": "skill-project-alpha",
            "uri": "crp://default/projects/project-alpha/project-skill.json",
        },
            {
                "kind": "atom",
                "object_id": "atom-alpha",
                "uri": "crp://default/projects/project-alpha/atoms/atom-alpha.json",
            },
            {
                "kind": "application_skill_resolution",
                "object_id": result.application_skill_resolution_id,
                "uri": (
                    "crp://default/application-skill-resolutions/"
                    f"{result.application_skill_resolution_id}.json"
                ),
            },
        ]
    assert "project skill recall evidence" in model_request["payload"]["content"]
    assert "project recall evidence atom" in model_request["payload"]["content"]
    assert model_request["provider_preference"]["allow_remote"] is False
    assert model_request["privacy"]["allow_remote"] is False
    assert "output" not in model_request
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "recall_results").exists()
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "model_requests").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "model_results").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / "library").exists()
