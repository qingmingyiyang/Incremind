"""Actual SDK/SSE decoding through Workbench; HTTP bytes alone are synthetic.

The normal capture fixture provides the source owner and real retrieval. Neither
the gateway nor the SDK response/stream classes are replaced by dictionaries.
"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.memory_app.v2.test_evidence_answer_flow import (
    CANDIDATE_ONLY, INVOCATION, PROJECT, _SyntheticTransport,
    evidence_flow_factory as _capture_fixture,
)

evidence_flow_factory = _capture_fixture


USAGE = {'prompt_tokens': 23, 'completion_tokens': 11, 'total_tokens': 34}
NORMALIZED_USAGE = {'input_tokens': 23, 'output_tokens': 11, 'total_tokens': 34}
PRIVATE_FRAGMENT = 'SDK_PRIVATE_RESPONSE_FRAGMENT'


class _ProviderBytes(httpx.SyncByteStream):
    def __init__(self, content):
        self.content, self.closed = content, False
        self.close_calls = 0

    def __iter__(self):
        # Split SSE frames and UTF-8 code points at the byte transport boundary.
        for offset in range(0, len(self.content), 37):
            yield self.content[offset:offset + 37]

    def close(self):
        self.close_calls += 1
        self.closed = True


class _SdkProvider:
    def __init__(self, case, mode):
        self.mode = mode
        self.synthetic = _SyntheticTransport(case, mode='missing' if mode == 'missing' else 'accepted')
        self.wires, self.streams, self.clients, self.sdk_results = [], [], [], []

    def client(self):
        client = httpx.Client(transport=httpx.MockTransport(self.http), trust_env=False)
        self.clients.append(client)
        return client

    def http(self, request):
        assert request.method == 'POST' and request.url.host == 'synthetic.invalid'
        body = json.loads(request.content)
        # Reuse only synthetic response content, never its fake transport shape.
        response = self.synthetic(messages=body['messages'], stream=False)
        stage = self.synthetic.calls[-1]['stage']
        raw = response['choices'][0]['message']['content']
        if stage == 'proposal':
            if self.mode == 'json_invalid':
                raw = '{' + PRIVATE_FRAGMENT
            elif self.mode == 'schema_invalid':
                raw = json.dumps({'requirements': [], 'statements': [], 'issues': [],
                                  'unrecognized_field': PRIVATE_FRAGMENT})
            elif self.mode in {'eof', 'eof_usage'}:
                raw = '{"requirements":['
        wire = {'stage': stage, 'request': deepcopy(body), 'raw': raw}
        self.wires.append(wire)
        if body.get('stream') is not True:
            # This is permitted only if the frozen retry policy explicitly
            # switches a later transport. The proposal's first wire must stream.
            return httpx.Response(200, json={'id': 'sdk-synthetic', 'object': 'chat.completion',
                'created': 1, 'model': body['model'], 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': raw}, 'finish_reason': 'stop'}],
                'usage': USAGE})

        def chunk(delta, *, choices=True, finish=None, usage=None):
            value = {'id': 'sdk-synthetic', 'object': 'chat.completion.chunk', 'created': 1,
                     'model': body['model'], 'choices': ([{'index': 0, 'delta': delta,
                        'finish_reason': finish}] if choices else [])}
            if usage is not None:
                value['usage'] = usage
            return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')

        frames = [chunk({'role': 'assistant', 'content': ''}),
                  chunk({'reasoning_content': 'Synthetic planning, never answer evidence.'})]
        frames += [chunk({'content': raw[index:index + 19]}) for index in range(0, len(raw), 19)]
        usage = {**USAGE, 'prompt_tokens_details': {'cached_tokens': 0}} if self.mode == 'eof_usage' else USAGE
        if stage == 'proposal' and self.mode == 'eof_usage':
            # Real reported usage does not supply the missing provider terminal.
            frames.append(chunk({}, choices=False, usage=usage))
        elif not (stage == 'proposal' and self.mode == 'eof'):
            reason = 'length' if stage == 'proposal' and self.mode == 'length' else 'stop'
            frames.append(chunk({}, finish=reason))
            # The real SDK must preserve a separate choices=[] usage trailer.
            if not (stage == 'proposal' and self.mode == 'no_usage'):
                frames.append(chunk({}, choices=False, usage=usage))
            frames.append(b'data: [DONE]\n\n')
        stream = _ProviderBytes(b''.join(frames))
        self.streams.append(stream)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=stream)


@pytest.fixture
def sdk_flow_factory(evidence_flow_factory, monkeypatch):
    # Imported fixture is intentionally used for normal capture/egress ownership.
    with ExitStack() as opened:
        def make(mode='accepted'):
            base = evidence_flow_factory('zh')
            from backend.memory_app.app import create_app
            from backend.memory_app import model_config
            from backend.memory_app.model_config import ModelConfiguration
            from backend.shared.deployment import DeploymentLayout
            from litellm.utils import CustomStreamWrapper
            from tools.rag_holdout import CompletionObserver

            provider = _SdkProvider(base.case, mode)
            actual_loader = model_config._load_litellm_completion

            def observed_sdk(**request):
                result = actual_loader(**request)
                provider.sdk_results.append({'stream': request.get('stream') is True,
                    'actual_sdk_stream': isinstance(getattr(result, 'stream', None), CustomStreamWrapper),
                    'outer_type': type(result).__name__})
                return result

            # Only observe the real loader. Its SDK conversion and owned HTTP
            # transport remain intact; HTTPX MockTransport supplies the bytes.
            monkeypatch.setattr(model_config, '_load_litellm_completion', observed_sdk)
            output = base.root / 'private-sdk-observation'
            output.mkdir()
            observer = CompletionObserver(output, 128, capture_private_responses=True)
            observer.label = 'synthetic-sdk:1'
            models = ModelConfiguration(base.records, base.root, base.query.models.secrets,
                completion_fn=observer.complete, model_http_client_factory=provider.client)
            collections = []

            def build():
                app = create_app(runtime_root=base.root, model_configuration=models,
                                 deployment_layout=DeploymentLayout('desktop', base.root))
                query = app.state.workspace_domains.query
                collect = query._collect_candidates

                def observed(*args, **kwargs):
                    value = collect(*args, **kwargs)
                    collections.append({'question': args[1],
                        'rows': [(row['kind'], row['id'], row['layer']) for row in value['candidates']]})
                    return value

                monkeypatch.setattr(query, '_collect_candidates', observed)
                return app

            app = build()
            http = opened.enter_context(TestClient(app))
            return SimpleNamespace(**{**vars(base), 'app': app, 'http': http,
                'query': app.state.workspace_domains.query, 'build': build,
                'models': models, 'provider': provider, 'collections': collections,
                'observer': observer, 'observation_root': output})

        yield make


def _ask(flow, key, *, retrieve='@13', http=None):
    from backend.memory_app.v2.policies import override

    with override(retrieve=retrieve, compose='@36'):
        return (http or flow.http).post('/api/v2/workbench/turns',
            headers={'Idempotency-Key': key},
            json={'project_id': PROJECT, 'text': flow.case['question'], 'intent': 'ask'})


def _native(flow, turn_id, *, retrieve='@13'):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    store = flow.app.state.ai_turn_store
    request = store.get_request(turn_id)
    assert request['policy_versions']['retrieve'] == retrieve
    assert request['policy_versions']['compose'] == '@36'
    [group] = kernel_call_groups(flow.root, turn_id=turn_id, project=PROJECT,
                                records=flow.records, include_answer_input=True)
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        connection.row_factory = sqlite3.Row
        attempts = [dict(row) for row in connection.execute(
            'SELECT r.attempt_id,r.model_request_id,r.status,r.terminal_status,t.payload_json '
            'FROM ai_model_attempt_reservations r '
            'LEFT JOIN ai_turn_payloads t ON t.payload_ref=r.terminal_receipt_ref '
            'WHERE r.turn_id=? ORDER BY r.rowid', (turn_id,))]
    for row in attempts:
        assert row['status'] == 'terminal' and row['payload_json'] is not None
        row['receipt'] = json.loads(row.pop('payload_json'))
    assert len(attempts) == len(flow.provider.wires)
    assert flow.provider.sdk_results and flow.provider.sdk_results[0]['stream'] is True
    assert flow.provider.sdk_results[0]['actual_sdk_stream'] is True
    assert all(client.is_closed for client in flow.provider.clients)
    assert all(stream.closed for stream in flow.provider.streams)
    assert len(flow.collections) == 1 and flow.collections[0]['question'] == flow.case['question']
    return store, group, attempts


def _retained_responses(flow):
    paths = sorted((flow.observation_root / 'private-responses').glob('completion-*.json'),
                   key=lambda path: int(path.stem.split('-')[-1]))
    assert len(paths) == len(flow.provider.wires)
    values = [json.loads(path.read_text(encoding='utf-8')) for path in paths]
    assert [value['completion_number'] for value in values] == list(range(1, len(values) + 1))
    assert [value['content'] for value in values] == [row['raw'] for row in flow.provider.wires]
    assert all(value['content_truncated'] is False for value in values)
    # Reasoning is separate from model answer text and never enters retained content.
    assert all('Synthetic planning' not in value['content'] for value in values)
    return values


@pytest.mark.parametrize('retrieve', ['@12', '@13'])
def test_actual_sdk_fragmented_private_stream_delivers_one_answer_and_frozen_replay(sdk_flow_factory, retrieve):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.v2.evidence_answer import EvidenceAnswerOutput
    from backend.memory_app.v2.policies import version

    flow = sdk_flow_factory()
    response = _ask(flow, 'sdk-direct', retrieve=retrieve)
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, answer = saved['turn'], saved['turn']['receipt']['ask']
    assert flow.case['answer'] in answer['answer']
    assert [(row['id'], row['layer']) for row in answer['citations']] == [(flow.sources[0], 'source')]
    route = answer['trace'][0]['evidence_fast_path']
    assert route['reason'] == 'evidence_answer_accepted' and route['route'] == 'direct'
    assert route['generation_invocations'] == route['generation_physical_attempts'] == 1
    assert route['decision_invocations'] == 0
    assert route['generation_usage_complete'] is True and 'generation_failure' not in route
    assert [row['stage'] for row in flow.provider.wires] == ['proposal']
    assert flow.provider.wires[0]['request']['stream'] is True
    assert flow.provider.wires[0]['request']['response_format'] == {'type': 'json_object'}
    store, group, attempts = _native(flow, turn['id'], retrieve=retrieve)
    assert [row['terminal_status'] for row in attempts] == ['succeeded']
    assert attempts[0]['receipt']['usage'] == NORMALIZED_USAGE
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-' + INVOCATION)
    assert frozen[1]['response_schema'] == EvidenceAnswerOutput.model_json_schema()
    assert read_answer_input(store, turn['id']) == frozen and group['answer_input'] == frozen[1]
    schema_hint = 'complete EvidenceAnswerOutput JSON Schema'
    assert any(schema_hint in row['content'] for row in frozen[1]['messages']) is (retrieve == '@13')
    for invocation in ('answer', REVIEW_KEY, 'raw-answer-v1'):
        assert store.get_immutable_payload(turn['id'], 'answer-model-input-' + invocation) is None
    assert store.get_immutable_payload(turn['id'], 'answer-model-failure-' + INVOCATION) is None
    assert answer['model_usage'] == NORMALIZED_USAGE
    [retained] = _retained_responses(flow)
    assert {key: retained['usage'][key] for key in USAGE} == USAGE
    assert retained['finish_reasons'] == ['stop']
    counts = len(flow.provider.wires), len(flow.collections)
    assert _ask(flow, 'sdk-direct', retrieve='@9').json() == saved
    with TestClient(flow.build()) as restarted:
        assert _ask(flow, 'sdk-direct', retrieve='@9', http=restarted).json() == saved
    assert (len(flow.provider.wires), len(flow.collections)) == counts
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


def test_actual_sdk_supported_schema_but_missing_evidence_returns_to_old_investigation(sdk_flow_factory):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = sdk_flow_factory('missing')
    response = _ask(flow, 'sdk-missing')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['reason'] == 'evidence_output_rejected' and route['route'] == 'investigate'
    assert route['generation_invocations'] == 1 and route['decision_invocations'] == 2
    assert route['reused_queries'] == 1
    assert [row['stage'] for row in flow.provider.wires] == ['proposal', 'decision', 'decision', 'first', 'review']
    assert CANDIDATE_ONLY not in json.dumps(flow.provider.synthetic.contexts, ensure_ascii=False)
    store, group, attempts = _native(flow, turn['id'])
    assert [row['terminal_status'] for row in attempts] == ['succeeded'] * 5
    assert store.get_immutable_payload(turn['id'], 'answer-model-failure-' + INVOCATION) is None
    review = store.get_immutable_payload(turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, turn['id']) == review and group['answer_input'] == review[1]
    assert answer['model_usage'] == {'input_tokens': 115, 'output_tokens': 55, 'total_tokens': 170}
    _retained_responses(flow)


def test_actual_sdk_real_terminal_without_provider_usage_delivers_with_unknown_counters(sdk_flow_factory):
    from backend.memory_app.answer_proof import read_answer_input

    flow = sdk_flow_factory('no_usage')
    response = _ask(flow, 'sdk-terminal-unknown-usage')
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['reason'] == 'evidence_answer_accepted' and route['route'] == 'direct'
    assert route['generation_invocations'] == route['generation_physical_attempts'] == 1
    assert route['generation_usage_complete'] is False
    assert flow.case['answer'] in answer['answer']
    assert answer['model_usage'] == {'observed_only': True}
    store, group, attempts = _native(flow, turn['id'])
    assert [row['terminal_status'] for row in attempts] == ['succeeded']
    assert not attempts[0]['receipt']['usage']
    assert group['calls'][0]['usage_status'] == 'not_recorded' and not group['calls'][0]['usage']
    frozen = store.get_immutable_payload(turn['id'], 'answer-model-input-' + INVOCATION)
    assert read_answer_input(store, turn['id']) == frozen and group['answer_input'] == frozen[1]
    assert store.get_immutable_payload(turn['id'], 'answer-model-failure-' + INVOCATION) is None
    [retained] = _retained_responses(flow)
    assert retained['finish_reasons'] == ['stop']


@pytest.mark.parametrize('mode,code', [('json_invalid', 'json_invalid'), ('schema_invalid', 'schema_invalid')])
def test_actual_sdk_completed_200_decode_failure_retains_usage_and_safe_diagnostic(sdk_flow_factory, mode, code):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = sdk_flow_factory(mode)
    response = _ask(flow, 'sdk-decode-' + mode)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['reason'] == 'evidence_generation_failed' and route['route'] == 'investigate'
    assert route['generation_invocations'] == route['generation_physical_attempts'] == 1
    assert route['decision_invocations'] == 2 and route['reused_queries'] == 1
    assert route['generation_failure'] == {'category': 'decode', 'code': code,
                                         'completed_wire': True, 'usage_complete': True}
    assert [row['stage'] for row in flow.provider.wires] == ['proposal', 'decision', 'decision', 'first', 'review']
    store, group, attempts = _native(flow, turn['id'])
    assert [row['terminal_status'] for row in attempts] == ['succeeded'] * 5
    assert attempts[0]['receipt']['usage'] == NORMALIZED_USAGE
    failed = store.get_immutable_payload(turn['id'], 'answer-model-failure-' + INVOCATION)
    diagnostic = failed[1]
    assert diagnostic['version'] == 'answer-model-failure-v1'
    assert diagnostic['category'] == 'decode' and diagnostic['code'] == code
    assert diagnostic['completed_wire'] is True and diagnostic['usage'] == NORMALIZED_USAGE
    assert diagnostic['usage_complete'] is True and diagnostic['receipt_ids']
    assert PRIVATE_FRAGMENT not in json.dumps(diagnostic)
    assert PRIVATE_FRAGMENT not in response.text
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-' + INVOCATION) is None
    review = store.get_immutable_payload(turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, turn['id']) == review and group['answer_input'] == review[1]
    assert answer['model_usage'] == {'input_tokens': 115, 'output_tokens': 55, 'total_tokens': 170}
    assert PRIVATE_FRAGMENT in _retained_responses(flow)[0]['content']


@pytest.mark.parametrize('mode', ['length', 'eof', 'eof_usage'])
def test_actual_sdk_incomplete_200_stream_keeps_attempts_unknown_usage_and_bounded_fallback(sdk_flow_factory, mode):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = sdk_flow_factory(mode)
    reported = mode == 'eof_usage'
    if reported:
        # Synthetic explicit rates plus provider-reported zero cache hits let
        # the existing price owner account for every failed attempt as well.
        flow.models.update_model_prices('generation', {'input_per_million': '2',
            'output_per_million': '3', 'cache_read_per_million': '0.1'},
            expected_revision=0,
            expected_configuration_revision=flow.models.public()['generation']['revision'])
    response = _ask(flow, 'sdk-incomplete-' + mode)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    answer = turn['receipt']['ask']
    route = answer['trace'][0]['evidence_fast_path']
    assert route['reason'] == 'evidence_generation_failed' and route['route'] == 'investigate'
    stages = [row['stage'] for row in flow.provider.wires]
    proposals = stages.count('proposal')
    assert 1 <= proposals <= 6 and stages == ['proposal'] * proposals + ['decision', 'decision', 'first', 'review']
    assert route['generation_invocations'] == 1 and route['generation_physical_attempts'] == proposals
    assert route['generation_usage_complete'] is reported and route['reused_queries'] == 1
    if reported:
        assert answer['model_usage'] == {key: value * (proposals + 4)
                                         for key, value in NORMALIZED_USAGE.items()}
    else:
        assert answer['model_usage']['observed_only'] is True
        assert answer['model_usage']['total_tokens'] == 136
    store, group, attempts = _native(flow, turn['id'])
    assert len(group['calls']) == 5
    assert [row['terminal_status'] for row in attempts[-4:]] == ['succeeded'] * 4
    assert all(row['terminal_status'] == 'failed_transport' for row in attempts[:-4])
    if reported:
        assert [row['receipt']['usage'] for row in attempts[:-4]] == [NORMALIZED_USAGE] * proposals
        expected_cost = Decimal('0.000079') * (proposals + 4)
        assert answer['model_cost'] == {'currency': 'CNY', 'amount': format(expected_cost, 'f')}
        assert sum(Decimal(call['cost']['amount']) for call in group['calls']) == expected_cost
        assert Decimal(group['calls'][0]['cost']['amount']) == Decimal('0.000079') * proposals
    else:
        assert all(not row['receipt']['usage'] for row in attempts[:-4])
    failed = store.get_immutable_payload(turn['id'], 'answer-model-failure-' + INVOCATION)[1]
    assert failed['completed_wire'] is None and failed['usage'] == {} and failed['usage_complete'] is reported
    if reported:
        assert failed['observations'] == [NORMALIZED_USAGE] * proposals
    assert failed['receipt_ids'] and route['generation_failure']['code'] == failed['code']
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-' + INVOCATION) is None
    assert read_answer_input(store, turn['id']) == store.get_immutable_payload(
        turn['id'], 'answer-model-input-' + REVIEW_KEY)
    retained = _retained_responses(flow)
    if mode == 'length':
        assert 'length' in retained[0]['finish_reasons']
    if reported:
        assert all(value['finish_reasons'] == [] for value in retained[:proposals])
        assert all({key: value['usage'][key] for key in USAGE} == USAGE for value in retained[:proposals])
