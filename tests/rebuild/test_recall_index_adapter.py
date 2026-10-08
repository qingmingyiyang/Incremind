from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.search_and_recall import (
    InMemoryRecallIndexAdapter,
    ObjectStoreRecallRepository,
    RecallHit,
    RecallIndexEntry,
    RecallQuery,
    RecallRepositoryError,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _query(*, project_id: str = "project-alpha", limit: int = 12) -> RecallQuery:
    return RecallQuery(
        text="recall budget project skill evidence",
        project_id=project_id,
        layers=("l3_project_skill", "l3_series_memory", "l2_scenario", "l1_atom"),
        allowed_trust_statuses=("trusted", "user_confirmed", "system_generated"),
        limit=limit,
    )


def test_recall_index_adapter_filters_and_ranks_traceable_project_evidence() -> None:
    adapter = InMemoryRecallIndexAdapter(
        (
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project recall budget evidence",
                source_refs=("source-alpha#atom",),
                trust_status="system_generated",
                base_score=0.55,
            ),
            RecallIndexEntry(
                object_id="skill-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="project skill recall evidence",
                source_refs=("source-alpha#skill",),
                trust_status="user_confirmed",
                base_score=0.72,
            ),
            RecallIndexEntry(
                object_id="series-alpha",
                project_id="project-alpha",
                layer="l3_series_memory",
                content="series memory without query overlap",
                source_refs=("source-alpha#series",),
                trust_status="trusted",
                base_score=0.5,
            ),
            RecallIndexEntry(
                object_id="foreign-beta",
                project_id="project-beta",
                layer="l1_atom",
                content="project skill recall evidence",
                source_refs=("source-beta#atom",),
                trust_status="trusted",
                base_score=1.0,
            ),
            RecallIndexEntry(
                object_id="untrusted-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project skill recall evidence",
                source_refs=("source-alpha#untrusted",),
                trust_status="imported_unverified",
                base_score=1.0,
            ),
            RecallIndexEntry(
                object_id="untraceable-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project skill recall evidence",
                source_refs=(),
                trust_status="trusted",
                base_score=1.0,
            ),
        )
    )

    hits = adapter.recall(_query(limit=3))

    assert [hit.object_id for hit in hits] == ["skill-alpha", "atom-alpha", "series-alpha"]
    assert [hit.layer for hit in hits] == ["l3_project_skill", "l1_atom", "l3_series_memory"]
    assert all(hit.source_refs for hit in hits)
    assert all(hit.trust_status in {"trusted", "user_confirmed", "system_generated"} for hit in hits)
    assert all(hit.object_id not in {"foreign-beta", "untrusted-alpha", "untraceable-alpha"} for hit in hits)
    assert hits[0].score > hits[1].score > hits[2].score


def test_recall_repository_persists_adapter_ranked_hits_as_result(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    repository = ObjectStoreRecallRepository(object_store)
    request = repository.create_project_default_request(
        project_id="project-alpha",
        query="recall project evidence",
        project_skill_id="skill-project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        created_at="2026-06-30T13:00:00+08:00",
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

    result = dict(
        repository.create_result_from_hits(
            request_id=str(request["id"]),
            hits=hits,
            created_at="2026-06-30T13:01:00+08:00",
        )
    )
    reloaded = ObjectStoreRecallRepository(JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library"))

    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), result) == []
    assert result["status"] == "evidence_found"
    assert [hit["object_id"] for hit in result["hits"]] == ["skill-project-alpha", "atom-alpha"]
    assert result["hits"][0]["source_refs"] == [{"source_id": "source-alpha", "locator": "char:0-20"}]
    assert result["coverage"] == {
        "status": "sufficient",
        "requested_layers": ["l3_project_skill", "l1_atom"],
        "covered_layers": ["l3_project_skill", "l1_atom"],
        "missing_layers": [],
        "low_trust": False,
        "source_ref_count": 2,
    }
    assert result["truncation"]["applied"] is False
    assert result["truncation"]["reason"] == "none"
    assert result["truncation"]["final_hit_count"] == 2
    assert result["truncation"]["final_token_estimate"] == sum(hit["token_estimate"] for hit in result["hits"])
    assert result["explanation"]["warnings"] == ["adapter_handoff_smoke"]
    assert result["errors"] == []
    assert "answer" not in result
    assert reloaded.get_result(str(result["id"])) == result
    assert reloaded.results_for_request(str(request["id"])) == (result,)
    assert not (tmp_path / "library").exists()


def test_recall_index_adapter_applies_query_layer_order_for_tied_scores() -> None:
    adapter = InMemoryRecallIndexAdapter(
        (
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="same overlap",
                source_refs=("source-alpha#atom",),
                trust_status="trusted",
                base_score=0.7,
            ),
            RecallIndexEntry(
                object_id="scenario-alpha",
                project_id="project-alpha",
                layer="l2_scenario",
                content="same overlap",
                source_refs=("source-alpha#scenario",),
                trust_status="trusted",
                base_score=0.7,
            ),
        )
    )

    hits = adapter.recall(
        RecallQuery(
            text="same overlap",
            project_id="project-alpha",
            layers=("l2_scenario", "l1_atom"),
            allowed_trust_statuses=("trusted",),
            limit=12,
        )
    )

    assert [hit.object_id for hit in hits] == ["scenario-alpha", "atom-alpha"]


