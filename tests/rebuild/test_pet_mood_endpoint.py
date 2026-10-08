"""GET /api/rebuild/pet/mood 端点测试。

验证端点已挂载、返回正确的 mood 结构、mood 推断规则正确。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402
from core.storage_provider import JsonObjectStore  # noqa: E402


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def _seed_activity_today(tmp_path, count: int = 1) -> None:
    """写入 count 条今日 source；资料活动不等于已发布记忆。"""
    store = _store(tmp_path)
    today = _today_iso()
    for i in range(count):
        source_id = f"src-today-{i}"
        store.write("sources", source_id, {
            "schema_version": "1.0.0",
            "id": source_id,
            "type": "text",
            "title": f"今日资料 {i}",
            "storage_uri": f"crp://default/sources/{source_id}.json",
            "media_type": "text/plain",
            "capture_mode": "manual",
            "processing_state": "captured",
            "content_hash": "sha256",
            "size_bytes": 0,
            "created_at": f"{today}T10:00:{i:02d}Z",
            "metadata": {},
        }, expected_revision=None)


def _seed_pending_candidate(tmp_path, candidate_id="candidate-pending-1") -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", candidate_id, {
        "id": candidate_id,
        "status": "pending_review",
        "target_layer": "atom",
        "proposed_content": "敏感正文不能进入宠物接口",
        "source_refs": [],
        "created_at": f"{_today_iso()}T10:00:00Z",
    }, expected_revision=None)


def _seed_published_memory(tmp_path, memory_id="atom-published-1") -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", memory_id, {
        "id": memory_id,
        "layer": "atom",
        "content": "敏感正文不能进入宠物接口",
        "trust_status": "user_confirmed",
        "created_at": f"{_today_iso()}T10:00:00Z",
    }, expected_revision=None)


def test_pet_mood_returns_calm_when_empty(tmp_path) -> None:
    """无记忆、无 running job 时应返回 calm（但今日 count=0 且 7 天 count=0 → idle）。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    payload = response.json()
    # 无任何活动，应为 idle
    assert payload["mood"] == "idle"
    assert payload["today_activity_count"] == 0
    assert payload["today_memory_count"] == 0
    assert payload["pending_memory_candidate_count"] == 0
    assert payload["published_memory_count"] == 0
    assert payload["execution"] == {"effect": "unavailable", "lease": "unavailable"}


def test_pet_mood_returns_curious_when_today_count_1(tmp_path) -> None:
    """今日 1 条记忆（无 running job）→ curious。"""
    _seed_activity_today(tmp_path, count=1)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mood"] == "curious"
    assert payload["today_activity_count"] == 1
    assert payload["today_memory_count"] == 0


def test_pet_mood_returns_focused_when_today_count_3(tmp_path) -> None:
    """今日 3 条记忆（无 running job）→ focused。"""
    _seed_activity_today(tmp_path, count=3)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mood"] == "focused"
    assert payload["today_activity_count"] == 3
    assert payload["today_memory_count"] == 0


def test_pet_mood_reads_only_the_effect_execution_projection(tmp_path, monkeypatch) -> None:
    observed = {}

    def load_projection(database, *, now):
        observed["database"] = database
        observed["now"] = now
        return {"effect": "active", "lease": "valid"}

    monkeypatch.setattr("backend.api.routes.companion_memory_state.load_companion_execution_projection", load_projection)
    _seed_activity_today(tmp_path, count=5)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mood"] == "focused"
    assert payload["execution"] == {"effect": "active", "lease": "valid"}
    assert observed["database"] == tmp_path / ".rebuild-data" / "jobs.sqlite3"
    assert isinstance(observed["now"], int)


def test_pet_mood_fails_closed_when_effect_projection_is_unavailable(tmp_path, monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("sqlite unavailable")

    monkeypatch.setattr("backend.api.routes.companion_memory_state.load_companion_execution_projection", unavailable)
    payload = _client(tmp_path).get("/api/rebuild/pet/mood").json()
    assert payload["execution"] == {"effect": "unavailable", "lease": "unavailable"}


def test_pet_mood_prioritizes_pending_candidate_and_projects_only_counts(tmp_path) -> None:
    _seed_pending_candidate(tmp_path)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mood"] == "curious"
    assert payload["pending_memory_candidate_count"] == 1
    assert payload["today_memory_count"] == 0
    assert set(payload) == {
        "mood",
        "today_activity_count",
        "recent_7d_activity_count",
        "execution",
        "pending_memory_candidate_count",
        "published_memory_count",
        "today_memory_count",
        "recent_7d_memory_count",
    }
    assert "candidate-pending-1" not in response.text
    assert "敏感正文" not in response.text


def test_pet_mood_counts_published_memory_separately(tmp_path) -> None:
    _seed_activity_today(tmp_path)
    _seed_published_memory(tmp_path)
    client = _client(tmp_path)
    payload = client.get("/api/rebuild/pet/mood").json()
    assert payload["today_activity_count"] == 2
    assert payload["published_memory_count"] == 1
    assert payload["today_memory_count"] == 1
    assert payload["recent_7d_memory_count"] == 1


def test_pet_mood_no_cache(tmp_path) -> None:
    """响应应携带 no-store 防缓存头。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/pet/mood")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"


def test_pet_mood_rejects_post(tmp_path) -> None:
    """端点只支持 GET，POST 应返回 405。"""
    client = _client(tmp_path)
    response = client.post("/api/rebuild/pet/mood")
    assert response.status_code == 405
