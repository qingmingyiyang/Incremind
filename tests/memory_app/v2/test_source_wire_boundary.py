"""原 Source 与 SQLite Runtime 的发送边界；driver 不代签产品 planner 的集成。"""
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import json
import sqlite3
import sys
from threading import Event, Thread, current_thread

import pytest

from backend.memory_app.model_config import ModelConfiguration, _GenerationEgressLease
from backend.memory_app.model_costs import PriceRecordingSink
from backend.memory_app.original_sources import source_store
from backend.memory_app.research_sources import ReadControl, ReadPlanner
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.turn_requests import freeze_product_turn, validate_frozen_inputs
from backend.recognition import RecognitionConflict, WorkScope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import _ProviderAttemptTerminal
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.ports import is_durable_model_wire_commit_witness
from core.ai_kernel.runtime import _PlannerExecutionContext
from core.context_graph.capability_artifact import CapabilityArtifactStore
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture(autouse=True)
def forbid_factory_and_artifact_capture(monkeypatch):
    factories = {'backend.memory_app.app', 'backend.api.app'}
    loaded_factories = factories.intersection(sys.modules)
    hits = []

    def forbidden(*args, **kwargs):
        hits.append(True)
        raise AssertionError('Source 边界测试禁止完整 factory 与 ArtifactStore')

    monkeypatch.setattr(CapabilityArtifactStore, '__init__', forbidden)
    monkeypatch.setattr(CapabilityArtifactStore, 'capture', forbidden)
    # 允许其他合法测试先导入 factory；本 case 仍不得调用或新增 factory。
    for name in loaded_factories:
        monkeypatch.setattr(sys.modules[name], 'create_app', forbidden)
    yield
    assert factories.intersection(sys.modules) <= loaded_factories and not hits


