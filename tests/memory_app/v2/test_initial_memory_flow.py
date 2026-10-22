"""Candidate routing through real Workbench owners; all model replies are synthetic."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


PROJECT = 'alpha'
BODY = 'Orion 的重试上限为 3 次。'
QUESTION = 'Orion 在开发环境的重试上限是多少？'
CONDITION = '开发环境'
INDEPENDENT_BODY = 'nebula beacon is described in an independent original record.'


class _SyntheticTransport:
    """Only the completion boundary is fixed; retrieval and publication stay real."""

    def __init__(self, *, question=QUESTION, clarify_missing=False):
        self.question = question
        self.clarify_missing = clarify_missing
        self.calls = []
        self.contexts = []
        self.review_text = BODY
        self.search_query = question
        self.search_depth = 'summary'
        self.expected_material = BODY
        self.initial_expected = None

    def __call__(self, **request):
        messages = request['messages']
        text = messages[-1]['content']
        if 'remaining_decisions' in text:
            context = json.loads(text)
            self.contexts.append(deepcopy(context))
            output = self._decision(context)
            stage = 'decision'
        else:
            assert '资料：\n' in text
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            unit = next(row for row in payload['evidence_catalog']['units'] if BODY in row['text'])
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            fact = {'kind': 'fact', 'text': self.review_text if stage == 'review' else BODY,
                    'evidence': [{'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}],
                    'date_refs': []}
            if stage == 'first':
                output = {'facts': [fact], 'unreviewed': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested bound is needed.'}}
            else:
                fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'adopt',
                                          'reason': 'Use the supported source proposition.'}]
                output = {'statements': [fact], 'unused': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested bound is needed.'},
                    'uncertain': [], 'excluded_facts': []}
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        if request.get('stream'):
            def chunks():
                yield {'choices': [{'delta': {'content': json.dumps(output, ensure_ascii=False)},
                                    'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}}
            return chunks()
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(output, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}}

    def _decision(self, context):
        decision = {'action': 'search', 'source': 'memory', 'query': '',
                    'depth': 'summary', 'ids': [], 'questions': []}
        if context['round'] == 1:
            assert context['steps'] == []
            if self.initial_expected is not None:
                assert self.initial_expected in json.dumps(context['materials'], ensure_ascii=False)
            decision.update(query=self.search_query, depth=self.search_depth)
        else:
            assert context['round'] == 2
            assert self.expected_material in json.dumps(context['materials'], ensure_ascii=False)
            if self.clarify_missing:
                decision.update(action='clarify', questions=['请补充超时上限的实际记录。'])
            else:
                material = next(row for row in context['materials'] if BODY in row['text'])
                decision.update(action='answer', ids=[material['id']])
        return decision


@pytest.fixture
def flow_factory(tmp_path, monkeypatch):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    connections = []

    def local_connections_only(original):
        def connect(sock, address):
            if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1', 'localhost'}:
                return original(sock, address)
            connections.append(address)
            raise AssertionError('initial_memory_network_forbidden')
        return connect

    monkeypatch.setattr(socket.socket, 'connect', local_connections_only(socket.socket.connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', local_connections_only(socket.socket.connect_ex))
    import litellm  # noqa: F401
    with ExitStack() as opened:
        def make(kind='recognition', *, question=QUESTION, clarify_missing=False):
            root = tmp_path / kind
            (root / 'config').mkdir(parents=True)
            code = Path(__file__).resolve().parents[3]
            shutil.copyfile(code / 'config/settings.toml.example', root / 'config/settings.toml')
            monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
            monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
            # Import-time application factories must also use this isolated root.
            from backend.memory_app.app import create_app
            from backend.memory_app.model_config import ModelConfiguration
            from backend.memory_app.storage_authority import resolve_recognition_document_store
            from backend.recognition import WorkScope
            from backend.security.secrets import InMemorySecretStore
            from backend.shared.deployment import DeploymentLayout

            records, namespace = resolve_recognition_document_store(root)
            assert namespace == 'default'
            transport = _SyntheticTransport(question=question, clarify_missing=clarify_missing)
            models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
            models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'synthetic-initial-memory', 'api_key': 'synthetic-only',
                'allow_remote': True, 'enabled': True, 'expected_revision': 0})

            def build():
                return create_app(runtime_root=root, model_configuration=models,
                                  deployment_layout=DeploymentLayout('desktop', root))

            app = build()
            query = app.state.workspace_domains.query
            recognition = None
            if kind in {'recognition', 'mismatched'}:
                service = app.state.recognition_service
                scope = WorkScope('local-user', PROJECT)
                condition = CONDITION if kind == 'recognition' else '生产环境'
                source = service.stage_experience(scope=scope, content=condition + '：' + BODY)
                proposed = service.propose(scope=scope, content=BODY, conditions=[condition],
                    source_experience_ids=[source.id if hasattr(source, 'id') else source])
                recognition = service.publish(scope=scope, candidate_id=proposed.id,
                    expected_revision=1, reviewer='local-user')
                if kind == 'mismatched':
                    query.source_store.write('sources', 'independent-source', {
                        'id': 'independent-source', 'title': 'Independent synthetic source',
                        'project_id': PROJECT, 'metadata': {'content': INDEPENDENT_BODY}},
                        expected_revision=0)
            else:
                assert kind == 'original'
                query.source_store.write('sources', 'orion-source', {
                    'id': 'orion-source', 'title': 'Synthetic Orion source', 'project_id': PROJECT,
                    'metadata': {'content': '[D1:1] Orion: ' + BODY + ' 适用条件为开发环境。'}},
                    expected_revision=0)
            query.retrieval_index.bootstrap()
            collections = []
            original_collect = query._collect_candidates

            def observed_collect(*args, **kwargs):
                collections.append({'question': args[1], 'layers': kwargs.get('only_layers')})
                return original_collect(*args, **kwargs)

            monkeypatch.setattr(query, '_collect_candidates', observed_collect)
            http = opened.enter_context(TestClient(app))
            return SimpleNamespace(root=root, app=app, http=http, records=records, models=models,
                transport=transport, build=build, query=query, recognition=recognition,
                collections=collections, question=question)

        yield make
    assert connections == []


def _ask(flow, key):
    return flow.http.post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
        json={'project_id': PROJECT, 'text': flow.question, 'intent': 'ask'})


def test_candidate_direct_keeps_two_answer_calls_and_frozen_replay(flow_factory):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    from backend.memory_app.v2.answer_evidence import FocusedReferencedFactsOutput, FocusedReviewedScopeOutput
    from backend.memory_app.v2.policies import override, version

    flow = flow_factory()
    assert (version('retrieve'), version('compose')) == ('@9', '@34')
    with override(retrieve='@10'):
        response = _ask(flow, 'initial-memory-direct')
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, receipt = saved['turn'], saved['turn']['receipt']['ask']
    assert BODY in receipt['answer'] and receipt['no_match'] is False
    assert [(row['id'], row['layer']) for row in receipt['citations']] == [(flow.recognition.id, 'insight')]
    assert BODY in receipt['citations'][0]['quote']
    assert [row['stage'] for row in flow.transport.calls] == ['first', 'review']
    assert flow.transport.contexts == []
    assert flow.collections == [{'question': QUESTION, 'layers': ('L3', 'L2', 'L1')}]
    initial = receipt['trace'][0]['initial_memory']
    assert initial['route'] == 'direct'
    assert initial['probe_collections'] == 1 and initial['decision_invocations'] == 0
    assert initial['remainder_collections'] == 0
    assert initial['probe_seconds'] >= 0
    assert receipt['trace'][0]['agentic'] == {
        'version': 1, 'status': 'skipped', 'stop': 'initial_evidence_sufficient', 'steps': []}
    assert receipt['model_usage']['total_tokens'] == 80

    store = flow.app.state.ai_turn_store
    frozen = store.get_request(turn['id'])
    assert frozen['policy_versions']['retrieve'] == '@10'
    assert frozen['policy_versions']['compose'] == '@34'
    assert store.get_immutable_payload(turn['id'], 'answer-gap-input-rag-decision-1-v1') is None
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-rag-decision-1') is None
    assert store.get_immutable_payload(turn['id'], 'answer-model-input-answer')[1]['response_schema'] == FocusedReferencedFactsOutput.model_json_schema()
    reviewed = store.get_immutable_payload(turn['id'], 'answer-model-input-' + REVIEW_KEY)
    assert reviewed[1]['response_schema'] == FocusedReviewedScopeOutput.model_json_schema()
    assert read_answer_input(store, turn['id']) == reviewed
    groups = kernel_call_groups(flow.root, turn_id=turn['id'], project=PROJECT,
                               records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and len(groups[0]['calls']) == 2
    assert all(row['status'] == 'completed' for row in groups[0]['calls'])
    counts = len(flow.transport.calls), len(flow.collections)
    assert _ask(flow, 'initial-memory-direct').json() == saved
    with TestClient(flow.build()) as restarted:
        replay = restarted.post('/api/v2/workbench/turns', headers={'Idempotency-Key': 'initial-memory-direct'},
            json={'project_id': PROJECT, 'text': QUESTION, 'intent': 'ask'})
        assert replay.status_code == 200 and replay.json() == saved
    assert (len(flow.transport.calls), len(flow.collections)) == counts
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


def test_original_only_falls_back_and_reuses_upper_probe(flow_factory):
    from backend.memory_app.v2.policies import override

    flow = flow_factory('original')
    with override(retrieve='@10'):
        response = _ask(flow, 'initial-memory-original')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert BODY in receipt['answer']
    assert receipt['citations'][0]['layer'] == 'source'
    assert [row['stage'] for row in flow.transport.calls] == ['decision', 'decision', 'first', 'review']
    assert flow.transport.contexts[0]['materials'] == []
    assert any(row['layer'] == 'L0' for row in flow.transport.contexts[1]['materials'])
    assert flow.collections == [
        {'question': QUESTION, 'layers': ('L3', 'L2', 'L1')},
        {'question': QUESTION, 'layers': ('L0',)},
    ]
    initial = receipt['trace'][0]['initial_memory']
    assert initial['route'] == 'investigate'
    assert initial['probe_collections'] == 1 and initial['remainder_collections'] == 1
    assert initial['reused_queries'] >= 1 and initial['decision_invocations'] == 2


def test_default_policy_still_investigates_without_initial_probe(flow_factory):
    from backend.memory_app.v2.policies import version

    flow = flow_factory()
    assert version('retrieve') == '@9'
    response = _ask(flow, 'initial-memory-default-control')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert [row['stage'] for row in flow.transport.calls] == ['decision', 'decision', 'first', 'review']
    assert flow.collections == [{'question': QUESTION, 'layers': None}]
    assert 'initial_memory' not in receipt['trace'][0]


def test_inapplicable_probe_does_not_hide_independent_original_on_new_query(flow_factory):
    from backend.memory_app.v2.policies import override

    flow = flow_factory('mismatched', clarify_missing=True)
    flow.transport.search_query = 'nebula beacon'
    flow.transport.search_depth = 'original'
    flow.transport.expected_material = INDEPENDENT_BODY
    flow.transport.initial_expected = BODY
    with override(retrieve='@10'):
        response = _ask(flow, 'initial-memory-independent-original')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert [row['stage'] for row in flow.transport.calls] == ['decision', 'decision']
    assert receipt['trace'][0]['agentic']['status'] == 'clarify'
    assert any(row['id'].endswith(':independent-source') and row['layer'] == 'L0'
               and INDEPENDENT_BODY in row['text'] for row in flow.transport.contexts[1]['materials'])
    assert flow.collections == [
        {'question': QUESTION, 'layers': ('L3', 'L2', 'L1')},
        {'question': 'nebula beacon', 'layers': None},
    ]
    initial = receipt['trace'][0]['initial_memory']
    assert initial['route'] == 'investigate' and initial['probe_collections'] == 1
    assert initial['remainder_collections'] == 0 and initial['decision_invocations'] == 2


def test_multislot_request_keeps_original_investigation_without_probe(flow_factory):
    from backend.memory_app.v2.policies import override

    question = 'Orion 在开发环境的重试上限以及超时上限分别是多少？'
    flow = flow_factory(question=question, clarify_missing=True)
    with override(retrieve='@10'):
        response = _ask(flow, 'initial-memory-multislot')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert [row['stage'] for row in flow.transport.calls] == ['decision', 'decision']
    assert flow.transport.contexts[0]['question'] == question
    assert BODY in json.dumps(flow.transport.contexts[1]['materials'], ensure_ascii=False)
    assert receipt['trace'][0]['agentic']['status'] == 'clarify'
    initial = receipt['trace'][0]['initial_memory']
    assert initial['route'] == 'investigate' and initial['probe_collections'] == 0
    assert initial['reason'].startswith('unsupported')
    assert all(row['layers'] is None for row in flow.collections)


def test_direct_wrong_final_value_fails_without_publishing_answer(flow_factory):
    from backend.memory_app.v2.policies import override

    flow = flow_factory()
    flow.transport.review_text = 'Orion 的重试上限为 4 次。'
    with override(retrieve='@10'):
        response = _ask(flow, 'initial-memory-wrong-final')
    assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
    assert [row['stage'] for row in flow.transport.calls] == ['first', 'review']
    assert flow.records.list('v2_turns') == ()
    receipts = flow.records.list('workspace_ask_receipts')
    assert len(receipts) == 1 and receipts[0].payload['status'] == 'failed'
