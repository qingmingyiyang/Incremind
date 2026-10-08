from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product import memory_hierarchy as product_memory_hierarchy
from core.aggregate_repository_factory import (
    JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY,
    AggregateRepositoryFactory,
    MemoryPublicationAuthorityResolution,
)
from core.memory_core import (
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    ObjectStoreMemoryCandidateRepository,
)
from tests.rebuild.test_memory_candidate_review import _layer_candidate


def _force_json_memory_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        AggregateRepositoryFactory,
        "memory_publication_authority_resolution",
        lambda _self: MemoryPublicationAuthorityResolution(
            records=None,
            authority_identity=JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY,
        ),
    )


def _assert_no_json_staging_side_effects(store, candidate_id: str) -> None:
    candidate = store.read("memory_candidates", candidate_id)
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert store.list("staging_atoms") == ()
    assert store.list(STAGING_PUBLICATION_CONTEXT_COLLECTION) == ()


def test_generic_memory_review_fails_closed_without_durable_sqlite_staging_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_id = _layer_candidate(tmp_path, target_layer="atom")
    app = create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(app) as client:
        _force_json_memory_authority(monkeypatch)
        response = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认进入 staging。"},
        )

    assert response.status_code == 400
    assert response.json()["reason"] == (
        "durable SQLite Memory publication authority is unavailable"
    )
    _assert_no_json_staging_side_effects(store, candidate_id)


def test_imported_memory_review_fails_closed_without_durable_sqlite_staging_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_id = _layer_candidate(tmp_path, target_layer="atom")
    app = create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(app) as client:
        _force_json_memory_authority(monkeypatch)
        response = client.post(
            "/api/rebuild/memory/candidates/review",
            json={
                "candidate_id": candidate_id,
                "action": "confirm",
                "comment": "用户确认导入候选进入 staging。",
            },
        )

    assert response.status_code == 409
    assert response.json()["reason"] == (
        "durable SQLite Memory publication authority is unavailable"
    )
    _assert_no_json_staging_side_effects(store, candidate_id)


def test_import_batch_bindings_are_limited_to_same_project_batch_and_layer(
    tmp_path: Path,
) -> None:
    store, scenario_id = _layer_candidate(
        tmp_path,
        target_layer="scenario",
        candidate_id="candidate-scenario",
    )
    _same_store, sibling_id = _layer_candidate(
        tmp_path,
        target_layer="series_memory",
        candidate_id="candidate-series",
    )
    scenario = dict(store.read("memory_candidates", scenario_id))
    scenario.update({"import_batch_id": "batch-alpha", "portable_object_id": "scenario-alpha"})
    store.write(
        "memory_candidates",
        scenario_id,
        scenario,
        expected_revision=store.revision("memory_candidates", scenario_id),
    )
    sibling = dict(store.read("memory_candidates", sibling_id))
    sibling.update({"import_batch_id": "batch-other", "portable_object_id": "series-alpha"})
    store.write(
        "memory_candidates",
        sibling_id,
        sibling,
        expected_revision=store.revision("memory_candidates", sibling_id),
    )
    candidates = ObjectStoreMemoryCandidateRepository(store)

    assert product_memory_hierarchy._same_import_batch_binding_ids(
        candidates=candidates,
        candidate=scenario,
        layer="series_memory",
    ) == set()

    sibling["import_batch_id"] = "batch-alpha"
    store.write(
        "memory_candidates",
        sibling_id,
        sibling,
        expected_revision=store.revision("memory_candidates", sibling_id),
    )
    assert product_memory_hierarchy._same_import_batch_binding_ids(
        candidates=candidates,
        candidate=scenario,
        layer="series_memory",
    ) == {"series-alpha"}

    sibling["status"] = "promoted"
    sibling["review"] = {
        **sibling["review"],
        "reason": "用户确认。",
        "reviewed_by": "user",
        "reviewed_at": "2026-08-15T10:00:00+08:00",
    }
    store.write(
        "memory_candidates",
        sibling_id,
        sibling,
        expected_revision=store.revision("memory_candidates", sibling_id),
    )
    assert product_memory_hierarchy._same_import_batch_binding_ids(
        candidates=candidates,
        candidate=scenario,
        layer="series_memory",
    ) == {"series-alpha"}
    assert product_memory_hierarchy._same_import_batch_binding_ids(
        candidates=candidates,
        candidate=scenario,
        layer="atom",
    ) == set()


def test_memory_publication_fails_closed_without_sqlite_authority_and_keeps_json_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _candidate_id = _layer_candidate(tmp_path, target_layer="atom")
    app = create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(app) as client:
        store.write(
            "staging_atoms",
            "atom-json-only",
            {"id": "atom-json-only", "content": "legacy staging evidence"},
            expected_revision=0,
        )
        _force_json_memory_authority(monkeypatch)
        response = client.post(
            "/api/rebuild/staging-atoms/atom-json-only/publication",
            json={"confirm": True, "reason": "用户确认发布。"},
        )

    assert response.status_code == 409
    assert response.json()["reason"] == (
        "durable SQLite Memory publication authority is unavailable"
    )
    assert store.read("staging_atoms", "atom-json-only") is not None
    assert store.list("memory_atoms") == ()
    assert store.list("memory_publications") == ()
    assert store.list("memory_transitions") == ()


def test_memory_rollback_fails_closed_without_sqlite_authority_and_keeps_json_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _candidate_id = _layer_candidate(tmp_path, target_layer="atom")
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    publication_id = "memory-publication-json-only"

    with TestClient(app) as client:
        store.write(
            "memory_atoms",
            "atom-json-only",
            {"id": "atom-json-only", "content": "legacy published evidence"},
            expected_revision=0,
        )
        store.write(
            "memory_publications",
            publication_id,
            {
                "id": publication_id,
                "publication_id": publication_id,
                "layer": "atom",
                "published_object_id": "atom-json-only",
                "status": "published",
            },
            expected_revision=0,
        )
        _force_json_memory_authority(monkeypatch)
        response = client.post(
            f"/api/rebuild/memory-publications/{publication_id}/rollback",
            json={"confirm": True, "reason": "用户确认撤回。"},
        )

    assert response.status_code == 409
    assert response.json()["reason"] == (
        "durable SQLite Memory publication authority is unavailable"
    )
    assert store.read("memory_atoms", "atom-json-only") is not None
    assert store.read("memory_publications", publication_id)["status"] == "published"
    assert store.list("memory_transitions") == ()
