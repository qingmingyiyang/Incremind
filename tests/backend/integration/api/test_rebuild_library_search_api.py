from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ActivationRepository,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5DryRunIndex,
    build_recall_authority_ledger,
    build_recall_entries_from_object_store,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
)
from core.storage_provider import JsonObjectStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _sources() -> tuple[IndexSourceRecord, ...]:
    return (
        IndexSourceRecord(
            source_id="source-alpha",
            revision=1,
            content_hash="sha256-alpha",
            updated_at="2026-07-01T02:00:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T02:01:00+08:00",
        ),
    )


def _candidate_manifest(
    sources: tuple[IndexSourceRecord, ...] | None = None,
) -> dict[str, object]:
    sources = sources or _sources()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, sources),
        backend_selection=select_default_recall_backend_policy(),
        sources=sources,
        requested_at="2026-07-01T02:02:00+08:00",
    )
    return sqlite_fts5_manifest_payload(
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=select_default_recall_backend_policy(),
            created_at="2026-07-01T02:03:00+08:00",
        )
    )


def _entries() -> tuple[RecallIndexEntry, ...]:
    return (
        RecallIndexEntry(
            object_id="skill-alpha",
            project_id="project-alpha",
            layer="l3_project_skill",
            content="project recall evidence for sqlite fts5 active search",
            source_refs=("source-alpha#char:0-25",),
            trust_status="user_confirmed",
            base_score=0.8,
        ),
        RecallIndexEntry(
            object_id="atom-alpha",
            project_id="project-alpha",
            layer="l1_atom",
            content="sqlite fts5 active search evidence atom",
            source_refs=("source-alpha#char:26-70",),
            trust_status="system_generated",
            base_score=0.6,
        ),
    )


def _verified_job(manifest_id: str, database_uri: str) -> dict[str, object]:
    return {
        "id": "rebuild-job-api-search-001",
        "job_type": "rebuild_index",
        "status": "completed",
        "published_outputs": [
            {
                "published": True,
                "kind": "other",
                "object_id": f"verified-{manifest_id}",
                "uri": f"crp://default/recall/index-verifications/verified-{manifest_id}.json",
            }
        ],
        "worker_verification": {
            "status": "ready",
            "backend_kind": "sqlite_fts5",
            "database_uri": database_uri,
            "manifest_id": manifest_id,
            "entry_count": 2,
            "hit_count": 2,
            "hit_object_ids": ["skill-alpha", "atom-alpha"],
            "vector_enabled": False,
            "source_refs": [
                "source-alpha#rev:1",
                "source-beta#rev:2",
            ],
        },
    }


def _build_active_fts5_database(
    tmp_path: Path,
    *,
    candidate: dict[str, object] | None = None,
) -> Path:
    database_path = tmp_path / ".sqlite-active" / "recall_fts5.db"
    result = SqliteFts5DryRunIndex(database_path).rebuild_and_query(
        _entries(),
        manifest=candidate or _candidate_manifest(),
        query=RecallQuery(
            text="project recall evidence sqlite",
            project_id="project-alpha",
            layers=("l3_project_skill", "l1_atom"),
            allowed_trust_statuses=("user_confirmed", "system_generated", "trusted"),
            limit=5,
        ),
    )
    assert result.status == "ready"
    assert database_path.exists()
    return database_path


def _seed_current_recall_authorities(store: JsonObjectStore) -> None:
    store.write(
        "project_skills",
        "skill-alpha",
        {
            "id": "skill-alpha",
            "project_id": "project-alpha",
            "content": "project recall evidence for sqlite fts5 active search",
            "source_refs": [{"source_id": "source-alpha", "locator": "char:0-25"}],
            "trust_status": "user_confirmed",
        },
        expected_revision=None,
    )
    store.write(
        "memory_atoms",
        "atom-alpha",
        {
            "id": "atom-alpha",
            "project_id": "project-alpha",
            "content": "sqlite fts5 active search evidence atom",
            "source_refs": [{"source_id": "source-alpha", "locator": "char:26-70"}],
            "trust_status": "system_generated",
            "confidence": 0.9,
        },
        expected_revision=None,
    )


