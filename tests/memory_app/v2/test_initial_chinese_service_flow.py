"""Chinese fast admission through document draft, edit, review and Workbench.

The service and native attempt records are real; provider replies and their
usage numbers are synthetic controls, never paid-model quality measurements.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


PROJECT = 'chinese-service'
CONDITION = '开发环境'
BODY = '星桥服务的重试上限为 3 次。'
OTHER_BODY = '星桥服务的重试上限为 4 次。'
QUESTION = '星桥服务在开发环境的重试上限是多少？'


class _SyntheticTransport:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []
        self.contexts = []

    @property
    def preparation_calls(self):
        return [call for call in self.calls if call['stage'] == 'preparation']

    @property
    def ask_calls(self):
        return [call for call in self.calls if call['stage'] != 'preparation']

    def __call__(self, **request):
        messages = request['messages']
        text = messages[-1]['content']
        if messages[0]['content'].startswith('只返回紧凑 JSON 对象，字段 title,summary'):
            # Normal document processing still has its own synthetic model cost.
            body = next(body for body in (BODY, OTHER_BODY) if body in text)
            stage = 'preparation'
            output = {'title': '星桥服务开发配置', 'summary': body + '\n适用条件：开发环境。',
                      'facts': [{'text': body, 'evidence': {'quote': body}}],
                      'topics': [], 'todos': [], 'uncertainties': [], 'people': [],
                      'dates': [], 'suggestions': []}
        elif 'remaining_decisions' in text:
            context = json.loads(text)
            self.contexts.append(deepcopy(context))
            stage = 'decision'
            output = {'action': 'search', 'source': 'memory', 'query': context['question'],
                      'depth': 'summary', 'ids': [], 'questions': []}
            if context['round'] == 1:
                assert context['steps'] == []
            else:
                assert context['round'] == 2
                assert BODY in json.dumps(context['materials'], ensure_ascii=False)
                if self.mode == 'pending':
                    assert all(row['layer'] != 'L3' for row in context['materials'])
                    question = '这条候选尚未确认，是否先完成确认？'
                else:
                    assert self.mode == 'conflict'
                    insights = [row for row in context['materials'] if row['layer'] == 'L3']
                    assert BODY in json.dumps(insights, ensure_ascii=False)
                    assert OTHER_BODY in json.dumps(insights, ensure_ascii=False)
                    question = '两份已确认记录数值不一致，请确认当前采用哪个上限。'
                output.update(action='clarify', query='', questions=[question])
        else:
            assert self.mode == 'confirmed'
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            catalog = payload['evidence_catalog']
            assert len(catalog['units']) == 1
            unit = catalog['units'][0]
            assert unit['text'] == BODY + '\n\n适用条件：\n- ' + CONDITION
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            fact = {'kind': 'fact', 'text': BODY, 'evidence': [
                {'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}], 'date_refs': []}
            if stage == 'first':
                output = {'facts': [fact], 'unreviewed': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested upper bound is needed.'}}
            else:
                fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt',
                                          'reason': 'Preserve the reviewed literal declaration.'}]
                output = {'statements': [fact], 'unused': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested upper bound is needed.'},
                    'uncertain': [], 'excluded_facts': []}
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        raw = json.dumps(output, ensure_ascii=False)
        usage = {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}
        if request.get('stream'):
            def chunks():
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}
            return chunks()
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': raw}}], 'usage': usage}


@pytest.fixture
def service_flow(tmp_path, monkeypatch):
    root = tmp_path / 'chinese-service'
    (root / 'config').mkdir(parents=True)
    code = Path(__file__).resolve().parents[3]
    shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    connections = []

    def local_connections_only(original):
        def connect(sock, address):
            if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}:
                return original(sock, address)
            connections.append(address)
            raise AssertionError('chinese_service_flow_network_forbidden')
        return connect

    monkeypatch.setattr(socket.socket, 'connect', local_connections_only(socket.socket.connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', local_connections_only(socket.socket.connect_ex))
    import litellm  # noqa: F401
    from backend.memory_app.app import create_app
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.recognition import WorkScope
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout

    records, namespace = resolve_recognition_document_store(root)
    assert namespace == 'default'
    transport = _SyntheticTransport('confirmed')
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
        'model': 'synthetic-chinese-service', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    app = create_app(runtime_root=root, model_configuration=models,
                     deployment_layout=DeploymentLayout('desktop', root))
    documents = app.state.recognition_documents
    service = app.state.recognition_service
    query = app.state.workspace_domains.query
    with TestClient(app) as http:
        def prepare(mode):
            transport.mode = mode
            prepared = []
            bodies = [BODY, OTHER_BODY] if mode == 'conflict' else [BODY]
            for body in bodies:
                added = http.post('/api/workspace/v1/items/text', json={
                    'project_id': PROJECT, 'text': body + '\n适用条件：开发环境。'})
                assert added.status_code == 200, added.text
                item_id = added.json()['id']
                path = '/api/workspace/v1/items/' + item_id
                processed = http.post(path + '/process', json={'project_id': PROJECT})
                assert processed.status_code == 200, processed.text
                ready = processed.json()
                assert ready['status'] == 'ready'
                confirmed = http.post(path + '/confirm', json={
                    'project_id': PROJECT, 'expected_revision': ready['revision']})
                assert confirmed.status_code == 200, confirmed.text
                publication = confirmed.json()
                assert publication['status'] == 'confirmed'
                document = documents.read(publication['document_id'])
                assert records.read('workspace_items', item_id).payload['document_id'] == document['id']
                markdown = documents.markdown(document['id'])
                assert body in markdown and CONDITION in markdown
                extracted_response = http.post(path + '/recognition', json={'project_id': PROJECT})
                assert extracted_response.status_code == 200, extracted_response.text
                extracted = extracted_response.json()
                candidate = records.read('recognition_candidates', extracted['candidate_id'])
                assert candidate.payload['state'] == 'pending' and candidate.payload['content'] == markdown.strip()
                assert candidate.payload['source_experience_ids'] == [extracted['experience_id']]
                experience = records.read('recognition_experiences', extracted['experience_id'])
                assert experience.payload['content'] == markdown.strip()
                assert experience.payload['provenance']['source_refs'] == [
                    {'type': 'document', 'id': document['id'], 'revision': document['revision']}]
                edited = http.patch(f"/api/recognition/candidates/{candidate.object_id}/draft", json={
                    'project_id': PROJECT, 'expected_revision': candidate.revision,
                    'content': body, 'conditions': [CONDITION]})
                assert edited.status_code == 200, edited.text
                draft = edited.json()
                assert draft['revision'] == candidate.revision + 1
                assert draft['content'] == body and draft['conditions'] == [CONDITION]
                recognition = None
                if mode != 'pending':
                    reviewed = http.patch(f"/api/recognition/candidates/{candidate.object_id}", json={
                        'project_id': PROJECT, 'expected_revision': draft['revision'],
                        'decision': 'approve', 'content': body})
                    assert reviewed.status_code == 200, reviewed.text
                    recognition = reviewed.json()
                    assert recognition['content'] == body and recognition['conditions'] == [CONDITION]
                    assert records.read('recognition_candidates', candidate.object_id).payload['state'] == 'published'
                    marker = records.read('v2_insight_validity', recognition['id'])
                    version = records.read('recognition_versions', recognition['id'] + '~v1')
                    assert marker.payload['valid_from'] == version.payload['snapshot']['created_at']
                    # The real confirmed workspace document authorizes this L3 source.
                    egress = SourceEgressService(records)
                    snapshot = egress.snapshot(WorkScope('local-user', PROJECT), [{
                        'type': 'recognition', 'id': recognition['id'], 'revision': recognition['revision']}])
                    egress.require(snapshot, 'generation')
                else:
                    assert records.read('recognition_candidates', candidate.object_id).payload['state'] == 'pending'
                prepared.append({'document': document, 'experience_id': extracted['experience_id'],
                                 'candidate_id': candidate.object_id, 'draft': draft, 'recognition': recognition})
            query.retrieval_index.bootstrap()
            assert len(transport.preparation_calls) == len(bodies)
            assert transport.ask_calls == []
            return prepared

        collections = []
        original_collect = query._collect_candidates

        def observed_collect(*args, **kwargs):
            result = original_collect(*args, **kwargs)
            collections.append({'question': args[1], 'layers': kwargs.get('only_layers'),
                                'rows': [(row['kind'], row['id'], row['layer']) for row in result['candidates']]})
            return result

        monkeypatch.setattr(query, '_collect_candidates', observed_collect)
        yield SimpleNamespace(root=root, app=app, http=http, records=records, query=query,
                              service=service, documents=documents, transport=transport,
                              prepare=prepare, collections=collections)
    assert connections == []


def _ask(flow, key, question=QUESTION):
    from backend.memory_app.v2.policies import override

    with override(retrieve='@10'):
        return flow.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
            json={'project_id': PROJECT, 'text': question, 'intent': 'ask'})


def _assert_native_calls(flow, turn_id, stages):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    assert [call['stage'] for call in flow.transport.ask_calls] == stages
    store = flow.app.state.ai_turn_store
    frozen = store.get_request(turn_id)
    assert frozen['policy_versions']['retrieve'] == '@10'
    assert frozen['policy_versions']['compose'] == '@34'
    groups = kernel_call_groups(flow.root, turn_id=turn_id, project=PROJECT,
                               records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and len(groups[0]['calls']) == len(stages)
    assert all(row['status'] == 'completed' for row in groups[0]['calls'])
    database = flow.root / '.rebuild-data' / 'ai-turns.sqlite3'
    with sqlite3.connect(database) as connection:
        rows = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                  'WHERE turn_id=?', (turn_id,)).fetchall()
    assert rows == [('terminal', 'succeeded')] * len(stages)
    return store


@pytest.mark.parametrize('question', [QUESTION, '星桥服务在开发环境的重试上限为多少？'])
def test_chinese_document_edit_and_confirmation_routes_to_direct(service_flow, question):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.v2.policies import version

    flow = service_flow
    [prepared] = flow.prepare('confirmed')
    response = _ask(flow, 'chinese-confirmed', question)
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, answer = saved['turn'], saved['turn']['receipt']['ask']
    assert BODY in answer['answer'] and answer['no_match'] is False
    identity = prepared['recognition']['id']
    assert [(row['id'], row['layer']) for row in answer['citations']] == [(identity, 'insight')]
    assert answer['citations'][0]['quote'].startswith(BODY)
    assert flow.transport.contexts == []
    assert flow.collections[0]['layers'] == ('L3', 'L2', 'L1')
    assert ('recognition', identity, 'L3') in flow.collections[0]['rows']
    initial = answer['trace'][0]['initial_memory']
    assert initial['route'] == 'direct' and initial['reason'] == 'initial_evidence_sufficient'
    assert initial['probe_collections'] == 1 and initial['remainder_collections'] == 0
    assert initial['decision_invocations'] == 0
    assert answer['trace'][0]['agentic']['stop'] == 'initial_evidence_sufficient'
    store = _assert_native_calls(flow, turn['id'], ['first', 'review'])
    assert store.get_immutable_payload(turn['id'], 'answer-gap-input-rag-decision-1-v1') is None
    assert read_answer_input(store, turn['id']) == store.get_immutable_payload(
        turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert answer['model_usage'] == {'input_tokens': 40, 'output_tokens': 40, 'total_tokens': 80}
    count = len(flow.transport.calls)
    assert _ask(flow, 'chinese-confirmed', question).json() == saved
    assert len(flow.transport.calls) == count
    assert version('retrieve') == '@9' and version('compose') == '@34'


@pytest.mark.parametrize('mode,reason', [
    ('pending', 'no_current_confirmed_slot'),
    ('conflict', 'matching_slot_values_conflict'),
])
def test_unconfirmed_or_confirmed_conflicting_chinese_memory_enters_investigation(service_flow, mode, reason):
    from backend.recognition import WorkScope

    flow = service_flow
    prepared = flow.prepare(mode)
    response = _ask(flow, 'chinese-fallback-' + mode)
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, answer = saved['turn'], saved['turn']['receipt']['ask']
    initial = answer['trace'][0]['initial_memory']
    assert initial['route'] == 'investigate' and initial['reason'] == reason
    assert initial['probe_collections'] == 1 and initial['decision_invocations'] == 2
    assert answer['trace'][0]['agentic']['status'] == 'clarify'
    assert answer['citations'] == [] and BODY not in answer['answer'] and OTHER_BODY not in answer['answer']
    store = _assert_native_calls(flow, turn['id'], ['decision', 'decision'])
    for number in (1, 2):
        wire = store.get_immutable_payload(turn['id'], f'answer-gap-input-rag-decision-{number}-v1')
        assert wire is not None
        actual = json.loads(wire[1]['messages'][-1]['content'])
        assert actual == flow.transport.contexts[number - 1]
    assert answer['model_usage'] == {'input_tokens': 40, 'output_tokens': 40, 'total_tokens': 80}
    if mode == 'pending':
        assert flow.service.retrieval_entries(scope=WorkScope('local-user', PROJECT)) == ()
        assert all(item['recognition'] is None for item in prepared)
    else:
        identities = {item['recognition']['id'] for item in prepared}
        actual = {row['id'] for row in flow.transport.contexts[1]['materials'] if row['layer'] == 'L3'}
        assert {identity.rsplit(':', 1)[-1] for identity in actual} == identities
