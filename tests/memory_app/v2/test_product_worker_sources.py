"""Real product worker reads bind retained draft permissions and confirmation."""
import json
import time
from types import SimpleNamespace

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.original_sources import source_store
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import override
from backend.recognition import RecognitionConflict, WorkScope
from tests.memory_app.v2.test_workbench_do import env as do_env


@pytest.fixture
def worker_draft(do_env):
    client, models = do_env
    state = client.app.state
    records, documents = state.recognition_records, state.recognition_documents
    store = source_store(records)
    text = '真实 worker 原件方法'
    store.write('sources', 'worker-source', {'id': 'worker-source', 'project_id': 'beta',
        'title': '方法原件', 'type': 'text', 'metadata': {'content_snapshot': text,
            'content_structure': {'status': 'completed', 'summary': text,
                'structured_body': text, 'key_points': [text]}}}, expected_revision=0)
    observed = []
    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '读方法原件产出整理稿', 'goal': '读方法原件',
                'deliverable': '整理稿', 'capabilities': ['source.evidence.read', 'document.draft.propose'],
                'depends_on': []}]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': text})
        reads = [event for event in context.get('events', []) if event.get('type') == 'tool.completed'
            and event.get('data', {}).get('capability_id') == 'source.evidence.read']
        if not reads:
            return json.dumps({'type': 'tool', 'capability_id': 'source.evidence.read',
                'arguments': {'source_id': 'worker-source', 'template_type': 'review'}})
        body = reads[0]['resolved_payload']['structured_body']
        observed.append(body)
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '方法整理稿', 'markdown': body, 'final_for': '整理稿'}})
    models.handler = respond
    state.recognition_turn_dispatcher._runtime()
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'intent': 'do', 'text': '读取方法原件产出整理稿'})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 90
    while True:
        receipt = client.get(f"/api/v2/workbench/threads/{response.json()['thread_id']}?project_id=beta").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
    # Receipt publication precedes the existing background timing finalizer.
    # Finish that writer before tests capture their complete zero-write baseline.
    while state.workbench_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not state.workbench_tasks
    assert observed == [text]
    assert documents.markdown(receipt['document_id']) == text
    experience, _ = ensure_document_experience(documents, state.recognition_service, 'beta', receipt['document_id'])
    return SimpleNamespace(client=client, models=models, records=records, store=store,
        documents=documents, service=state.recognition_service, experience=experience,
        document=receipt['document_id'], scope=WorkScope('local-user', 'beta'))


def snapshot(env):
    return SourceEgressService(env.records).snapshot(env.scope,
        [{'type': 'experience', 'id': env.experience, 'revision': 1}])


@pytest.mark.parametrize('corruption', ['foreign_user', 'style_budget', 'unknown_configuration', 'boolean_configuration_revision'])
def test_wire_lock_collector_rejects_unsafe_expansions(corruption):
    from copy import deepcopy
    from backend.memory_app.research_sources import _WireLocks
    frozen = {'schema_version': 1, 'scope': {'user_id': 'local-user', 'project_id': 'beta'},
        'privacy_revision': 0, 'roots': [{'type': 'original_source', 'id': 'worker-source', 'revision': 1}],
        'nodes': [{'type': 'original_source', 'id': 'worker-source', 'source_revision': 1,
            'policy_revision': 0, 'effective_purposes': ['embedding', 'generation', 'rerank']}]}
    collector = _WireLocks(None, 'local-user')
    collector.snapshot(frozen)
    assert collector.identities == {('sources', 'worker-source')}
    if corruption == 'foreign_user':
        frozen['scope']['user_id'] = 'another-user'
    elif corruption == 'style_budget':
        frozen['nodes'][0]['research_style_sources'] = [deepcopy(frozen) for _ in range(257)]
    else:
        config = {'collection': 'memory_persona', 'id': 'global', 'revision': 1}
        if corruption == 'unknown_configuration':
            config['collection'] = 'arbitrary-directory'
        else:
            config['revision'] = True
        with pytest.raises(RecognitionConflict):
            collector.configuration({'configurations': [config]})
        return
    with pytest.raises(RecognitionConflict):
        collector.snapshot(frozen)


