import json
import time
import os
from types import SimpleNamespace
from pathlib import Path

from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_candidate_destinations import seed, confirm_to
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope
from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionConflict
import pytest


def snapshot_structure(snapshot):
    identities, visits, rows = set(), 0, 0
    def walk(table):
        nonlocal visits, rows
        for node in table['nodes']:
            visits += 1
            identities.add((table['scope']['user_id'], table['scope']['project_id'],
                node['type'], node['id'], node['source_revision']))
            dependency = node.get('dependency_revisions', {})
            graph = dependency.get('current_source_graph')
            if graph:
                rows += len(graph['nodes'])
                for entry in graph['nodes']:
                    visits += 1
                    assert not {'current_source_graph', 'source_snapshot', 'source_snapshots',
                        'current_source_snapshots'}.intersection(entry.get('dependency_revisions', {}))
                    if entry['kind'] == 'material':
                        identities.add((entry['scope']['user_id'], entry['scope']['project_id'],
                            entry['type'], entry['id'], entry['source_revision']))
            if 'source_snapshot' in dependency:
                walk(dependency['source_snapshot'])
            for field in ('source_snapshots', 'current_source_snapshots'):
                for source in dependency.get(field, []):
                    walk(source)
            for source in node.get('research_style_sources', []):
                walk(source)
    walk(snapshot)
    return {'json_utf8_bytes': len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')),
        'unique_materials': len(identities), 'nested_node_visits': visits, 'flat_metadata_rows': rows}


