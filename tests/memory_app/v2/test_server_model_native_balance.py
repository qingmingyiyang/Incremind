from contextlib import contextmanager
from contextvars import copy_context
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
from cryptography.fernet import Fernet
from pydantic import BaseModel
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.model_costs import SharedModelMeteringError
from backend.memory_app.v2.memory_turn import MemoryTurn, embedding_request
from backend.memory_app.retrieval_models import ConfiguredTransport
from backend.memory_app.v2.server_model_balance import ServerModelBalanceError
from backend.memory_app.v2.server_models import ServerModelError
from backend.security.secrets import ServerFileSecretStore
from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.connection_scope import connection_scope
from tests.memory_app.v2.test_server_model_balance import shared
from tests.memory_app.v2.test_server_model_integration import server, profile, allocate


class _Output(BaseModel):
    answer: str


@contextmanager
def local_deny_proxy():
    requests = []
    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            requests.append(self.path)
            self.send_error(403)

        def do_POST(self):
            requests.append('POST:' + self.path)
            self.send_error(403)

        def log_message(self, *_arguments):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 8094), Proxy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:8094', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def native_models(shared, proxy):
    users, _, _, user, _, central, balances, _, _ = shared
    root = users.root_for(user['user_id'])
    models = ModelConfiguration(SQLiteStructuredRecordStore(root / 'recognition.sqlite3'), root,
        secrets=ServerFileSecretStore(root / 'private-secrets.json'),
        server_model_access=central.access_for(user['user_id'], root), server_model_balances=balances,
        model_http_client_factory=lambda: httpx.Client(proxy=proxy, trust_env=False))
    models.update('generation', {'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    return models


def generation(shared, models, key):
    users, _, _, user, _, _, _, _, _ = shared
    return MemoryTurn(models.records, models, kind='memory.overview', project='alpha', key=key,
        materials=[], validate=lambda: users.get(user['user_id']))


def test_zero_balance_rejects_actual_kernel_model_attempt_before_any_http_and_without_retry(shared):
    users, _, admin, user, member, _, balances, _, _ = shared
    balances.adjust(admin, user['user_id'], amount='-10', expected_revision=1,
        expected_user_revision=1, reason='零余额检查')
    with local_deny_proxy() as (proxy, http):
        models = native_models(shared, proxy)
        with user_context(authorize_user(users, member)):
            turn = generation(shared, models, 'zero-balance')
            with pytest.raises(ModelConfigurationError, match='^server_model_balance_insufficient$') as failure:
                turn.generate([{'role': 'user', 'content': 'budget boundary check'}],
                    response_model=_Output, max_tokens=100)
        assert failure.value.retryable is False
        assert http == []
    events = turn.store.events_after(turn.turn_id)
    dispatches = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatches) == len(terminals) == 1
    terminal = turn.store.get(terminals[0]['data']['receipt_ref'])
    assert terminal['error_code'] == 'ai.server_model_balance_insufficient'
    assert terminal['status'] == 'failed_transport' and terminal['usage_status'] == 'unavailable'
    assert events[-1]['type'] == 'turn.failed'
    assert balances.read(member, user['user_id'])['reserved'] == '0'
    assert balances.attempts(member, user['user_id'])['items'] == []


@pytest.mark.parametrize('missing_price', [True, False])
def test_unknown_budget_configuration_is_not_retried_or_sent(shared, missing_price):
    users, _, admin, user, member, central, _, profile, _ = shared
    changes = {'prices': {}} if missing_price else {'base_url': 'https://unverified.example/v1'}
    central.update_profile(admin, profile['profile_id'], expected_revision=1, **changes)
    expected = 'server_model_balance_price_unavailable' if missing_price else 'server_model_balance_bound_unavailable'
    with local_deny_proxy() as (proxy, http):
        models = native_models(shared, proxy)
        with user_context(authorize_user(users, member)):
            turn = generation(shared, models, 'unsupported-' + str(missing_price))
            with pytest.raises(ModelConfigurationError, match='^' + expected + '$'):
                turn.generate([{'role': 'user', 'content': 'budget boundary check'}],
                    response_model=_Output, max_tokens=100)
        assert http == []
    events = turn.store.events_after(turn.turn_id)
    assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 1
    assert sum(event['type'] == 'model.attempt.terminal' for event in events) == 1


def test_real_http_connection_rejection_retains_each_native_retry_reservation(shared):
    users, _, _, user, member, _, balances, _, _ = shared
    with local_deny_proxy() as (proxy, http):
        models = native_models(shared, proxy)
        with user_context(authorize_user(users, member)):
            turn = generation(shared, models, 'real-http-denial')
            with pytest.raises(ModelConfigurationError, match='^server_model_balance_insufficient$') as failure:
                turn.generate([{'role': 'user', 'content': 'public transport boundary check'}],
                    response_model=_Output, max_tokens=100)
        assert http == ['api.deepseek.com:443'] * 4
        assert failure.value.retryable is False
    events = turn.store.events_after(turn.turn_id)
    dispatches = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatches) == len(terminals) == 5
    dispatched_ids = [turn.store.get(event['data']['payload_ref'])['attempt_id'] for event in dispatches]
    receipts = [turn.store.get(event['data']['receipt_ref']) for event in terminals]
    assert [receipt['attempt_id'] for receipt in receipts] == dispatched_ids
    assert receipts[-1]['error_code'] == 'ai.server_model_balance_insufficient'
    assert all(receipt['status'] == 'failed_transport' and receipt['usage_status'] == 'unavailable'
               for receipt in receipts)
    pending = balances.attempts(member, user['user_id'])['items']
    assert {row['attempt_id'] for row in pending} == set(dispatched_ids[:4])
    assert len(pending) == 4
    assert all(row['status'] == 'pending_review' and row['charged_amount'] is None for row in pending)
    assert all(row['reserved_amount'] == '2.097752' for row in pending)
    assert Decimal(balances.read(member, user['user_id'])['reserved']) == Decimal('2.097752') * len(http)
    assert balances.read(member, user['user_id'])['balance'] == '10'


@pytest.mark.parametrize('outcome', ['cancelled', 'missing_receipt', 'usage_input', 'settlement_conflict'])
def test_original_handle_terminal_and_billing_boundary_without_provider_response(shared, outcome):
    users, _, _, user, member, _, balances, _, _ = shared
    # 这里只给生产 finish 接点传领域用量输入，不构造供应商响应或模型成功发送。
    usage = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
    cache = {'cache_read_input_tokens': 40, 'cache_creation_input_tokens': 0, 'cache_miss_input_tokens': 60}
    with local_deny_proxy() as (proxy, http):
        models = native_models(shared, proxy)
        turn = generation(shared, models, 'handle-boundary-' + outcome)

        def invoke(control, validate):
            validate()
            sink = models.price_wire_sink(control, configuration=models.snapshot('generation'), max_output_tokens=100)
            attempt = sink.begin_model_wire_attempt()
            assert attempt.durable_commit_witness is attempt._handle.durable_commit_witness
            assert attempt.observe_provider_checkpoint.__self__ is attempt._handle
            assert attempt.bind_provider_resume.__self__ is attempt._handle

            def wire():
                if outcome == 'cancelled':
                    control.request_cancel()
                    control.checkpoint()
                if outcome == 'missing_receipt':
                    return None
                if outcome == 'settlement_conflict':
                    balances.settle(attempt.dispatch['attempt_id'], usage={'input_tokens': 1, 'output_tokens': 0},
                        cache_observation=None, terminal_status='succeeded')
                attempt.succeeded(usage=usage, cache_observation=cache)

            attempt.invoke_wire(wire)
            raise ValueError('domain_finish_boundary_exercised')

        with user_context(authorize_user(users, member)):
            if outcome == 'usage_input':
                expected, message = ValueError, 'domain_finish_boundary_exercised'
            elif outcome == 'cancelled':
                expected, message = RuntimeError, 'planner execution was cancelled'
            elif outcome == 'missing_receipt':
                expected, message = SharedModelMeteringError, 'server_model_billing_unavailable'
            else:
                expected, message = SharedModelMeteringError, 'server_model_balance_settlement_conflict'
            with pytest.raises(expected, match='^' + message + '$') as failure:
                turn.generate([], response_model=_Output, max_tokens=100, invoke=invoke)
        assert http == []
    events = turn.store.events_after(turn.turn_id)
    dispatches = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatches) == len(terminals) == 1
    receipt = turn.store.get(terminals[0]['data']['receipt_ref'])
    financial = balances.attempts(member, user['user_id'], state='all')['items'][0]
    assert financial['attempt_id'] == receipt['attempt_id']
    if outcome == 'usage_input':
        assert receipt['status'] == 'succeeded' and receipt['usage'] == usage
        assert financial['status'] == 'settled' and financial['charged_amount'] == '0.0002816'
        assert balances.read(member, user['user_id'])['reserved'] == '0'
        assert balances.read(member, user['user_id'])['balance'] == '9.9997184'
    else:
        assert financial['status'] == 'pending_review' and financial['charged_amount'] is None
        assert balances.read(member, user['user_id'])['reserved'] == '2.097752'
        assert balances.read(member, user['user_id'])['balance'] == '10'
        if outcome == 'cancelled':
            assert receipt['status'] == 'consumer_cancelled'
        elif outcome == 'missing_receipt':
            assert receipt['status'] == 'failed_transport' and receipt['usage_status'] == 'unavailable'
            assert receipt['error_code'] == 'ai.server_model_wire_receipt_missing'
            assert failure.value.retryable is False
        else:
            assert receipt['status'] == 'succeeded' and receipt['usage'] == usage
            assert financial['pending_reason'] == 'settlement_unavailable'
            assert failure.value.retryable is False


def test_real_scheduler_job_scope_expires_and_never_replaces_revoked_request_identity(server):
    client, app, users, registry, admin, paired, user, ordinary = server
    assert allocate(client, paired, user, profile(client, paired, official=True)).status_code == 200
    context = app.state.server_context
    balances, root = context.model_balances, users.root_for(user['user_id'])
    balances.adjust(admin, user['user_id'], amount='20', expected_revision=0,
        expected_user_revision=1, reason='真实后台资格检查')
    binding = context.models.access_for(user['user_id'], root).selection('generation')['server_model_binding']
    caller = registry.authenticate(ordinary['key'])
    access = authorize_user(users, caller)
    copied = []

    def reserve(attempt_id):
        return balances.reserve(user_id=user['user_id'], user_root=root, binding=binding,
            attempt_id=attempt_id, model='deepseek-flash', max_output_tokens=100)

    async def actual_job():
        assert USER_ACCESS.get() is None
        copied.append(copy_context())
        assert reserve('native-real-job-first')['status'] == 'reserved'
        balances.mark_unknown('native-real-job-first', reason_code='usage_unavailable')
        with user_context(access):
            registry.revoke(user['user_id'], caller.device_id, expected_revision=caller.revision)
            with pytest.raises(ServerModelBalanceError, match='^server_model_balance_unauthorized$'):
                reserve('native-revoked-device')
        assert reserve('native-real-job-second')['status'] == 'reserved'
        balances.mark_unknown('native-real-job-second', reason_code='usage_unavailable')
        users.update(admin, user['user_id'], disabled=True, expected_revision=1)
        with pytest.raises(ServerModelError, match='^server_model_user_disabled$'):
            reserve('native-disabled-user')
        users.update(admin, user['user_id'], disabled=False, expected_revision=2)
        return 'completed'

    async def submit():
        return await context.jobs.submit(user['user_id'], 'native-balance-check', actual_job)

    future = client.portal.start_task_soon(submit)
    assert future.result(timeout=30) == 'completed'

    def after_close(scope):
        # 用原公开 API 的真实新工作单元独立核验 grant，关闭的旧工作单元不参与拒绝。
        with connection_scope(scope):
            assert USER_ACCESS.get() is None
            assert users.get(user['user_id'])['disabled_at'] is None
            assert context.models.access_for(user['user_id'], root).validate_binding(binding)['mode'] == 'shared'
            return reserve('native-expired-job')

    with connection_scope() as scope:
        with pytest.raises(ServerModelBalanceError, match='^server_model_balance_job_unauthorized$'):
            copied[0].run(after_close, scope)
    assert app.state.server_user_pool._model_balance_jobs == {}
    assert {row['attempt_id'] for row in balances.attempts(admin, user['user_id'])['items']} == {
        'native-real-job-first', 'native-real-job-second'}


@pytest.mark.parametrize('purpose', ['embedding', 'rerank'])
def test_retrieval_actual_transport_is_blocked_with_public_budget_error_before_http(shared, purpose):
    users, _, admin, user, member, central, balances, generation_profile, _ = shared
    selected = central.create_profile(admin, name='辅助用途预算检查', purpose=purpose, provider='openai',
        base_url='https://api.deepseek.com/v1', model='deepseek-flash', allowed_models=['deepseek-flash'],
        enabled=True, api_key=Fernet.generate_key().decode('ascii'), prices={'deepseek-flash': {
            'input_per_million': '2', 'output_per_million': '8', 'cache_read_per_million': '0.04'}})
    central.update_allocation(admin, user['user_id'], mode='shared',
        profiles={'generation': generation_profile['profile_id'], purpose: selected['profile_id']},
        expected_revision=1, expected_user_revision=1)
    with local_deny_proxy() as (proxy, http):
        models = native_models(shared, proxy)
        models.update(purpose, {'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        transport = ConfiguredTransport(models.snapshot(purpose)['api_key'])
        with user_context(authorize_user(users, member)):
            with pytest.raises(ModelConfigurationError, match='^server_model_balance_bound_unavailable$') as failure:
                embedding_request(models.records, models, 'alpha', [], 'retrieval-budget-' + purpose,
                    lambda: users.get(user['user_id']), transport,
                    {'endpoint': 'http://127.0.0.1:8094/' + purpose, 'payload': {'input': 'budget check'}},
                    purpose=purpose)
        assert failure.value.retryable is False and http == []
    turn_id = models.records.list('v2_memory_turn_keys')[0].object_id
    store = MemoryTurn.store_for(models.records)
    events = store.events_after(turn_id)
    assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 1
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(terminals) == 1
    assert store.get(terminals[0]['data']['receipt_ref'])['error_code'] == 'ai.server_model_balance_bound_unavailable'
    assert balances.attempts(member, user['user_id'])['items'] == []
    assert balances.read(member, user['user_id'])['reserved'] == '0'