def test_successful_worker_source_is_bound_and_private_source_blocks_copy_and_wire(worker_draft):
    env = worker_draft
    frozen = snapshot(env)
    graph = frozen['nodes'][0]['dependency_revisions']['current_source_graph']
    proof = [node for node in graph['nodes'] if node['kind'] == 'read_proof']
    assert len(proof) == 1 and proof[0]['proof_revision'] == 1
    assert any(node.get('type') == 'original_source' and node.get('id') == 'worker-source'
        for node in graph['nodes'])
    env.models.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': 'worker 方法', 'conditions': ['处理方法原件时'],
        'target_id': None, 'scope_hint': 'me'}], 'supports': []})
    with override(extract='@2'):
        candidates = generate_insights(env.models, env.service, env.documents, 'beta', env.document)
    assert len(candidates) == 1
    SourceEgressService(env.records).set_policy(env.scope, 'original_source', 'worker-source', 1, 0, [])
    before, count = env.records.list_all(), len(env.models.calls)
    copy = env.client.post(f"/api/v2/library/inbox/insight/{candidates[0]['id']}/file", json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True, 'expected_revision': 1})
    assert copy.status_code in {400, 409}, copy.text
    assert env.records.list_all() == before
    assert env.service.read_candidate_experiences(scope=env.scope, experience_ids=[env.experience])[0].content
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).require(snapshot(env), 'generation')
    assert generate_insights(env.models, env.service, env.documents, 'beta', env.document) == []
    assert len(env.models.calls) == count


@pytest.mark.parametrize('corruption', ['changed_proof', 'missing_proof', 'changed_source'])
def test_worker_owner_corruption_denies_draft_reuse_without_new_wire(worker_draft, corruption):
    env = worker_draft
    frozen = snapshot(env)
    proof = env.records.list('v2_research_source_reads')[0]
    if corruption == 'changed_source':
        original = env.store.read('sources', 'worker-source')
        env.store.write('sources', 'worker-source', {**original, 'title': '已修订'}, expected_revision=1)
    else:
        with env.records.begin() as tx:
            if corruption == 'missing_proof':
                tx.delete('v2_research_source_reads', proof.object_id, expected_revision=proof.revision)
            else:
                tx.put('v2_research_source_reads', proof.object_id, proof.payload, expected_revision=proof.revision)
            tx.commit()
    before, count = env.records.list_all(), len(env.models.calls)
    with pytest.raises(RecognitionConflict):
        snapshot(env)
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).validate_snapshot(env.scope, frozen)
    assert generate_insights(env.models, env.service, env.documents, 'beta', env.document) == []
    assert len(env.models.calls) == count
    assert env.records.list_all() == before


def test_only_exact_registered_host_admission_can_extend_a_product_request(worker_draft):
    from copy import deepcopy
    import sqlite3
    from backend.memory_app.research_reads import product_read_sources
    from backend.memory_app.research_packets import authority_stores
    env = worker_draft
    turns, agents = authority_stores(env.records)
    execution = env.records.list('v2_task_executions')[0]
    original = execution.payload['request']
    identity = original['turn_id']
    accepted = turns.get_request(identity)
    assert 'agent_binding' in accepted and 'agent_policy_binding' in accepted
    product_read_sources(env.records, turns, agents, original, 'beta')

    before, calls = env.records.list_all(), len(env.models.calls)
    path = env.store.root / 'ai-turns.sqlite3'
    corruptions = []
    for field, value in [('allowed', 'arbitrary.extra.tool'), ('denied', 'source.evidence.read'),
            ('require_approval', 'source.evidence.read')]:
        bad = deepcopy(accepted)
        bad['capability_policy'][field] = [*bad['capability_policy'][field], value]
        corruptions.append((field, bad))
    for name in ('input', 'privacy', 'unknown', 'binding', 'policy', 'operation'):
        bad = deepcopy(accepted)
        if name == 'input': bad['input']['text'] += ' changed'
        elif name == 'privacy': bad['privacy']['invented_owner_fact'] = True
        elif name == 'unknown': bad['invented_owner_field'] = True
        elif name == 'binding': bad['agent_binding']['profile_revision'] += 1
        elif name == 'policy': bad['agent_policy_binding']['snapshot_ref'] = 'crp://session/other/agent-policy-snapshot-v1'
        else: bad['operation_id'] += '-other'
        corruptions.append((name, bad))
    try:
        for name, bad in corruptions:
            with sqlite3.connect(path) as connection:
                connection.execute('UPDATE ai_turns SET request_json=? WHERE turn_id=?',
                    (json.dumps(bad), identity))
            with pytest.raises(RecognitionConflict, match='product draft'):
                product_read_sources(env.records, turns, agents, original, 'beta')
            assert env.records.list_all() == before, name
            assert len(env.models.calls) == calls, name
    finally:
        with sqlite3.connect(path) as connection:
            connection.execute('UPDATE ai_turns SET request_json=? WHERE turn_id=?',
                (json.dumps(accepted), identity))
    product_read_sources(env.records, turns, agents, original, 'beta')


