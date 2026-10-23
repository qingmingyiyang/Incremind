"""General fast answers through normal service ownership and frozen receipts.

Provider responses are synthetic. These tests exercise routing and provenance,
not whether a model's self-assessed semantic coverage is accurate in practice.
"""
from contextlib import ExitStack
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


PROJECT = 'evidence-answer-service'
CAPTURED_AT = '2026-10-22T08:00:00Z'
INVOCATION = 'evidence-answer-v1'
CANDIDATE_ONLY = 'CANDIDATE_ONLY_NOT_EVIDENCE'
CASES = {
    'en': {
        'question': 'Who went with Nora when she visited the glass conservatory?',
        'answer': 'Nora visited the glass conservatory with her cousin.',
        'quote': 'My cousin came with me to the glass conservatory, and we took many photos.',
        'dialogue': (
            '[D1:1] Ellis: Tell me about the places you explored; I enjoyed your photographs.\n'
            '[D1:2] Nora: My cousin came with me to the glass conservatory, and we took many photos.\n'
            '[D1:3] Ellis: Which exhibits caught your attention during the visit?\n'
            '[D1:4] Nora: The orchids were beautiful. We also examined the hanging ferns and the pond.\n'
            '[D1:5] Ellis: I have been sketching plants, but it is hard to capture the details.\n'
            '[D1:6] Nora: Try concentrating on one leaf at a time, then draw the outline carefully.\n'
            '[D1:7] Ellis: I might bring a sketchbook on my own visit. Were the paths easy to follow?\n'
            '[D1:8] Nora: Yes, the signs explained the different habitats and the paths connected them.\n'
            '[D1:9] Ellis: Your pictures of the orchids made me want to learn more about them.\n'
            '[D1:10] Nora: The little information cards beside the plants were particularly helpful.\n'),
    },
    'zh': {
        'question': '林岚参观玻璃温室时是谁陪她一起去的？',
        'answer': '林岚和妹妹一起参观了玻璃温室。',
        'quote': '我和妹妹一起参观了玻璃温室，还拍了很多照片。',
        'dialogue': (
            '[D1:1] 周宁：看了你分享的照片，我也想了解这次参观。\n'
            '[D1:2] 林岚：我和妹妹一起参观了玻璃温室，还拍了很多照片。\n'
            '[D1:3] 周宁：里面哪些展区最吸引你？我对植物很感兴趣。\n'
            '[D1:4] 林岚：兰花很好看，我们还看了悬挂的蕨类和水池里的植物。\n'
            '[D1:5] 周宁：我也在练习画植物，但总是画不好细小的纹路。\n'
            '[D1:6] 林岚：可以先观察一片叶子，慢慢描出轮廓，再补上叶脉。\n'
            '[D1:7] 周宁：我想带一本速写本过去，温室里的参观路线好找吗？\n'
            '[D1:8] 林岚：路线很清楚，指示牌介绍了不同环境，走道把它们连起来。\n'
            '[D1:9] 周宁：照片里的兰花让我想多了解一些相关知识。\n'
            '[D1:10] 林岚：植物旁边的小卡片有不少介绍，读起来很有帮助。\n'),
    },
}


