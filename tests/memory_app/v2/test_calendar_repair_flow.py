"""Synthetic calendar repairs through real source owners, HTTP and native receipts.

No held-out input is imported here. Only the completion boundary is synthetic;
capture, source authorization, retrieval, frozen model output and replay are real.
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


PROJECT = 'calendar-repair-synthetic'
RECORD_DATE = '2025-06-19'
CAPTURED_AT = '2026-10-22T08:00:00Z'
SOURCE_LINE = ('[D1:1] Nia: I archived a draft last week, '
               'and I calibrated the Aster gauge two days ago.')
SOURCE_TEXT = 'Session date: ' + RECORD_DATE + '\n' + SOURCE_LINE
QUESTION = 'When did Nia calibrate the Aster gauge?'
EXPECTED_DAY = '2025-06-17'
WRONG_DAY = '2025-07-01'


class _CalendarTransport:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []
        self.contexts = []
        self.outputs = []
        self.on_review = None
        self.empty_first_diagnostics = False

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
                selected = next(row for row in context['materials'] if SOURCE_LINE in row['text'])
                output.update(action='answer', query='', ids=[selected['id']])
        else:
            assert '资料：\n' in text
            payload, _ = json.JSONDecoder().raw_decode(text.split('资料：\n', 1)[1])
            # These IDs come from the real frozen catalog, not a fabricated projection.
            ref = next(row for row in payload['date_provenance'] if row['expression'] == 'two days ago')
            assert ref['date_ref_id'] == 2
            assert any(option['start'] == option['end'] == EXPECTED_DAY for option in ref['options'])
            unit = next(row for row in payload['evidence_catalog']['units'] if row['id'] == ref['unit_id'])
            assert SOURCE_LINE in unit['text']
            stage = 'review' if 'untrusted_first_fact_plan' in payload else 'first'
            day = WRONG_DAY if self.mode == 'fallback' else EXPECTED_DAY
            fact = {'kind': 'fact', 'text': 'Nia calibrated the Aster gauge on ' + day + '.',
                    'evidence': [{'unit_id': unit['id'], 'quote': unit['text'], 'role': 'support'}],
                    'date_refs': [ref['date_ref_id']]}
            anchor_mode = self.mode in {'anchor_explanation', 'anchor_event'}
            if anchor_mode:
                fact['kind'] = 'date'
                fact['text'] = (
                    f'Nia calibrated the Aster gauge on {EXPECTED_DAY} (two days before {RECORD_DATE}).'
                    if stage == 'first' else
                    f'Nia calibrated the Aster gauge two days before {RECORD_DATE}, i.e. on {EXPECTED_DAY}.')
                if self.mode == 'anchor_event' and stage == 'review':
                    fact['text'] += ' Nia calibrated it on ' + RECORD_DATE + '.'
            if stage == 'first':
                output = {'facts': [fact], 'unreviewed': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested calibration date.'}}
            else:
                assert payload['untrusted_first_fact_plan'] == self.outputs[0]
                if self.empty_first_diagnostics:
                    assert payload['first_fact_diagnostics'] == []
                else:
                    assert payload['first_fact_diagnostics']
                fact['first_fact_uses'] = [{'fact_index': 1, 'action': 'correct' if anchor_mode else 'adopt',
                                          'reason': 'Retain the first statement.'}]
                if self.mode == 'forged_ref':
                    fact['date_refs'] = [999]
                output = {'statements': [fact], 'unused': {
                    'scope': 'all_unreferenced', 'reason': 'Only the requested calibration evidence.'},
                    'uncertain': [], 'excluded_facts': []}
            self.outputs.append(deepcopy(output))
        self.calls.append({'stage': stage, 'messages': deepcopy(messages), 'output': deepcopy(output)})
        if stage == 'review' and self.on_review is not None:
            self.on_review()
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
def calendar_flow(tmp_path, monkeypatch):
    root = tmp_path / 'calendar-repair'
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
            raise AssertionError('calendar_repair_network_forbidden')
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
    from backend.shared.job_runtime import build_rebuild_job_repository
    from core.ingestion_core import ObjectStoreSourceRegistrar
    from core.product_core.workbench_source_intake import CaptureWorkbenchTextSource

    records, namespace = resolve_recognition_document_store(root)
    assert namespace == 'default'
    transport = _CalendarTransport('repair')
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=transport)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
        'model': 'synthetic-calendar-repair', 'api_key': 'synthetic-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    collections = []

    def build():
        instance = create_app(runtime_root=root, model_configuration=models,
                              deployment_layout=DeploymentLayout('desktop', root))
        query = instance.state.workspace_domains.query
        original = query._collect_candidates

        def observed(*args, **kwargs):
            collected = original(*args, **kwargs)
            collections.append({'question': args[1], 'layers': kwargs.get('only_layers'),
                                'rows': [(row['id'], row['layer']) for row in collected['candidates']]})
            return collected

        monkeypatch.setattr(query, '_collect_candidates', observed)
        return instance

    app = build()
    query = app.state.workspace_domains.query
    jobs = build_rebuild_job_repository(root, query.source_store)
    captured = CaptureWorkbenchTextSource(source_registrar=ObjectStoreSourceRegistrar(
        query.source_store, namespace_id=namespace, project_id=PROJECT, created_at=CAPTURED_AT),
        job_repository=jobs, namespace_id=namespace, now=CAPTURED_AT).execute(
            title='Synthetic Aster session ' + RECORD_DATE, content=SOURCE_TEXT)
    assert captured.published_output_kinds == ('source',)
    assert captured.library_bridge_item.memory_publication_state == 'not_started'
    assert jobs.get(captured.job_id)['status'] == 'completed'
    source_id = captured.source_id
    revision = query.source_store.revision('sources', source_id)
    source = query.source_store.read('sources', source_id)
    assert source['metadata']['content'] == SOURCE_TEXT and source['trust_status'] == 'user_confirmed'
    authority = SourceEgressService(records)
    scope = WorkScope('local-user', PROJECT)
    roots = [{'type': 'original_source', 'id': source_id, 'revision': revision}]
    snapshot = authority.snapshot(scope, roots)
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(scope, snapshot)
    query.retrieval_index.bootstrap()
    assert records.list('documents') == () and records.list('recognitions') == ()
    assert records.list('recognition_experiences') == ()
    assert transport.calls == []

    def revoke():
        authority.set_policy(scope, 'original_source', source_id, revision, 0, [])

    with TestClient(app) as http:
        yield SimpleNamespace(root=root, app=app, http=http, records=records, query=query,
            transport=transport, source_id=source_id, build=build, collections=collections,
            revoke=revoke, authority=authority, snapshot=snapshot, scope=scope)
    assert connections == []


def _ask(flow, key, *, compose='@35', http=None):
    from backend.memory_app.v2.policies import override

    with override(retrieve='@11', compose=compose):
        return (http or flow.http).post('/api/v2/workbench/turns', headers={'Idempotency-Key': key},
            json={'project_id': PROJECT, 'text': QUESTION, 'intent': 'ask'})


def _native(flow, *, compose, all_completed=True):
    from backend.memory_app.answer_proof import REVIEW_KEY
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups

    assert [call['stage'] for call in flow.transport.calls] == ['decision', 'decision', 'first', 'review']
    assert len(flow.collections) == 1 and flow.collections[0]['question'] == QUESTION
    assert set(flow.collections[0]['rows']) == {(flow.source_id, 'L0')}
    groups = kernel_call_groups(flow.root, project=PROJECT, records=flow.records, include_answer_input=True)
    assert len(groups) == 1 and len(groups[0]['calls']) == 4
    group = groups[0]
    turn_id = group['turn_id']
    store = flow.app.state.ai_turn_store
    request = store.get_request(turn_id)
    assert request['input']['text'] == QUESTION
    assert request['policy_versions']['retrieve'] == '@11'
    assert request['policy_versions']['compose'] == compose
    primary = [event for event in store.events_after(turn_id) if event['type'] == 'model.completed'
               and event['data']['model_call_purpose'] == 'primary']
    if all_completed:
        assert len(primary) == 2
        assert all(call['status'] == 'completed' for call in group['calls'])
        assert sum(call['usage']['total_tokens'] for call in group['calls']) == 160
    with sqlite3.connect(flow.root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        wires = connection.execute('SELECT status,terminal_status FROM ai_model_attempt_reservations '
                                   'WHERE turn_id=?', (turn_id,)).fetchall()
    assert len(wires) == 4 and all(state == 'terminal' for state, _ in wires)
    if all_completed:
        assert wires == [('terminal', 'succeeded')] * 4
        first = store.get_immutable_payload(turn_id, 'answer-model-result-answer')
        review = store.get_immutable_payload(turn_id, 'answer-model-result-' + REVIEW_KEY)
        assert first[1]['output'] == flow.transport.outputs[0]
        assert review[1]['output'] == flow.transport.outputs[1]
    return store, group


def test_previous_calendar_contract_preserves_the_two_completed_call_failure(calendar_flow):
    flow = calendar_flow
    response = _ask(flow, 'calendar-baseline', compose='@34')
    assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
    store, group = _native(flow, compose='@34')
    assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
    assert flow.transport.outputs[0]['facts'][0]['kind'] == 'fact'
    assert flow.transport.outputs[0]['facts'][0]['date_refs'] == [2]
    assert flow.transport.outputs[1]['statements'][0]['first_fact_uses'][0]['action'] == 'adopt'


def _proof_and_replay(flow, saved, key, store, group):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input, select_answer_input
    from backend.memory_app.v2.policies import version

    identity = group['turn_id']
    review = store.get_immutable_payload(identity, 'answer-model-input-' + REVIEW_KEY)
    assert read_answer_input(store, identity) == review
    assert group['answer_input'] == review[1]
    canonical = store.get_immutable_payload(identity, 'answer-model-input-answer')[1]
    first = store.get_immutable_payload(identity, 'answer-model-result-answer')[1]
    final = store.get_immutable_payload(identity, 'answer-model-result-' + REVIEW_KEY)[1]
    capsule = store.get_immutable_payload(identity, 'answer-continuation-plan-v1')[1]
    product = store.get_immutable_payload(identity, 'product-answer-result-v2')[1]
    altered = deepcopy(final)
    altered['output']['statements'][0]['date_refs'] = [999]
    assert select_answer_input(store.get_request(identity), product, canonical, first, review[1], altered, capsule) is None
    before = len(flow.transport.calls), len(flow.collections)
    assert _ask(flow, key, compose='@34').json() == saved
    with TestClient(flow.build()) as restarted:
        replay = _ask(flow, key, compose='@34', http=restarted)
        assert replay.status_code == 200 and replay.json() == saved
    assert (len(flow.transport.calls), len(flow.collections)) == before
    assert (version('retrieve'), version('compose')) == ('@9', '@34')


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_calendar_classification_repair_keeps_raw_models_and_replays_actual_review_input(calendar_flow, compose):
    flow = calendar_flow
    response = _ask(flow, 'calendar-repaired', compose=compose)
    assert response.status_code == 200, response.text
    saved = response.json()
    answer = saved['turn']['receipt']['ask']
    assert EXPECTED_DAY in answer['answer'] and answer['citations']
    report = answer['trace'][0]['calendar_review']
    assert report['route'] == 'classification_repair'
    assert report['repaired_statements'] == 1 and report['fallback_statements'] == 0
    assert report['codes']
    store, group = _native(flow, compose=compose)
    assert flow.transport.outputs[0]['facts'][0]['kind'] == 'fact'
    assert flow.transport.outputs[1]['statements'][0]['kind'] == 'fact'
    assert flow.transport.outputs[1]['statements'][0]['first_fact_uses'][0]['action'] == 'adopt'
    assert answer['model_usage'] == {'input_tokens': 80, 'output_tokens': 80, 'total_tokens': 160}
    _proof_and_replay(flow, saved, 'calendar-repaired', store, group)


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_unverified_calendar_value_falls_back_to_speaker_and_relative_evidence(calendar_flow, compose):
    from backend.memory_app.answer_proof import REVIEW_KEY, read_answer_input

    flow = calendar_flow
    flow.transport.mode = 'fallback'
    response = _ask(flow, 'calendar-fallback', compose=compose)
    assert response.status_code == 200, response.text
    answer = response.json()['turn']['receipt']['ask']
    report = answer['trace'][0]['calendar_review']
    assert report['route'] == 'evidence_fallback'
    assert report['repaired_statements'] == 0 and report['fallback_statements'] == 1
    assert 'Nia' in answer['answer'] and 'two days ago' in answer['answer']
    assert '本轮换算未验证' in answer['answer'] and WRONG_DAY not in answer['answer']
    assert EXPECTED_DAY not in answer['answer']
    assert answer['citations'] and answer['model_usage']['total_tokens'] == 160
    store, group = _native(flow, compose=compose)
    assert read_answer_input(store, group['turn_id']) == store.get_immutable_payload(
        group['turn_id'], 'answer-model-input-' + REVIEW_KEY)


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_forged_calendar_reference_still_hard_fails_after_two_completed_generations(calendar_flow, compose):
    flow = calendar_flow
    flow.transport.mode = 'forged_ref'
    response = _ask(flow, 'calendar-forged-ref', compose=compose)
    assert response.status_code == 502
    store, group = _native(flow, compose=compose)
    assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
    assert flow.transport.outputs[1]['statements'][0]['date_refs'] == [999]


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_revoked_source_still_hard_fails_without_another_generation(calendar_flow, compose):
    flow = calendar_flow
    flow.transport.on_review = flow.revoke
    response = _ask(flow, 'calendar-revoked-source', compose=compose)
    assert response.status_code in {409, 502}
    store, group = _native(flow, compose=compose, all_completed=False)
    assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
    from backend.recognition import RecognitionError

    with pytest.raises(RecognitionError):
        flow.authority.validate_snapshot(flow.scope, flow.snapshot)


@pytest.mark.parametrize('compose', ['@35', '@36'])
def test_record_anchor_explanation_retains_dates_and_frozen_model_text(calendar_flow, compose):
    flow = calendar_flow
    flow.transport.mode = 'anchor_explanation'
    flow.transport.empty_first_diagnostics = compose == '@36'
    response = _ask(flow, 'calendar-record-anchor', compose=compose)
    assert response.status_code == (200 if compose == '@36' else 502), response.text
    store, group = _native(flow, compose=compose)
    first = flow.transport.outputs[0]['facts'][0]
    review = flow.transport.outputs[1]['statements'][0]
    assert first['kind'] == review['kind'] == 'date'
    assert first['date_refs'] == review['date_refs'] == [2]
    assert first['text'] != review['text']
    assert first['evidence'] == review['evidence']
    if compose == '@35':
        assert response.json() == {'detail': 'answer_generation_failed'}
        assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
        return
    saved = response.json()
    answer = saved['turn']['receipt']['ask']
    assert EXPECTED_DAY in answer['answer'] and RECORD_DATE in answer['answer']
    assert review['text'] in answer['answer'] and answer['citations']
    assert answer['model_usage'] == {'input_tokens': 80, 'output_tokens': 80, 'total_tokens': 160}
    _proof_and_replay(flow, saved, 'calendar-record-anchor', store, group)


def test_record_anchor_cannot_authorize_an_added_same_day_event(calendar_flow):
    flow = calendar_flow
    flow.transport.mode = 'anchor_event'
    flow.transport.empty_first_diagnostics = True
    response = _ask(flow, 'calendar-record-anchor-event', compose='@36')
    assert response.status_code == 502 and response.json() == {'detail': 'answer_generation_failed'}
    store, group = _native(flow, compose='@36')
    assert store.get_immutable_payload(group['turn_id'], 'product-answer-result-v2') is None
    assert 'Nia calibrated it on ' + RECORD_DATE in flow.transport.outputs[1]['statements'][0]['text']
