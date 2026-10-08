from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.storage_provider import JsonObjectStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_atom(
    store: JsonObjectStore,
    atom_id: str,
    *,
    source_id: str = "source-shared",
    series_id: str = "",
    title: str = "",
) -> None:
    store.write(
        "memory_atoms",
        atom_id,
        {
            "schema_version": "1.0.0",
            "id": atom_id,
            "layer": "atom",
            "source_id": source_id,
            "series_id": series_id,
            "title": title,
            "summary": "",
            "source_refs": [{"source_id": source_id, "locator": "char:0-80"}],
            "trust_status": "user_confirmed",
        },
        expected_revision=None,
    )


# ── 参数校验 ──

def test_related_returns_400_when_object_id_missing(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/related?layer=atom")
    assert response.status_code == 400
    assert "object_id" in response.json()["detail"]


def test_related_returns_400_when_layer_missing(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/library/related?object_id=atom-001")
    assert response.status_code == 400
    assert "layer" in response.json()["detail"]


def test_related_returns_400_when_limit_invalid(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-001&layer=atom&limit=abc"
        )
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_related_returns_400_when_layer_unsupported(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-001&layer=unknown"
        )
    assert response.status_code == 400


# ── 正常返回 ──

def test_related_returns_empty_when_seed_missing(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=missing-001&layer=atom"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["object_id"] == "missing-001"
    assert body["layer"] == "atom"
    assert body["related"] == []


def test_related_returns_hits_when_same_source(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_atom(store, "atom-a", source_id="source-shared", title="Atom A")
    _seed_atom(store, "atom-b", source_id="source-shared", title="Atom B")
    _seed_atom(store, "atom-c", source_id="source-other", title="Atom C")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-a&layer=atom"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["object_id"] == "atom-a"
    related_ids = [hit["object_id"] for hit in body["related"]]
    assert "atom-b" in related_ids
    assert "atom-c" not in related_ids
    # 验证 hit 结构
    atom_b = next(hit for hit in body["related"] if hit["object_id"] == "atom-b")
    assert atom_b["layer"] == "atom"
    assert atom_b["relation"] == "same_source"
    assert atom_b["title"] == "Atom B"
    assert "source_refs" in atom_b


def test_related_excludes_self_from_results(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_atom(store, "atom-a", source_id="source-shared")

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-a&layer=atom"
        )
    assert response.status_code == 200
    body = response.json()
    assert all(hit["object_id"] != "atom-a" for hit in body["related"])


def test_related_respects_limit_param(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        _seed_atom(store, f"atom-{i}", source_id="source-shared")
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-0&layer=atom&limit=3"
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["related"]) <= 3


def test_related_response_has_no_store_cache_header(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=x&layer=atom"
        )
    assert response.headers.get("Cache-Control") == "no-store"


# ── 跨层关联 ──

def test_related_atom_finds_containing_scenario(tmp_path) -> None:
    store = _store(tmp_path)
    _seed_atom(store, "atom-a", source_id="source-001")
    store.write(
        "memory_scenarios",
        "scen-1",
        {
            "schema_version": "1.0.0",
            "id": "scen-1",
            "layer": "scenario",
            "atom_ids": ["atom-a"],
            "series_id": "",
            "title": "包含 atom-a 的场景",
            "source_refs": [],
        },
        expected_revision=None,
    )

    with _client(tmp_path) as client:
        response = client.get(
            "/api/rebuild/library/related?object_id=atom-a&layer=atom"
        )
    assert response.status_code == 200
    body = response.json()
    scen_hit = next(
        (hit for hit in body["related"] if hit["object_id"] == "scen-1"),
        None,
    )
    assert scen_hit is not None
    assert scen_hit["layer"] == "scenario"
    assert scen_hit["relation"] == "contains"
