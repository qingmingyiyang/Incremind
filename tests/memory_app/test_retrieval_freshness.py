import json
import sqlite3

import pytest

from backend.memory_app.retrieval_models import configured_retrieve
from backend.recognition import RecognitionConflict
from backend.recognition_retrieval import RecognitionRetrievalError


class MutableModels:
    def __init__(self, *, embedding=True, rerank=True):
        self.revision = 1
        self.embedding = embedding
        self.rerank = rerank

    def public(self):
        return {
            purpose: {
                "purpose": purpose, "provider": "openai", "base_url": "https://provider.test/v1",
                "model": purpose + "-model", "allow_remote": True, "enabled": enabled,
                "revision": self.revision, "configured": enabled, "has_api_key": enabled,
            }
            for purpose, enabled in (("embedding", self.embedding), ("rerank", self.rerank))
        }

    def snapshot(self, purpose):
        enabled = self.embedding if purpose == "embedding" else self.rerank
        return {
            "provider": "openai", "base_url": "https://provider.test/v1", "model": purpose + "-model",
            "allow_remote": True, "enabled": enabled, "revision": self.revision, "api_key": "fixture-key",
        }

    def change(self):
        self.revision += 1


class _Response:
    def __init__(self, body, change):
        self._body = body
        self._change = change

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        self._change()
        yield json.dumps(self._body).encode("utf-8")


class FakeClient:
    def __init__(self, *, change, calls, **kwargs):
        self._change = change
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, endpoint, json, headers):
        self._calls.append(endpoint)
        if endpoint.endswith("/embeddings"):
            body = {"data": [{"index": index, "embedding": [1.0, 0.0]} for index in range(len(json["input"]))]}
        else:
            body = {"results": [{"index": index, "relevance_score": 0.9} for index in range(len(json["documents"]))]}
        return _Response(body, self._change(endpoint))


def _entry():
    return {"id": "r1", "project_id": "p1", "revision": 1, "current_revision": 1,
            "content": "网页优先验证业务闭环", "status": "active", "authorized": True,
            "source_refs": ["source://fixture"]}


def _client_patch(monkeypatch, *, on_response, calls):
    def factory(**kwargs):
        return FakeClient(change=on_response, calls=calls, **kwargs)
    monkeypatch.setattr("backend.memory_app.retrieval_models.httpx.Client", factory)


def test_embedding_source_change_blocks_following_rerank_and_raises(tmp_path, monkeypatch):
    models, calls = MutableModels(), []
    current = {"value": True}

    def validate_current():
        if not current["value"]:
            raise RecognitionConflict("recognition selection changed")

    _client_patch(monkeypatch, calls=calls,
                  on_response=lambda endpoint: (lambda: current.__setitem__("value", False)) if endpoint.endswith("/embeddings") else (lambda: None))
    with pytest.raises(RecognitionConflict, match="selection changed"):
        configured_retrieve(models, tmp_path, "p1", "网页业务闭环", [_entry()], validate_current=validate_current)
    assert calls == ["https://provider.test/v1/embeddings"]


def test_rerank_revoke_is_never_accepted_after_response(tmp_path, monkeypatch):
    models, calls = MutableModels(), []
    current = {"value": True}

    def validate_current():
        if not current["value"]:
            raise RecognitionConflict("recognition revoked")

    _client_patch(monkeypatch, calls=calls,
                  on_response=lambda endpoint: (lambda: current.__setitem__("value", False)) if endpoint.endswith("/rerank") else (lambda: None))
    with pytest.raises(RecognitionConflict, match="revoked"):
        configured_retrieve(models, tmp_path, "p1", "网页业务闭环", [_entry()], validate_current=validate_current)
    assert calls == ["https://provider.test/v1/embeddings", "https://provider.test/v1/rerank"]


def test_vector_cache_fallback_still_checks_source_before_and_after(tmp_path, monkeypatch):
    checks = []

    def validate_current():
        checks.append("checked")

    monkeypatch.setattr("backend.memory_app.retrieval_models.retrieval_options",
                        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("fixture cache failure")))
    result = configured_retrieve(MutableModels(embedding=False, rerank=False), tmp_path, "p1", "网页业务", [_entry()],
                                validate_current=validate_current)
    assert [hit.id for hit in result.hits] == ["r1"]
    assert result.trace["vector"]["reason"] == "local_vector_cache_unavailable"
    assert len(checks) >= 3


def test_changed_remote_configuration_rejects_result_and_prevents_rerank_wire(tmp_path, monkeypatch):
    models, calls = MutableModels(), []
    _client_patch(monkeypatch, calls=calls,
                  on_response=lambda endpoint: models.change if endpoint.endswith("/embeddings") else (lambda: None))
    with pytest.raises(RecognitionRetrievalError, match="configured_model_changed_before_request"):
        configured_retrieve(models, tmp_path, "p1", "网页业务闭环", [_entry()])
    assert calls == ["https://provider.test/v1/embeddings"]
