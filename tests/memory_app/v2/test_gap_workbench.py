"""Real workbench HTTP requests select a frozen policy and governed gap wires."""
import json
from decimal import Decimal
from threading import Event
import time

from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER
from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.original_sources import document_roots
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.policies import ACTIVE, override
from backend.recognition import WorkScope
from tests.memory_app.v2.test_fast_aux_calls import fast_env
from tests.memory_app.v2.test_ladder import document, recognition
from tests.memory_app.v2.test_workbench_ask import ask


QUESTION = 'meridian orbit telescope aperture observer tolerance 具体?'
BINDING = 'answer-gap-binding-gap-drilldown-v1'


def materials(env):
    document(env, summary='meridian cobalt', body='meridian telescope aperture guidance')
    document(env, summary='orbit silica', body='orbit telescope aperture guidance')
    third = document(env, summary='archive inventory', body='deepomega detail calibrated',
        original='deepomega detail original calibration')
    return third


def transport(env, *, gap_queries=('deepomega detail',), condensed=QUESTION, gap_failure=False, after_gap=None,
              cache_reported=False, before_primary=None):
    original = env.model._completion_fn
    kinds, proofs = [], []
    def complete(**request):
        text = '\n'.join(row['content'] for row in request['messages'])
        kind = 'condense' if 'condensed_question' in text else 'gap' if '还缺哪几点' in text else 'rewrite' if '"queries"' in text else 'primary'
        kinds.append(kind)
        if kind == 'gap':
            active = ACTIVE_ANSWER.get()
            fact = active[1].get_immutable_payload(active[0]['turn_id'], BINDING)
            assert fact is not None
            frozen = active[1].get(fact[1]['input_ref'])
            assert frozen['messages'] == request['messages']
            assert frozen['policy_versions']['retrieve'] == '@4'
            proofs.append((active[0]['turn_id'], fact[1], frozen))
            if gap_failure:
                raise ConnectionError('synthetic external gap failure')
        elif kind == 'primary' and before_primary:
            before_primary()
        result = original(**request)
        if cache_reported:
            result['usage']['prompt_tokens_details'] = {'cached_tokens': 0}
        if kind == 'gap':
            result['choices'][0]['message']['content'] = json.dumps({'queries': list(gap_queries)})
            if after_gap:
                after_gap()
        elif kind == 'condense':
            result['choices'][0]['message']['content'] = json.dumps({'condensed_question': condensed})
        return result
    env.model._completion_fn = complete
    return kinds, proofs


def test_http_frozen_v4_gap_reaches_real_lower_source_and_persists_proof(fast_env):
    env = fast_env
    third = materials(env)
    kinds, proofs = transport(env)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    store = env.app.state.ai_turn_store
    assert store.get_request(turn['id'])['policy_versions']['retrieve'] == '@4'
    assert kinds == ['gap', 'primary']
    assert len(proofs) == 1 and proofs[0][0] == turn['id']
    receipt = turn['receipt']['ask']
    assert receipt['trace'][0]['rewrite'] == {'queries': ['deepomega detail'], 'used': True}
    assert any(row['id'] == third and row['layer'] in {'note', 'source'} for row in receipt['citations'])
    assert receipt['model_usage']['total_tokens'] == 10
    assert ACTIVE['retrieve'] == '@3'


def test_http_sufficient_v4_upper_material_sends_no_gap(fast_env):
    env = fast_env
    insight = recognition(env, 'alpha beta gamma')
    kinds, proofs = transport(env)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text='alpha beta gamma?')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert kinds == ['primary'] and proofs == []
    assert [row['id'] for row in receipt['citations']] == [insight.id]
    assert receipt['trace'][0]['rewrite_status'] == 'skipped'


def test_http_v4_detail_keeps_original_lower_read_when_upper_numeric_coverage_is_sufficient(fast_env):
    env = fast_env
    doc = document(env, summary='alpha beta gamma 原文', body='alpha beta gamma 原文 original detail')
    kinds, proofs = transport(env)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text='alpha beta gamma 原文?')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert kinds == ['primary'] and proofs == []
    summary = next(row for row in receipt['trace'] if row['layer'] == 'summary')
    assert summary['coverage'] == 1 and summary['stopped'] is False
    assert any(row['id'] == doc and row['layer'] == 'note' for row in receipt['citations'])