class _SyntheticTransport:
    def __init__(self, case, *, mode='accepted', report_usage=True):
        self.case, self.mode = case, mode
        self.report_usage = report_usage
        self.calls, self.contexts = [], []
        self.after_proposal = None

    @property
    def ask_calls(self):
        return [row for row in self.calls if row['stage'] != 'preparation']

    def _statement(self, payload):
        units = payload['evidence_catalog']['units']
        # Every identifier and quotation comes from the real service projection.
        unit = next((row for row in units if self.case['answer'] in row['text']), None)
        if unit is not None:
            quote = self.case['answer']
        else:
            unit = next(row for row in units if self.case['quote'] in row['text'])
            quote = self.case['quote']
        return {'kind': 'fact', 'text': self.case['answer'], 'date_refs': [],
                'evidence': [{'unit_id': unit['id'], 'quote': quote, 'role': 'support'}]}

    def __call__(self, **request):
        messages, stage = request['messages'], None
        text = messages[-1]['content']
        if messages[0]['content'].startswith('只返回紧凑 JSON 对象，字段 title,summary'):
            stage = 'preparation'
            output = {'title': 'Glass conservatory visit', 'summary': self.case['answer'],
                'facts': [{'text': self.case['answer'], 'evidence': {'quote': self.case['quote']}}],
                'topics': [], 'todos': [], 'uncertainties': [], 'people': [], 'dates': [],
                'suggestions': []}
        elif 'remaining_decisions' in text:
            stage, context = 'decision', json.loads(text)
            self.contexts.append(deepcopy(context))
            assert CANDIDATE_ONLY not in json.dumps(context, ensure_ascii=False)
            output = {'action': 'search', 'source': 'memory', 'query': context['question'],
                      'depth': 'original', 'ids': [], 'questions': []}
            if context['round'] == 1:
                assert context['steps'] == []
                assert any(self.case['quote'] in row['text'] for row in context['materials'])
            else:
                assert context['round'] == 2
                material = next(row for row in context['materials'] if self.case['quote'] in row['text'])
                output.update(action='answer', query='', ids=[material['id']])
        elif 'evidence_answer_contract' in text:
            stage, payload = 'proposal', json.loads(text)
            if self.mode == 'provider_failure':
                self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': None})
                raise TimeoutError('synthetic_general_proposal_timeout')
            statement = self._statement(payload)
            output = {'requirements': [{'text': self.case['question'], 'status': 'supported',
                       'statement_indices': [1]}], 'statements': [statement], 'issues': []}
            if self.mode in {'missing', 'conflict', 'attribution'}:
                output['requirements'][0].update(status='missing' if self.mode == 'missing' else 'uncertain',
                                                 statement_indices=[])
                output['statements'][0]['text'] = CANDIDATE_ONLY
                output['issues'] = [{'reason': self.mode, 'requirement_indices': [1],
                    'unit_ids': [statement['evidence'][0]['unit_id']],
                    'detail': 'The synthetic control requires investigation.'}]
            elif self.mode == 'bad_quote':
                output['statements'][0]['evidence'][0]['quote'] = 'This quotation does not exist in the source.'
            elif self.mode == 'invalid_date':
                output['statements'][0]['text'] += ' This happened on 2025-07-01.'
            else:
                assert self.mode == 'accepted'
        else:
            assert '资料：\n' in text
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            statement = self._statement(payload)
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            if stage == 'first':
                output = {'facts': [statement], 'unreviewed': {'scope': 'all_unreferenced',
                          'reason': 'Other dialogue concerns exhibits and drawing.'}}
            else:
                statement['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt',
                    'reason': 'The quoted source supports the same participant statement.'}]
                output = {'statements': [statement], 'uncertain': [], 'excluded_facts': [],
                          'unused': {'scope': 'all_unreferenced',
                                     'reason': 'Other dialogue concerns exhibits and drawing.'}}
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        if stage == 'proposal' and self.after_proposal is not None:
            self.after_proposal()
        raw = json.dumps(output, ensure_ascii=False)
        usage = None if stage == 'proposal' and not self.report_usage else {
            'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}
        if request.get('stream'):
            def chunks():
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       **({'usage': usage} if usage is not None else {})}
            return chunks()
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': raw}}], **({'usage': usage} if usage is not None else {})}