class SourceWireScenario:
    """模型传输是唯一替身；其余包装只观察并委托原方法。"""
    def __init__(self, tmp_path, monkeypatch, case):
        self.case = case
        self.records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data/structured-records.sqlite3')
        self.source = source_store(self.records)
        self.scope, self.identity = WorkScope('local-user', 'alpha'), 'boundary-source'
        self.text = 'Synthetic original source revision one'
        self.body = {'id': self.identity, 'project_id': 'alpha', 'type': 'text', 'title': 'Synthetic original',
            'metadata': {'content_snapshot': self.text, 'content_structure': {'status': 'completed',
                'summary': self.text, 'structured_body': self.text, 'key_points': [self.text]}}}
        assert self.source.write('sources', self.identity, self.body, expected_revision=0) == 1
        self.models = ModelConfiguration(self.records, tmp_path, InMemorySecretStore(),
            completion_fn=self.completion)
        self.models.update('generation', {'base_url': 'https://proxy.invalid/v1', 'model': 'writer',
            'api_key': 'synthetic-source-key', 'allow_remote': True, 'expected_revision': 0})
        self.models.update_model_prices('generation', {'input_per_million': '2', 'output_per_million': '3',
            'cache_read_per_million': '0.1'}, expected_revision=0,
            expected_configuration_revision=self.models.public()['generation']['revision'])
        self.database_path = tmp_path / '.rebuild-data/ai-turns.sqlite3'
        self.store = SQLiteAITurnStore(self.database_path)
        self.turn_id = 'turn-source-' + case
        self.request = freeze_product_turn('project.answer', records=self.records, models=self.models,
            project_id='alpha', materials=[{'type': 'original_source', 'id': self.identity,
                'revision': 1, 'project_id': 'alpha'}],
            load_text=lambda item: item['payload']['metadata']['content_snapshot'],
            text='Synthetic source boundary', turn_id=self.turn_id, session_id='session-source-boundary',
            operation_id='operation-source-' + case, idempotency_key=self.turn_id,
            created_at='2026-10-07T00:00:00Z', capabilities=[])
        self.frozen = self.request['privacy']['source_snapshots'][0]
        self.authority = SourceEgressService(self.records)
        self.authority.validate_snapshot(self.scope, self.frozen)
        self.authority.require(self.frozen, 'generation')
        self.held = ContextVar('source_boundary_actual_locks', default=0)
        self.sql_attempted, self.json_attempted, self.finished = Event(), Event(), Event()
        self.writer_errors, self.native, self.qualifications = [], [], []
        self.observers, self.leases, self.terminals = [], [], []
        self.dispatched, self.returned, self.guard_checks = [], [], []
        self.writer = Thread(target=self.write_source, name='original-source-writer')
        self.install_observers(monkeypatch)
        owner = self

        class Driver:
            def plan(self, request, events, capabilities, payloads, execution_control):
                assert isinstance(execution_control, ReadControl)
                cfg = owner.models.public()['generation']
                public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                    'allow_remote', 'revision', 'configured', 'has_api_key')}
                ref = owner.store.get_or_create_immutable_payload(owner.turn_id, 'memory-model-route-v1', public)
                route = {'payload_ref': ref, 'revision': 'a' * 64, 'prompt_cache_scope_identity': 'b' * 64,
                    'configuration': public, 'execution_location': 'remote'}

                def validate_current():
                    stage = 'post' if owner.native else 'pre'
                    try:
                        validate_frozen_inputs(owner.records, owner.models, request)
                        execution_control.checkpoint()
                    except RecognitionConflict:
                        owner.guard_checks.append((stage, 'conflict'))
                        raise
                    owner.guard_checks.append((stage, 'ok'))

                owner.models.complete_governed([{'role': 'user', 'content': request['input']['text']}],
                    routing_snapshot=route, execution_control=execution_control,
                    metadata_sink=execution_control, wire_attempt_sink=execution_control,
                    validate_current=validate_current)
                owner.returned.append(True)
                return {'type': 'complete', 'summary': 'Synthetic completed'}

        self.runtime = SynchronousAIRuntime(planner=ReadPlanner(Driver(), self.records, self.store, None),
            registry=ScopedCapabilityRegistry(), events=self.store, payloads=self.store, state=self.store)

    @contextmanager
    def database(self):
        connection = sqlite3.connect(self.database_path.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def one(self, connection, sql, params):
        rows = connection.execute(sql, params).fetchall()
        assert len(rows) == 1
        return rows[0]

    def write_source(self):
        try:
            changed = {**self.body, 'title': 'Synthetic revised original', 'metadata': {
                'content_snapshot': 'Synthetic original source revision two'}}
            assert self.source.write('sources', self.identity, changed, expected_revision=1) == 2
        except BaseException as error:
            self.writer_errors.append(error)
        finally:
            self.finished.set()

    def completion(self, **request):
        assert self.held.get() == 0, 'Source 资格事务不得覆盖原模型传输'
        assert len(self.records.list('v2_model_wire_prices')) == 1
        assert self.text in json.dumps(request['messages'])
        self.native.append(self.source.revision('sources', self.identity))
        if self.case == 'during_wire':
            self.writer.start()
            assert self.finished.wait(10), '原 writer 被发送期来源锁阻塞'
            assert not self.writer_errors
        if self.case == 'transport_failure':
            raise RuntimeError('synthetic provider failure')
        if self.case == 'cancel':
            raise asyncio.CancelledError('synthetic consumer cancellation')
        return {'choices': [{'message': {'content': 'Synthetic output'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}

    def install_observers(self, monkeypatch):
        original_lock, original_record_begin = type(self.source).locked, type(self.records).begin
        original_validate = SourceEgressService.validate_snapshot
        original_source_begin, original_core_begin = ReadControl.begin_model_wire_attempt, _PlannerExecutionContext.begin_model_wire_attempt
        original_price_begin = PriceRecordingSink.begin_model_wire_attempt
        original_terminal_init, original_lease_finish = _ProviderAttemptTerminal.__init__, _GenerationEgressLease.finish
        owner = self

        @contextmanager
        def locked(store, collection, identity):
            target = store.root == owner.source.root and collection == 'sources' and identity == owner.identity
            if target and current_thread() is owner.writer:
                owner.json_attempted.set()
            with original_lock(store, collection, identity):
                token = owner.held.set(owner.held.get() + int(target))
                try:
                    yield
                finally:
                    owner.held.reset(token)

        def record_begin(records):
            if current_thread() is owner.writer and records.database_path == owner.records.database_path:
                owner.sql_attempted.set()
            return original_record_begin(records)

        def validate(authority, scope, snapshot):
            enlisted = isinstance(authority._records, TransactionRecords)
            state = None
            if owner.dispatched:
                with owner.database() as connection:
                    state = owner.one(connection, 'SELECT state FROM effect WHERE operation_id=?',
                        (owner.dispatched[0]['dispatch']['attempt_id'],))['state']
            entry = {'held': owner.held.get(), 'enlisted': enlisted,
                'in_transaction': enlisted and authority._records.connection.in_transaction,
                'effect': state, 'result': 'ok'}
            try:
                return original_validate(authority, scope, snapshot)
            except RecognitionConflict:
                entry['result'] = 'conflict'
                raise
            finally:
                owner.qualifications.append(entry)

        def source_begin(control, *args, **kwargs):
            if owner.case == 'before_commit':
                owner.writer.start()
                assert owner.finished.wait(10) and not owner.writer_errors
            return original_source_begin(control, *args, **kwargs)

        def core_begin(control):
            assert owner.held.get() > 0
            if owner.case == 'after_commit':
                owner.writer.start()
                assert owner.sql_attempted.wait(5)
                assert not owner.finished.is_set() and not owner.json_attempted.is_set()
            handle = original_core_begin(control)
            owner.capture_commit(control, handle)
            assert owner.held.get() > 0
            return handle

        def price_begin(sink):
            handle = original_price_begin(sink)
            if owner.case == 'after_commit':
                assert owner.finished.wait(10) and not owner.writer_errors
                assert owner.source.revision('sources', owner.identity) == 2
            return handle

        def terminal_init(terminal, *args, **kwargs):
            original_terminal_init(terminal, *args, **kwargs)
            owner.terminals.append(terminal)
            delegate = terminal._observer

            def observer(status, error):
                owner.observers.append(status)
                if delegate is not None:
                    delegate(status, error)

            terminal._observer = observer

        def lease_finish(lease, status, *, error_code=None):
            owner.leases.append((status, error_code))
            return original_lease_finish(lease, status, error_code=error_code)

        monkeypatch.setattr(type(self.source), 'locked', locked)
        monkeypatch.setattr(type(self.records), 'begin', record_begin)
        monkeypatch.setattr(SourceEgressService, 'validate_snapshot', validate)
        monkeypatch.setattr(ReadControl, 'begin_model_wire_attempt', source_begin)
        monkeypatch.setattr(_PlannerExecutionContext, 'begin_model_wire_attempt', core_begin)
        monkeypatch.setattr(PriceRecordingSink, 'begin_model_wire_attempt', price_begin)
        monkeypatch.setattr(_ProviderAttemptTerminal, '__init__', terminal_init)
        monkeypatch.setattr(_GenerationEgressLease, 'finish', lease_finish)

    def capture_commit(self, control, handle):
        assert is_durable_model_wire_commit_witness(handle.durable_commit_witness)
        dispatch = dict(handle.dispatch)
        assert dispatch['turn_id'] == control.turn_id == self.turn_id
        assert dispatch['model_request_id'] == control.model_request_id
        assert dispatch['input_stored'] is False and dispatch['output_stored'] is False
        with self.database() as connection:
            reservation = self.one(connection, 'SELECT * FROM ai_model_attempt_reservations WHERE attempt_id=?',
                (dispatch['attempt_id'],))
            assert reservation['status'] == 'committed' and reservation['terminal_receipt_ref'] is None
            for field in ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number'):
                assert reservation[field] == dispatch[field]
            assert reservation['dispatch_payload_ref'] == handle.dispatch_ref
            payload = self.one(connection, 'SELECT turn_id,kind,payload_json FROM ai_turn_payloads WHERE payload_ref=?',
                (handle.dispatch_ref,))
            assert payload['turn_id'] == self.turn_id and payload['kind'] == 'model-wire-attempt-dispatch'
            assert json.loads(payload['payload_json']) == dispatch
            events = [event for event in self.store.events_after(self.turn_id) if event['type'] == 'model.attempt.dispatched']
            assert len(events) == 1 and events[0]['data']['payload_ref'] == handle.dispatch_ref
            assert events[0]['correlation']['model_request_id'] == dispatch['model_request_id']
            effect = self.one(connection, 'SELECT * FROM effect WHERE operation_id=?', (dispatch['attempt_id'],))
            assert effect['state'] == 'PLANNED' and effect['kind'] == 'model_call'
            assert effect['turn_id'] == self.turn_id and effect['idem_key'] == dispatch['attempt_id']
            assert effect['intent_ref'] == handle.dispatch_ref
            assert effect['step_key'] == f"model-wire:{dispatch['model_request_id']}:{dispatch['attempt_number']}"
            assert json.loads(effect['rev_set']) == {'routing_snapshot': dispatch['routing_snapshot_revision'],
                'provider': dispatch['provider_id'], 'model': dispatch['model_id']}
            assert effect['gate_decision_id'] == 'frozen-route:' + dispatch['routing_snapshot_revision']
            assert effect['contract_version'] == 'legacy-v1'
        self.dispatched.append({'dispatch': dispatch, 'ref': handle.dispatch_ref})

    def run(self):
        try:
            return self.runtime.submit_turn(self.request)
        finally:
            if self.writer.ident is not None:
                self.writer.join(10)
                assert not self.writer.is_alive() and self.finished.is_set()
                assert self.sql_attempted.is_set() and self.json_attempted.is_set()
            assert not self.writer_errors

    def assert_wire_terminal(self, status):
        assert len(self.dispatched) == 1
        dispatch, ref = self.dispatched[0]['dispatch'], self.dispatched[0]['ref']
        with self.database() as connection:
            reservation = self.one(connection, 'SELECT * FROM ai_model_attempt_reservations WHERE attempt_id=?',
                (dispatch['attempt_id'],))
            assert reservation['status'] == 'terminal' and reservation['terminal_status'] == status
            assert reservation['dispatch_payload_ref'] == ref
            receipt_ref = reservation['terminal_receipt_ref']
            terminal = self.store.get(receipt_ref)
            for field in ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number',
                          'routing_snapshot_revision', 'provider_id', 'model_id', 'execution_location'):
                assert terminal[field] == dispatch[field]
            assert terminal['status'] == status
            assert terminal['input_stored'] is False and terminal['output_stored'] is False
            events = [event for event in self.store.events_after(self.turn_id) if event['type'] == 'model.attempt.terminal']
            assert len(events) == 1 and events[0]['data']['receipt_ref'] == receipt_ref
            assert events[0]['data']['evidence_refs'] == [ref]
            assert events[0]['correlation']['model_request_id'] == dispatch['model_request_id']
            effect = self.one(connection, 'SELECT * FROM effect WHERE operation_id=?', (dispatch['attempt_id'],))
            assert effect['state'] == ('SETTLED_OK' if status == 'succeeded' else 'UNKNOWN')
            if status == 'succeeded':
                assert effect['result_ref'] == receipt_ref
            assert connection.execute('SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind=?',
                (self.turn_id, 'model-wire-attempt-receipt')).fetchone()[0] == 1
        assert len(self.observers) == len(self.leases) == 1
        assert self.observers == [{'succeeded': 'succeeded', 'failed_transport': 'failed',
            'consumer_cancelled': 'cancelled'}[status]]
        assert self.leases[0][0] == self.observers[0]
        assert len(self.terminals) == 1 and self.terminals[0]._finished

    def assert_logical_failed(self):
        with self.database() as connection:
            logical = json.loads(self.one(connection,
                'SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind=?',
                (self.turn_id, 'model-call-receipt'))['payload_json'])
        assert logical['status'] == 'failed' and logical['output_recorded'] is False
        assert not self.returned


def test_source_change_before_commit_rejects_dispatch_and_provider(tmp_path, monkeypatch):
    scenario = SourceWireScenario(tmp_path, monkeypatch, 'before_commit')
    receipt = scenario.run()
    assert receipt.status == 'failed' and scenario.finished.is_set()
    assert scenario.native == [] and scenario.dispatched == []
    assert not [event for event in scenario.store.events_after(scenario.turn_id)
        if event['type'].startswith('model.attempt.')]
    with scenario.database() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
    assert scenario.observers == [] and scenario.leases == [('not_sent', 'model_attempt_dispatch_failed')]
    scenario.assert_logical_failed()


def test_source_change_after_commit_before_guard_rejects_provider_and_finishes_once(tmp_path, monkeypatch):
    scenario = SourceWireScenario(tmp_path, monkeypatch, 'after_commit')
    receipt = scenario.run()
    assert receipt.status == 'failed' and scenario.finished.is_set()
    assert scenario.native == []
    assert any(item['result'] == 'conflict' and item['enlisted'] and item['in_transaction']
        and item['held'] > 0 and item['effect'] == 'INFLIGHT' for item in scenario.qualifications)
    scenario.assert_wire_terminal('failed_transport')
    scenario.assert_logical_failed()


def test_source_change_during_wire_finishes_writer_and_keeps_real_wire_receipt(tmp_path, monkeypatch):
    scenario = SourceWireScenario(tmp_path, monkeypatch, 'during_wire')
    receipt = scenario.run()
    assert receipt.status == 'failed' and scenario.native == [1] and scenario.finished.is_set()
    assert scenario.source.revision('sources', scenario.identity) == 2
    assert scenario.guard_checks[-1] == ('post', 'conflict')
    with pytest.raises(RecognitionConflict):
        scenario.authority.validate_snapshot(scenario.scope, scenario.frozen)
    scenario.assert_wire_terminal('succeeded')
    scenario.assert_logical_failed()


@pytest.mark.parametrize('case,status', [('unchanged', 'succeeded'),
    ('transport_failure', 'failed_transport'), ('cancel', 'consumer_cancelled')])
def test_original_wire_success_failure_and_cancel_finish_once(tmp_path, monkeypatch, case, status):
    scenario = SourceWireScenario(tmp_path, monkeypatch, case)
    if case == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            scenario.run()
    else:
        receipt = scenario.run()
        assert receipt.status == ('completed' if case == 'unchanged' else 'failed')
        assert scenario.returned == ([True] if case == 'unchanged' else [])
    assert scenario.native == [1]
    assert any(item['result'] == 'ok' and item['enlisted'] and item['in_transaction']
        and item['held'] > 0 and item['effect'] == 'INFLIGHT' for item in scenario.qualifications)
    assert scenario.writer.ident is None
    scenario.assert_wire_terminal(status)
