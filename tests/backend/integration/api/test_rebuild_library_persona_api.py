from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.bootstrap import build_api_container
from core.product_core import ObjectStorePersonaRepository, PersonaExtractor
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[4]


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _confirmed_atom() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "atom-persona-api-001",
        "layer": "atom",
        "project_id": "project-alpha",
        "content": "已确认的 Atom 用于 Persona API 测试。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
        "language_style": "克制、温柔、避免命令式",
        "format_preferences": ["Markdown", "短段落"],
        "avoidances": ["不要使用 emoji"],
    }


def _publish_persona(store: JsonObjectStore) -> None:
    record = PersonaExtractor().extract(
        scope="global",
        confirmed_entries=[_confirmed_atom()],
    )
    repository = ObjectStorePersonaRepository(store)
    repository.save(record)
    repository.update_confirmation(
        "global",
        status="confirmed",
        reason="test publication",
    )


def test_library_persona_endpoint_returns_empty_when_no_persona(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/persona")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["scope"] == "global"
    assert body["revision"] == 0
    assert body["layer"] == "l4_persona"
    assert body["draft_available"] is False
    assert body["current_digest"]["ready"] is False
    assert body["language_style"] == []
    assert body["evidence_refs"] == []


def test_library_persona_endpoint_returns_digest_when_published(tmp_path) -> None:
    _publish_persona(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/persona")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["scope"] == "global"
    assert body["revision"] == 1
    assert "克制、温柔、避免命令式" in body["language_style"]
    assert "Markdown" in body["format_preferences"]
    assert "project-alpha" in body["common_projects"]
    assert "不要使用 emoji" in body["avoidances"]
    assert len(body["evidence_refs"]) == 1
    assert body["evidence_refs"][0]["object_id"] == "atom-persona-api-001"


def test_confirmed_persona_is_exposed_by_project_brain_only_as_l4(tmp_path) -> None:
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())
    _publish_persona(store)

    with _client(tmp_path) as client:
        overview = client.get("/api/rebuild/project-brain?project_id=project-alpha")
    # Rebuild the API container to prove the five-layer view reads persisted authority.
    with _client(tmp_path) as client:
        drill = client.get(
            "/api/rebuild/project-brain/layer/L4/"
            "persona-global~persona-statement-style-001"
        )
        legacy_l3_drill = client.get(
            "/api/rebuild/project-brain/layer/L3/persona-global"
        )

    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert [item["layer"] for item in body["layer_summaries"]] == [
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
    ]
    persona_items = [
        item for item in body["memories"] if item["memory_id"].startswith("persona-")
    ]
    assert persona_items
    assert {item["layer"] for item in persona_items} == {"L4"}
    assert body["persona_ready"] is True
    counts = {item["layer"]: item["count"] for item in body["layer_summaries"]}
    assert counts["L4"] == len(persona_items)
    assert counts["L3"] == 0

    assert drill.status_code == 200, drill.text
    drill_body = drill.json()
    assert drill_body["current"]["layer"] == "L4"
    assert legacy_l3_drill.status_code == 404
    assert drill_body["current"]["trust_status"] == "user_confirmed"
    assert any(
        node["layer"] == "L1" and node["memory_id"] == "atom-persona-api-001"
        for node in drill_body["evidence_path"]
    )


def test_library_persona_endpoint_supports_scope_query(tmp_path) -> None:
    _publish_persona(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/persona?scope=series")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["scope"] == "series"


def test_library_persona_endpoint_rejects_invalid_scope(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/persona?scope=invalid")
    assert response.status_code == 400
    body = response.json()
    assert "scope" in body["detail"]


# ---------------------------------------------------------------------------
# Phase 5 子项2：POST /api/rebuild/library/persona/distill
# ---------------------------------------------------------------------------


def _publish_confirmed_atom_to_store(store: JsonObjectStore, atom: dict[str, object]) -> None:
    """写入一条已确认 atom 到 memory_atoms collection（模拟 memory publication）。"""
    store.write("memory_atoms", atom["id"], atom, expected_revision=None)


def test_persona_distill_endpoint_distills_from_confirmed_entries(tmp_path) -> None:
    """有已确认 memory entries → 只生成待确认 L4 草稿，不进入 current。"""
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/library/persona/distill")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "distilled"
    assert body["entry_count"] == 1
    digest = body["digest"]
    assert digest["ready"] is True
    assert digest["draft_available"] is True
    assert digest["current_digest"]["ready"] is False
    assert digest["scope"] == "global"
    assert digest["revision"] == 1
    assert "克制、温柔、避免命令式" in digest["language_style"]
    assert "Markdown" in digest["format_preferences"]
    assert "不要使用 emoji" in digest["avoidances"]
    assert len(digest["evidence_refs"]) == 1
    assert digest["evidence_refs"][0]["object_id"] == "atom-persona-api-001"


def test_persona_distill_endpoint_skips_when_no_confirmed_entries(tmp_path) -> None:
    """无已确认 memory entries → skipped + reason=no_confirmed_entries + digest ready=false。"""
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/library/persona/distill")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "no_confirmed_entries"
    assert body["digest"]["ready"] is False


def test_persona_distill_endpoint_persists_review_draft_across_restart(tmp_path) -> None:
    """蒸馏后重建客户端仍能读取草稿，但 current 继续为空。"""
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())

    with _client(tmp_path) as client:
        distill_response = client.post("/api/rebuild/library/persona/distill")
    with _client(tmp_path) as client:
        get_response = client.get("/api/rebuild/library/persona")

    assert distill_response.status_code == 200
    assert distill_response.json()["status"] == "distilled"

    assert get_response.status_code == 200
    digest = get_response.json()
    assert digest["ready"] is True
    assert digest["revision"] == 1
    assert digest["draft_available"] is True
    assert digest["current_digest"]["ready"] is False
    assert "克制、温柔、避免命令式" in digest["language_style"]


def test_persona_confirm_promotes_draft_to_current_with_dual_cas(tmp_path) -> None:
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())
    with _client(tmp_path) as client:
        distilled = client.post("/api/rebuild/library/persona/distill").json()["digest"]
        response = client.post(
            "/api/rebuild/library/persona/confirm",
            json={
                "scope": "global",
                "status": "confirmed",
                "reason": "用户确认稳定画像",
                "expected_draft_revision": distilled["draft_cas_revision"],
                "expected_current_revision": distilled["current_cas_revision"],
            },
        )

    assert response.status_code == 200
    digest = response.json()["digest"]
    assert digest["draft_available"] is False
    assert digest["current_digest"]["ready"] is True
    assert digest["current_digest"]["confirmation"]["status"] == "confirmed"
    assert ObjectStorePersonaRepository(store).digest("global").ready is True


def test_persona_confirm_rejects_stale_draft_cas(tmp_path) -> None:
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())
    with _client(tmp_path) as client:
        distilled = client.post("/api/rebuild/library/persona/distill").json()["digest"]
        response = client.post(
            "/api/rebuild/library/persona/confirm",
            json={
                "scope": "global",
                "status": "confirmed",
                "reason": "过期页面提交",
                "expected_draft_revision": distilled["draft_cas_revision"] + 1,
                "expected_current_revision": distilled["current_cas_revision"],
            },
        )

    assert response.status_code == 409
    assert ObjectStorePersonaRepository(store).digest("global").ready is False


