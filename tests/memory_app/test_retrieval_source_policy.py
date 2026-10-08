import pytest

from backend.memory_app.retrieval_models import ConfiguredTransport, configured_retrieve
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


class Models:
    def public(self):
        return {p: dict(purpose=p, provider="openai", base_url="https://example.test/v1",
                        model=p, allow_remote=True, enabled=True, revision=1,
                        configured=True, has_api_key=True) for p in ("embedding", "rerank")}

    def snapshot(self, purpose):
        return {**self.public()[purpose], "api_key": "test-only"}


def setup_sources(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    service = RecognitionService(records)
    authority = SourceEgressService(records)
    scope = WorkScope("user", "project")
    sources = []
    for text in ("alpha plan vector", "alpha plan rerank", "alpha plan private"):
        eid = service.stage_experience(scope=scope, content=text)
        candidate = service.propose(scope=scope, content=text, source_experience_ids=[eid])
        recognition = service.publish(scope=scope, candidate_id=candidate.id,
                                      expected_revision=1, reviewer="user")
        sources.append((eid, recognition.id))
    return service, authority, scope, sources


def fake_transport(monkeypatch, hook=None):
    calls = []
    def post(self, *, endpoint, payload):
        self._check_current()
        calls.append((endpoint, payload))
        if hook:
            hook()
        self._check_current()
        if endpoint.endswith("embeddings"):
            return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))]}
        return {"results": [{"index": i, "relevance_score": 0.9} for i in range(len(payload["documents"]))]}
    monkeypatch.setattr(ConfiguredTransport, "post_json", post)
    return calls


def test_nonprivate_sources_allow_both_remote_purposes_and_preserve_local_results(tmp_path, monkeypatch):
    service, authority, scope, sources = setup_sources(tmp_path)
    authority.set_policy(scope, "experience", sources[0][0], 1, 0, ["generation", "embedding", "rerank"])
    authority.set_policy(scope, "experience", sources[1][0], 1, 0, ["generation", "embedding", "rerank"])
    authority.set_policy(scope, "experience", sources[2][0], 1, 0, [])
    # Stored partial-purpose policies remain non-private for both adapters.
    with service.records.begin() as tx:
        for index, legacy in enumerate((["embedding"], ["rerank"])):
            source_id = sources[index][0]
            row = tx.read("source_egress_experience_policies", source_id)
            tx.put("source_egress_experience_policies", source_id,
                   {**row.payload, "allowed_purposes": legacy}, expected_revision=row.revision)
        tx.commit()
    calls = fake_transport(monkeypatch)
    result = configured_retrieve(Models(), tmp_path, "project", "alpha plan",
                                 service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
    assert {h.id for h in result.hits} == {r for _, r in sources}
    assert len(calls) == 2
    assert calls[0][1]["input"][0] == "alpha plan"
    assert set(calls[0][1]["input"][1:]) == {"alpha plan vector", "alpha plan rerank"}
    assert set(calls[1][1]["documents"]) == {"alpha plan vector", "alpha plan rerank"}
    assert all("alpha plan private" not in str(payload) for _, payload in calls)


def test_no_source_policies_allow_remote_but_private_project_preserves_local_hits(tmp_path, monkeypatch):
    service, authority, scope, sources = setup_sources(tmp_path)
    calls = fake_transport(monkeypatch)
    result = configured_retrieve(Models(), tmp_path, "project", "alpha plan",
                                 service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
    assert len(result.hits) == 3
    assert len(calls) == 2
    assert calls[0][1]["input"][0] == "alpha plan"
    assert set(calls[0][1]["input"][1:]) == {"alpha plan vector", "alpha plan rerank", "alpha plan private"}
    assert set(calls[1][1]["documents"]) == {"alpha plan vector", "alpha plan rerank", "alpha plan private"}

    calls.clear()
    set_private_project(service.records, scope.project_id, True, expected_revision=0)
    private_result = configured_retrieve(Models(), tmp_path, "project", "alpha plan",
                                        service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
    assert len(private_result.hits) == 3
    assert calls == []


def test_revoke_during_embedding_rejects_result_and_prevents_rerank(tmp_path, monkeypatch):
    service, authority, scope, sources = setup_sources(tmp_path)
    eid = sources[0][0]
    authority.set_policy(scope, "experience", eid, 1, 0, ["generation", "embedding", "rerank"])
    calls = fake_transport(monkeypatch, lambda: authority.set_policy(scope, "experience", eid, 1, 1, []))
    with pytest.raises(RecognitionConflict):
        configured_retrieve(Models(), tmp_path, "project", "alpha plan",
                            service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
    assert len(calls) == 1 and calls[0][0].endswith("embeddings")


def test_revoked_source_is_not_reused_from_persistent_vector_cache(tmp_path, monkeypatch):
    import sqlite3
    service, authority, scope, sources = setup_sources(tmp_path)
    eid = sources[0][0]
    authority.set_policy(scope, "experience", eid, 1, 0, ["generation", "embedding", "rerank"])
    for other_eid, _ in sources[1:]:
        authority.set_policy(scope, "experience", other_eid, 1, 0, [])
    calls = fake_transport(monkeypatch)
    for revision in (0, 1):
        if revision:
            authority.set_policy(scope, "experience", eid, 1, 1, [])
        result = configured_retrieve(Models(), tmp_path, "project", "alpha plan",
                                    service.retrieval_entries(scope=scope), source_egress=authority, source_scope=scope)
        assert len(result.hits) == 3
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / "recognition-vectors.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM recognition_embedding_cache").fetchone()[0] == 0
