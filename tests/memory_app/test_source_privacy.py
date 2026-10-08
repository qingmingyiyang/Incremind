import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


PURPOSES = ["generation", "embedding", "rerank"]


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "privacy.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("user", "project")
    source = service.stage_experience(scope=scope, content="source")
    return records, service, SourceEgressService(records), scope, source


@pytest.mark.parametrize("legacy", [["generation"], ["embedding", "rerank"], None, "legacy"])
def test_only_empty_list_is_private_in_legacy_records(env, legacy):
    records, _, authority, scope, source = env
    with records.begin() as tx:
        tx.put("source_egress_experience_policies", source, {
            "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
            "source_revision": 1, "allowed_purposes": legacy,
        }, expected_revision=0)
        tx.commit()
    snapshot = authority.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    assert snapshot["nodes"][0]["effective_purposes"] == sorted(PURPOSES)
    for purpose in PURPOSES:
        authority.require(snapshot, purpose)
    assert records.read("source_egress_experience_policies", source).revision == 1


@pytest.mark.parametrize("partial", [["generation"], ["embedding"], ["rerank"], ["generation", "embedding"]])
def test_new_writes_reject_partial_grants_without_persisting(env, partial):
    records, _, authority, scope, source = env
    with pytest.raises(RecognitionError):
        authority.set_policy(scope, "experience", source, 1, 0, partial)
    assert records.read("source_egress_experience_policies", source) is None


def test_privacy_inherits_and_revoke_regrant_invalidates_snapshots(env):
    _, service, authority, scope, source = env
    candidate = service.propose(scope=scope, content="derived", source_experience_ids=[source])
    child = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="user")
    refs = [{"type": "recognition", "id": child.id, "revision": child.revision}]
    before = authority.snapshot(scope, refs)
    authority.set_policy(scope, "experience", source, 1, 0, [])
    private = authority.snapshot(scope, refs)
    for purpose in PURPOSES:
        with pytest.raises(RecognitionConflict):
            authority.require(private, purpose)
    with pytest.raises(RecognitionError, match="cannot broaden"):
        authority.set_policy(scope, "recognition", child.id, child.revision, 0, PURPOSES)
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, before)
    authority.set_policy(scope, "experience", source, 1, 1, PURPOSES)
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, private)
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, before)
    refreshed = authority.snapshot(scope, refs)
    for purpose in PURPOSES:
        authority.require(refreshed, purpose)


@pytest.mark.parametrize("partial", [["generation"], ["embedding", "rerank"], None, "generation"])
def test_policy_put_rejects_partial_or_invalid_values(tmp_path, monkeypatch, partial):
    from pathlib import Path
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.toml').write_bytes(
        (Path(__file__).parents[2] / 'config' / 'settings.toml.example').read_bytes())
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_app

    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI())
    service = app.state.recognition_service
    scope = WorkScope("local-user", "project")
    source = service.stage_experience(scope=scope, content="source")
    with TestClient(app) as client:
        response = client.put(f"/api/recognition/source-policies/experience/{source}", json={
            "project_id": scope.project_id, "expected_source_revision": 1,
            "expected_policy_revision": 0, "allowed_purposes": partial,
        })
        assert response.status_code == 400
        assert service.records.read("source_egress_experience_policies", source) is None