@pytest.mark.parametrize('wire_case', ['locked', 'writer', 'late_source'])
def test_copied_worker_profile_holds_original_lock_at_real_product_wire(worker_draft, monkeypatch, wire_case):
    from contextlib import contextmanager
    from contextvars import ContextVar
    from threading import Event, Thread, current_thread
    from core.ai_kernel.runtime import _PlannerExecutionContext
    from backend.memory_app.research_sources import ReadControl
    env = worker_draft
    assert env.client.get('/api/v2/projects').status_code == 200
    env.models.handler = lambda *_args, **_kwargs: json.dumps({'insights': [{
        'kind': 'new_method', 'relation': 'new', 'text': 'worker 方法', 'conditions': ['处理方法原件时'],
        'target_id': None, 'scope_hint': 'me'}], 'supports': []})
    with override(extract='@2'):
        candidate = generate_insights(env.models, env.service, env.documents, 'beta', env.document)[0]
    copied = env.client.post(f"/api/v2/library/inbox/insight/{candidate['id']}/file", json={
        'source_project_id': 'beta', 'target_project_id': 'me', 'confirm': True, 'expected_revision': 1})
    assert copied.status_code == 200, copied.text
    held, observed = ContextVar('worker_original_locks', default=0), []
    attempted, sql_attempted, finished = Event(), Event(), Event()
    changed, writer_errors, primary_responses, wire_observations = [], [], [], []
    source = env.store.read('sources', 'worker-source')
    def write():
        try:
            env.store.write('sources', 'worker-source', {**source, 'title': '已修订'}, expected_revision=1)
        except BaseException as error:
            writer_errors.append(error)
        finally:
            finished.set()
    writer = Thread(target=write, name='competing-original-writer')
    records_type = type(env.records)
    original_records_begin = records_type.begin
    def records_begin(records):
        if current_thread() is writer and records.database_path == env.records.database_path:
            sql_attempted.set()
        return original_records_begin(records)
    store_type = type(env.store)
    original_lock = store_type.locked
    @contextmanager
    def lock(store, collection, identity):
        target = collection == 'sources' and identity == 'worker-source'
        if target and current_thread() is writer:
            attempted.set()
        with original_lock(store, collection, identity):
            token = held.set(held.get() + int(target))
            try:
                yield
            finally:
                held.reset(token)
    original_begin = _PlannerExecutionContext.begin_model_wire_attempt
    def begin(control):
        if control.purpose == 'primary':
            observed.append(held.get())
            if wire_case == 'writer' and len(observed) == 1:
                writer.start()
                assert sql_attempted.wait(5), 'real JSON writer did not attempt the original SQL write lock'
                assert not finished.is_set(), 'real JSON write bypassed the held original lock'
        return original_begin(control)
    original_wire = ReadControl.begin_model_wire_attempt
    def wire(control, *args, **kwargs):
        if wire_case == 'late_source' and not changed:
            changed.append(True)
            write()
        return original_wire(control, *args, **kwargs)
    monkeypatch.setattr(store_type, 'locked', lock)
    monkeypatch.setattr(records_type, 'begin', records_begin)
    monkeypatch.setattr(_PlannerExecutionContext, 'begin_model_wire_attempt', begin)
    monkeypatch.setattr(ReadControl, 'begin_model_wire_attempt', wire)
    def respond(messages, **kwargs):
        wire_observations.append(held.get())
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '运用方法', 'goal': '运用方法',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        primary_responses.append(context)
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': '方法整理稿'})
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '方法整理稿', 'markdown': '运用 worker 方法', 'final_for': '整理稿'}})
    env.models.handler = respond
    response = env.client.post('/api/v2/workbench/turns', json={
        'project_id': 'beta', 'intent': 'do', 'text': '运用画像中的 worker 方法'})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 90
    while True:
        receipt = env.client.get(f"/api/v2/workbench/threads/{response.json()['thread_id']}?project_id=beta").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    if writer.ident is not None:
        writer.join(10)
        assert not writer.is_alive()
    assert not writer_errors
    if wire_case == 'late_source':
        assert changed and finished.is_set()
        assert observed == [] and primary_responses == []
        assert receipt['state'] in {'partial', 'failed'}, receipt
        assert env.store.revision('sources', 'worker-source') == 2
        return
    if wire_case == 'writer':
        assert sql_attempted.is_set()
        assert attempted.is_set() and finished.is_set()
        assert wire_observations and wire_observations[0] > 0
        assert env.store.revision('sources', 'worker-source') == 2
        assert observed[0] > 0
        with pytest.raises(RecognitionConflict):
            snapshot(env)
        return
    assert receipt['state'] == 'done', receipt
    execution = env.records.read('v2_task_executions', response.json()['turn']['id'])
    assert copied.json()['id'] in {row['id'] for row in execution.payload['request']['privacy']['material_refs']}
    assert observed and all(count > 0 for count in observed), observed