def test_recall_index_adapter_returns_empty_for_no_eligible_evidence() -> None:
    adapter = InMemoryRecallIndexAdapter(
        (
            {
                "object_id": "foreign-beta",
                "project_id": "project-beta",
                "layer": "l1_atom",
                "content": "recall budget project skill evidence",
                "source_refs": ["source-beta#atom"],
                "trust_status": "trusted",
                "base_score": 1.0,
            },
        )
    )

    assert adapter.recall(_query(project_id="project-alpha")) == ()
    assert adapter.recall(_query(project_id="project-beta", limit=0)) == ()


def test_recall_index_adapter_rejects_invalid_entries() -> None:
    with pytest.raises(RecallRepositoryError, match="layer"):
        InMemoryRecallIndexAdapter(
            (
                {
                    "object_id": "invalid-layer",
                    "project_id": "project-alpha",
                    "layer": "legacy_flat_chunk",
                    "content": "recall evidence",
                    "source_refs": ["source-alpha#chunk"],
                    "trust_status": "trusted",
                    "base_score": 0.5,
                },
            )
        )


def test_recall_repository_rejects_adapter_hits_without_parseable_source_refs(tmp_path: Path) -> None:
    repository = ObjectStoreRecallRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    request = repository.create_project_default_request(
        project_id="project-alpha",
        query="recall project evidence",
        project_skill_id="skill-project-alpha",
        layers=("l3_project_skill", "l1_atom"),
        created_at="2026-06-30T13:05:00+08:00",
    )
    adapter = InMemoryRecallIndexAdapter(
        (
            RecallIndexEntry(
                object_id="atom-alpha",
                project_id="project-alpha",
                layer="l1_atom",
                content="project recall evidence atom",
                source_refs=("source-alpha-char-20-60",),
                trust_status="trusted",
                base_score=0.8,
            ),
        )
    )

    with pytest.raises(RecallRepositoryError, match="source_id#locator"):
        repository.create_result_from_hits(
            request_id=str(request["id"]),
            hits=adapter.recall(
                RecallQuery(
                    text=str(request["query"]),
                    project_id=str(request["project_id"]),
                    layers=tuple(request["layers"]),
                    allowed_trust_statuses=tuple(request["trust_filter"]["include"]),
                    limit=12,
                )
            ),
        )


def test_recall_repository_applies_request_budgets_to_adapter_hits(tmp_path: Path) -> None:
    repository = ObjectStoreRecallRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    request = dict(
        repository.create_project_default_request(
            project_id="project-alpha",
            query="progressive memory recall",
            project_skill_id="skill-project-alpha",
            layers=("l3_project_skill", "l2_scenario", "l1_atom"),
            created_at="2026-08-13T15:00:00+08:00",
        )
    )
    request["budget"] = {
        "max_hits": 3,
        "max_tokens": 40,
        "per_layer_limits": {
            "l3_project_skill": 1,
            "l2_scenario": 1,
            "l1_atom": 2,
        },
    }
    repository.object_store.write(
        repository.request_collection,
        str(request["id"]),
        request,
        expected_revision=None,
    )
    hits = (
        RecallHit(
            object_id="skill-alpha",
            layer="l3_project_skill",
            content="skill context",
            source_refs=("source-alpha#skill",),
            trust_status="user_confirmed",
            score=1.0,
        ),
        RecallHit(
            object_id="scenario-alpha",
            layer="l2_scenario",
            content="scenario context",
            source_refs=("source-alpha#scenario",),
            trust_status="trusted",
            score=0.9,
        ),
        RecallHit(
            object_id="scenario-extra",
            layer="l2_scenario",
            content="extra scenario context",
            source_refs=("source-alpha#scenario-extra",),
            trust_status="trusted",
            score=0.8,
        ),
        RecallHit(
            object_id="atom-too-large",
            layer="l1_atom",
            content="x" * 30,
            source_refs=("source-alpha#atom-large",),
            trust_status="trusted",
            score=0.7,
        ),
        RecallHit(
            object_id="atom-fits",
            layer="l1_atom",
            content="small",
            source_refs=("source-alpha#atom-small",),
            trust_status="trusted",
            score=0.6,
        ),
    )

    result = repository.create_result_from_hits(
        request_id=str(request["id"]),
        hits=hits,
        created_at="2026-08-13T15:01:00+08:00",
    )

    assert [hit["object_id"] for hit in result["hits"]] == [
        "skill-alpha",
        "scenario-alpha",
        "atom-fits",
    ]
    assert result["truncation"]["applied"] is True
    assert result["truncation"]["reason"] == "budget"
    assert result["truncation"]["final_hit_count"] == 3
    assert result["truncation"]["final_token_estimate"] == 34
    assert len(result["truncation"]["dropped_hit_ids"]) == 2
