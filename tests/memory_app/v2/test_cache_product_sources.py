"""Actual copied profile, worker read proof and product recognition parents."""
import json
from types import SimpleNamespace

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.privacy_policy import set_private_project
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope, RecognitionConflict
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace
from tests.memory_app.v2.test_candidate_destinations import seed, confirm_to
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_product_worker_sources import worker_draft
from tests.memory_app.v2.test_cache_sources import put_vectors, vector_rows, recognition


@pytest.fixture
def copied_product(do_env, request):
    client, models = do_env
    state = client.app.state
    env = SimpleNamespace(http=client, model=models, records=state.recognition_records,
        service=state.recognition_service, documents=state.recognition_documents, domains=state.workspace_domains)
    models.handler = lambda *_args, **_kwargs: json.dumps({'title': '原件', 'summary': '新方法',
        'facts': [], 'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []})
    old, _, document, item = seed(env, 'beta')
    copied = confirm_to(env, old, 'beta')
    assert copied.status_code == 200, copied.text
    beta = copied.json()['id']
    beta_row = env.records.read('recognitions', beta)
    proposed = env.service.propose(scope=WorkScope('local-user', 'beta'), content='新方法',
        source_experience_ids=beta_row.payload['source_experience_ids'], conditions=['送礼时'])
    with env.records.begin() as tx:
        tx.put('v2_candidate_hints', proposed.id, {'project_id': 'beta', 'relation': 'new',
            'target_id': None, 'scope_hint': 'me'}, expected_revision=0)
        tx.commit()
    copied = client.post(f'/api/v2/library/inbox/insight/{proposed.id}/file', json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True, 'expected_revision': 1})
    assert copied.status_code == 200, copied.text
    personal = copied.json()['id']
    worker = request.getfixturevalue('worker_draft')
    models.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': 'worker 方法', 'conditions': ['处理方法原件时'],
        'target_id': None, 'scope_hint': 'me'}], 'supports': []})
    with override(extract='@2'):
        candidate = generate_insights(models, env.service, env.documents, 'beta', worker.document)[0]
    copied = client.post(f"/api/v2/library/inbox/insight/{candidate['id']}/file", json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True,
        'expected_revision': candidate['revision']})
    assert copied.status_code == 200, copied.text
    final = copied.json()['id']
    current = env.records.read('recognitions', final)
    graph = SourceGraph()
    graph.snapshot(SourceEgressService(env.records).snapshot(WorkScope('local-user', 'me'),
        [{'type': 'recognition', 'id': final, 'revision': current.revision}]))
    graph = graph.result()
    assert any(node['kind'] == 'read_proof' for node in graph['nodes'])
    assert any(node.get('type') == 'original_item' and node.get('id') == item['id']
        and node['scope']['project_id'] == 'alpha' for node in graph['nodes'])
    assert any(node.get('type') == 'original_source' and node.get('id') == 'worker-source'
        and node['scope']['project_id'] == 'beta' for node in graph['nodes'])
    independent = env.service.stage_experience(scope=worker.scope, content='Independent evidence')
    independent = recognition(env.service, worker.scope, independent)
    path = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    affected = {('beta', beta), ('me', personal), ('me', final),
        (chunk_cache_namespace('alpha', {'kind': 'document', 'id': document}), 'chunk')}
    unaffected = {('beta', independent.id),
        (chunk_cache_namespace('beta', {'kind': 'source', 'id': 'worker-source'}), 'chunk')}
    put_vectors(path, affected | unaffected)
    return SimpleNamespace(**vars(env), path=path, affected=affected, unaffected=unaffected,
        item=item, worker=worker, graph=graph)


@pytest.mark.parametrize('change', ['source', 'project'])
def test_private_origin_evicts_all_real_product_descendants_and_preserves_worker_only_parent(copied_product, change):
    env = copied_product
    before, calls = vector_rows(env.path), len(env.model.calls)
    if change == 'project':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), 'original_item',
            env.item['id'], env.records.read('workspace_items', env.item['id']).revision, 0, [])
    assert vector_rows(env.path) == tuple(row for row in before if row[0:2] in env.unaffected)
    assert len(env.model.calls) == calls


def test_corrupt_worker_completion_proof_blocks_policy_before_any_cache_deletion(copied_product):
    env = copied_product
    proof = next(node for node in env.graph['nodes'] if node['kind'] == 'read_proof')
    from backend.memory_app.research_reads import COLLECTION
    row = env.records.read(COLLECTION, proof['tool_call_id'])
    assert row is not None and row.revision == 1
    with env.records.begin() as tx:
        tx.put(COLLECTION, row.object_id, {**row.payload, 'completed_sequence': proof['completed_sequence'] + 1},
            expected_revision=row.revision)
        tx.commit()
    before, facts = vector_rows(env.path), env.records.list_all()
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), 'original_item',
            env.item['id'], env.records.read('workspace_items', env.item['id']).revision, 0, [])
    assert vector_rows(env.path) == before
    assert env.records.list_all() == facts
