"""Write-side invalidation follows real scoped origins and current authority."""
import sqlite3
import json
from types import SimpleNamespace

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.privacy_policy import set_private_project
from backend.recognition import RecognitionService, RecognitionConflict, WorkScope
from backend.recognition_retrieval import SQLiteEmbeddingCache
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from tests.memory_app.v2.test_workbench_do import env as do_env


def put_vectors(path, pairs):
    cache = SQLiteEmbeddingCache(str(path))
    try:
        for project, identity in pairs:
            for model in ('model-a', 'model-b'):
                for revision in (1, 2):
                    cache.write(model_id=model, entry=SimpleNamespace(
                        project_id=project, id=identity, revision=revision), vector=(1., 0.))
    finally:
        cache.close()


def vector_rows(path):
    with sqlite3.connect(path) as connection:
        return tuple(connection.execute('SELECT * FROM recognition_embedding_cache ORDER BY '
            'project_id, recognition_id, revision, model_id'))


def recognition(service, scope, experience, content='Synthetic method'):
    candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience])
    return service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer=scope.user_id)


@pytest.fixture
def origins(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    scopes = {project: WorkScope('local-user', project) for project in ('alpha', 'beta', 'me')}
    first = service.stage_experience(scope=scopes['alpha'], content='Original method')
    beta = service.stage_experience(scope=scopes['beta'], content='Original method',
        provenance=records.read('recognition_experiences', first).payload['provenance'],
        copy_from={'project_id': 'alpha', 'experience_id': first, 'revision': 1})
    personal = service.stage_experience(scope=scopes['me'], content='Original method',
        provenance=records.read('recognition_experiences', beta).payload['provenance'],
        copy_from={'project_id': 'beta', 'experience_id': beta, 'revision': 1})
    items = {project: recognition(service, scopes[project], identity)
        for project, identity in [('alpha', first), ('beta', beta), ('me', personal)]}
    independent = service.stage_experience(scope=scopes['beta'], content='Independent evidence')
    independent = recognition(service, scopes['beta'], independent, 'Independent method')
    other_user = WorkScope('other-user', 'alpha')
    separate = service.stage_experience(scope=other_user, content='Other user evidence')
    separate = recognition(service, other_user, separate, 'Other user method')
    path = tmp_path / 'recognition-vectors.sqlite3'
    affected = {(project, value.id) for project, value in items.items()}
    untouched = {('beta', independent.id), ('alpha', separate.id)}
    put_vectors(path, affected | untouched)
    return SimpleNamespace(records=records, service=service, scopes=scopes, items=items,
        first=first, beta=beta, personal=personal, path=path, affected=affected, untouched=untouched)


@pytest.mark.parametrize('change', ['source', 'project'])
def test_privacy_write_evicts_copied_alpha_beta_me_all_models_and_revisions(origins, change):
    env = origins
    before = vector_rows(env.path)
    if change == 'source':
        SourceEgressService(env.records).set_policy(env.scopes['alpha'], 'experience', env.first, 1, 0, [])
        expected = env.affected
    else:
        set_private_project(env.records, 'alpha', True, 0)
        # Project privacy is a project-wide fact in this physical user space.
        expected = env.affected | {(row[0], row[1]) for row in before if row[0] == 'alpha'}
    assert vector_rows(env.path) == tuple(row for row in before if (row[0], row[1]) not in expected)
    assert all(row[0:2] in env.untouched for row in vector_rows(env.path))


@pytest.mark.parametrize('change', ['source_cas', 'policy_cas', 'foreign_scope', 'project_cas'])
def test_invalid_cas_or_scope_preserves_every_vector_and_authoritative_fact(origins, change):
    env = origins
    before, facts = vector_rows(env.path), env.records.list_all()
    with pytest.raises((RecognitionConflict, SQLiteUnitOfWorkConflict)):
        if change == 'project_cas':
            set_private_project(env.records, 'alpha', True, 1)
        else:
            SourceEgressService(env.records).set_policy(
                WorkScope('other-user', 'alpha') if change == 'foreign_scope' else env.scopes['alpha'],
                'experience', env.first, 2 if change == 'source_cas' else 1,
                1 if change == 'policy_cas' else 0, [])
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts


@pytest.mark.parametrize('change', ['source', 'project'])
def test_cache_delete_failure_rejects_privacy_write_without_fact_changes(origins, change):
    env = origins
    before, facts = vector_rows(env.path), env.records.list_all()
    lock = sqlite3.connect(env.path)
    try:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            if change == 'source':
                SourceEgressService(env.records).set_policy(env.scopes['alpha'], 'experience', env.first, 1, 0, [])
            else:
                set_private_project(env.records, 'alpha', True, 0)
    finally:
        lock.rollback()
        lock.close()
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts


def test_unqualified_cached_descendant_prevents_all_deletions_before_policy_write(origins):
    env = origins
    row = env.records.read('v2_experience_origins', env.personal)
    assert row is not None
    with env.records.begin() as tx:
        tx.put('v2_experience_origins', row.object_id,
            {**row.payload, 'source_revision': 2}, expected_revision=row.revision)
        tx.commit()
    before, facts = vector_rows(env.path), env.records.list_all()
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).set_policy(env.scopes['alpha'], 'experience', env.first, 1, 0, [])
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts


def test_real_inbox_writer_inherits_application_cache_dependency(do_env, monkeypatch):
    from backend.memory_app.cache_sources import prepare_sources
    from backend.memory_app.transaction_records import TransactionRecords
    from tests.memory_app.v2.test_candidate_destinations import seed, confirm_to
    client, model = do_env
    app = client.app.state
    env = SimpleNamespace(http=client, model=model, service=app.recognition_service,
        records=app.recognition_records, documents=app.recognition_documents, domains=app.workspace_domains)
    model.handler = lambda *_args, **_kwargs: json.dumps({'title': '原件', 'summary': '新方法',
        'facts': [], 'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []})
    env.service.cache_invalidation = prepare_sources
    old, _, _, _ = seed(env, 'beta')
    observed = []
    original = RecognitionService.__init__
    def construct(writer, *args, **kwargs):
        original(writer, *args, **kwargs)
        observed.append(writer)
    # Observation delegates the actual constructor and all domain operations.
    monkeypatch.setattr(RecognitionService, '__init__', construct)
    response = confirm_to(env, old, 'beta')
    assert response.status_code == 200, response.text
    writers = [writer for writer in observed if isinstance(writer.records, TransactionRecords)]
    assert len(writers) == 1
    assert writers[0].cache_invalidation is prepare_sources
    copied = env.records.read('recognitions', response.json()['id'])
    assert copied.payload['state'] == 'active' and copied.payload['scope']['project_id'] == 'beta'
    origin = env.records.read('v2_experience_origins', copied.payload['source_experience_ids'][0])
    assert origin.payload['source_project_id'] == 'alpha' and origin.payload['target_project_id'] == 'beta'
    assert env.records.read('recognition_candidates', old.id).payload['state'] == 'rejected'
