from pathlib import Path
import pytest
import anyio
from starlette.concurrency import run_in_threadpool

from backend.memory_app.retrieval_models import configured_adapter, retrieval_options, configured_retrieve, ConfiguredTransport
from backend.recognition_retrieval import RecognitionRetrievalError


class Models:
    def public(self):
        return {purpose: {
            "purpose": purpose, "provider": "openai", "base_url": "https://example.test/v1",
            "model": purpose + "-test", "allow_remote": True, "enabled": False,
            "revision": 3, "configured": True, "has_api_key": True,
        } for purpose in ("embedding", "rerank")}
    def snapshot(self, purpose):
        return {"provider": "openai", "base_url": "https://example.test/v1", "api_key": "private-fixture-key",
                "model": purpose + "-test", "allow_remote": True, "enabled": False, "revision": 3}


def test_configured_models_stay_disabled_until_explicitly_enabled(tmp_path):
    assert retrieval_options(Models(), tmp_path) == {}


def test_embedding_identity_contains_route_and_config_revision():
    adapter = configured_adapter(Models(), "embedding")
    assert adapter.endpoint == "https://example.test/v1/embeddings"
    assert "private-fixture-key" not in adapter.cache_identity
    assert adapter.cache_identity.endswith("|3")


def test_remote_error_does_not_expose_key_or_response(monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        def __enter__(self):
            raise RuntimeError("private-fixture-key provider-secret-response")
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("backend.memory_app.retrieval_models.httpx.Client", BrokenClient)
    with pytest.raises(RecognitionRetrievalError) as caught:
        ConfiguredTransport("private-fixture-key").post_json(endpoint="https://example.test/v1/embeddings", payload={})
    assert str(caught.value) == "configured_model_request_failed"


def test_enabled_vector_query_owns_cache_in_worker_and_reuses_it(tmp_path, monkeypatch):
    class EnabledModels(Models):
        def public(self):
            return {
                "embedding": {"purpose": "embedding", "provider": "openai", "base_url": "https://example.test/v1",
                              "model": "embedding-test", "allow_remote": True, "enabled": True,
                              "revision": 3, "configured": True, "has_api_key": True},
                "rerank": {"purpose": "rerank", "provider": "openai", "base_url": "https://example.test/v1",
                           "model": "rerank-test", "allow_remote": True, "enabled": False,
                           "revision": 3, "configured": False, "has_api_key": False},
            }

        def snapshot(self, purpose):
            payload = super().snapshot(purpose)
            payload["enabled"] = purpose == "embedding"
            return payload

    calls = []
    def fake_post(self, *, endpoint, payload):
        calls.append(payload["input"])
        return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))]}
    monkeypatch.setattr(ConfiguredTransport, "post_json", fake_post)
    entry = {"id": "r1", "project_id": "p1", "revision": 1, "current_revision": 1,
             "content": "网页优先", "status": "active", "authorized": True, "source_refs": ["source://fixture"]}

    async def exercise():
        for _ in range(2):
            result = await run_in_threadpool(configured_retrieve, EnabledModels(), tmp_path, "p1", "原型", [entry])
            assert result.payload()["trace"]["vector"]["status"] == "used"
            assert [hit.id for hit in result.hits] == ["r1"]
        await run_in_threadpool(configured_retrieve, EnabledModels(), tmp_path, "p1", "原型", [])
    anyio.run(exercise)
    assert [len(batch) for batch in calls] == [2, 1]
    import sqlite3
    with sqlite3.connect(tmp_path / "recognition-vectors.sqlite3") as connection:
        # Physical cleanup belongs to explicit write/daily maintenance; an
        # empty retrieval must leave unrelated persisted vectors untouched.
        assert connection.execute("SELECT count(*) FROM recognition_embedding_cache").fetchone()[0] == 1


def test_broken_local_vector_cache_falls_back_to_visible_keyword_results(tmp_path, monkeypatch):
    import sqlite3
    def broken_options(*args, **kwargs):
        raise sqlite3.DatabaseError("fixture corrupt database")
    monkeypatch.setattr("backend.memory_app.retrieval_models.retrieval_options", broken_options)
    entries = [{"id": "r1", "project_id": "p1", "revision": 1, "current_revision": 1,
                "content": "网页原型", "status": "active", "authorized": True, "source_refs": ["source://fixture"]}]
    result = configured_retrieve(Models(), tmp_path, "p1", "网页原型", entries)
    assert [hit.id for hit in result.hits] == ["r1"]
    assert result.trace["vector"] == {"status": "degraded", "reason": "local_vector_cache_unavailable"}


@pytest.mark.parametrize("rerank_enabled", [False, True])
def test_actual_corrupt_cache_degrades_without_leaking_excluded_records(tmp_path, monkeypatch, rerank_enabled):
    class EnabledModels(Models):
        def public(self):
            value = super().public()
            value["embedding"]["enabled"] = True
            value["rerank"]["enabled"] = rerank_enabled
            return value

        def snapshot(self, purpose):
            return {**super().snapshot(purpose), "enabled": purpose == "embedding" or rerank_enabled}

    path = tmp_path / "recognition-vectors.sqlite3"
    corrupt = b"not a sqlite database" * 100
    path.write_bytes(corrupt)
    calls = []
    monkeypatch.setattr(ConfiguredTransport, "post_json", lambda *a, **k: calls.append(k))
    valid = {"id": "current", "project_id": "p1", "revision": 1, "current_revision": 1,
             "content": "网页原型", "status": "active", "authorized": True, "source_refs": []}
    entries = [valid, {**valid, "id": "other", "project_id": "p2"},
               {**valid, "id": "revoked", "status": "revoked"},
               {**valid, "id": "old", "current_revision": 2},
               {**valid, "id": "denied", "authorized": False}]
    result = configured_retrieve(EnabledModels(), tmp_path, "p1", "网页原型", entries)
    assert [hit.id for hit in result.hits] == ["current"]
    assert result.trace["vector"] == {"status": "degraded", "reason": "local_vector_cache_unavailable"}
    assert result.trace["rerank"] == ({"status": "degraded", "reason": "local_vector_cache_unavailable"}
                                      if rerank_enabled else {"status": "not_configured"})
    assert calls == []
    assert path.read_bytes() == corrupt


def test_corrupt_cache_constructor_closes_its_connection(tmp_path, monkeypatch):
    import sqlite3
    from backend.recognition_retrieval import SQLiteEmbeddingCache
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"invalid sqlite content" * 100)
    connect = sqlite3.connect
    opened = []

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteEmbeddingCache(str(path))
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
