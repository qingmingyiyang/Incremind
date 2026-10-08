"""Real lifecycle writes invalidate derived rows only after original guards."""
from types import SimpleNamespace

import pytest

from backend.recognition import RecognitionService, RecognitionConflict, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_cache_sources import recognition, put_vectors, vector_rows


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service, scope = RecognitionService(records), WorkScope('local-user', 'alpha')
    experience = service.stage_experience(scope=scope, content='Original evidence')
    parent = recognition(service, scope, experience)
    child = service.propose(scope=scope, content='Derived method', source_experience_ids=[],
        source_recognition_ids=[parent.id])
    child = service.publish(scope=scope, candidate_id=child.id, expected_revision=1, reviewer='local-user')
    independent = recognition(service, scope, service.stage_experience(scope=scope, content='Independent evidence'))
    path = tmp_path / 'recognition-vectors.sqlite3'
    put_vectors(path, {('alpha', row.id) for row in (parent, child, independent)})
    return SimpleNamespace(records=records, service=service, scope=scope, experience=experience,
        parent=parent, child=child, independent=independent, path=path)


@pytest.mark.parametrize('operation', ['revise', 'revoke', 'experience', 'split'])
def test_bare_domain_write_evicts_exact_same_scope_cascade(env, operation):
    before = vector_rows(env.path)
    if operation == 'experience':
        env.service.revoke_experience(scope=env.scope, experience_id=env.experience, expected_revision=1)
    elif operation == 'revise':
        env.service.revise(scope=env.scope, recognition_id=env.parent.id, expected_revision=1, content='Updated method')
    elif operation == 'revoke':
        env.service.revoke(scope=env.scope, recognition_id=env.parent.id, expected_revision=1, reason='Manual revocation')
    else:
        env.service.split(scope=env.scope, recognition_id=env.parent.id, expected_revision=1,
            parts=['One', 'Two'], new_ids=['child-one', 'child-two'])
    assert vector_rows(env.path) == tuple(row for row in before if row[1] == env.independent.id)


@pytest.mark.parametrize('invalid', ['cas', 'source', 'child_collision'])
def test_failed_restructure_keeps_all_vectors_and_facts(env, invalid):
    before, facts = vector_rows(env.path), env.records.list_all()
    with pytest.raises(RecognitionConflict):
        if invalid == 'cas':
            env.service.revise(scope=env.scope, recognition_id=env.parent.id, expected_revision=2, content='Changed')
        elif invalid == 'source':
            env.service.split(scope=env.scope, recognition_id=env.parent.id, expected_revision=1,
                parts=[{'content': 'One', 'source_experience_ids': [env.experience]},
                       {'content': 'Two', 'source_experience_ids': ['missing-source']}],
                new_ids=['first-child', 'second-child'])
        else:
            env.service.split(scope=env.scope, recognition_id=env.parent.id, expected_revision=1,
                parts=['One', 'Two'], new_ids=['new-child', env.independent.id])
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts


def test_explicit_invalidation_uses_original_transaction_and_scope(env):
    from core.search_and_recall.vector_cache_invalidation import vector_cache_path
    observed = []
    def invalidate(tx, scope, identities):
        assert vector_cache_path(tx) == env.path
        assert tx.read('recognitions', env.parent.id).payload['state'] == 'active'
        assert tx.read('recognitions', env.parent.id).revision == 1
        observed.append((scope, identities))
        return lambda: None
    service = RecognitionService(env.records, cache_invalidation=invalidate)
    service.revise(scope=env.scope, recognition_id=env.parent.id, expected_revision=1, content='Changed')
    assert observed == [(env.scope, (('recognition', env.parent.id),))]


@pytest.mark.parametrize('invalid', ['source', 'child_collision'])
def test_prepared_product_plan_is_not_applied_after_later_output_guard_fails(env, invalid):
    from backend.memory_app.cache_sources import prepare_sources
    prepared, applied = [], []
    def prepare(tx, scope, identities):
        assert tx.read('recognitions', env.parent.id).payload['state'] == 'active'
        apply = prepare_sources(tx, scope, identities)
        prepared.append(identities)
        def commit_cache():
            applied.append(identities)
            return apply()
        return commit_cache
    service = RecognitionService(env.records, cache_invalidation=prepare)
    before, facts = vector_rows(env.path), env.records.list_all()
    parts = ['One', {'content': 'Two', 'source_experience_ids': ['missing-source']}] if invalid == 'source' else ['One', 'Two']
    with pytest.raises(RecognitionConflict):
        service.split(scope=env.scope, recognition_id=env.parent.id, expected_revision=1, parts=parts,
            new_ids=['new-child', env.independent.id if invalid == 'child_collision' else 'next-child'])
    assert prepared == [(('recognition', env.parent.id),)]
    assert applied == []
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts


def test_prepared_product_cache_failure_rolls_back_lifecycle_facts(env):
    import sqlite3
    from backend.memory_app.cache_sources import prepare_sources
    service = RecognitionService(env.records, cache_invalidation=prepare_sources)
    before, facts = vector_rows(env.path), env.records.list_all()
    lock = sqlite3.connect(env.path)
    try:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            service.revoke(scope=env.scope, recognition_id=env.parent.id, expected_revision=1, reason='Manual')
    finally:
        lock.rollback()
        lock.close()
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts
