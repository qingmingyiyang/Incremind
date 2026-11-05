import os
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
from shutil import copyfile
import sqlite3
import socket

from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def real_app(tmp_path_factory, monkeypatch):
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path_factory.mktemp('r') / 'app'
    root.mkdir()
    assert root.is_absolute() and root.is_relative_to(assigned)
    (root / 'config').mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    from backend.memory_app.app import create_app
    application = create_app(runtime_root=root)
    with TestClient(application, base_url='http://127.0.0.1:18115', client=('127.0.0.1', 1234)) as client:
        current = client.get('/api/v2/settings').json()['external_agent']
        response = client.patch('/api/v2/settings/external-agent', json={
            'allow_remote': True, 'include_profile': False, 'daily_limit': 200,
            'clients': {**current['clients'], 'codex': True}, 'expected_revision': current['revision']})
        assert response.status_code == 200, response.text
        yield application, client, root


def deliver(client):
    response = client.post('/api/v2/settings/external-agent/snapshot', json={
        'client': 'codex', 'project_id': 'default', 'budget': 1200})
    assert response.status_code == 200, response.text
    return response.json()['turn_id']


def test_real_external_delivery_identity_projection_keeps_legacy_shape_and_no_model_call(real_app):
    application, client, _root = real_app
    identity = deliver(client)
    from backend.memory_app.v2.external_context import delivery_receipts
    records = application.state.recognition_records
    legacy = delivery_receipts(records, _root)
    actual = delivery_receipts(records, _root, include_identity=True, strict=True)
    assert len(legacy) == 1 and set(legacy[0]) == {'at', 'items'}
    assert actual['rows'][0]['turn_id'] == identity and actual['rows'][0]['client'] == 'codex'
    assert actual['unverified_count'] == actual['incomplete_count'] == 0
    assert actual['rows'][0]['materials'] == [] and actual['rows'][0]['excluded'] == []
    events = application.state.ai_turn_store.events_after(identity)
    assert events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)


def test_pending_external_scope_and_actual_sqlite_damage_are_not_complete_zero_usage(real_app):
    application, client, root = real_app
    deliver(client)
    from backend.memory_app.v2.external_context import delivery_receipts
    owner = application.state.external_context
    identity = 'turn-pending-receipt-inspection'
    request = {'client': 'codex', 'tool': 'read', 'query': '合成待执行读取',
               'scope': {'user_id': 'local-user', 'project_id': 'default'}, 'budget': 1200}
    frozen = owner.prepare(identity, request, [], session_id='session-receipt-inspection',
        operation_id='receipt-inspection-pending', idempotency_key=identity, created_at=datetime.now(timezone.utc).isoformat())
    owner.runtime.accept_turn(frozen)
    result = delivery_receipts(owner.records, root, include_identity=True, strict=True)
    assert len(result['rows']) == 1 and result['incomplete_count'] == 1
    assert len(delivery_receipts(owner.records, root)) == 1
    damaged = root / 'damaged-read'
    (damaged / '.rebuild-data').mkdir(parents=True)
    (damaged / '.rebuild-data/ai-turns.sqlite3').write_bytes(b'actual invalid sqlite file')
    with pytest.raises(sqlite3.DatabaseError):
        delivery_receipts(owner.records, damaged, include_identity=True, strict=True)
    assert delivery_receipts(owner.records, damaged) == []


def test_complete_month_counts_fifty_five_real_deliveries_and_keyset_pages_without_models(real_app):
    application, client, root = real_app
    identities = {deliver(client) for _index in range(55)}
    # 等真实周期任务停止后，保留全库修订不变断言核验读取无业务写入。
    client.portal.call(application.state.memory_daily_jobs.stop)
    records = application.state.recognition_records
    before = records.change_stamp()
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    summary = client.get('/api/v2/settings/usage-month', params={'month': month, 'timezone': 'UTC'})
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body['receipt_count'] == 55 and body['model_call_count'] == 0
    assert body['scope'] == 'egress' and body['external_delivery_count'] == 55 and body['wire_attempt_count'] == 0
    assert body['cost']['state'] == 'unknown' and body['cost']['amounts'] == []
    assert body['usage'] == {'input': None, 'output': None, 'state': 'unknown'}
    assert body['window']['month'] == month and body['window']['start_inclusive'] is True and body['window']['end_exclusive'] is True
    assert body['coverage']['external_incomplete'] == body['coverage']['external_unverified'] == 0
    cursor, rows, sizes = None, [], []
    while True:
        page = client.get('/api/v2/settings/receipt-history', params={
            'month': month, 'timezone': 'UTC', 'limit': 20, **({'cursor': cursor} if cursor else {})})
        assert page.status_code == 200, page.text
        page = page.json()
        assert page['total'] == 55
        rows.extend(page['rows']); sizes.append(len(page['rows']))
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert sizes == [20, 20, 15] and len({row['id'] for row in rows}) == 55
    assert {row['origin']['turn_id'] for row in rows} == identities
    assert all(row['purpose_id'] == 'external' and row['cost']['state'] == 'unknown' for row in rows)
    filtered = client.get('/api/v2/settings/receipt-history', params={'month': month, 'purpose': 'ask'}).json()
    assert filtered['total'] == 0 and filtered['rows'] == []
    detail = client.get('/api/v2/settings/receipt-history/' + rows[0]['id'])
    assert detail.status_code == 200 and detail.json()['materials'] == detail.json()['excluded'] == []
    assert detail.json()['calls'] == [] and detail.json()['origin']['turn_id'] in identities
    legacy = client.get('/api/v2/settings/egress-receipts')
    assert legacy.status_code == 200 and len(legacy.json()) == 50
    assert set(legacy.json()[0]) == {'at', 'purpose', 'model', 'items', 'usage', 'duration', 'model_cost'}
    assert len(client.get('/api/v2/settings/egress-receipts?limit=500').json()) == 55
    models = client.get('/api/v2/settings/usage-month', params={'month': month, 'scope': 'models'}).json()
    assert models['scope'] == 'models' and models['model_call_count'] == models['receipt_count'] == models['wire_attempt_count'] == 0
    assert models['external_delivery_count'] == 55 and models['cost']['state'] == 'unknown'
    assert client.get('/api/v2/settings/receipt-history', params={'scope': 'models'}).json()['total'] == 0
    assert client.get('/api/v2/settings/receipt-history', params={'purpose': 'invalid'}).status_code == 400
    assert client.get('/api/v2/settings/receipt-history', params={'cursor': 'invalid'}).status_code == 400
    assert client.get('/api/v2/settings/usage-month', params={'timezone': 'invalid/timezone'}).status_code == 400
    assert client.get('/api/v2/settings/receipt-history/not-an-own-receipt').status_code == 404
    assert records.change_stamp() == before
    with sqlite3.connect((root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0


def test_material_inspection_reuses_source_authority_and_never_substitutes_a_new_revision(real_app):
    application, client, _root = real_app
    deliver(client)
    records, owner = application.state.recognition_records, application.state.external_context
    items = application.state.workspace_domains.items
    source = items.create('default', 'text', '合成材料标题', 'g15-private-source-body')
    identity = 'turn-g15-real-material'
    request = {'client': 'codex', 'tool': 'read', 'query': '读取合成原件',
               'scope': {'user_id': 'local-user', 'project_id': 'default'}, 'budget': 1200}
    selected = {'type': 'original_item', 'id': source['id'], 'project_id': 'default',
                'revision': source['revision'], 'layer': 'L0', 'windows': []}
    owner.prepare(identity, request, [selected], session_id='session-g15-material', operation_id='g15-material',
                  idempotency_key=identity, created_at=datetime.now(timezone.utc).isoformat())
    owner.execute(identity, runtime=owner.runtime, runner=application.state.ai_turn_runner)
    listing = client.get('/api/v2/settings/receipt-history').json()
    row = next(row for row in listing['rows'] if row['origin']['turn_id'] == identity)
    endpoint = '/api/v2/settings/receipt-history/' + row['id']
    result = client.get(endpoint)
    assert result.status_code == 200, result.text
    detail = result.json()
    assert detail['materials'][0]['title'] == '合成材料标题' and detail['materials'][0]['availability'] == 'available'
    assert 'g15-private-source-body' not in result.text
    scoped = client.get(endpoint, params={'month': datetime.now(timezone.utc).strftime('%Y-%m'), 'timezone': 'UTC'})
    assert scoped.status_code == 200 and scoped.json()['window']['timezone'] == 'UTC' and scoped.json()['scope'] == 'egress'
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    SourceEgressService(records).set_policy(WorkScope('local-user', 'default'), 'original_item', source['id'],
        source['revision'], 0, [])
    private = client.get(endpoint).json()
    assert private['materials'][0]['availability'] == 'available' and private['materials'][0]['current_private'] is True
    items.update(source['id'], 'default', {'staged'}, title='同编号的新修订', source_text='new-source-body')
    changed = client.get(endpoint)
    assert changed.status_code == 200
    assert changed.json()['materials'][0]['availability'] == 'unavailable'
    assert '同编号的新修订' not in changed.text and 'new-source-body' not in changed.text
    assert 'g15-private-source-body' not in changed.text
    assert not any(event['type'].startswith('model.') for event in application.state.ai_turn_store.events_after(identity))


def test_real_read_failure_returns_a_safe_error_and_retries_after_schema_recovery(real_app):
    application, client, root = real_app
    deliver(client)
    database = root / '.rebuild-data/ai-turns.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('ALTER TABLE ai_turn_payloads RENAME TO g15_temporarily_unavailable_payloads')
    try:
        for endpoint in ('usage-month', 'receipt-history'):
            response = client.get('/api/v2/settings/' + endpoint)
            assert response.status_code == 503
            assert response.json() == {'detail': 'receipt_history_unavailable'}
    finally:
        with sqlite3.connect(database) as connection:
            connection.execute('ALTER TABLE g15_temporarily_unavailable_payloads RENAME TO ai_turn_payloads')
    retried = client.get('/api/v2/settings/usage-month')
    assert retried.status_code == 200 and retried.json()['receipt_count'] == 1
    assert application.state.recognition_records.list('v2_model_wire_prices') == ()


def test_actual_local_owner_keeps_its_endpoint_guard_before_closed_loopback_dispatch(real_app):
    application, client, root = real_app
    address = ipaddress.ip_address('127.0.0.1')
    assert address.is_loopback
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
        check.bind((str(address), 18121))
        assert check.getsockname() == (str(address), 18121)
    deliver(client)
    models = application.state.recognition_models
    assert models._local_models_root.resolve().is_relative_to(root)
    assert models._local_model_installed() is False
    before = client.get('/api/v2/settings').json()['model']
    result = client.put('/api/v2/settings/generation-mode', json={
        'mode': 'local', 'local_enabled': True, 'local_base_url': 'http://127.0.0.1:18121/v1',
        'expected_revision': before['generation_mode']['revision']})
    assert result.status_code == 400 and result.json()['detail'] == 'invalid_local_model_endpoint'
    after = client.get('/api/v2/settings').json()['model']
    assert after['generation_mode'] == before['generation_mode'] and after['generation'] == before['generation']
    with sqlite3.connect((root / '.rebuild-data/ai-turns.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        attempts = connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0]
    assert attempts == 0
    artifact = root.parent / 'local-owner-boundary-final.json'
    artifact.write_text(json.dumps({'address': str(address), 'port': 18121, 'port_bind_succeeded': True,
        'asset_installed': False, 'mode_status': result.status_code, 'mode_error': result.json()['detail'],
        'attempt_count': attempts, 'wire_fields_verified': False, 'verification': 'actual pre-wire endpoint guard'}, indent=2), encoding='utf8')


def test_real_server_receipt_reads_use_only_the_authenticated_target_space(tmp_path_factory, monkeypatch):
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path_factory.mktemp('srv') / 'server'
    root.mkdir()
    assert root.is_relative_to(assigned)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from backend.security.user_context import user_context
    from core.storage_provider.connection_scope import with_connection_scope
    application = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=18121)
    registry = application.state.device_registry
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='synthetic receipt admin')
    admin = {'Authorization': 'Bearer ' + paired['key']}
    actor = registry.authenticate(paired['key'])
    with TestClient(application, base_url='http://127.0.0.1:18121', client=('127.0.0.1', 1234)) as client:
        users = []
        for name in ('synthetic receipt A', 'synthetic receipt B'):
            created = client.post('/api/v2/server/users', headers=admin, json={'name': name})
            assert created.status_code == 201, created.text
            user_id = created.json()['user_id']
            device = registry.exchange(registry.issue_pairing(user_id=user_id, actor=actor.device_id)['code'], name=name)
            users.append((user_id, {'Authorization': 'Bearer ' + device['key']}))
        user_a, headers_a = users[0]
        user_b, headers_b = users[1]
        settings = client.get('/api/v2/settings', headers=headers_a).json()['external_agent']
        changed = client.patch('/api/v2/settings/external-agent', headers=headers_a, json={
            'allow_remote': True, 'include_profile': False, 'daily_limit': 200,
            'clients': {**settings['clients'], 'codex': True}, 'expected_revision': settings['revision']})
        assert changed.status_code == 200, changed.text
        context = application.state.server_context
        async def submit():
            async def execute():
                async with context.pool.lease(user_a) as child:
                    def deliver_in_held_job():
                        child.state.workspace_domains.query.answer_turns._runtime()
                        owner = child.state.external_context
                        source = child.state.workspace_domains.items.create('default', 'text', '仅用户A的合成材料', 'receipt-space-a-body')
                        identity = 'turn-server-receipt-a'
                        request = {'client': 'codex', 'tool': 'read', 'query': '合成跨用户回执检查',
                            'scope': {'user_id': 'local-user', 'project_id': 'default'}, 'budget': 1200}
                        owner.prepare(identity, request, [{'type': 'original_item', 'id': source['id'],
                            'revision': source['revision'], 'project_id': 'default', 'layer': 'L0', 'windows': []}],
                            session_id='session-server-receipt-a', operation_id='server-receipt-a', idempotency_key=identity,
                            created_at=datetime.now(timezone.utc).isoformat())
                        owner.execute(identity, runtime=owner.runtime, runner=child.state.ai_turn_runner)
                        return identity
                    import asyncio
                    return await asyncio.to_thread(with_connection_scope(deliver_in_held_job))
            with user_context(None), resource_context(context.resources):
                return await context.jobs.submit(user_a, 'daily', execute, retain_running=True)
        turn = client.portal.call(submit)
        page = client.get('/api/v2/settings/receipt-history', headers=headers_a)
        assert page.status_code == 200 and page.json()['total'] == 1, page.text
        row = page.json()['rows'][0]
        assert row['origin']['turn_id'] == turn
        endpoint = '/api/v2/settings/receipt-history/' + row['id']
        own = client.get(endpoint, headers=headers_a)
        assert own.status_code == 200 and own.json()['materials'][0]['title'] == '仅用户A的合成材料'
        assert 'receipt-space-a-body' not in own.text
        other = client.get('/api/v2/settings/receipt-history', headers=headers_b)
        assert other.status_code == 200 and other.json()['rows'] == []
        assert client.get(endpoint, headers=headers_b).status_code == 404
        assert client.get(endpoint, headers={**headers_b, 'X-Chriptmas-Target-User': user_a}).status_code == 403
        assert client.get(endpoint, headers=admin).status_code == 404
        assert client.get(endpoint, headers={**admin, 'X-Chriptmas-Target-User': user_a}).status_code == 200
        assert client.get('/api/v2/settings/usage-month', headers=headers_b).json()['receipt_count'] == 0
        assert client.get('/api/v2/settings/usage-month', params={'scope': 'models'}, headers=headers_b).json()['receipt_count'] == 0
        assert client.get('/api/v2/settings/receipt-history', params={'scope': 'models'},
            headers={**headers_b, 'X-Chriptmas-Target-User': user_a}).status_code == 403
        assert client.get('/api/v2/settings/usage-month').status_code == 401
        assert client.get(endpoint).status_code == 401
        assert context.users.root_for(user_a) != context.users.root_for(user_b)


def test_pure_dto_cost_mathematics_preserves_currencies_partial_values_and_exact_decimals():
    from backend.memory_app.v2.receipt_history import cost_totals, usage_view
    result = cost_totals([{'currency': 'CNY', 'amount': '0.123456789012345678901234567890'},
        {'currency': 'CNY', 'amount': '0.000000000000000000000000000001'}, {'currency': 'USD', 'amount': '1.02'}, None])
    assert result == {'state': 'partial', 'known_count': 3, 'unknown_count': 1, 'amounts': [
        {'currency': 'CNY', 'amount': '0.123456789012345678901234567891'}, {'currency': 'USD', 'amount': '1.02'}]}
    assert cost_totals([None, {'currency': 'CNY', 'amount': 'NaN'}])['state'] == 'unknown'
    assert cost_totals([{'currency': 'CNY', 'amount': '0'}])['amounts'] == [{'currency': 'CNY', 'amount': '0'}]
    assert usage_view({'input_tokens': 0}) == {'input': 0, 'output': None, 'state': 'partial'}
    assert usage_view(None) == {'input': None, 'output': None, 'state': 'unknown'}


def test_pure_calendar_month_uses_the_selected_timezone_and_calendar_boundaries():
    from backend.memory_app.v2.receipt_history import month_window, timestamp
    february = month_window('2024-02', 'Asia/Shanghai')
    assert february['start'] == '2024-02-01T00:00:00+08:00' and february['end'] == '2024-03-01T00:00:00+08:00'
    march = month_window('2026-03', 'America/New_York')
    assert march['start'] == '2026-03-01T00:00:00-05:00' and march['end'] == '2026-04-01T00:00:00-04:00'
    assert (timestamp(march['end']) - timestamp(march['start'])).total_seconds() == 743 * 3600
    with pytest.raises(ValueError):
        month_window('2026-13', 'UTC')


def test_pure_dto_month_math_splits_each_retry_at_its_actual_completion_boundary():
    from backend.memory_app.v2.receipt_history import ReceiptHistory
    row = {'id': 'dto-month', 'at': '2026-12-01T00:00:00Z', 'model': None, '_calls': [{
        'model_id': None, 'completed_at': '2026-12-01T00:00:00Z', 'wire_attempts': [
            {'completed_at': '2026-10-31T23:59:59Z', 'duration_ms': 1,
             'cost': {'currency': 'CNY', 'amount': '0.25'}, 'usage': {'input_tokens': 1, 'output_tokens': 2}},
            {'completed_at': '2026-11-01T00:00:00Z', 'duration_ms': 1, 'cost': None, 'usage': None},
            {'completed_at': '2026-12-01T00:00:00Z', 'duration_ms': 1,
             'cost': {'currency': 'USD', 'amount': '1.02'}, 'usage': {'input_tokens': 3, 'output_tokens': 4}},
        ]}]}
    october, _window = ReceiptHistory._scope([row], '2026-10', 'UTC')
    november, _window = ReceiptHistory._scope([row], '2026-11', 'UTC')
    december, _window = ReceiptHistory._scope([row], '2026-12', 'UTC')
    assert october[0]['cost']['amounts'] == [{'currency': 'CNY', 'amount': '0.25'}]
    assert october[0]['at'] == '2026-10-31T23:59:59Z' and october[0]['wire_attempt_count'] == 1
    assert november[0]['cost'] == {'state': 'unknown', 'known_count': 0, 'unknown_count': 1, 'amounts': []}
    assert november[0]['usage']['input'] is november[0]['usage']['output'] is None
    assert december[0]['cost']['amounts'] == [{'currency': 'USD', 'amount': '1.02'}]
    local_october, _window = ReceiptHistory._scope([row], '2026-10', 'America/New_York')
    assert local_october[0]['wire_attempt_count'] == 2 and local_october[0]['cost']['state'] == 'partial'
    assert row['at'] == '2026-12-01T00:00:00Z' and len(row['_calls'][0]['wire_attempts']) == 3


def test_pure_dto_partial_counters_stay_partial_through_call_row_and_summary():
    from backend.memory_app.v2.receipt_history import ReceiptHistory, _record, usage_totals, usage_view
    wires = [
        {'completed_at': '2026-11-03T12:00:00Z', 'duration_ms': 1,
         'usage': {'input_tokens': 10}, 'usage_status': 'partial', 'cost': None},
        {'completed_at': '2026-11-03T12:00:01Z', 'duration_ms': 1,
         'usage': {'output_tokens': 20}, 'usage_status': 'partial', 'cost': None},
    ]
    usage = usage_totals(wires)
    assert usage == {'input_tokens': 10, 'output_tokens': 20, 'observed_only': True}
    assert usage_view(usage) == {'input': 10, 'output': 20, 'state': 'partial'}
    call = {'model_id': None, 'completed_at': wires[-1]['completed_at'], 'duration_ms': 2,
            'usage': usage, 'usage_status': 'partial', 'wire_attempts': wires}
    row = _record('kernel', 'dto-partial', call['completed_at'], '问', calls=[call], usage=usage,
                  costs=[None, None], project_id='default')
    assert row['usage'] == {'input': 10, 'output': 20, 'state': 'partial'}
    scoped, _window = ReceiptHistory._scope([row], '2026-11', 'UTC')
    assert scoped[0]['_calls'][0]['usage_status'] == 'partial'
    assert scoped[0]['_calls'][0]['usage'] == usage
    assert scoped[0]['_usage'] == usage
    assert scoped[0]['usage']['state'] == 'partial'
    assert ReceiptHistory._totals(scoped)['usage'] == {'input': 10, 'output': 20, 'state': 'partial'}
    assert usage_totals([{'usage': {'input_tokens': 10, 'output_tokens': 20, 'observed_only': True}}]) == usage
    assert usage_totals([{'usage': {'input_tokens': 10, 'output_tokens': 20}, 'usage_status': 'partial'}]) == usage
    assert usage_totals([{'usage': {'input_tokens': 10, 'output_tokens': 20}}, {'usage': None}]) == usage
    assert usage_totals([{'usage': {'input_tokens': 10, 'output_tokens': 20}}]) == {
        'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}