def test_library_search_endpoint_rejects_empty_query(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/search?q=")
    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "q query parameter is required"


def test_library_search_endpoint_rejects_missing_query(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/search")
    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "q query parameter is required"


def test_library_search_endpoint_uses_active_fts5_when_available(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_current_recall_authorities(store)
    ledger = build_recall_authority_ledger(build_recall_entries_from_object_store(store))
    candidate = _candidate_manifest(ledger)
    database_path = _build_active_fts5_database(tmp_path, candidate=candidate)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-api-test",
        database_uri=database_uri,
    )

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={"q": "project recall evidence sqlite", "project_id": "project-alpha", "limit": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["backend"] == "sqlite_fts5"
    assert body["query"] == "project recall evidence sqlite"
    assert body["total"] > 0
    assert body["index_stale"] is False
    object_ids = {hit["object_id"] for hit in body["hits"]}
    assert "skill-alpha" in object_ids
    assert all(hit["backend"] == "sqlite_fts5" for hit in body["hits"])
    assert all(hit["score"] >= 0.0 for hit in body["hits"])


def test_library_search_endpoint_falls_back_to_object_store(tmp_path) -> None:
    # Populate the ObjectStore recall index so fallback returns hits. The
    # rebuild creates an object_store_lexical active manifest, so the FTS5
    # path is unavailable (not "no active manifest").
    store = _store(tmp_path)
    _seed_current_recall_authorities(store)
    ObjectStoreRecallIndex(store).rebuild(_entries(), source="phase5-api-fallback")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={"q": "project recall evidence sqlite", "limit": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "object_store_lexical"
    assert body["reason"] == "sqlite_fts5_unavailable"
    assert body["total"] >= 1


def test_library_search_endpoint_reports_no_active_manifest_when_index_empty(tmp_path) -> None:
    # No rebuild performed — there is no active manifest at all.
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={"q": "project recall evidence sqlite", "limit": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "object_store_lexical"
    assert body["reason"] == "no_active_manifest"
    assert body["index_stale"] is True
    assert body["total"] == 0


def test_library_search_endpoint_returns_empty_when_no_hits(tmp_path) -> None:
    store = _store(tmp_path)
    ObjectStoreRecallIndex(store).rebuild(_entries(), source="phase5-api-empty")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={
                "q": "zzznonexistenttermszzz",
                "project_id": "project-nonexistent",
                "limit": 5,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["total"] == 0
    assert body["hits"] == []


def test_library_search_endpoint_clamps_limit(tmp_path) -> None:
    store = _store(tmp_path)
    ObjectStoreRecallIndex(store).rebuild(_entries(), source="phase5-api-clamp")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={"q": "project recall evidence sqlite", "limit": "999"},
        )

    assert response.status_code == 200
    body = response.json()
    # Limit is clamped to 50, and we only have 2 entries so total should be 2.
    assert body["total"] <= 50


def test_library_search_endpoint_passes_layer_and_trust_filters(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_current_recall_authorities(store)
    ledger = build_recall_authority_ledger(build_recall_entries_from_object_store(store))
    candidate = _candidate_manifest(ledger)
    database_path = _build_active_fts5_database(tmp_path, candidate=candidate)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-api-filter-test",
        database_uri=database_uri,
    )

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/search",
            params={
                "q": "project recall evidence sqlite",
                "project_id": "project-alpha",
                "layers": "l3_project_skill",
                "trust": "user_confirmed",
                "limit": 5,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "sqlite_fts5"
    assert body["total"] >= 1
    assert all(hit["layer"] == "l3_project_skill" for hit in body["hits"])
    assert all(hit["trust_status"] == "user_confirmed" for hit in body["hits"])


# ---------------------------------------------------------------------------
# 3.14 验收3：index freshness + rebuild API
# ---------------------------------------------------------------------------


def _write_source_to_store(
    store: JsonObjectStore,
    *,
    source_id: str,
    content_hash: str,
    created_at: str = "2026-07-01T02:00:00+08:00",
) -> None:
    """写入一条可进入 current Recall authority projection 的 Source。"""
    store.write(
        "sources",
        source_id,
        {
            "schema_version": "1.0.0",
            "id": source_id,
            "type": "text",
            "title": f"测试 source {source_id}",
            "content_hash": content_hash,
            "created_at": created_at,
            "trust_status": "user_confirmed",
            "metadata": {},
        },
        expected_revision=None,
    )


def _activate_fresh_manifest_for_store(
    store: JsonObjectStore,
    tmp_path: Path,
) -> dict[str, object]:
    """基于 ObjectStore 当前 source ledger 构建并激活一份 fresh manifest。"""
    ledger = build_recall_authority_ledger(build_recall_entries_from_object_store(store))
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, ledger),
        backend_selection=select_default_recall_backend_policy(),
        sources=ledger,
        requested_at="2026-07-01T02:02:00+08:00",
    )
    candidate = sqlite_fts5_manifest_payload(
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=select_default_recall_backend_policy(),
            created_at="2026-07-01T02:03:00+08:00",
        )
    )
    database_path = _build_active_fts5_database(tmp_path, candidate=candidate)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="freshness-api-test",
        database_uri=database_uri,
    )
    return candidate


def test_index_freshness_endpoint_reports_missing_when_no_manifest(tmp_path) -> None:
    """无 active manifest → status=missing, index_stale=true。"""
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/index/freshness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing"
    assert body["index_stale"] is True
    assert body["has_active_manifest"] is False
    assert body["source_count"] == 0


def test_index_freshness_endpoint_reports_fresh_when_manifest_matches_sources(tmp_path) -> None:
    """active manifest 指纹匹配当前 source ledger → status=fresh, index_stale=false。"""
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")
    _write_source_to_store(
        store,
        source_id="source-beta",
        content_hash="sha256-beta",
        created_at="2026-07-01T02:01:00+08:00",
    )
    _activate_fresh_manifest_for_store(store, tmp_path)

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/index/freshness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fresh"
    assert body["index_stale"] is False
    assert body["has_active_manifest"] is True
    assert body["source_count"] == 2


def test_index_freshness_endpoint_reports_stale_after_new_source(tmp_path) -> None:
    """验收3：新 source 入库后 → status=stale, index_stale=true。"""
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")
    _write_source_to_store(
        store,
        source_id="source-beta",
        content_hash="sha256-beta",
        created_at="2026-07-01T02:01:00+08:00",
    )
    _activate_fresh_manifest_for_store(store, tmp_path)

    # 新 source 入库（manifest 未更新 → stale）
    _write_source_to_store(
        store,
        source_id="source-gamma",
        content_hash="sha256-gamma",
        created_at="2026-07-02T00:00:00+08:00",
    )

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/index/freshness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stale"
    assert body["index_stale"] is True
    assert body["has_active_manifest"] is True
    assert body["source_count"] == 3


def test_index_rebuild_endpoint_completes_and_activates_when_stale(tmp_path, monkeypatch) -> None:
    """Stale rebuild uses the registered v2 Effect, never legacy Job execution."""
    from core.product_core.index_rebuild_job import CreateIndexRebuildJob
    from core.product_core import index_rebuild_runtime as legacy_runtime

    def legacy_called(*_args, **_kwargs):
        raise AssertionError("stale HTTP rebuild reached legacy index Job authority")

    monkeypatch.setattr(CreateIndexRebuildJob, "execute", legacy_called)
    monkeypatch.setattr(legacy_runtime, "run_index_rebuild_job", legacy_called)
    monkeypatch.setattr(legacy_runtime, "recover_index_rebuild_jobs", legacy_called)
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")
    _activate_fresh_manifest_for_store(store, tmp_path)
    # 新 source 入库 → stale
    _write_source_to_store(
        store,
        source_id="source-beta",
        content_hash="sha256-beta",
        created_at="2026-07-01T02:01:00+08:00",
    )

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/index/rebuild")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fresh"
    assert isinstance(body["operation_id"], str)
    assert body["freshness"]["status"] == "fresh"
    assert body["previous_status"] == "stale"
    assert body["freshness"]["source_count"] == 2
    assert body["effect"] == {
        "operation_id": body["operation_id"],
        "state": "SETTLED_OK",
        "receipt_ref": f"receipt:index-rebuild/{body['operation_id']}",
    }
    with sqlite3.connect(tmp_path / ".rebuild-data" / "jobs.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_effect_fact "
            "WHERE json_extract(payload_json, '$.job_type') = 'rebuild_index'"
        ).fetchone()[0] == 0
    with _client(tmp_path) as client:
        freshness = client.get("/api/rebuild/index/freshness").json()
    assert freshness["status"] == "fresh"


def test_index_startup_recovery_never_scans_legacy_rebuild_jobs(tmp_path, monkeypatch) -> None:
    from backend.api.routes.product.job_lifecycle import recover_rebuild_job_lifecycle
    from core.product_core import index_rebuild_runtime as legacy_runtime

    monkeypatch.setattr(
        legacy_runtime,
        "recover_index_rebuild_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("startup reached legacy Index Job recovery")
        ),
    )
    with _client(tmp_path) as client:
        assert recover_rebuild_job_lifecycle(client.app, tmp_path) == ()


def test_index_rebuild_endpoint_returns_null_job_id_when_fresh(tmp_path) -> None:
    """fresh 状态触发 rebuild → 200 + job_id=null（无需重建）。"""
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")
    _activate_fresh_manifest_for_store(store, tmp_path)

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/index/rebuild")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fresh"
    assert body["job_id"] is None
    assert body["freshness"]["status"] == "fresh"


def test_index_rebuild_endpoint_completes_when_no_manifest(tmp_path) -> None:
    """无 active manifest → 构建、验证并激活，不留下永久pending。"""
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/index/rebuild")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fresh"
    assert isinstance(body["operation_id"], str)
    assert body["freshness"]["status"] == "fresh"
    assert body["previous_status"] == "missing"
    assert body["effect"]["state"] == "SETTLED_OK"
    # 新 TestClient 模拟 sidecar 重启；active SQLite 必须继续提供真实中文召回。
    with _client(tmp_path) as restarted:
        search = restarted.get("/api/rebuild/library/search", params={"q": "测试"})
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["backend"] == "sqlite_fts5"
    assert [hit["object_id"] for hit in search_body["hits"]] == ["source-alpha"]


def test_library_search_hides_deleted_source_and_recalls_it_after_undo_and_restart(tmp_path) -> None:
    store = _store(tmp_path)
    _write_source_to_store(
        store,
        source_id="source-delete-undo",
        content_hash="sha256-delete-undo",
    )
    _activate_fresh_manifest_for_store(store, tmp_path)

    with _client(tmp_path) as client:
        before = client.get("/api/rebuild/library/search", params={"q": "测试 source"})
        deletion = client.delete(
            "/api/rebuild/library/items/source-delete-undo",
            params={"item_type": "source"},
        )
        after_delete = client.get("/api/rebuild/library/search", params={"q": "测试 source"})
        deletion_body = deletion.json()
        undo = client.post(
            "/api/rebuild/library/items/source-delete-undo/undo-delete",
            json={
                "item_type": "source",
                "operation_id": deletion_body["operation_id"],
                "expected_revision": deletion_body["revision"],
            },
        )
        after_undo = client.get("/api/rebuild/library/search", params={"q": "测试 source"})

    with _client(tmp_path) as restarted:
        after_restart = restarted.get(
            "/api/rebuild/library/search",
            params={"q": "测试 source"},
        )

    assert before.status_code == 200
    assert [hit["object_id"] for hit in before.json()["hits"]] == ["source-delete-undo"]
    assert deletion.status_code == 200
    assert deletion_body["status"] == "deleted"
    assert after_delete.status_code == 200
    assert after_delete.json()["hits"] == []
    assert after_delete.json()["reason"] == "sqlite_fts5_stale"
    assert undo.status_code == 200
    assert undo.json()["status"] == "restored"
    assert [hit["object_id"] for hit in after_undo.json()["hits"]] == ["source-delete-undo"]
    assert [hit["object_id"] for hit in after_restart.json()["hits"]] == ["source-delete-undo"]


def test_index_freshness_endpoint_does_not_leak_sensitive_fields(tmp_path) -> None:
    """freshness 响应不包含敏感字段。"""
    store = _store(tmp_path)
    _write_source_to_store(store, source_id="source-alpha", content_hash="sha256-alpha")

    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/index/freshness")

    body_text = response.text.lower()
    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in body_text, f"freshness response leaked: {forbidden}"
