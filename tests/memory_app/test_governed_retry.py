"""Governed configuration, durable attempts and prices; only provider is fake."""
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.policies.retry import decide
from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore


class Answer(BaseModel):
    answer: str


class TemporaryFailure(RuntimeError):
    status_code = 503


class Stream:
    def __init__(self, chunks, error=None, close_error=None):
        self.chunks, self.error = iter(chunks), error
        self.closed = False
        self.close_error, self.close_calls = close_error, 0

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.error is not None:
                raise self.error
            raise

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


def no_wait_policy(request):
    # A deterministic injected test recipe, not an ACTIVE selection.
    decision = decide(request)
    return {**decision, 'delay': 0} if decision.get('retry') else decision


def governed_runtime(tmp_path, *, streaming=False, retry=True, changed=None, deadline=None,
                     reported_cache=True, enough_budget=False, eof_phase=None, close_error=None, close_supported=True):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    models.update('generation', {'base_url': 'https://proxy.invalid/v1', 'model': 'writer',
        'api_key': 'test-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_model_prices('generation', {'input_per_million': '2',
        'output_per_million': '3', 'cache_read_per_million': '0.1'},
        expected_revision=0, expected_configuration_revision=1)
    baseline = models.public(), models.snapshot('generation')
    calls, streams, deltas, checks, outputs = [], [], [], [], []
    current = {'allowed': True}
    remaining_observations = []
    retry_requests = []
    usage = {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
    if reported_cache:
        usage['prompt_tokens_details'] = {'cached_tokens': 0}

    def validate_current():
        checks.append(len(calls))
        if not current['allowed']:
            raise PermissionError('synthetic source private')

    def completion(**request):
        if streams:
            assert streams[-1].closed
        calls.append(request)
        assert request['max_retries'] == 0
        if len(calls) == 1:
            if changed == 'source':
                current['allowed'] = False
            if changed == 'configuration':
                models.update('generation', {'model': 'changed', 'expected_revision': 1})
            if streaming:
                delta = {'content': '{"answer":"partial\\n\\n'} if eof_phase == 'body' else {'reasoning_content': 'synthetic'}
                stream = Stream([{'choices': [{'delta': delta}],
                    'usage': usage}],
                    None if eof_phase is not None else TemporaryFailure('temporary'), close_error)
                if not close_supported:
                    stream.close = None
                streams.append(stream)
                return stream
            raise TemporaryFailure('temporary')
        if streaming:
            stream = Stream([{'choices': [{'delta': {'content': '{"answer":"done"}'}}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': usage}])
            streams.append(stream)
            return stream
        return {'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}],
            'usage': usage}

    models._completion_fn = completion
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    identity = 'governed-retry-real'

    def policy(request):
        retry_requests.append(request)
        if 'remaining' in request:
            remaining_observations.append(request['remaining'])
        return no_wait_policy(request)

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            cfg = models.public()['generation']
            public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                'allow_remote', 'revision', 'configured', 'has_api_key')}
            ref = store.get_or_create_immutable_payload(identity, 'memory-model-route-v1', public)
            route = {'payload_ref': ref, 'revision': 'a' * 64,
                'prompt_cache_scope_identity': 'b' * 64, 'configuration': public,
                'execution_location': 'remote'}
            options = {'retry_policy': policy} if retry else {}
            if deadline is not None:
                options['timeout_seconds'] = deadline
            outputs.append(models.complete_governed([{'role': 'user', 'content': 'synthetic'}],
                routing_snapshot=route, execution_control=execution_control,
                metadata_sink=execution_control, wire_attempt_sink=execution_control,
                validate_current=validate_current,
                response_model=Answer if streaming else None,
                on_delta=deltas.append if streaming else None, **options))
            return {'type': 'complete', 'summary': 'done'}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store,
        **({'planner_timeout_ms': 900_000} if enough_budget else {}))
    if enough_budget:
        # Existing historical generic request schema permits the actual 900s
        # runtime budget. Product template budgets remain untouched and capped.
        request = json.loads((Path(__file__).resolve().parents[2] /
            'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
        request['turn_id'] = identity
    else:
        request = freeze_turn_request('project.answer', turn_id=identity, session_id='retry-session',
            operation_id='retry-operation', idempotency_key=identity, project_id='alpha',
            created_at='2026-10-05T00:00:00Z', text='synthetic', capabilities=[],
            privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
                'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    receipt = runtime.submit_turn(request)
    attempts = [store.get(event['data']['receipt_ref']) for event in runtime.events_after(identity)
        if event['type'] == 'model.attempt.terminal']
    return locals()


@pytest.mark.parametrize('streaming', [False, True])
def test_governed_injected_recipe_retries_actual_wire_and_preserves_configuration(tmp_path, streaming):
    state = governed_runtime(tmp_path, streaming=streaming)
    assert state['receipt'].status == 'completed'
    assert len(state['calls']) == 2
    assert len(state['attempts']) == 2
    assert len({r['attempt_id'] for r in state['attempts']}) == 2
    assert [r['status'] for r in state['attempts']] == ['failed_transport', 'succeeded']
    assert state['models'].public() == state['baseline'][0]
    assert state['models'].snapshot('generation') == state['baseline'][1]
    assert 1 in state['checks'] and 2 in state['checks']
    if streaming:
        assert state['deltas'] == ['done']
        assert all(stream.closed for stream in state['streams'])
        assert state['outputs'][0][1]['usage'] == {'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12}
        groups = kernel_call_groups(tmp_path, records=state['records'])
        assert groups[0]['calls'][0]['cost'] == {'currency': 'CNY', 'amount': '0.000028'}
        assert [r['usage'] for r in state['attempts']] == [
            {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}] * 2
        assert len(state['records'].list('v2_model_wire_prices')) == 2


@pytest.mark.parametrize('changed', ['source', 'configuration'])
def test_governed_revalidation_blocks_retry_after_actual_first_wire(tmp_path, changed):
    state = governed_runtime(tmp_path, changed=changed)
    assert state['receipt'].status == 'failed'
    assert len(state['calls']) == 1 and len(state['attempts']) == 1
    assert state['outputs'] == []


def test_no_injected_recipe_keeps_historical_single_attempt(tmp_path):
    state = governed_runtime(tmp_path, retry=False)
    assert state['receipt'].status == 'failed'
    assert len(state['calls']) == 1 and len(state['attempts']) == 1


def test_failed_attempt_without_reported_cache_is_not_a_known_total_cost(tmp_path):
    state = governed_runtime(tmp_path, streaming=True, reported_cache=False)
    assert state['receipt'].status == 'completed'
    assert len(state['calls']) == len(state['attempts']) == 2
    assert state['outputs'][0][1]['usage']['total_tokens'] == 12
    assert kernel_call_groups(tmp_path, records=state['records'])[0]['calls'][0]['cost'] is None


def test_injected_retry_does_not_extend_existing_four_second_route_budget(tmp_path):
    state = governed_runtime(tmp_path, deadline=4)
    assert state['receipt'].status == 'completed'
    assert all(0 < r['timeout'] <= 4 for r in state['calls'])
    assert state['calls'][1]['timeout'] <= state['calls'][0]['timeout']


def test_frozen_retry_limits_replace_fixed_service_default_with_real_execution_budget(tmp_path):
    state = governed_runtime(tmp_path, enough_budget=True)
    assert state['receipt'].status == 'completed'
    assert state['calls'][0]['timeout'] == 180
    assert 590 < state['remaining_observations'][0] <= 600
