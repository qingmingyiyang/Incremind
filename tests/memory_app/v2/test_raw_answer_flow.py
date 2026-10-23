"""Raw fast answers through real capture, retrieval, Workbench and native receipts.

Only provider responses and their token counts are synthetic. Source ingestion,
source authority, retrieval, immutable answer inputs and wire accounting are real.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


PROJECT = 'raw-answer-service'
CAPTURED_AT = '2026-10-22T08:00:00Z'
CASES = {
    'zh': {'question': '星河项目的重试上限是多少？', 'body': '星河项目的重试上限是3次。',
           'other': '星河项目的重试上限是4次。', 'title': '星河项目原始记录',
           'subject': '星河项目', 'predicate': '重试上限', 'value': '3次'},
    'en': {'question': 'What is the retry limit of Project Cedar?',
           'body': 'The retry limit of Project Cedar is 3 retries.',
           'other': 'The retry limit of Project Cedar is 4 retries.',
           'title': 'Project Cedar original record',
           'subject': 'Project Cedar', 'predicate': 'retry limit', 'value': '3 retries'},
    'en_production_owner': {'question': 'In production, What is the owner of Project Cedar?',
           'body': 'In production, The owner of Project Cedar is Alice.',
           'other': 'The owner of Project Cedar is Bob.', 'title': 'Project Cedar original owner records',
           'subject': 'Project Cedar', 'predicate': 'owner', 'value': 'Alice'},
    'en_unqualified_owner': {'question': 'What is the owner of Project Cedar?',
           'body': 'In production, The owner of Project Cedar is Alice.',
           'other': 'The owner of Project Cedar is Bob.', 'title': 'Project Cedar original owner records',
           'subject': 'Project Cedar', 'predicate': 'owner', 'value': 'Alice'},
    'zh_latest': {'question': '星河项目的最新重试上限是多少？',
           'body': '星河项目的最新重试上限是3次。', 'title': '星河项目原始记录',
           'subject': '星河项目', 'predicate': '最新重试上限', 'value': '3次'},
    'en_latest': {'question': 'What is the latest retry limit of Project Cedar?',
           'body': "Project Cedar's latest retry limit is 3 retries.",
           'title': 'Project Cedar original record',
           'subject': 'Project Cedar', 'predicate': 'latest retry limit', 'value': '3 retries'},
    'en_newest': {'question': 'What is the newest retry limit of Project Cedar?',
           'body': "Project Cedar's newest retry limit is 3 retries.",
           'title': 'Project Cedar original record',
           'subject': 'Project Cedar', 'predicate': 'newest retry limit', 'value': '3 retries'},
}


class _SyntheticTransport:
    def __init__(self, case, *, raw_mode='accepted', fallback='clarify', report_raw_usage=True):
        self.case = case
        self.raw_mode = raw_mode
        self.fallback = fallback
        self.report_raw_usage = report_raw_usage
        self.calls = []
        self.contexts = []

    def __call__(self, **request):
        messages = request['messages']
        text = messages[-1]['content']
        if 'remaining_decisions' in text:
            stage = 'decision'
            context = json.loads(text)
            self.contexts.append(deepcopy(context))
            output = {'action': 'search', 'source': 'memory', 'query': context['question'],
                      'depth': 'original', 'ids': [], 'questions': []}
            if context['round'] == 1:
                assert context['steps'] == []
            else:
                assert context['round'] == 2
                assert self.case['body'] in json.dumps(context['materials'], ensure_ascii=False)
                assert all(row['layer'] == 'L0' for row in context['materials'])
                if self.fallback == 'answer':
                    selected = next(row for row in context['materials'] if self.case['body'] in row['text'])
                    output.update(action='answer', query='', ids=[selected['id']])
                else:
                    output.update(action='clarify', query='', questions=['请确认采用哪条原始记录。'])
        elif any(row.get('role') == 'system' and 'raw-answer-v1' in row['content'] for row in messages):
            stage = 'raw'
            if self.raw_mode == 'provider_failure':
                # Every actual transport entry is retained, including retries.
                self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': None})
                raise TimeoutError('synthetic_raw_provider_timeout')
            output = self._raw_output(text)
        else:
            assert '资料：\n' in text
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            unit = next(row for row in payload['evidence_catalog']['units'] if self.case['body'] in row['text'])
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            fact = {'kind': 'fact', 'text': self.case['body'], 'evidence': [
                {'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}], 'date_refs': []}
            if stage == 'first':
                output = {'facts': [fact], 'unreviewed': {
                    'scope': 'all_unreferenced', 'reason': 'The requested fact is the only claim.'}}
            else:
                fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt',
                                          'reason': 'The original supports the literal claim.'}]
                output = {'statements': [fact], 'unused': {
                    'scope': 'all_unreferenced', 'reason': 'The requested fact is the only claim.'},
                    'uncertain': [], 'excluded_facts': []}
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        raw = json.dumps(output, ensure_ascii=False)
        usage = ({'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}
                 if stage != 'raw' or self.report_raw_usage else None)
        if request.get('stream'):
            def chunks():
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       **({'usage': usage} if usage is not None else {})}
            return chunks()
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': raw}}], **({'usage': usage} if usage is not None else {})}

    def _raw_output(self, text):
        payload = json.loads(text)
        contract = payload['raw_question_contract']
        [record] = contract['records']
        assert {key: record[key] for key in ('subject', 'predicate', 'value')} == {
            key: self.case[key] for key in ('subject', 'predicate', 'value')}
        assert record['quote'] == self.case['body']
        unit = next(row for row in payload['evidence_catalog']['units'] if row['id'] == record['unit_id'])
        assert unit['text'] == self.case['body']
        output = {'status': 'answer', 'answer': self.case['value'], 'unit_id': unit['id'],
                  'quote': self.case['body'], 'coverage': {key: record[key]
                    for key in ('subject', 'predicate', 'value', 'condition', 'time')}}
        if self.raw_mode == 'missing_coverage':
            # Schema-valid strings cannot substitute for a locally verified value.
            output['coverage']['value'] = ''
        elif self.raw_mode == 'wrong_quote':
            output['quote'] = self.case['value']
        else:
            assert self.raw_mode == 'accepted'
        return output


@pytest.fixture
def raw_flow_factory(tmp_path, monkeypatch):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    connections = []

    def local_connections_only(original):
        def connect(sock, address):
            if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}:
                return original(sock, address)
            connections.append(address)
            raise AssertionError('raw_answer_network_forbidden')
        return connect

    monkeypatch.setattr(socket.socket, 'connect', local_connections_only(socket.socket.connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', local_connections_only(socket.socket.connect_ex))
    import litellm  # noqa: F401
    with ExitStack() as opened:
        def make(language='zh', *, raw_mode='accepted', fallback='clarify', conflict=False,
                 report_raw_usage=True):
            case = CASES[language]
            root = tmp_path / language
            (root / 'config').mkdir(parents=True)
            code = Path(__file__).resolve().parents[3]
            shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
            monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
            monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
            from backend.memory_app.app import create_app
            from backend.memory_app.model_config import ModelConfiguration
            from backend.memory_app.original_sources import resolve_turn_material
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
            transport = _SyntheticTransport(case, raw_mode=raw_mode, fallback=fallback,
                                             report_raw_usage=report_raw_usage)
            models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
            models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'synthetic-raw-answer', 'api_key': 'synthetic-only',
                'allow_remote': True, 'enabled': True, 'expected_revision': 0})
            collections = []

            def build():
                instance = create_app(runtime_root=root, model_configuration=models,
                                      deployment_layout=DeploymentLayout('desktop', root))
                original_collect = instance.state.workspace_domains.query._collect_candidates

                def observed_collect(*args, **kwargs):
                    collected = original_collect(*args, **kwargs)
                    collections.append({'question': args[1], 'layers': kwargs.get('only_layers'),
                        'rows': [(row['kind'], row['id'], row['layer']) for row in collected['candidates']]})
                    return collected

                monkeypatch.setattr(instance.state.workspace_domains.query, '_collect_candidates', observed_collect)
                return instance

            app = build()
            query = app.state.workspace_domains.query
            jobs = build_rebuild_job_repository(root, query.source_store)
            capture = CaptureWorkbenchTextSource(
                source_registrar=ObjectStoreSourceRegistrar(query.source_store,
                    namespace_id=namespace, project_id=PROJECT, created_at=CAPTURED_AT),
                job_repository=jobs, namespace_id=namespace, now=CAPTURED_AT)
            sources = []
            authority = SourceEgressService(records)
            scope = WorkScope('local-user', PROJECT)
            for body in ([case['body'], case['other']] if conflict else [case['body']]):
                captured = capture.execute(title=case['title'], content=body)
                assert captured.status == 'captured'
                assert captured.published_output_kinds == ('source',)
                assert captured.library_bridge_item.memory_publication_state == 'not_started'
                job = jobs.get(captured.job_id)
                assert job['status'] == 'completed'
                assert [step['name'] for step in job['steps']] == ['persist_source']
                assert [output['kind'] for output in job['published_outputs']] == ['source']
                source = query.source_store.read('sources', captured.source_id)
                revision = query.source_store.revision('sources', captured.source_id)
                assert source['project_id'] == PROJECT and source['metadata']['content'] == body
                assert source['trust_status'] == 'user_confirmed' and source['created_at'] == CAPTURED_AT
                assert revision >= 1 and source['content_hash'] == captured.content_hash
                material, roots = resolve_turn_material(records, scope, {
                    'type': 'original_source', 'id': captured.source_id,
                    'revision': revision, 'project_id': PROJECT})
                assert material['payload']['metadata']['content'] == body
                snapshot = authority.snapshot(scope, roots)
                authority.require(snapshot, 'generation')
                authority.validate_snapshot(scope, snapshot)
                sources.append(captured.source_id)
            query.retrieval_index.bootstrap()
            assert records.list('documents') == ()
            assert records.list('recognitions') == ()
            assert records.list('recognition_experiences') == ()
            assert app.state.recognition_service.retrieval_entries(scope=scope) == ()
            assert transport.calls == []  # Capturing raw material requires no preparation model call.
            http = opened.enter_context(TestClient(app))
            return SimpleNamespace(root=root, app=app, http=http, query=query, records=records,
                transport=transport, case=case, sources=sources, collections=collections, build=build)

        yield make
    assert connections == []


def _ask(flow, key, *, retrieve='@11', http=None):
    from backend.memory_app.v2.policies import override

    with override(retrieve=retrieve):
        return (http or flow.http).post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
            json={'project_id': PROJECT, 'text': flow.case['question'], 'intent': 'ask'})


def _native(flow, turn_id, stages, *, retrieve='@11', compose='@34'):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    assert [call['stage'] for call in flow.transport.calls] == stages
    store = flow.app.state.ai_turn_store
    request = store.get_request(turn_id)
    assert request['policy_versions']['retrieve'] == retrieve
    assert request['policy_versions']['compose'] == compose
    groups = kernel_call_groups(flow.root, turn_id=turn_id, project=PROJECT,
                               records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and len(groups[0]['calls']) == len(stages)
    assert all(call['status'] == 'completed' for call in groups[0]['calls'])
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        rows = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                  'WHERE turn_id=?', (turn_id,)).fetchall()
    assert rows == [('terminal', 'succeeded')] * len(stages)
    return store, groups[0]


def _one_real_raw_collection(flow):
    assert len(flow.collections) == 1
    collected = flow.collections[0]
    assert collected['question'] == flow.case['question']
    assert collected['layers'] is None or set(collected['layers']) == {'L3', 'L2', 'L1', 'L0'}
    assert collected['rows']
    assert {layer for _, _, layer in collected['rows']} == {'L0'}
    assert {identity for _, identity, _ in collected['rows']} == set(flow.sources)


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_raw_only_literal_fact_uses_one_generation_and_replays_frozen_version(raw_flow_factory, language):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.v2.policies import version
    from backend.memory_app.v2.raw_answer import RawAnswerOutput

    flow = raw_flow_factory(language)
    assert (version('retrieve'), version('compose')) == ('@9', '@34')
    response = _ask(flow, 'raw-direct')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, answer = saved['turn'], saved['turn']['receipt']['ask']
    assert flow.case['value'] in answer['answer'] and answer['no_match'] is False
    assert [(row['id'], row['layer']) for row in answer['citations']] == [(flow.sources[0], 'source')]
    assert answer['citations'][0]['quote'] == flow.case['body']
    raw = answer['trace'][0]['raw_fast_path']
    assert raw['route'] == 'direct' and raw['reason'] == 'raw_answer_accepted'
    assert raw['probe_collections'] == 1 and raw['remainder_collections'] == 0
    assert raw['decision_invocations'] == 0 and raw['generation_invocations'] == 1
    assert raw['generation_usage_complete'] is True
    assert flow.transport.contexts == []
    _one_real_raw_collection(flow)
    store, group = _native(flow, turn['id'], ['raw'])
    frozen_input = store.get_immutable_payload(turn['id'], 'answer-model-input-raw-answer-v1')
    frozen_result = store.get_immutable_payload(turn['id'], 'answer-model-result-raw-answer-v1')
    assert frozen_input is not None and frozen_result is not None
    assert frozen_input[1]['purpose'] == 'primary'
    assert frozen_input[1]['response_schema'] == RawAnswerOutput.model_json_schema()
    assert frozen_result[1]['output'] == flow.transport.calls[0]['output']
    assert read_answer_input(store, turn['id']) == frozen_input
    assert group['answer_input'] == frozen_input[1]
    for key in ('answer', REVIEW_KEY, 'rag-decision-1'):
        assert store.get_immutable_payload(turn['id'], 'answer-model-result-' + key) is None
    assert answer['model_usage'] == {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40}
    counts = len(flow.transport.calls), len(flow.collections)
    # An ordinary caller replays the original opt-in request rather than activating @11 globally.
    assert _ask(flow, 'raw-direct', retrieve='@9').json() == saved
    with TestClient(flow.build()) as restarted:
        replay = _ask(flow, 'raw-direct', retrieve='@10', http=restarted)
        assert replay.status_code == 200 and replay.json() == saved
    assert (len(flow.transport.calls), len(flow.collections)) == counts
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_calendar_candidate_preserves_raw_single_generation_and_frozen_replay(raw_flow_factory, compose):
    from backend.memory_app.answer_proof import read_answer_input
    from backend.memory_app.v2.policies import override, version

    flow = raw_flow_factory('en')
    with override(compose=compose):
        response = _ask(flow, 'raw-calendar-compatible')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn = saved['turn']
    store, group = _native(flow, turn['id'], ['raw'], compose=compose)
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-raw-answer-v1')
    assert frozen is not None and read_answer_input(store, turn['id']) == frozen
    assert group['answer_input'] == frozen[1]
    assert turn['receipt']['ask']['trace'][0]['raw_fast_path']['generation_invocations'] == 1
    counts = len(flow.transport.calls), len(flow.collections)
    with TestClient(flow.build()) as restarted:
        replay = _ask(flow, 'raw-calendar-compatible', retrieve='@9', http=restarted)
        assert replay.status_code == 200 and replay.json() == saved
    assert (len(flow.transport.calls), len(flow.collections)) == counts
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


@pytest.mark.parametrize('raw_mode,fallback,stages', [
    ('missing_coverage', 'clarify', ['raw', 'decision', 'decision']),
    ('wrong_quote', 'answer', ['raw', 'decision', 'decision', 'first', 'review']),
])
def test_rejected_raw_witness_keeps_cost_and_reuses_retrieval_before_investigation(
        raw_flow_factory, raw_mode, fallback, stages):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = raw_flow_factory(raw_mode=raw_mode, fallback=fallback)
    response = _ask(flow, 'raw-rejected-' + raw_mode)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    raw = answer['trace'][0]['raw_fast_path']
    assert raw['route'] == 'investigate' and raw['reason'] == 'raw_output_rejected'
    assert raw['generation_invocations'] == 1 and raw['decision_invocations'] == 2
    assert raw['generation_usage_complete'] is True and raw['reused_queries'] == 1
    assert raw['probe_collections'] == 1 and raw['remainder_collections'] == 0
    assert flow.case['body'] in json.dumps(flow.transport.contexts[0]['materials'], ensure_ascii=False)
    _one_real_raw_collection(flow)
    store, _ = _native(flow, turn['id'], stages)
    rejected = store.get_immutable_payload(turn['id'], 'answer-model-result-raw-answer-v1')
    assert rejected[1]['output'] == flow.transport.calls[0]['output']
    for number, context in enumerate(flow.transport.contexts, 1):
        frozen = store.get_immutable_payload(turn['id'], f'answer-gap-input-rag-decision-{number}-v1')
        assert json.loads(frozen[1]['messages'][-1]['content']) == context
    assert answer['model_usage'] == {'input_tokens': 20 * len(stages),
        'output_tokens': 20 * len(stages), 'total_tokens': 40 * len(stages)}
    if fallback == 'answer':
        assert flow.case['body'] in answer['answer']
        assert read_answer_input(store, turn['id']) == store.get_immutable_payload(
            turn['id'], 'answer-model-input-' + REVIEW_KEY)
    else:
        assert answer['citations'] == [] and flow.case['body'] not in answer['answer']
        assert answer['trace'][0]['agentic']['status'] == 'clarify'


@pytest.mark.parametrize('language', ['zh', 'en_production_owner', 'en_unqualified_owner'])
def test_conflicting_or_incomplete_raw_originals_skip_generation_and_investigate(raw_flow_factory, language):
    flow = raw_flow_factory(language, conflict=True)
    response = _ask(flow, 'raw-conflict')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    raw = answer['trace'][0]['raw_fast_path']
    assert raw['route'] == 'investigate' and raw['reason'] == 'raw_evidence_not_complete'
    assert raw['generation_invocations'] == 0
    assert raw['decision_invocations'] == 2 and raw['reused_queries'] == 1
    assert raw['probe_collections'] == 1 and raw['remainder_collections'] == 0
    _one_real_raw_collection(flow)
    _native(flow, turn['id'], ['decision', 'decision'])
    for body in (flow.case['body'], flow.case['other']):
        assert body in json.dumps(flow.transport.contexts[1]['materials'], ensure_ascii=False)
        assert body not in answer['answer']
    assert answer['citations'] == []


@pytest.mark.parametrize('retrieve', ['@9', '@10'])
def test_default_and_previous_candidate_do_not_call_raw_generation(raw_flow_factory, retrieve):
    flow = raw_flow_factory()
    response = _ask(flow, 'raw-disabled-' + retrieve.removeprefix('@'), retrieve=retrieve)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    assert 'raw_fast_path' not in turn['receipt']['ask']['trace'][0]
    store, _ = _native(flow, turn['id'], ['decision', 'decision'], retrieve=retrieve)
    assert store.get_immutable_payload(turn['id'], 'answer-model-input-raw-answer-v1') is None


@pytest.mark.parametrize('language', ['zh_latest', 'en_latest', 'en_newest'])
def test_freshness_requests_keep_original_wording_and_enter_investigation(raw_flow_factory, language):
    from backend.memory_app.v2.policies import version

    flow = raw_flow_factory(language)
    response = _ask(flow, 'raw-freshness-' + language)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    raw = answer['trace'][0]['raw_fast_path']
    assert raw['route'] == 'investigate' and raw['reason'] == 'unsupported_question'
    assert raw['generation_invocations'] == 0 and raw['generation_physical_attempts'] == 0
    assert raw['probe_collections'] == 0 and raw['decision_invocations'] == 2
    assert answer['trace'][0]['agentic']['status'] == 'clarify'
    _one_real_raw_collection(flow)
    store, _ = _native(flow, turn['id'], ['decision', 'decision'])
    assert store.get_request(turn['id'])['input']['text'] == flow.case['question']
    assert store.get_immutable_payload(turn['id'], 'answer-model-input-raw-answer-v1') is None
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-raw-answer-v1') is None
    for number, context in enumerate(flow.transport.contexts, 1):
        assert context['question'] == flow.case['question']
        frozen = store.get_immutable_payload(turn['id'], f'answer-gap-input-rag-decision-{number}-v1')
        assert json.loads(frozen[1]['messages'][-1]['content']) == context
    assert any(flow.case['body'] in row['text'] for row in flow.transport.contexts[1]['materials'])
    assert answer['citations'] == [] and flow.case['value'] not in answer['answer']
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


def test_unknown_raw_usage_remains_unknown_after_local_rejection(raw_flow_factory):
    flow = raw_flow_factory(raw_mode='missing_coverage', report_raw_usage=False)
    response = _ask(flow, 'raw-unknown-usage')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    assert answer['trace'][0]['raw_fast_path']['generation_usage_complete'] is False
    assert answer['model_usage'].get('observed_only') is True
    assert answer['model_usage'].get('total_tokens') == 80
    store, group = _native(flow, turn['id'], ['raw', 'decision', 'decision'])
    raw_result = store.get_immutable_payload(turn['id'], 'answer-model-result-raw-answer-v1')
    assert raw_result[1]['output'] == flow.transport.calls[0]['output']
    assert sum((call.get('usage') or {}).get('total_tokens', 0) for call in group['calls']) == 80


def test_raw_provider_failure_retains_every_attempt_and_unknown_usage_before_investigation(raw_flow_factory):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    flow = raw_flow_factory(raw_mode='provider_failure')
    response = _ask(flow, 'raw-provider-failure')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    raw = answer['trace'][0]['raw_fast_path']
    assert raw['route'] == 'investigate' and raw['reason'] == 'raw_generation_failed'
    assert raw['generation_invocations'] == 1 and raw['decision_invocations'] == 2
    assert raw['generation_usage_complete'] is False and raw['reused_queries'] == 1
    _one_real_raw_collection(flow)
    stages = [call['stage'] for call in flow.transport.calls]
    attempts = stages.count('raw')
    assert attempts >= 1 and stages == ['raw'] * attempts + ['decision', 'decision']
    store = flow.app.state.ai_turn_store
    assert store.get_immutable_payload(turn['id'], 'answer-model-input-raw-answer-v1') is not None
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-raw-answer-v1') is None
    groups = kernel_call_groups(flow.root, turn_id=turn['id'], project=PROJECT, records=flow.records)
    assert len(groups) == 1 and len(groups[0]['calls']) == 3  # One raw logical call may have several wires.
    failed = [call for call in groups[0]['calls'] if call['status'] == 'failed']
    assert len(failed) == 1 and failed[0]['error_code']
    assert not failed[0]['usage'] and failed[0]['usage_status'] == 'not_recorded'
    assert sum(call['status'] == 'completed' for call in groups[0]['calls']) == 2
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        wires = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                   'WHERE turn_id=?', (turn['id'],)).fetchall()
    assert len(wires) == len(flow.transport.calls)
    assert wires.count(('terminal', 'failed_transport')) == attempts
    assert wires.count(('terminal', 'succeeded')) == 2
    assert answer['model_usage'].get('observed_only') is True
    assert answer['model_usage']['total_tokens'] == 80
    assert answer['citations'] == []