def test_http_default_v3_preserves_original_full_ladder_without_gap_binding(fast_env):
    env = fast_env
    materials(env)
    kinds, proofs = transport(env)
    response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    store = env.app.state.ai_turn_store
    assert store.get_request(turn['id'])['policy_versions']['retrieve'] == '@3'
    assert kinds == ['primary'] and proofs == []
    assert store.get_immutable_payload(turn['id'], BINDING) is None
    assert ACTIVE['retrieve'] == '@3'


def test_http_gap_network_failure_preserves_original_citations_ladder_and_budget(fast_env):
    env = fast_env
    materials(env)
    kinds, proofs = transport(env, gap_failure=True)
    baseline = ask(env, intent='ask', text=QUESTION)
    assert baseline.status_code == 200, baseline.text
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 200, response.text
    old, new = (reply.json()['turn']['receipt']['ask'] for reply in (baseline, response))
    assert kinds == ['primary', 'gap', 'primary'] and len(proofs) == 1
    assert new['citations'] == old['citations'] and new['layers'] == old['layers']
    omitted = {'rewrite', 'rewrite_status', 'rewrite_receipt_ids'}
    assert [{key: value for key, value in row.items() if key not in omitted} for row in new['trace']] == [
        {key: value for key, value in row.items() if key not in omitted} for row in old['trace']]
    assert new['context']['window'] == old['context']['window']
    assert new['context']['reserve'] == old['context']['reserve']
    assert new['trace'][0]['rewrite_status'] == 'failed'
    assert new['model_usage']['total_tokens'] == 5 and new['model_usage']['observed_only'] is True


def test_http_private_lower_source_is_absent_from_gap_primary_and_proof(fast_env):
    env = fast_env
    third = materials(env)
    root = document_roots(env.records, WorkScope('local-user', 'alpha'), env.documents.read(third)['source_refs'])[0]
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), *root, 0, [])
    kinds, proofs = transport(env)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 200, response.text
    assert kinds == ['gap', 'primary'] and len(proofs) == 1
    assert all('deepomega' not in '\n'.join(row['content'] for row in call['messages']) for call in env.calls)
    assert all(ref['id'] != third for ref in proofs[0][2]['material_proof']['privacy']['material_refs'])
    assert all(row['id'] != third for row in response.json()['turn']['receipt']['ask']['citations'])


def test_http_private_project_and_empty_scope_create_no_model_wire(fast_env):
    env = fast_env
    kinds, proofs = transport(env)
    with override(retrieve='@4'):
        empty = ask(env, intent='ask', text=QUESTION)
    assert empty.status_code == 200 and empty.json()['turn']['receipt']['ask']['no_match'] is True
    set_private_project(env.records, 'alpha', True, 0)
    with override(retrieve='@4'):
        private = ask(env, intent='ask', text=QUESTION)
    assert private.status_code == 409 and private.json()['detail'] == 'private_project_remote_blocked'
    assert kinds == [] and proofs == [] and env.calls == []


