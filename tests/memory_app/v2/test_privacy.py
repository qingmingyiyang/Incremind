"""Global setting and private scopes constrain the existing source authority."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


PURPOSES = ("generation", "embedding", "rerank")


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("user", "project")
    source_id = service.stage_experience(scope=scope, content="test evidence")
    return records, service, SourceEgressService(records), scope, source_id


@pytest.fixture
def privacy():
    from backend.memory_app.v2 import privacy

    return privacy


def snapshot(env, revision=1):
    _, _, authority, scope, source_id = env
    return authority.snapshot(scope, [{"type": "experience", "id": source_id, "revision": revision}])


def update_source(env):
    records, _, _, _, source_id = env
    with records.begin() as tx:
        current = tx.read("recognition_experiences", source_id)
        tx.put("recognition_experiences", source_id, {**current.payload, "content": "revised evidence"},
               expected_revision=current.revision)
        tx.commit()


@pytest.mark.parametrize("purpose", PURPOSES)
def test_unpolicied_source_allows_each_purpose(env, purpose):
    _, _, authority, scope, _ = env
    current = snapshot(env)
    authority.validate_snapshot(scope, current)
    authority.require(current, purpose)


@pytest.mark.parametrize("purpose", PURPOSES)
def test_source_private_flag_survives_source_revision(env, purpose):
    _, _, authority, _, source_id = env
    authority.set_policy(env[3], "experience", source_id, 1, 0, [])
    with pytest.raises(RecognitionConflict, match="not authorized"):
        authority.require(snapshot(env), purpose)
    update_source(env)
    with pytest.raises(RecognitionConflict, match="not authorized"):
        authority.require(snapshot(env, 2), purpose)


def test_stale_nonempty_policy_reverts_to_default_allow_but_old_snapshot_is_invalid(env):
    _, _, authority, scope, source_id = env
    authority.set_policy(scope, "experience", source_id, 1, 0, ["generation", "embedding", "rerank"])
    old = snapshot(env)
    authority.require(old, "embedding")
    update_source(env)
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, old)
    for purpose in PURPOSES:
        authority.require(snapshot(env, 2), purpose)
    authority.set_policy(scope, "experience", source_id, 2, 1, [])
    with pytest.raises(RecognitionConflict, match="not authorized"):
        authority.require(snapshot(env, 2), "generation")


def test_project_private_blocks_then_restore_allows_without_mutating_sources(env, privacy):
    records, _, authority, scope, source_id = env
    original = records.read("recognition_experiences", source_id)
    assert not privacy.is_private_project(records, scope.project_id)
    assert not privacy.is_private_project(records, None)
    assert privacy.privacy_revision(records) == 0
    privacy.set_private_project(records, scope.project_id, True, 0)
    assert privacy.is_private_project(records, scope.project_id)
    assert not privacy.is_private_project(records, "other-project")
    assert privacy.privacy_revision(records) == 1
    for purpose in PURPOSES:
        with pytest.raises(RecognitionConflict, match="not authorized"):
            authority.require(snapshot(env), purpose)
    privacy.set_private_project(records, scope.project_id, False, 1)
    assert not privacy.is_private_project(records, scope.project_id)
    assert privacy.privacy_revision(records) == 2
    for purpose in PURPOSES:
        authority.require(snapshot(env), purpose)
    assert records.read("recognition_experiences", source_id) == original
    assert records.read("v2_privacy_state", "default").payload == {"revision": 2}


def test_private_project_cannot_be_bypassed_by_legacy_policy_override(env, privacy):
    records, _, authority, scope, source_id = env
    privacy.set_private_project(records, scope.project_id, True, 0)
    with pytest.raises(RecognitionError, match="cannot broaden"):
        authority.set_policy(scope, "experience", source_id, 1, 0, ["generation", "embedding", "rerank"])
    authority.set_policy(scope, "experience", source_id, 1, 0, [])


def test_privacy_changes_and_roundtrip_invalidate_frozen_snapshot(env, privacy):
    records, _, authority, scope, _ = env
    old = snapshot(env)
    assert old["privacy_revision"] == 0
    privacy.set_private_project(records, scope.project_id, True, 0)
    with pytest.raises(RecognitionConflict, match="snapshot conflicted"):
        authority.validate_snapshot(scope, old)
    blocked = snapshot(env)
    assert blocked["privacy_revision"] == 1
    privacy.set_private_project(records, scope.project_id, False, 1)
    for stale in (old, blocked):
        with pytest.raises(RecognitionConflict, match="snapshot conflicted"):
            authority.validate_snapshot(scope, stale)
    authority.validate_snapshot(scope, snapshot(env))


def test_private_scope_write_conflict_rolls_back_counter(env, privacy):
    records, _, _, scope, _ = env
    privacy.set_private_project(records, scope.project_id, True, 0)
    with pytest.raises(SQLiteUnitOfWorkConflict):
        privacy.set_private_project(records, scope.project_id, False, 0)
    assert privacy.is_private_project(records, scope.project_id)
    assert privacy.privacy_revision(records) == 1


def test_privacy_counter_preserves_changes_across_projects_and_restart(env, privacy):
    records, _, _, scope, _ = env
    privacy.set_private_project(records, scope.project_id, True, 0)
    privacy.set_private_project(records, "other-project", True, 0)
    restored = SQLiteStructuredRecordStore(records.database_path)
    assert privacy.is_private_project(restored, scope.project_id)
    assert privacy.is_private_project(restored, "other-project")
    assert privacy.privacy_revision(restored) == 2


def test_repeated_privacy_setting_is_noop_but_stale_cas_still_conflicts(env, privacy):
    records, _, _, scope, _ = env
    privacy.set_private_project(records, scope.project_id, False, 0)
    assert privacy.privacy_revision(records) == 0
    saved = privacy.set_private_project(records, scope.project_id, True, 0)
    repeated = privacy.set_private_project(records, scope.project_id, True, 1)
    assert repeated == saved
    assert privacy.privacy_revision(records) == 1
    with pytest.raises(SQLiteUnitOfWorkConflict):
        privacy.set_private_project(records, scope.project_id, True, 0)
    privacy.set_private_project(records, scope.project_id, False, 1)
    with pytest.raises(SQLiteUnitOfWorkConflict):
        privacy.set_private_project(records, scope.project_id, True, 0)
    assert privacy.privacy_revision(records) == 2


@pytest.mark.parametrize("purpose", PURPOSES)
def test_global_remote_switch_and_private_project_both_gate_egress(env, privacy, purpose):
    records, _, _, scope, _ = env
    configuration = {kind: {"allow_remote": True} for kind in PURPOSES}
    models = SimpleNamespace(public=lambda: configuration)
    assert privacy.egress_allowed(records, models, scope.project_id, purpose)
    configuration[purpose]["allow_remote"] = False
    assert not privacy.egress_allowed(records, models, scope.project_id, purpose)
    configuration[purpose]["allow_remote"] = True
    privacy.set_private_project(records, scope.project_id, True, 0)
    assert not privacy.egress_allowed(records, models, scope.project_id, purpose)


def test_asr_uses_enabled_flag_and_private_project_gate(env, privacy):
    records, _, _, scope, _ = env
    configuration = {"asr": {"enabled": False, "allow_remote": True}}
    models = SimpleNamespace(public=lambda: configuration)
    assert not privacy.egress_allowed(records, models, scope.project_id, "asr")
    configuration["asr"] = {"enabled": True, "allow_remote": False}
    assert privacy.egress_allowed(records, models, scope.project_id, "asr")
    privacy.set_private_project(records, scope.project_id, True, 0)
    assert not privacy.egress_allowed(records, models, scope.project_id, "asr")


@pytest.mark.parametrize("value", [None, True, -1, "0"])
def test_malformed_privacy_snapshot_revision_is_rejected(env, value):
    _, _, authority, scope, _ = env
    invalid = deepcopy(snapshot(env))
    invalid["privacy_revision"] = value
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, invalid)


def test_privacy_scope_is_reused_for_derived_recognition(env, privacy):
    records, service, authority, scope, source_id = env
    draft = service.propose(scope=scope, content="derived", source_experience_ids=[source_id])
    recognition = service.publish(scope=scope, candidate_id=draft.id, expected_revision=1, reviewer="user")
    privacy.set_private_project(records, scope.project_id, True, 0)
    frozen = authority.snapshot(scope, [{"type": "recognition", "id": recognition.id, "revision": 1}])
    with pytest.raises(RecognitionConflict, match="not authorized"):
        authority.require(frozen, "generation")
