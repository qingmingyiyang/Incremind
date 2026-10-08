"""Consume a real edited filing through product ask/do and its draft authority."""
import asyncio
import json
import time

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import validate_graph
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_do import env


def completed(client, project, submission):
    deadline = time.monotonic() + 90
    while True:
        turns = client.get(f"/api/v2/workbench/threads/{submission['thread_id']}?project_id={project}").json()['turns']
        turn = next(turn for turn in turns if turn['id'] == submission['turn']['id'])
        receipt = turn['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
    return receipt


def test_edited_filing_is_consumed_by_real_ask_do_and_reusable_product_draft(env):
    client, model = env
    state = client.app.state
    records, service, documents = state.recognition_records, state.recognition_service, state.recognition_documents
    model.handler = lambda *_args, **_kwargs: json.dumps({'title': '原件', 'summary': '原件事实',
        'facts': [], 'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []})
    item = asyncio.run(state.workspace_domains.intake.add_text({'project_id': 'alpha', 'text': '原件事实与证据'}))
    original = asyncio.run(process_and_confirm(state.workspace_domains, item['id'], 'alpha'))['document_id']
    assert client.get('/api/v2/projects').status_code == 200
    project = client.post('/api/v2/projects', json={'name': '目标'}).json()['id']

    def respond(messages, **kwargs):
        try: context = json.loads(messages[-1]['content'])
        except json.JSONDecodeError: return json.dumps({'answer': '依据补充证据', 'citations': [1]})
        if 'neighbors' in context: return json.dumps({'insights': [], 'supports': []})
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '运用补充证据', 'goal': '运用补充证据',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': '补充证据成果'})
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '补充证据成果', 'markdown': '依据补充证据的成果', 'final_for': '整理稿'}})
    model.handler = respond
    moved = client.post(f'/api/v2/library/notes/{original}/file', json={
        'project_id': 'alpha', 'target_project_id': project, 'expected_revision': 1})
    assert moved.status_code == 200, moved.text
    target = moved.json()['target_document_id']
    documents.save_user_edit(target, markdown='用户补充证据用于写材料', expected_revision=1)
    experience, revision = ensure_document_experience(documents, service, project, target)
    assert revision == 2
    from backend.memory_app.v2.insight_generation import generate_insights
    model.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': '补充证据可供写材料', 'conditions': ['写材料时'],
        'target_id': None, 'scope_hint': 'me'}], 'supports': []})
    candidate, = generate_insights(model, service, documents, project, target)
    model.handler = respond
    personal = client.post(f"/api/v2/library/inbox/insight/{candidate['id']}/file", json={
        'source_project_id': project, 'target_project_id': 'me', 'expected_revision': candidate['revision'], 'confirm': True})
    assert personal.status_code == 200, personal.text
    asked = client.post('/api/v2/workbench/turns', json={'project_id': project, 'text': '补充证据', 'intent': 'ask'})
    assert asked.status_code == 200, asked.text
    assert target in {row['id'] for row in asked.json()['turn']['receipt']['ask']['citations']}
    state.recognition_turn_dispatcher._runtime()
    started = client.post('/api/v2/workbench/turns', json={'project_id': project, 'text': '用补充证据写材料', 'intent': 'do'})
    assert started.status_code == 200, started.text
    receipt = completed(client, project, started.json())
    execution = records.read('v2_task_executions', started.json()['turn']['id'])
    assert personal.json()['id'] in {row['id'] for row in execution.payload['request']['privacy']['material_refs']}
    artifact, _ = ensure_document_experience(documents, service, project, receipt['document_id'])
    authority = SourceEgressService(records)
    scope = WorkScope('local-user', project)
    refs = [{'type': 'experience', 'id': artifact, 'revision': 1}]
    frozen = authority.snapshot(scope, refs)
    authority.validate_snapshot(scope, frozen); authority.require(frozen, 'generation')
    graph = frozen['nodes'][0]['dependency_revisions']['current_source_graph']
    validate_graph(graph, 'local-user')
    assert any(row.get('dependency_revisions', {}).get('filing_document_id') == target for row in graph['nodes'])
    source = records.read('workspace_items', item['id'])
    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', source.object_id, source.revision, 0, [])
    import pytest
    from backend.recognition import RecognitionError
    with pytest.raises(RecognitionError): authority.require(authority.snapshot(scope, refs), 'generation')
    assert documents.markdown(target) == '用户补充证据用于写材料'
    calls = len(model.calls)
    fresh = client.post('/api/v2/workbench/turns', json={'project_id': project, 'text': '再次写材料', 'intent': 'do'})
    assert fresh.status_code == 200, fresh.text
    completed(client, project, fresh.json())
    request = records.read('v2_task_executions', fresh.json()['turn']['id']).payload['request']
    assert request['privacy']['material_refs'] == request['privacy']['source_snapshots'] == []
    sent = json.dumps(model.calls[calls:], ensure_ascii=False)
    assert '用户补充证据用于写材料' not in sent and '补充证据可供写材料' not in sent
    assert personal.json()['id'] not in sent