def test_http_history_condense_gap_primary_use_three_actual_calls_and_original_cost(fast_env):
    env = fast_env
    materials(env)
    recognition(env, 'meridian orbit')
    env.model.update_model_prices('generation', {'input_per_million': '2', 'output_per_million': '8',
        'cache_read_per_million': '0.04'}, expected_revision=0, expected_configuration_revision=1)
    kinds, proofs = transport(env, cache_reported=True)
    with override(retrieve='@4'):
        first = ask(env, intent='ask', text='meridian orbit?')
    assert first.status_code == 200, first.text
    assert kinds == ['primary']
    body = {'project_id': 'alpha', 'intent': 'ask', 'text': '它还缺哪些细节？', 'thread_id': first.json()['thread_id']}
    with override(retrieve='@4'):
        response = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'gap-followup'})
    assert response.status_code == 200, response.text
    turn, receipt = response.json()['turn'], response.json()['turn']['receipt']['ask']
    assert kinds == ['primary', 'condense', 'gap', 'primary']
    store = env.app.state.ai_turn_store
    assert store.get_request(turn['id'])['input']['text'] == body['text']
    assert store.get_request(turn['id'])['policy_versions']['retrieve'] == '@4'
    assert proofs[0][0] == turn['id'] and QUESTION in proofs[0][2]['messages'][-1]['content']
    assert receipt['trace'][0]['condensed_question'] == QUESTION
    assert receipt['trace'][0]['history_turn_ids'] == [{'id': first.json()['turn']['id'], 'revision': 1}]
    assert body['text'] in env.calls[-1]['messages'][-1]['content']
    assert 'Synthetic answer' in env.calls[-1]['messages'][-1]['content']
    completed = [store.get(event['data']['receipt_ref']) for event in store.events_after(turn['id'])
        if event['type'] == 'model.completed']
    assert [row['model_call_purpose'] for row in completed] == ['aux', 'aux', 'primary']
    assert len({row['model_request_id'] for row in completed}) == 3
    assert completed[1]['model_request_id'] == proofs[0][1]['model_request_id']
    assert receipt['model_usage']['total_tokens'] == 15
    calls = next(group['calls'] for group in kernel_call_groups(env.root, turn_id=turn['id'], records=env.records)
        if group['turn_id'] == turn['id'])
    assert len(calls) == 3 and all(row['usage']['total_tokens'] == 5 for row in calls)
    prices = [row for row in env.records.list('v2_model_wire_prices') if row.payload['turn_id'] == turn['id']]
    assert len(prices) == 3 and all(row.revision == 1 and row.payload['price_revision'] == 1
        and row.payload['configuration_revision'] == 1 and row.payload['model_id'] == 'main'
        and row.payload['rates'] == {'input_per_million': '2', 'output_per_million': '8',
                                   'cache_read_per_million': '0.04'} for row in prices)
    wires = [store.get(event['data']['receipt_ref']) for event in store.events_after(turn['id'])
        if event['type'] == 'model.attempt.terminal']
    assert len(wires) == 3 and all(row['usage_status'] == 'reported' and row['cache_status'] == 'reported'
        and row['cache_metadata']['cache_read_input_tokens'] == 0 for row in wires)
    assert all(Decimal(row['cost']['amount']) == Decimal('0.000028') for row in calls)
    assert Decimal(receipt['model_cost']['amount']) == sum(Decimal(row['cost']['amount']) for row in calls)
    assert Decimal(receipt['model_cost']['amount']) == Decimal('0.000084')
    env.model.update('generation', {'allow_remote': False, 'expected_revision': 1})
    with override(retrieve='@3'):
        replay = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'gap-followup'})
    assert replay.status_code == 200 and replay.json() == response.json()
    assert kinds == ['primary', 'condense', 'gap', 'primary']


def test_http_source_revoked_after_gap_dispatch_blocks_primary_and_keeps_actual_wire(fast_env):
    env = fast_env
    materials(env)
    def revoke():
        active = ACTIVE_ANSWER.get()
        frozen = active[1].get(active[1].get_immutable_payload(active[0]['turn_id'], BINDING)[1]['input_ref'])
        root = frozen['material_proof']['privacy']['source_snapshots'][0]['roots'][0]
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
            root['type'], root['id'], root['revision'], 0, [])
    kinds, proofs = transport(env, after_gap=revoke)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 409 and response.json()['detail'] == 'source_changed_retry'
    assert kinds == ['gap'] and len(proofs) == 1
    store, identity = env.app.state.ai_turn_store, proofs[0][0]
    wires = [store.get(event['data']['receipt_ref']) for event in store.events_after(identity)
        if event['type'] == 'model.attempt.terminal']
    assert len(wires) == 1 and wires[0]['usage']['total_tokens'] == 5
    assert store.get_immutable_payload(identity, 'answer-model-result-gap-drilldown') is None


