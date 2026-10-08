import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "memory.sqlite3")
    return records, RecognitionService(records), SourceEgressService(records), WorkScope("user", "project")


def _experience(service, scope, content="evidence", provenance=None, experience_id=None):
    return service.stage_experience(scope=scope, content=content, provenance=provenance, experience_id=experience_id)


def _recognition(service, scope, experience_ids=(), recognition_ids=(), name=None):
    candidate = service.propose(scope=scope, content="derived", source_experience_ids=experience_ids, source_recognition_ids=recognition_ids)
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="user", recognition_id=name)


def test_multi_parent_inherits_privacy_only(env):
    _, service, egress, scope = env
    a, b = _experience(service, scope, "a"), _experience(service, scope, "b")
    egress.set_policy(scope, "experience", a, 1, 0, ["generation", "embedding", "rerank"])
    egress.set_policy(scope, "experience", b, 1, 0, ["generation", "embedding", "rerank"])
    child = _recognition(service, scope, (a, b), name="child")
    snapshot = egress.snapshot(scope, [{"type": "recognition", "id": child.id, "revision": child.revision}])
    assert snapshot["nodes"][-1]["effective_purposes"] == ["embedding", "generation", "rerank"]
    for purpose in ("generation", "embedding", "rerank"):
        egress.require(snapshot, purpose)
    egress.set_policy(scope, "experience", b, 1, 1, [])
    private = egress.snapshot(scope, [{"type": "recognition", "id": child.id, "revision": child.revision}])
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict): egress.require(private, purpose)


def test_child_policy_cannot_broaden_parent(env):
    _, service, egress, scope = env
    source = _experience(service, scope)
    egress.set_policy(scope, "experience", source, 1, 0, [])
    child = _recognition(service, scope, (source,), name="child")
    with pytest.raises(RecognitionError):
        egress.set_policy(scope, "recognition", child.id, child.revision, 0, ["generation", "embedding", "rerank"])
    assert egress.set_policy(scope, "recognition", child.id, child.revision, 0, [])["policy_revision"] == 1


def test_empty_refs_reject_and_unpolicied_refs_allow_unless_private(env):
    _, service, egress, scope = env
    source = _experience(service, scope)
    with pytest.raises(RecognitionError): egress.snapshot(scope, [])
    snapshot = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    for purpose in ("generation", "embedding", "rerank"):
        egress.require(snapshot, purpose)
    egress.set_policy(scope, "experience", source, 1, 0, [])
    private = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict): egress.require(private, purpose)


def test_128_character_source_id_can_be_authorized(env):
    _, service, egress, scope = env
    experience_id = "e" * 128
    source = _experience(service, scope, experience_id=experience_id)
    result = egress.set_policy(scope, "experience", source, 1, 0, ["generation", "embedding", "rerank"])
    assert result["source_id"] == experience_id
    snapshot = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    egress.require(snapshot, "generation")


def test_source_and_policy_changes_invalidate_snapshot(env):
    records, service, egress, scope = env
    source = _experience(service, scope)
    egress.set_policy(scope, "experience", source, 1, 0, ["generation", "embedding", "rerank"])
    snapshot = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    egress.set_policy(scope, "experience", source, 1, 1, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionConflict): egress.validate_snapshot(scope, snapshot)
    refreshed = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    # A source mutation still invalidates the frozen snapshot. A stale nonempty
    # compatibility policy falls back to the default permission for the new revision.
    with records.begin() as tx:
        current = tx.read("recognition_experiences", source)
        tx.put("recognition_experiences", source, {**dict(current.payload), "content": "changed"}, expected_revision=1)
        tx.commit()
    with pytest.raises(RecognitionConflict): egress.validate_snapshot(scope, refreshed)
    changed = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 2}])
    for purpose in ("generation", "embedding", "rerank"):
        egress.require(changed, purpose)
    egress.set_policy(scope, "experience", source, 2, 2, [])
    with records.begin() as tx:
        current = tx.read("recognition_experiences", source)
        tx.put("recognition_experiences", source, {**dict(current.payload), "content": "changed again"}, expected_revision=2)
        tx.commit()
    private = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 3}])
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict): egress.require(private, purpose)


def test_new_policy_invalidates_snapshot_that_observed_no_policy(env):
    records, service, egress, scope = env
    source = _experience(service, scope)
    snapshot = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    assert snapshot["nodes"] == [{"type": "experience", "id": source, "source_revision": 1, "policy_revision": 0, "effective_purposes": ["embedding", "generation", "rerank"]}]
    egress.set_policy(scope, "experience", source, 1, 0, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionConflict): egress.validate_snapshot(scope, snapshot)
    set_private_project(records, scope.project_id, True, expected_revision=0)
    private = egress.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict): egress.require(private, purpose)


def test_cross_scope_cycle_missing_and_external_provenance_deny(env):
    records, service, egress, scope = env
    source = _experience(service, scope)
    egress.set_policy(scope, "experience", source, 1, 0, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionConflict):
        egress.snapshot(WorkScope("other", "project"), [{"type": "experience", "id": source, "revision": 1}])
    with pytest.raises(RecognitionConflict):
        egress.snapshot(scope, [{"type": "recognition", "id": "missing", "revision": 1}])
    external = _experience(service, scope, provenance={"kind": "model_generated_artifact", "source_refs": [{"type": "document", "id": "d", "revision": 1}]})
    with pytest.raises(RecognitionError):
        egress.set_policy(scope, "experience", external, 1, 0, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionError):
        egress.snapshot(scope, [{"type": "experience", "id": external, "revision": 1}])
    # Insert a malformed cycle to exercise fail-closed recursion independently of lifecycle writes.
    with records.begin() as tx:
        tx.put("recognitions", "loop", {"id": "loop", "scope": {"user_id": "user", "project_id": "project"}, "state": "active", "source_experience_ids": [], "source_experience_revisions": {}, "source_recognition_ids": ["loop"], "source_recognition_revisions": {"loop": 1}}, expected_revision=0)
        tx.commit()
    with pytest.raises(RecognitionConflict): egress.snapshot(scope, [{"type": "recognition", "id": "loop", "revision": 1}])


def test_persists_across_store_restart(tmp_path):
    path = tmp_path / "memory.sqlite3"
    records = SQLiteStructuredRecordStore(path); service = RecognitionService(records); egress = SourceEgressService(records); scope = WorkScope("user", "project")
    source = _experience(service, scope)
    egress.set_policy(scope, "experience", source, 1, 0, ["generation", "embedding", "rerank"])
    restored = SourceEgressService(SQLiteStructuredRecordStore(path))
    snapshot = restored.snapshot(scope, [{"type": "experience", "id": source, "revision": 1}])
    restored.validate_snapshot(scope, snapshot); restored.require(snapshot, "generation")


def test_overlong_source_chain_is_rejected_without_recursion_error(env):
    records, _, egress, scope = env
    count = 257
    with records.begin() as tx:
        for index in range(count):
            item_id = f"chain-{index}"
            next_id = f"chain-{index + 1}"
            tx.put("recognitions", item_id, {
                "id": item_id, "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
                "state": "active", "source_experience_ids": [], "source_experience_revisions": {},
                "source_recognition_ids": [] if index == count - 1 else [next_id],
                "source_recognition_revisions": {} if index == count - 1 else {next_id: 1},
            }, expected_revision=0)
        tx.commit()
    with pytest.raises(RecognitionConflict, match="too large"):
        egress.snapshot(scope, [{"type": "recognition", "id": "chain-0", "revision": 1}])