def test_copy_ask_product_do_and_draft_reuse_preserve_source_authority(do_env, monkeypatch):
    client, model = do_env
    state = client.app.state
    env = SimpleNamespace(http=client, model=model, records=state.recognition_records,
        service=state.recognition_service, documents=state.recognition_documents,
        domains=state.workspace_domains)
    model.handler = lambda *_args, **_kwargs: json.dumps({'title': '原件', 'summary': '新方法',
        'facts': [], 'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []})
    old, experience, _, _ = seed(env, 'beta')
    copied = confirm_to(env, old, 'beta')
    assert copied.status_code == 200, copied.text
    beta_identity = copied.json()['id']
    own = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='新方法',
        source_experience_ids=[experience], conditions=['送礼时'])
    with env.records.begin() as tx:
        tx.put('v2_candidate_hints', own.id, {'project_id': 'alpha', 'relation': 'new',
            'target_id': None, 'scope_hint': 'me'}, expected_revision=0)
        tx.commit()
    personal = confirm_to(env, own, 'me')
    assert personal.status_code == 200, personal.text
    identity = personal.json()['id']

    def respond(messages, **kwargs):
        try:
            context = json.loads(messages[-1]['content'])
        except json.JSONDecodeError:
            return json.dumps({'answer': '依据新方法', 'citations': [1]})
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '运用新方法', 'goal': '运用新方法',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': '新方法整理稿'})
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '新方法整理稿', 'markdown': '依据新方法的成果', 'final_for': '整理稿'}})

    model.handler = respond
    asked = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '新方法', 'intent': 'ask'})
    assert asked.status_code == 200, asked.text
    assert beta_identity in [row['id'] for row in asked.json()['turn']['receipt']['ask']['citations']]
    state.recognition_turn_dispatcher._runtime()
    done = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '用新方法产出整理稿', 'intent': 'do'})
    assert done.status_code == 200, done.text
    thread = done.json()['thread_id']
    deadline = time.monotonic() + 90
    while True:
        receipt = client.get(f'/api/v2/workbench/threads/{thread}?project_id=beta').json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
    execution = env.records.read('v2_task_executions', done.json()['turn']['id'])
    assert identity in [row['id'] for row in execution.payload['request']['privacy']['material_refs']]
    assert env.documents.read(receipt['document_id'])['project_id'] == 'beta'
    experience, _ = ensure_document_experience(env.documents, env.service, 'beta', receipt['document_id'])
    first_frozen = SourceEgressService(env.records).snapshot(WorkScope('local-user', 'beta'),
        [{'type': 'experience', 'id': experience, 'revision': 1}])
    before = len(model.calls)
    model.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': '成果画像可复用', 'conditions': ['运用新方法时'],
        'target_id': None, 'scope_hint': 'me'}, {
        'kind': 'new_method', 'relation': 'new', 'text': '成果项目可复用', 'conditions': ['运用新方法时'],
        'target_id': None, 'scope_hint': None}], 'supports': []})
    with override(extract='@2'):
        candidates = generate_insights(model, env.service, env.documents, 'beta', receipt['document_id'])
    assert len(candidates) == 2
    assert len(model.calls) == before + 1
    local = client.post(f"/api/v2/library/insights/{candidates[1]['id']}/confirm", json={
        'project_id': 'beta', 'expected_revision': candidates[1]['revision']})
    assert local.status_code == 200, local.text
    personal = client.post(f"/api/v2/library/inbox/insight/{candidates[0]['id']}/file", json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True,
        'expected_revision': candidates[0]['revision']})
    assert personal.status_code == 200, personal.text
    model.handler = respond
    again = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '成果项目可复用', 'intent': 'ask'})
    assert again.status_code == 200, again.text
    assert local.json()['id'] in [row['id'] for row in again.json()['turn']['receipt']['ask']['citations']]
    repeated = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '再次运用成果画像产出整理稿', 'intent': 'do'})
    assert repeated.status_code == 200, repeated.text
    deadline = time.monotonic() + 90
    while True:
        next_receipt = client.get(f"/api/v2/workbench/threads/{repeated.json()['thread_id']}?project_id=beta").json()['turns'][0]['receipt']['do']
        if next_receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert next_receipt['state'] == 'done', next_receipt
    next_execution = env.records.read('v2_task_executions', repeated.json()['turn']['id'])
    assert personal.json()['id'] in [row['id'] for row in next_execution.payload['request']['privacy']['material_refs']]
    latest, _ = ensure_document_experience(env.documents, env.service, 'beta', next_receipt['document_id'])
    authority = SourceEgressService(env.records)
    frozen = authority.snapshot(WorkScope('local-user', 'beta'), [{'type': 'experience', 'id': latest, 'revision': 1}])
    model.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': '第三代方法', 'conditions': ['再运用方法时'],
        'target_id': None, 'scope_hint': 'me'}], 'supports': []})
    with override(extract='@2'):
        third_candidates = generate_insights(model, env.service, env.documents, 'beta', next_receipt['document_id'])
    assert len(third_candidates) == 1
    third_personal = client.post(f"/api/v2/library/inbox/insight/{third_candidates[0]['id']}/file", json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True,
        'expected_revision': third_candidates[0]['revision']})
    assert third_personal.status_code == 200, third_personal.text
    model.handler = respond
    third = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'text': '第三次用成果产出整理稿', 'intent': 'do'})
    assert third.status_code == 200, third.text
    deadline = time.monotonic() + 90
    while True:
        third_receipt = client.get(f"/api/v2/workbench/threads/{third.json()['thread_id']}?project_id=beta").json()['turns'][0]['receipt']['do']
        if third_receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert third_receipt['state'] == 'done', third_receipt
    third_source, _ = ensure_document_experience(env.documents, env.service, 'beta', third_receipt['document_id'])
    from collections import Counter
    import backend.memory_app.source_egress as source_egress
    original_owner = source_egress.read_product_draft_dependencies
    original_branch = source_egress._artifact_authority
    owner_reads, branches = Counter(), Counter()
    def counted_owner(uow, scope, payload):
        result = original_owner(uow, scope, payload)
        if result is not None:
            owner_reads[(scope.user_id, scope.project_id, result.revisions['product_turn_id'])] += 1
        return result
    def counted_branch(authority, uow, scope, kind, source, *args):
        branches[(scope.user_id, scope.project_id, kind,
            getattr(source, 'object_id', None) or source.payload['id'])] += 1
        return original_branch(authority, uow, scope, kind, source, *args)
    with monkeypatch.context() as observer:
        observer.setattr(source_egress, 'read_product_draft_dependencies', counted_owner)
        observer.setattr(source_egress, '_artifact_authority', counted_branch)
        third_snapshot = authority.snapshot(WorkScope('local-user', 'beta'),
            [{'type': 'experience', 'id': third_source, 'revision': 1}])
    Path(os.environ.get('T12_5_STRUCTURE_REPORT', 'work/T12.5-product-source-flat-structure.json')).write_text(json.dumps({
        'first': snapshot_structure(first_frozen), 'second': snapshot_structure(frozen),
        'third': snapshot_structure(third_snapshot),
        'third_product_owner_reads': [{'key': key, 'count': count} for key, count in sorted(owner_reads.items())],
        'third_source_branches': [{'key': key, 'count': count} for key, count in sorted(branches.items())],
        'first_snapshot': first_frozen,
        'second_snapshot': frozen, 'third_snapshot': third_snapshot}, ensure_ascii=False, indent=2), encoding='utf-8')
    assert all('current_source_graph' in snapshot['nodes'][0]['dependency_revisions']
        for snapshot in (first_frozen, frozen, third_snapshot))
    assert snapshot_structure(third_snapshot)['nested_node_visits'] <= 256 * 257
    assert all(count == 1 for count in owner_reads.values()), owner_reads
    assert all(count == 1 for count in branches.values()), branches
    authority.require(third_snapshot, 'generation')
    authority.require(frozen, 'generation')
    before = len(model.calls)
    set_private_project(env.records, 'alpha', True, 0)
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(WorkScope('local-user', 'beta'), frozen)
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(WorkScope('local-user', 'beta'),
            [{'type': 'experience', 'id': latest, 'revision': 1}]), 'generation')
    assert generate_insights(model, env.service, env.documents, 'beta', next_receipt['document_id']) == []
    assert len(model.calls) == before