def test_http_remote_revoked_after_gap_dispatch_preserves_original_permission_error(fast_env):
    env = fast_env
    materials(env)
    def revoke():
        env.model.update('generation', {'allow_remote': False, 'expected_revision': 1})
    kinds, proofs = transport(env, after_gap=revoke)
    with override(retrieve='@4'):
        response = ask(env, intent='ask', text=QUESTION)
    assert response.status_code == 409 and response.json()['detail'] == 'remote_disabled'
    assert kinds == ['gap'] and len(proofs) == 1
    identity, _, _ = proofs[0]
    store = env.app.state.ai_turn_store
    assert len([event for event in store.events_after(identity) if event['type'] == 'model.requested']) == 1
    assert store.get_immutable_payload(identity, 'answer-model-result-gap-drilldown') is None


def test_http_gap_timeout_late_wire_is_discarded_and_cached_turn_creates_no_new_call(fast_env):
    env = fast_env
    materials(env)
    release, settled = Event(), Event()
    retained = {}
    def delay():
        retained['handle'] = ACTIVE_ANSWER.get()[3]['gap-drilldown']
        try:
            retained['released'] = release.wait(timeout=60)
            assert retained['released']
        finally:
            settled.set()
    def fallback_dispatch():
        # The original asyncio.run drains workers before HTTP returns. Observe
        # the aux terminal at the actual fallback wire, while its response waits.
        handle = retained['handle']
        assert handle.terminal == 'failed' and not handle.discarded and not settled.is_set()
        retained['primary_call'] = ACTIVE_ANSWER.get()[3]['answer'].model_request_id
        release.set()
    kinds, proofs = transport(env, after_gap=delay, before_primary=fallback_dispatch)
    body = {'project_id': 'alpha', 'intent': 'ask', 'text': QUESTION}
    try:
        with override(retrieve='@4'):
            response = env.http.post('/api/v2/workbench/turns', json=body,
                headers={'Idempotency-Key': 'gap-timeout'})
        assert response.status_code == 200, response.text
        turn, receipt = response.json()['turn'], response.json()['turn']['receipt']['ask']
        assert kinds == ['gap', 'primary'] and len(proofs) == 1
        assert receipt['trace'][0]['rewrite_status'] == 'timeout'
        assert receipt['trace'][0]['rewrite'] == {'queries': [], 'used': False}
        assert receipt['model_usage']['total_tokens'] == 10 and receipt['model_usage']['observed_only'] is True
        store = env.app.state.ai_turn_store
        saved = env.records.read('v2_turns', turn['id'])
        result = store.get_immutable_payload(turn['id'], 'product-answer-result-v2')
        handle = retained['handle']
        assert handle.terminal == 'failed' and retained['primary_call'] != handle.model_request_id
        assert result[1]['receipt']['ask']['model_usage']['total_tokens'] == 5
        assert result[1]['receipt']['ask']['model_usage']['observed_only'] is True
    finally:
        release.set()
    assert settled.wait(timeout=3)
    assert retained['released'] is True
    deadline = time.monotonic() + 3
    while not handle.discarded and time.monotonic() < deadline:
        time.sleep(.01)
    assert handle.discarded and handle.terminal == 'failed'
    assert env.records.read('v2_turns', turn['id']) == saved
    assert store.get_immutable_payload(turn['id'], 'product-answer-result-v2') == result
    assert store.get_immutable_payload(turn['id'], 'answer-model-result-gap-drilldown') is None
    wires = [store.get(event['data']['receipt_ref']) for event in store.events_after(turn['id'])
        if event['type'] == 'model.attempt.terminal']
    assert len(wires) == 2 and all(row['usage']['total_tokens'] == 5 for row in wires)
    assert {row['model_request_id'] for row in wires} == {handle.model_request_id, retained['primary_call']}
    env.model.update('generation', {'allow_remote': False, 'expected_revision': 1})
    with override(retrieve='@3'):
        replay = env.http.post('/api/v2/workbench/turns', json=body,
            headers={'Idempotency-Key': 'gap-timeout'})
    assert replay.status_code == 200 and replay.json()['turn']['id'] == turn['id']
    assert replay.json()['turn']['receipt']['ask']['trace'] == receipt['trace']
    assert kinds == ['gap', 'primary'] and len(proofs) == 1 and len(env.calls) == 2
    assert len([event for event in store.events_after(turn['id']) if event['type'] == 'model.requested']) == 2