@pytest.fixture
def evidence_flow_factory(tmp_path, monkeypatch):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    connections = []

    def local_only(original):
        def connect(sock, address):
            if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}:
                return original(sock, address)
            connections.append(address)
            raise AssertionError('evidence_answer_external_network_forbidden')
        return connect

    monkeypatch.setattr(socket.socket, 'connect', local_only(socket.socket.connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', local_only(socket.socket.connect_ex))
    import litellm  # noqa: F401

    with ExitStack() as opened:
        def make(language='en', *, mode='accepted', upper=False, report_usage=True):
            case = CASES[language]
            root = tmp_path / (language + ('-upper' if upper else '-raw'))
            (root / 'config').mkdir(parents=True)
            code = Path(__file__).resolve().parents[3]
            shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
            monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
            monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
            from backend.memory_app.app import create_app
            from backend.memory_app.model_config import ModelConfiguration
            from backend.memory_app.source_egress import SourceEgressService
            from backend.memory_app.storage_authority import resolve_recognition_document_store
            from backend.recognition import WorkScope
            from backend.security.secrets import InMemorySecretStore
            from backend.shared.deployment import DeploymentLayout
            from backend.shared.job_runtime import build_rebuild_job_repository
            from core.ingestion_core import ObjectStoreSourceRegistrar
            from core.product_core.workbench_source_intake import CaptureWorkbenchTextSource

            records, namespace = resolve_recognition_document_store(root)
            assert namespace == 'default'
            transport = _SyntheticTransport(case, mode=mode, report_usage=report_usage)
            models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
            models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'synthetic-deepseek-evidence-answer', 'api_key': 'synthetic-only',
                'allow_remote': True, 'enabled': True, 'expected_revision': 0})
            collections = []

            def build():
                app = create_app(runtime_root=root, model_configuration=models,
                                 deployment_layout=DeploymentLayout('desktop', root))
                original = app.state.workspace_domains.query._collect_candidates

                def observed(*args, **kwargs):
                    result = original(*args, **kwargs)
                    collections.append({'question': args[1], 'layers': kwargs.get('only_layers'),
                        'rows': [(row['kind'], row['id'], row['layer']) for row in result['candidates']]})
                    return result

                monkeypatch.setattr(app.state.workspace_domains.query, '_collect_candidates', observed)
                return app

            app = build()
            http = opened.enter_context(TestClient(app))
            query, authority = app.state.workspace_domains.query, SourceEgressService(records)
            scope = WorkScope('local-user', PROJECT)
            sources, prepared = [], None
            if not upper:
                jobs = build_rebuild_job_repository(root, query.source_store)
                capture = CaptureWorkbenchTextSource(
                    source_registrar=ObjectStoreSourceRegistrar(query.source_store,
                        namespace_id=namespace, project_id=PROJECT, created_at=CAPTURED_AT),
                    job_repository=jobs, namespace_id=namespace, now=CAPTURED_AT)
                result = capture.execute(title='Glass conservatory conversation', content=case['dialogue'])
                assert result.status == 'captured' and result.published_output_kinds == ('source',)
                assert result.library_bridge_item.memory_publication_state == 'not_started'
                assert jobs.get(result.job_id)['status'] == 'completed'
                source_id = result.source_id
                source = query.source_store.read('sources', source_id)
                revision = query.source_store.revision('sources', source_id)
                assert source['metadata']['content'] == case['dialogue'].strip()
                assert source['trust_status'] == 'user_confirmed'
                sources.append(source_id)
                owner = {'type': 'original_source', 'id': source_id, 'revision': revision}
                assert records.list('documents') == records.list('recognitions') == ()
                assert records.list('recognition_experiences') == () and transport.calls == []
            else:
                added = http.post('/api/workspace/v1/items/text', json={
                    'project_id': PROJECT, 'text': case['dialogue']})
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
                document = app.state.recognition_documents.read(publication['document_id'])
                markdown = app.state.recognition_documents.markdown(document['id'])
                assert case['answer'] in markdown
                extracted = http.post(path + '/recognition', json={'project_id': PROJECT})
                assert extracted.status_code == 200, extracted.text
                ids = extracted.json()
                candidate = records.read('recognition_candidates', ids['candidate_id'])
                experience = records.read('recognition_experiences', ids['experience_id'])
                assert candidate.payload['state'] == 'pending'
                assert candidate.payload['content'] == markdown.strip()
                assert candidate.payload['source_experience_ids'] == [ids['experience_id']]
                assert experience.payload['provenance']['source_refs'] == [
                    {'type': 'document', 'id': document['id'], 'revision': document['revision']}]
                edited = http.patch(f'/api/recognition/candidates/{candidate.object_id}/draft', json={
                    'project_id': PROJECT, 'expected_revision': candidate.revision,
                    'content': case['answer'], 'conditions': []})
                assert edited.status_code == 200, edited.text
                draft = edited.json()
                assert draft['revision'] == candidate.revision + 1 and draft['conditions'] == []
                reviewed = http.patch(f'/api/recognition/candidates/{candidate.object_id}', json={
                    'project_id': PROJECT, 'expected_revision': draft['revision'],
                    'decision': 'approve', 'content': case['answer']})
                assert reviewed.status_code == 200, reviewed.text
                recognition = reviewed.json()
                assert recognition['content'] == case['answer']
                assert records.read('recognition_candidates', candidate.object_id).payload['state'] == 'published'
                marker = records.read('v2_insight_validity', recognition['id'])
                version = records.read('recognition_versions', recognition['id'] + '~v1')
                assert marker.payload['valid_from'] == version.payload['snapshot']['created_at']
                owner = {'type': 'recognition', 'id': recognition['id'], 'revision': recognition['revision']}
                prepared = {'document': document, 'recognition': recognition,
                            'candidate_id': candidate.object_id, 'experience_id': ids['experience_id']}
                assert [row['stage'] for row in transport.calls] == ['preparation']
            snapshot = authority.snapshot(scope, [owner])
            authority.require(snapshot, 'generation')
            authority.validate_snapshot(scope, snapshot)
            query.retrieval_index.bootstrap()

            def revoke():
                authority.set_policy(scope, owner['type'], owner['id'], owner['revision'], 0, [])

            return SimpleNamespace(root=root, app=app, http=http, query=query, records=records,
                case=case, transport=transport, collections=collections, sources=sources,
                prepared=prepared, build=build, revoke=revoke, authority=authority,
                snapshot=snapshot, scope=scope)

        yield make
    assert connections == []


