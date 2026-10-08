"""Numbered JSON inputs retain their own original SQL experience incarnation."""
from copy import deepcopy
import json

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_input_dependencies import ExternalInputDependencyError, read_external_input_dependencies
from backend.recognition.sql_source_identities import KIND as SQL_IDENTITIES
from core.storage_provider.record_lineage import FACTS, HEAD_COLLECTIONS, WITNESS_COLLECTIONS, capture_lineage, verify_lineage
from tests.memory_app.v2.test_external_context import settings
from tests.memory_app.v2.test_mcp_evidence import (
    env, pending, publish, delivered_original, propose, DEPENDENCIES, COMPANION,
)
from tests.memory_app.v2.test_mcp_intake import env as intake_env, post as intake_post


SCOPE = WorkScope('local-user', 'alpha')


def experience(env, candidate):
    return env.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])


def rewrite_marker(env, identity, change):
    with env.records.begin() as tx:
        marker = tx.read(DEPENDENCIES, identity)
        value = deepcopy(marker.payload)
        change(value)
        tx.connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=?',
            (json.dumps(value), DEPENDENCIES, identity))
        tx.commit()


def assert_qualified_and_egress_rejected(env, recognition):
    before = env.records.list_all()
    assert not env.service.get_recognition(scope=SCOPE, recognition_id=recognition.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(SCOPE,
            [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    assert env.records.list_all() == before
    assert env.records.read('recognitions', recognition.id).payload['state'] == 'active'
    assert env.model.calls == 0


def test_new_numbered_json_marker_has_own_created_identity_and_explicit_kind(env):
    _api, turn, _store, _payload, candidate = pending(env)
    row = experience(env, candidate)
    marker = env.records.read(DEPENDENCIES, row.object_id)
    assert marker.revision == 1 and marker.payload['schema_version'] == 2
    identity = marker.payload['ownexperience_identity']
    assert set(identity) == {'collection', 'object_id', 'fact_id'}
    assert identity['collection'] == 'recognition_experiences' and identity['object_id'] == row.object_id
    fact = env.records.read(FACTS, identity['fact_id'])
    assert fact.payload['origin'] == 'created' and fact.payload['observed_revision'] == 1
    verify_lineage(env.records, identity)
    assert marker.payload['references'][0]['turn_id'] == turn
    assert marker.payload['references'][0]['identities_kind'] == SQL_IDENTITIES
    assert set(marker.payload['references'][0]) == {'turn_id', 'id', 'immutable_ref', 'identities_ref',
        'identities_kind', 'outcome_ref', 'completed_sequence'}
    assert candidate.payload['state'] == 'pending' and env.records.list('recognitions') == ()
    recognition = publish(env, candidate)
    assert env.service.get_recognition(scope=SCOPE, recognition_id=recognition.id).authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(SCOPE,
        [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(SCOPE, snapshot)
    assert env.model.calls == 0


@pytest.mark.parametrize('phase', ['pending', 'qualified'])
def test_same_id_revision_and_payload_recreation_of_own_experience_is_rejected(env, phase):
    _api, _turn, _store, _payload, candidate = pending(env)
    row = experience(env, candidate)
    with env.records.begin() as tx:
        original = capture_lineage(tx, 'recognition_experiences', row.object_id)
        tx.commit()
    recognition = publish(env, candidate) if phase == 'qualified' else None
    if recognition is not None:
        assert env.service.get_recognition(scope=SCOPE, recognition_id=recognition.id).authorized
    with env.records.begin() as tx:
        tx.delete(row.collection, row.object_id, expected_revision=row.revision)
        rebuilt = tx.put(row.collection, row.object_id, row.payload, expected_revision=0)
        fresh = capture_lineage(tx, row.collection, row.object_id)
        tx.commit()
    assert rebuilt == row and fresh != original
    if recognition is None:
        before = env.records.list_all()
        with pytest.raises(RecognitionConflict):
            publish(env, candidate)
        assert env.records.list_all() == before and env.records.list('recognitions') == ()
    else:
        assert_qualified_and_egress_rejected(env, recognition)


@pytest.mark.parametrize('fault', ['legacy1', 'missing', 'malformed', 'foreign_id', 'foreign_fact', 'foreign_collection',
    'missing_kind', 'unknown_kind'])
def test_missing_legacy_malformed_or_foreign_own_birth_cannot_gain_authority(env, fault):
    _api, _turn, _store, _payload, candidate = pending(env)
    recognition = publish(env, candidate)
    row = experience(env, candidate)
    foreign = env.service.stage_experience(scope=SCOPE, content='Synthetic unrelated statement',
        provenance={'kind': 'user_statement', 'actor': 'local-user'})
    with env.records.begin() as tx:
        foreign_identity = capture_lineage(tx, 'recognition_experiences', foreign)
        own_identity = capture_lineage(tx, 'recognition_experiences', row.object_id)
        tx.commit()
    def damage(value):
        if fault == 'legacy1':
            value['schema_version'] = 1
            value.pop('ownexperience_identity', None)
            for reference in value['references']:
                reference.pop('identities_kind', None)
        elif fault == 'missing': value.pop('ownexperience_identity', None)
        elif fault == 'malformed': value['ownexperience_identity'] = []
        elif fault == 'foreign_id': value['ownexperience_identity'] = foreign_identity
        elif fault == 'foreign_fact': value['ownexperience_identity'] = {**own_identity, 'fact_id': foreign_identity['fact_id']}
        elif fault == 'foreign_collection': value['ownexperience_identity'] = {**own_identity, 'collection': 'documents'}
        elif fault == 'missing_kind': value['references'][0].pop('identities_kind', None)
        else: value['references'][0]['identities_kind'] = 'external-context-unknown-identities-v1'
    rewrite_marker(env, row.object_id, damage)
    assert_qualified_and_egress_rejected(env, recognition)


def test_legacy_schema1_cannot_publish_or_be_adopted_as_a_current_identity(env):
    _api, _turn, _store, _payload, candidate = pending(env)
    row = experience(env, candidate)
    def legacy(value):
        value['schema_version'] = 1
        value.pop('ownexperience_identity', None)
        for reference in value['references']:
            reference.pop('identities_kind', None)
    rewrite_marker(env, row.object_id, legacy)
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict):
        publish(env, candidate)
    assert env.records.list_all() == before
    assert env.records.read(DEPENDENCIES, row.object_id).payload['schema_version'] == 1
    assert env.records.list('recognitions') == ()


def test_legacy_schema1_denies_qualification_without_changing_retained_history(env):
    _api, _turn, _store, _payload, candidate = pending(env)
    recognition = publish(env, candidate)
    row = experience(env, candidate)
    history = env.service.list_experiences(scope=SCOPE, include_revoked=True)
    def legacy(value):
        value['schema_version'] = 1
        value.pop('ownexperience_identity', None)
        for reference in value['references']:
            reference.pop('identities_kind', None)
    rewrite_marker(env, row.object_id, legacy)
    assert_qualified_and_egress_rejected(env, recognition)
    retained = env.service.get_recognition(scope=SCOPE, recognition_id=recognition.id)
    assert retained.content == recognition.content and retained.revision == recognition.revision
    assert retained.state == recognition.state == 'active'
    assert env.records.read(row.collection, row.object_id) == row
    assert env.service.list_experiences(scope=SCOPE, include_revoked=True) == history
    old = next(item for item in history if item.id == row.object_id)
    assert old.content == row.payload['content'] and old.revision == row.revision
    assert env.records.read(DEPENDENCIES, row.object_id).payload['schema_version'] == 1


@pytest.mark.parametrize('part', ['head', 'witness', 'fact'])
def test_corrupt_own_lineage_is_rejected_without_writing_or_adopting(env, part):
    _api, _turn, _store, _payload, candidate = pending(env)
    recognition = publish(env, candidate)
    row = experience(env, candidate)
    identity = env.records.read(DEPENDENCIES, row.object_id).payload['ownexperience_identity']
    collection, object_id = {
        'head': (HEAD_COLLECTIONS[row.collection], row.object_id),
        'witness': (WITNESS_COLLECTIONS[row.collection], row.object_id),
        'fact': (FACTS, identity['fact_id']),
    }[part]
    with env.records.begin() as tx:
        metadata = tx.read(collection, object_id)
        damaged = {**metadata.payload, 'schema_version': False}
        tx.connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=?',
            (json.dumps(damaged), collection, object_id))
        tx.commit()
    before = env.records.list_all()
    with pytest.raises(ExternalInputDependencyError, match='external_context_evidence_invalid'):
        read_external_input_dependencies(env.records, SCOPE, row)
    assert_qualified_and_egress_rejected(env, recognition)
    assert env.records.list_all() == before
    assert env.records.read(collection, object_id).payload == damaged


def test_ordinary_unreferenced_statement_keeps_its_original_qualification(intake_env):
    app, _http, records = intake_env
    response = intake_post(intake_env, 'propose_insight', {'text': 'Synthetic plain statement', 'project': 'alpha'})
    assert response.status_code == 200, response.text
    candidate = records.read('recognition_candidates', response.json()['result']['candidate_id'])
    row = records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    assert records.read(DEPENDENCIES, row.object_id) is None
    assert read_external_input_dependencies(records, SCOPE, row) is None
    service = app.state.recognition_service
    recognition = service.publish(scope=SCOPE, candidate_id=candidate.object_id,
        expected_revision=1, reviewer='local-user')
    assert service.get_recognition(scope=SCOPE, recognition_id=recognition.id).authorized


def test_current_private_source_and_revoked_client_keep_local_qualification_only(env):
    _api, _turn, _store, payload, candidate = pending(env)
    settings(env, allow_remote=False, clients={'claude': True, 'codex': False})
    authority = SourceEgressService(env.records)
    authority.set_policy(SCOPE, 'original_source', payload['id'], 1, 0, [])
    recognition = publish(env, candidate)
    assert env.service.get_recognition(scope=SCOPE, recognition_id=recognition.id).authorized
    snapshot = authority.snapshot(SCOPE,
        [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    with pytest.raises(RecognitionConflict):
        authority.require(snapshot, 'generation')
    assert env.model.calls == 0


@pytest.mark.parametrize('stage', ['marker', 'own_fact'])
def test_real_transaction_abort_rolls_back_experience_marker_and_all_lineage_facts(env, stage):
    _api, turn, _store, _payload = delivered_original(env)
    condition = (f"NEW.collection='{DEPENDENCIES}'" if stage == 'marker' else
        f"NEW.collection='{FACTS}' AND json_extract(NEW.payload_json,'$.collection')='recognition_experiences'")
    with env.records.begin() as tx:
        tx.connection.execute('CREATE TRIGGER reject_external_experience_identity BEFORE INSERT ON crp_structured_records '
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT,'synthetic identity rejection'); END")
        tx.commit()
    before = env.records.list_all()
    response = propose(env, turn)
    assert response.status_code == 409 and response.json() == {'detail': 'external_context_unavailable'}
    assert env.records.list_all() == before
    assert env.records.list('recognition_experiences') == env.records.list(DEPENDENCIES) == ()
    assert env.records.list('recognition_candidates') == () and env.model.calls == 0