def test_persona_distill_reads_fresh_formal_sqlite_authority(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    copyfile(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    app = create_app(build_api_container(tmp_path))
    with TestClient(app) as client:
        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
        )
        with records.begin() as transaction:
            transaction.put(
                "memory_atoms",
                "atom-formal-persona",
                {
                    **_confirmed_atom(),
                    "id": "atom-formal-persona",
                    "project_id": "project-formal",
                },
                expected_revision=0,
            )
            transaction.commit()
        response = client.post("/api/rebuild/library/persona/distill")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "distilled"
    assert body["authority"] == "sqlite:structured-records-v1"
    assert body["entry_count"] == 1
    assert "project-formal" in body["digest"]["common_projects"]
    assert body["digest"]["current_digest"]["ready"] is False


def test_persona_distill_endpoint_ignores_non_confirmed_entries(tmp_path) -> None:
    """trust_status != user_confirmed 的条目应被忽略。"""
    store = _store(tmp_path)
    unconfirmed = {
        **_confirmed_atom(),
        "id": "atom-unconfirmed-001",
        "trust_status": "system_generated",
    }
    _publish_confirmed_atom_to_store(store, unconfirmed)

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/library/persona/distill")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "no_confirmed_entries"


def test_persona_distill_endpoint_rejects_invalid_scope(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/library/persona/distill?scope=invalid")
    assert response.status_code == 400
    body = response.json()
    assert "scope" in body["detail"]


def test_persona_distill_endpoint_does_not_leak_sensitive_fields(tmp_path) -> None:
    """distill 响应不包含敏感字段。"""
    store = _store(tmp_path)
    _publish_confirmed_atom_to_store(store, _confirmed_atom())

    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/library/persona/distill")

    body_text = response.text.lower()
    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in body_text, f"distill response leaked: {forbidden}"