def _ask(flow, key, *, retrieve='@12', compose='@36', http=None):
    from backend.memory_app.v2.policies import override

    with override(retrieve=retrieve, compose=compose):
        return (http or flow.http).post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
            json={'project_id': PROJECT, 'text': flow.case['question'], 'intent': 'ask'})


def _native(flow, turn_id, stages):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    assert [row['stage'] for row in flow.transport.ask_calls] == stages
    store = flow.app.state.ai_turn_store
    request = store.get_request(turn_id)
    assert request['policy_versions']['retrieve'] == '@12'
    assert request['policy_versions']['compose'] == '@36'
    assert request['input']['text'] == flow.case['question']
    groups = kernel_call_groups(flow.root, turn_id=turn_id, project=PROJECT,
                               records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and len(groups[0]['calls']) == len(stages)
    assert all(row['status'] == 'completed' for row in groups[0]['calls'])
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        wires = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                   'WHERE turn_id=?', (turn_id,)).fetchall()
    assert wires == [('terminal', 'succeeded')] * len(stages)
    return store, groups[0]


def _one_collection(flow):
    assert len(flow.collections) == 1
    initial = flow.collections[0]
    assert initial['question'] == flow.case['question'] and initial['rows']
    assert initial['layers'] is None or set(initial['layers']) == {'L3', 'L2', 'L1', 'L0'}


