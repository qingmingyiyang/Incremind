"""跨尝试只绑定原执行事实；闭合产品胶囊和动作授权仍由原产品主人提供。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from core.ai_kernel import (AIKernelRuntimeError, RunLeaseRevoked, ScopedCapabilityRegistry,
    SQLiteAITurnStore, SynchronousAIRuntime, TurnEventConflict)


ROOT = Path(__file__).resolve().parents[2]


class _ExternalPlanner:
    # 仅供应外部计划决定；SQLite、Runtime、Handle 和 Effect 均使用原实现。
    def plan(self, *_args, **_kwargs):
        return {'type': 'complete', 'summary': 'done', 'evidence_refs': []}


class _Owner:
    def __init__(self, tmp_path):
        self.database = tmp_path / 'provider-resume.sqlite3'
        self.store = SQLiteAITurnStore(self.database)
        self.runtime = SynchronousAIRuntime(planner=_ExternalPlanner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store)
        self.request = json.loads((ROOT / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text())
        self.runtime.accept_turn(self.request)
        now = datetime.now(timezone.utc)
        self.lease = self.store.try_acquire_run_lease(self.request['turn_id'], 'resume-original-owner',
            now=now, stale_after=now + timedelta(seconds=120))
        assert self.lease is not None
        self.token = self.runtime._run_lease_context.set(self.lease)
        self.route_ref = self.store.get_or_create_immutable_payload(self.request['turn_id'], 'resume-test-route',
            {'project_id': self.request['scope']['project_id'], 'provider': 'openai', 'model': 'gpt-5.4-mini'})

    def begin(self, *, revision='a' * 64, provider='openai', model='gpt-5.4-mini', route_ref=None):
        control = self.runtime._begin_planner_control(self.request['turn_id'],
            step_id='resume-step-' + uuid4().hex, model_request_id='resume-model-' + uuid4().hex)
        control.model_call_routed(snapshot_ref=route_ref or self.route_ref, snapshot_revision=revision,
            prompt_cache_scope_identity='b' * 64, provider=provider, model=model, execution_location='remote')
        control.model_call_started(provider=provider, model=model)
        self.control = control
        return control.begin_model_wire_attempt()

    def failed_source(self, *, cancelled=False):
        handle = self.begin()
        refs = []
        def external_failure():
            for number in (0, 2):
                refs.append(handle.observe_provider_checkpoint({'response_id': 'resp_owned_resume', 'sequence_number': number}))
            if cancelled:
                handle.consumer_cancelled()
            else:
                handle.failed_transport(error_code='ai.original_interrupted')
            raise ConnectionError('synthetic external provider disconnect')
        with pytest.raises(ConnectionError):
            handle.invoke_wire(external_failure)
        assert handle._receipt_ref is not None
        # 原执行者先记逻辑调用终态并释放 planner control，后续才是新的真实尝试。
        self.control.model_call_failed()
        assert not self.runtime._record_planner_model_terminal(self.request['turn_id'], self.control,
            fallback_status='failed', fallback_error_code='ai.original_interrupted')
        self.runtime._finish_planner_control(self.request['turn_id'], self.control)
        return handle, refs

    def resolve(self, handle, **changes):
        values = {'attempt_id': handle.dispatch['attempt_id'], 'dispatch_ref': handle.dispatch_ref,
            'terminal_ref': handle._receipt_ref}
        values.update(changes)
        return self.store.resolve_model_provider_resume_source(self.request['turn_id'], **values)

    def immutable_facts(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute('SELECT * FROM ai_turn_immutable_payloads ORDER BY payload_ref').fetchall()

    def source_facts(self, handle):
        with sqlite3.connect(self.database) as connection:
            return (self.store.get(handle._receipt_ref),
                connection.execute('SELECT * FROM effect WHERE operation_id=?', (handle.dispatch['attempt_id'],)).fetchone(),
                connection.execute('SELECT * FROM ai_model_attempt_reservations WHERE attempt_id=?',
                    (handle.dispatch['attempt_id'],)).fetchone())


@pytest.fixture
def owner(tmp_path):
    result = _Owner(tmp_path)
    try:
        yield result
    finally:
        result.runtime._run_lease_context.reset(result.token)


def test_real_failed_source_binds_once_inside_new_original_handler(owner):
    old, refs = owner.failed_source()
    source = owner.resolve(old)
    assert source == {'attempt_id': old.dispatch['attempt_id'], 'dispatch_ref': old.dispatch_ref,
        'terminal_ref': old._receipt_ref, 'checkpoint_ref': refs[-1],
        'cursor': {'response_id': 'resp_owned_resume', 'sequence_number': 2}}
    old_facts = deepcopy(owner.source_facts(old))
    current = owner.begin()
    wires, links = [], []
    def external_get():
        links.append(current.bind_provider_resume(source))
        assert current.bind_provider_resume(source) == links[-1]
        wires.append('GET')
        current.succeeded(usage={'input_tokens': 4, 'output_tokens': 3}, cache_observation=None)
        return 'complete original output'
    assert current.invoke_wire(external_get) == 'complete original output'
    assert wires == ['GET'] and owner.source_facts(old) == old_facts
    assert current.dispatch['attempt_id'] != old.dispatch['attempt_id']
    assert current.dispatch['model_request_id'] != old.dispatch['model_request_id']
    linked = owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + current.dispatch['attempt_id'])
    assert linked[0] == links[0]
    assert linked[1]['source'] == source and linked[1]['dispatch'] == current.dispatch
    assert linked[1]['dispatch_ref'] == current.dispatch_ref
    assert set(linked[1]) == {'schema_version', 'source', 'dispatch', 'dispatch_ref', 'run_lease', 'effect_lease'}
    events = owner.store.events_after(owner.request['turn_id'])
    assert sum(row['type'] == 'model.attempt.dispatched' for row in events) == 2
    assert sum(row['type'] == 'model.attempt.terminal' for row in events) == 2
    with sqlite3.connect(owner.database) as connection:
        states = dict(connection.execute('SELECT operation_id,state FROM effect'))
    assert states[old.dispatch['attempt_id']] == 'UNKNOWN'
    assert states[current.dispatch['attempt_id']] == 'SETTLED_OK'


def test_source_descriptor_is_detached_and_cannot_bind_outside_effect(owner):
    old, _ = owner.failed_source()
    source = owner.resolve(old)
    source['cursor']['sequence_number'] = 17
    assert owner.resolve(old)['cursor']['sequence_number'] == 2
    current = owner.begin()
    before = owner.immutable_facts()
    with pytest.raises(RunLeaseRevoked):
        current.bind_provider_resume(owner.resolve(old))
    assert owner.immutable_facts() == before


@pytest.mark.parametrize('field', ['attempt_id', 'dispatch_ref', 'terminal_ref'])
def test_source_exact_refs_cannot_be_substituted(owner, field):
    old, _ = owner.failed_source()
    before = owner.immutable_facts()
    with pytest.raises(TurnEventConflict):
        owner.resolve(old, **{field: 'crp://session/foreign/wrong'})
    assert owner.immutable_facts() == before


def test_source_without_terminal_and_cancelled_source_are_rejected(owner):
    active = owner.begin()
    with pytest.raises(TurnEventConflict):
        owner.resolve(active)
    def end():
        active.consumer_cancelled()
        raise ConnectionError('synthetic cancellation')
    with pytest.raises(ConnectionError):
        active.invoke_wire(end)
    with pytest.raises(TurnEventConflict):
        owner.resolve(active)


@pytest.mark.parametrize('change', [{'revision': 'c' * 64}, {'provider': 'other'}, {'model': 'other-model'}])
def test_new_attempt_route_provider_model_drift_has_zero_provider_wire(owner, change):
    old, _ = owner.failed_source()
    source, current = owner.resolve(old), owner.begin(**change)
    wires = []
    def rejected():
        try:
            current.bind_provider_resume(source)
            wires.append('GET')
        except TurnEventConflict:
            current.failed_transport(error_code='ai.resume_source_invalid')
            raise
    with pytest.raises(TurnEventConflict):
        current.invoke_wire(rejected)
    assert wires == []
    assert owner.store.get(current._receipt_ref)['status'] == 'failed_transport'
    assert owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + current.dispatch['attempt_id']) is None


@pytest.mark.parametrize('field,value', [('previous_ref', 'crp://session/foreign/predecessor'),
    ('dispatch_ref', 'crp://session/foreign/dispatch'), ('cursor', {'response_id': 'resp_other', 'sequence_number': 2}),
    ('run_lease', {'owner_id': 'forged', 'generation': 1}),
    ('effect_lease', {'owner_id': 'forged', 'attempt': True})])
def test_full_immutable_chain_corruption_is_not_resolved(owner, field, value):
    old, refs = owner.failed_source()
    with sqlite3.connect(owner.database) as connection:
        payload = json.loads(connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=?',
            (refs[-1],)).fetchone()[0])
        payload[field] = value
        connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE payload_ref=?',
            (json.dumps(payload), refs[-1]))
    before = owner.immutable_facts()
    with pytest.raises(TurnEventConflict):
        owner.resolve(old)
    assert owner.immutable_facts() == before


def test_new_real_run_generation_binds_current_lease_without_rebuilding_old_token(owner):
    old, _ = owner.failed_source()
    source, old_facts, old_lease = owner.resolve(old), deepcopy(owner.source_facts(old)), owner.lease
    owner.store.release_strict_run_lease(old_lease)
    now = datetime.now(timezone.utc)
    replacement = owner.store.try_acquire_run_lease(old_lease.turn_id, 'resume-new-owner',
        now=now, stale_after=now + timedelta(seconds=120))
    assert replacement is not None and replacement.generation == old_lease.generation + 1
    token = owner.runtime._run_lease_context.set(replacement)
    try:
        current, wires = owner.begin(), []
        def external_get():
            before = owner.immutable_facts()
            with pytest.raises(RunLeaseRevoked):
                owner.store.commit_model_provider_resume_binding(dispatch_payload=current.dispatch,
                    dispatch_payload_ref=current.dispatch_ref, source=source, run_lease=old_lease)
            assert owner.immutable_facts() == before and wires == []
            link = current.bind_provider_resume(source)
            payload = owner.store.get(link)
            assert payload['run_lease'] == {'owner_id': replacement.owner_id, 'generation': replacement.generation}
            assert payload['source'] == source
            wires.append('GET')
            current.succeeded(usage={'input_tokens': 4, 'output_tokens': 3}, cache_observation=None)
            return 'complete original output'
        assert current.invoke_wire(external_get) == 'complete original output'
        assert wires == ['GET'] and owner.source_facts(old) == old_facts
        with sqlite3.connect(owner.database) as connection:
            assert connection.execute('SELECT state FROM effect WHERE operation_id=?',
                (old.dispatch['attempt_id'],)).fetchone()[0] == 'UNKNOWN'
            assert connection.execute('SELECT state FROM effect WHERE operation_id=?',
                (current.dispatch['attempt_id'],)).fetchone()[0] == 'SETTLED_OK'
    finally:
        owner.runtime._run_lease_context.reset(token)


@pytest.mark.parametrize('case', ['expired-run', 'takeover', 'expired-effect', 'wrong-effect-owner', 'wrong-effect-attempt'])
def test_current_actual_lease_loss_cannot_bind_or_reach_provider(owner, case):
    old, _ = owner.failed_source()
    source, current, wires = owner.resolve(old), owner.begin(), []
    def rejected():
        if case == 'takeover':
            now = datetime.now(timezone.utc) + timedelta(seconds=130)
            assert owner.store.mark_run_lease_stale(owner.lease, now=now) is not None
            replacement = owner.store.takeover_run_lease(owner.lease.turn_id, expected_generation=owner.lease.generation,
                owner_id='replacement-owner', now=now, stale_after=now + timedelta(seconds=120), disposition='safe')
            assert replacement is not None
        else:
            with sqlite3.connect(owner.database) as connection:
                if case == 'expired-run':
                    connection.execute('UPDATE ai_turn_run_leases SET stale_after=? WHERE turn_id=?',
                        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), owner.lease.turn_id))
                elif case == 'expired-effect':
                    connection.execute('UPDATE effect SET lease_expires_at=? WHERE operation_id=?',
                        (datetime.now(timezone.utc).timestamp() - 1, current.dispatch['attempt_id']))
                elif case == 'wrong-effect-owner':
                    connection.execute('UPDATE effect SET lease_owner=? WHERE operation_id=?',
                        ('foreign-effect-owner', current.dispatch['attempt_id']))
                else:
                    connection.execute('UPDATE effect SET attempt=attempt+1 WHERE operation_id=?',
                        (current.dispatch['attempt_id'],))
        before = owner.immutable_facts()
        with pytest.raises(RunLeaseRevoked):
            current.bind_provider_resume(source)
        assert owner.immutable_facts() == before and wires == []
        raise ConnectionError('lease rejected before external provider')
    with pytest.raises(ConnectionError):
        current.invoke_wire(rejected)
    assert wires == []
    assert owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + current.dispatch['attempt_id']) is None


def test_same_revision_with_another_actual_route_ref_has_zero_provider_wire(owner):
    old, _ = owner.failed_source()
    source = owner.resolve(old)
    alternate = owner.store.get_or_create_immutable_payload(owner.request['turn_id'], 'different-route-instance',
        {'project_id': owner.request['scope']['project_id'], 'provider': 'openai', 'model': 'gpt-5.4-mini'})
    current, wires = owner.begin(route_ref=alternate), []
    def rejected():
        with pytest.raises(TurnEventConflict):
            current.bind_provider_resume(source)
        assert wires == []
        current.failed_transport(error_code='ai.resume_source_invalid')
        raise ConnectionError('route rejected before external provider')
    with pytest.raises(ConnectionError):
        current.invoke_wire(rejected)
    assert wires == [] and owner.store.get(current._receipt_ref)['status'] == 'failed_transport'


def test_changed_descriptor_and_failed_sql_binding_leave_zero_provider_wire(owner):
    old, _ = owner.failed_source()
    source, current, wires = owner.resolve(old), owner.begin(), []
    def rejected():
        changed = deepcopy(source)
        changed['cursor']['sequence_number'] = 17
        before = owner.immutable_facts()
        with pytest.raises(TurnEventConflict):
            current.bind_provider_resume(changed)
        assert owner.immutable_facts() == before
        with sqlite3.connect(owner.database) as connection:
            connection.execute("CREATE TRIGGER reject_resume BEFORE INSERT ON ai_turn_immutable_payloads "
                "WHEN NEW.kind LIKE 'model-provider-resume-%' BEGIN SELECT RAISE(ABORT,'synthetic binding failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            current.bind_provider_resume(source)
        assert owner.immutable_facts() == before and wires == []
        current.failed_transport(error_code='ai.resume_binding_failed')
        raise ConnectionError('binding rejected before external provider')
    with pytest.raises(ConnectionError):
        current.invoke_wire(rejected)
    assert wires == [] and owner.store.get(current._receipt_ref)['status'] == 'failed_transport'
    with sqlite3.connect(owner.database) as connection:
        assert connection.execute('SELECT state FROM effect WHERE operation_id=?',
            (current.dispatch['attempt_id'],)).fetchone()[0] == 'UNKNOWN'
    assert owner.store.get_immutable_payload(owner.request['turn_id'], 'model-provider-resume-' + current.dispatch['attempt_id']) is None
