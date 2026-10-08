import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.insights import source_experiences
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.recognition import RecognitionError
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'origin.sqlite3')
    return records, RecognitionService(records)


def copy(service, original, source='alpha', destination='beta'):
    row = service.records.read('recognition_experiences', original)
    return service.stage_experience(scope=WorkScope('user', destination), content=row.payload['content'],
        copy_from={'project_id': source, 'experience_id': original, 'revision': row.revision})


def test_payload_identity_cannot_borrow_another_legal_copy_origin(env):
    records, service = env
    original = service.stage_experience(scope=WorkScope('user', 'alpha'), content='source')
    first, second = copy(service, original), copy(service, original)
    with records.begin() as tx:
        row = tx.read('recognition_experiences', first)
        tx.put('recognition_experiences', first, {**row.payload, 'id': second}, expected_revision=row.revision)
        tx.commit()
    before = records.list_all()
    with pytest.raises(RecognitionConflict):
        SourceEgressService(records).snapshot(WorkScope('user', 'beta'),
            [{'type': 'experience', 'id': first, 'revision': 2}])
    assert records.list_all() == before


@pytest.mark.parametrize('change', ['absent', 'wrong_key', 'content', 'provenance', 'original_revision', 'cycle'])
def test_new_copy_requires_its_exact_marker_and_unchanged_origin(env, change):
    records, service = env
    alpha, beta = WorkScope('user', 'alpha'), WorkScope('user', 'beta')
    original = service.stage_experience(scope=alpha, content='source',
        provenance={'kind': 'user_statement', 'actor': 'user', 'source_refs': []})
    copied = copy(service, original)
    candidate = service.propose(scope=beta, content='conclusion', source_experience_ids=[copied])
    recognition = service.publish(scope=beta, candidate_id=candidate.id, expected_revision=1, reviewer='user')
    next_copy = copy(service, copied, source='beta', destination='alpha') if change == 'cycle' else None
    with records.begin() as tx:
        marker = tx.read('v2_experience_origins', copied)
        if change in {'absent', 'wrong_key'}:
            tx.delete('v2_experience_origins', copied, expected_revision=marker.revision)
            if change == 'wrong_key':
                tx.put('v2_experience_origins', 'wrong-key', marker.payload, expected_revision=0)
        elif change == 'original_revision':
            row = tx.read('recognition_experiences', original)
            tx.put('recognition_experiences', original, dict(row.payload), expected_revision=row.revision)
        elif change == 'cycle':
            tx.put('v2_experience_origins', copied, {**marker.payload,
                'source_experience_id': next_copy}, expected_revision=marker.revision)
        else:
            row = tx.read('recognition_experiences', copied)
            payload = {**row.payload, 'content': 'forged'} if change == 'content' else {
                **row.payload, 'provenance': {**row.payload['provenance'], 'actor': 'forged'}}
            tx.put('recognition_experiences', copied, payload, expected_revision=row.revision)
        tx.commit()
    assert not service.get_recognition(scope=beta, recognition_id=recognition.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(records).snapshot(beta, [{'type': 'experience', 'id': copied,
            'revision': records.read('recognition_experiences', copied).revision}])


def test_reserved_namespace_rejects_plain_creation_and_keeps_historical_statements(env):
    records, service = env
    scope = WorkScope('user', 'alpha')
    with pytest.raises(RecognitionError):
        service.stage_experience(scope=scope, content='forged',
            experience_id='experience-copy-v2-' + '0' * 32)
    for identity in ('ordinary', 'experience-copy-old', 'experience-copy-v2-old'):
        service.stage_experience(scope=scope, content='old source', experience_id=identity,
            provenance={'kind': 'user_statement', 'actor': 'user', 'source_refs': []})
        candidate = service.propose(scope=scope, content='old conclusion', source_experience_ids=[identity])
        assert service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='user').authorized
        SourceEgressService(records).require(SourceEgressService(records).snapshot(scope,
            [{'type': 'experience', 'id': identity, 'revision': 1}]), 'generation')


@pytest.mark.parametrize('revisions', [None, {'source': True}, {'source': 1.0}, {'source': 1, 'extra': 1}])
def test_frozen_copy_inputs_are_strictly_parsed_before_expansion(env, revisions):
    records, service = env
    scope = WorkScope('user', 'alpha')
    service.stage_experience(scope=scope, content='source', experience_id='source')
    before = records.list_all()
    with records.begin() as tx:
        with pytest.raises(RecognitionError):
            source_experiences(tx, scope, ['source'], [],
                experience_revisions=revisions, recognition_revisions={})
    assert records.list_all() == before


def test_flattening_rejects_a_recognition_cycle_before_any_copy(env):
    records, service = env
    scope = WorkScope('user', 'alpha')
    experience = service.stage_experience(scope=scope, content='source')
    candidate = service.propose(scope=scope, content='conclusion', source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='user')
    with records.begin() as tx:
        row = tx.read('recognitions', recognition.id)
        tx.put('recognitions', recognition.id, {**row.payload,
            'source_recognition_ids': [recognition.id], 'source_recognition_revisions': {recognition.id: 2}},
            expected_revision=row.revision)
        tx.commit()
    before = records.list_all()
    with records.begin() as tx:
        with pytest.raises(RecognitionConflict, match='cycle'):
            source_experiences(tx, scope, [], [recognition.id])
    assert records.list_all() == before