def _report_actual_native(flow, saved, selected_input):
    """Export real native rows and actual synthetic wires without replacing proof."""
    module_path = Path(__file__).resolve().parents[3] / 'tools/rag_report.py'
    spec = importlib.util.spec_from_file_location('evidence_flow_rag_report', module_path)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    turn_id = saved['turn']['id']
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        connection.row_factory = sqlite3.Row
        native = {
            'turn': [dict(row) for row in connection.execute(
                'SELECT turn_id,request_json FROM ai_turns WHERE turn_id=?', (turn_id,))],
            'events': [dict(row) for row in connection.execute(
                'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))],
            'attempts': [dict(row) for row in connection.execute(
                'SELECT r.*,d.payload_json AS dispatch_payload,t.payload_json AS terminal_receipt '
                'FROM ai_model_attempt_reservations r '
                'JOIN ai_turn_payloads d ON d.payload_ref=r.dispatch_payload_ref '
                'LEFT JOIN ai_turn_payloads t ON t.payload_ref=r.terminal_receipt_ref '
                'WHERE r.turn_id=? ORDER BY r.rowid', (turn_id,))],
            'immutable': [{'kind': row[0], 'payload': json.loads(row[1])} for row in connection.execute(
                'SELECT kind,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (turn_id,))],
        }
    for attempt in native['attempts']:
        for key in ('dispatch_payload', 'terminal_receipt'):
            attempt[key] = json.loads(attempt[key])
    directory = flow.root / 'report-export' / 'synthetic-dialogue'
    directory.mkdir(parents=True)
    http, wires = [], []
    assert len(native['attempts']) == len(flow.transport.ask_calls)
    for number, call in enumerate(flow.transport.ask_calls, 1):
        wire = {'number': number, 'label': 'synthetic-dialogue:1', 'turn_id': turn_id}
        http.append({'completion': wire})
        wires.append(wire)
        (directory.parent / f'messages-{number}.json').write_text(
            json.dumps(call['messages'], ensure_ascii=False), encoding='utf-8')
    question_path = directory / 'question-1.json'
    question_path.write_text(json.dumps({'conversation_id': 'synthetic-dialogue', 'qa_index': 1,
        'http_status': 200, 'seconds': 0.0, 'physical_http': len(http),
        'source': {'revision': 'synthetic-normal-service'}, 'body': saved, 'native': native},
        ensure_ascii=False), encoding='utf-8')
    reader = report.SourceReader()
    row = report.heldout_question(reader, question_path, http, wires, '@36')
    catalog = report.message_catalog(selected_input['messages'], json_payload=True)
    expected = {identity for unit in catalog['units']
                for identity in re.findall(r'\bD\d+:\d+\b', unit['text'])}
    assert expected and row['selected'] == expected
    assert row['status'] == 'answered' and row['input_state'] == 'recorded'
    assert len(row['attempts']) == len(flow.transport.ask_calls)
    reader.verify_unchanged()
    return row


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_general_original_dialogue_uses_one_generation_and_frozen_restart(evidence_flow_factory, language):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.v2.evidence_answer import EvidenceAnswerOutput
    from backend.memory_app.v2.policies import version

    flow = evidence_flow_factory(language)
    assert (version('retrieve'), version('compose')) == ('@9', '@34')
    response = _ask(flow, 'general-direct')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, answer = saved['turn'], saved['turn']['receipt']['ask']
    assert flow.case['answer'] in answer['answer'] and answer['no_match'] is False
    assert [(row['id'], row['layer']) for row in answer['citations']] == [(flow.sources[0], 'source')]
    assert flow.case['quote'] in answer['citations'][0]['quote']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['route'] == 'direct' and route['reason'] == 'evidence_answer_accepted'
    assert route['generation_invocations'] == 1 and route['decision_invocations'] == 0
    assert route['probe_collections'] == 1 and route['remainder_collections'] == 0
    assert flow.transport.contexts == []
    _one_collection(flow)
    assert {layer for _, _, layer in flow.collections[0]['rows']} == {'L0'}
    store, group = _native(flow, turn['id'], ['proposal'])
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-' + INVOCATION)
    result = store.get_immutable_payload(turn['id'], 'answer-model-result-' + INVOCATION)
    assert frozen is not None and result is not None
    assert frozen[1]['response_schema'] == EvidenceAnswerOutput.model_json_schema()
    assert frozen[1]['purpose'] == 'primary'
    assert result[1]['output'] == flow.transport.ask_calls[0]['output']
    assert read_answer_input(store, turn['id']) == frozen
    assert group['answer_input'] == frozen[1]
    _report_actual_native(flow, saved, frozen[1])
    for invocation in ('answer', REVIEW_KEY, 'rag-decision-1', 'raw-answer-v1'):
        assert store.get_immutable_payload(turn['id'], 'answer-model-input-' + invocation) is None
    assert answer['model_usage'] == {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}
    counts = len(flow.transport.calls), len(flow.collections)
    assert _ask(flow, 'general-direct', retrieve='@9', compose='@34').json() == saved
    with TestClient(flow.build()) as restarted:
        replay = _ask(flow, 'general-direct', retrieve='@9', compose='@34', http=restarted)
        assert replay.status_code == 200 and replay.json() == saved
    assert (len(flow.transport.calls), len(flow.collections)) == counts
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


def test_normal_confirmed_upper_memory_can_supply_general_single_generation(evidence_flow_factory):
    from backend.memory_app.answer_proof import read_answer_input

    flow = evidence_flow_factory('zh', upper=True)
    response = _ask(flow, 'general-upper')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn = saved['turn']
    answer, identity = turn['receipt']['ask'], flow.prepared['recognition']['id']
    assert flow.case['answer'] in answer['answer']
    assert [(row['id'], row['layer']) for row in answer['citations']] == [(identity, 'insight')]
    assert answer['citations'][0]['quote'].startswith(flow.case['answer'])
    assert answer['trace'][0]['evidence_fast_path']['route'] == 'direct'
    _one_collection(flow)
    assert ('recognition', identity, 'L3') in flow.collections[0]['rows']
    store, group = _native(flow, turn['id'], ['proposal'])
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-' + INVOCATION)
    assert read_answer_input(store, turn['id']) == frozen and group['answer_input'] == frozen[1]
    assert len(flow.transport.calls) == 2  # Building upper memory is separately accounted.
    assert answer['model_usage'] == {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}


@pytest.mark.parametrize('mode', ['missing', 'conflict', 'attribution', 'bad_quote', 'invalid_date'])
def test_rejected_general_candidate_reuses_original_windows_and_keeps_real_cost(evidence_flow_factory, mode):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = evidence_flow_factory(mode=mode)
    response = _ask(flow, 'general-rejected-' + mode)
    assert response.status_code == 200, response.text
    saved = response.json()
    turn = saved['turn']
    answer = turn['receipt']['ask']
    assert flow.case['answer'] in answer['answer'] and CANDIDATE_ONLY not in answer['answer']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['route'] == 'investigate' and route['reason'] == 'evidence_output_rejected'
    assert route['generation_invocations'] == 1 and route['decision_invocations'] == 2
    assert route['probe_collections'] == 1 and route['remainder_collections'] == 0
    assert route['reused_queries'] == 1 and route['generation_usage_complete'] is True
    _one_collection(flow)
    store, group = _native(flow, turn['id'], ['proposal', 'decision', 'decision', 'first', 'review'])
    proposal = store.get_immutable_payload(turn['id'], 'answer-model-result-' + INVOCATION)
    assert proposal[1]['output'] == flow.transport.ask_calls[0]['output']
    review = store.get_immutable_payload(turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, turn['id']) == review and group['answer_input'] == review[1]
    if mode == 'missing':
        _report_actual_native(flow, saved, review[1])
    for index, context in enumerate(flow.transport.contexts, 1):
        assert CANDIDATE_ONLY not in json.dumps(context, ensure_ascii=False)
        frozen = store.get_immutable_payload(turn['id'], f'answer-gap-input-rag-decision-{index}-v1')
        assert json.loads(frozen[1]['messages'][-1]['content']) == context
    assert answer['model_usage'] == {'input_tokens': 100, 'output_tokens': 100, 'total_tokens': 200}


def test_unknown_failed_proposal_usage_remains_unknown(evidence_flow_factory):
    flow = evidence_flow_factory(mode='missing', report_usage=False)
    response = _ask(flow, 'general-unknown-usage')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    assert answer['trace'][0]['evidence_fast_path']['generation_usage_complete'] is False
    assert answer['model_usage']['observed_only'] is True
    assert answer['model_usage']['total_tokens'] == 160
    _, group = _native(flow, turn['id'], ['proposal', 'decision', 'decision', 'first', 'review'])
    assert sum((row.get('usage') or {}).get('total_tokens', 0) for row in group['calls']) == 160


def test_source_authority_change_aborts_without_investigation_or_publication(evidence_flow_factory):
    from backend.memory_app.answer_proof import read_answer_input
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    from backend.recognition import RecognitionError

    flow = evidence_flow_factory()
    flow.transport.after_proposal = flow.revoke
    response = _ask(flow, 'general-authority-revoked')
    assert response.status_code in {409, 502}, response.text
    assert [row['stage'] for row in flow.transport.ask_calls] == ['proposal']
    assert flow.transport.contexts == []
    _one_collection(flow)
    [group] = kernel_call_groups(flow.root, project=PROJECT, records=flow.records)
    store = flow.app.state.ai_turn_store
    assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
    assert read_answer_input(store, group['turn_id']) is None
    with pytest.raises(RecognitionError):
        flow.authority.validate_snapshot(flow.scope, flow.snapshot)


def test_failed_provider_retains_physical_attempts_before_old_investigation(evidence_flow_factory):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    flow = evidence_flow_factory(mode='provider_failure')
    response = _ask(flow, 'general-provider-failure')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    stages = [row['stage'] for row in flow.transport.ask_calls]
    attempts = stages.count('proposal')
    assert attempts >= 1 and stages == ['proposal'] * attempts + ['decision', 'decision', 'first', 'review']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['route'] == 'investigate' and route['reason'] == 'evidence_generation_failed'
    assert route['generation_invocations'] == 1 and route['generation_usage_complete'] is False
    assert route['generation_physical_attempts'] == attempts and route['reused_queries'] == 1
    _one_collection(flow)
    [group] = kernel_call_groups(flow.root, turn_id=turn['id'], project=PROJECT,
                                records=flow.records, include_answer_input=True)
    assert len(group['calls']) == 5  # Logical calls and physical provider attempts stay distinct.
    [failed] = [row for row in group['calls'] if row['status'] == 'failed']
    assert failed['error_code'] and not failed['usage'] and failed['usage_status'] == 'not_recorded'
    assert sum(row['status'] == 'completed' for row in group['calls']) == 4
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        wires = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                   'WHERE turn_id=?', (turn['id'],)).fetchall()
    assert len(wires) == len(stages)
    assert wires.count(('terminal', 'failed_transport')) == attempts
    assert wires.count(('terminal', 'succeeded')) == 4
    assert answer['model_usage']['observed_only'] is True and answer['model_usage']['total_tokens'] == 160
    store = flow.app.state.ai_turn_store
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-' + INVOCATION) is None
    assert read_answer_input(store, turn['id']) == store.get_immutable_payload(
        turn['id'], 'answer-model-input-' + REVIEW_KEY)
