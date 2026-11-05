from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.memory_app.v2.server_model_balance import (
    BALANCES, ENTRIES, ServerModelBalanceError, ServerModelBalances, verified_token_bound,
)


def owners(root):
    devices = DeviceRegistry(root / 'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理电脑')
    users = ServerUsers(root, records=devices.records)
    admin = devices.authenticate(pair['key'])
    user = users.create(admin, name='使用者')
    pair = devices.exchange(devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name='手机')
    member = devices.authenticate(pair['key'])
    return users, devices, admin, user, member


def test_amounts_are_exact_and_adjustments_use_two_revisions(tmp_path):
    users, _, admin, user, member = owners(tmp_path)
    balances = ServerModelBalances(users)
    empty = balances.read(member, user['user_id'])
    assert (empty['balance'], empty['available'], empty['revision']) == ('0', '0', 0)
    first = balances.adjust(admin, user['user_id'], amount='0.100000000001',
        expected_revision=0, expected_user_revision=1, reason='分配金额')
    assert first['balance'] == '0.100000000001' and first['revision'] == 1
    second = balances.adjust(admin, user['user_id'], amount='0.2',
        expected_revision=1, expected_user_revision=1, reason='增加金额')
    assert second['balance'] == '0.300000000001'
    with pytest.raises(ServerModelBalanceError, match='balance_revision_conflict'):
        balances.adjust(admin, user['user_id'], amount='1', expected_revision=1,
            expected_user_revision=1, reason='迟到更新')
    with pytest.raises(ServerModelBalanceError, match='balance_user_revision_conflict'):
        balances.adjust(admin, user['user_id'], amount='1', expected_revision=2,
            expected_user_revision=2, reason='迟到用户')
    assert len(users.records.list(ENTRIES)) == 2
    assert balances.read(admin, user['user_id'])['balance'] == '0.300000000001'


@pytest.mark.parametrize('amount', [1, 0.1, True, 'NaN', 'Infinity', '1e2', '+1', ' 1',
    '01', '0', '-0', '0.0000000000001', '1000000000000'])
def test_invalid_money_is_rejected_without_writes(tmp_path, amount):
    users, _, admin, user, _ = owners(tmp_path)
    balances = ServerModelBalances(users)
    with pytest.raises(ServerModelBalanceError, match='balance_amount_invalid'):
        balances.adjust(admin, user['user_id'], amount=amount, expected_revision=0,
            expected_user_revision=1, reason='金额校验')
    assert users.records.list(BALANCES) == () and users.records.list(ENTRIES) == ()


def test_real_device_and_user_permissions_are_rechecked(tmp_path):
    users, devices, admin, user, member = owners(tmp_path)
    balances = ServerModelBalances(users)
    with pytest.raises(ServerModelBalanceError, match='balance_forbidden'):
        balances.adjust(member, user['user_id'], amount='1', expected_revision=0,
            expected_user_revision=1, reason='越权增加')
    with pytest.raises(ServerModelBalanceError, match='balance_forbidden'):
        balances.read(member, 'local-user')
    devices.revoke(member.user_id, member.device_id, expected_revision=member.revision)
    with pytest.raises(ServerModelBalanceError, match='balance_unauthorized'):
        balances.read(member, user['user_id'])
    users.update(admin, user['user_id'], expected_revision=1, disabled=True)
    assert balances.read(admin, user['user_id'])['balance'] == '0'


def test_adjustment_cas_is_serialized_across_real_sqlite_owners(tmp_path):
    users, _, admin, user, _ = owners(tmp_path)
    one = ServerModelBalances(users)
    two = ServerModelBalances(ServerUsers(tmp_path))
    def credit(owner):
        try:
            owner.adjust(admin, user['user_id'], amount='5', expected_revision=0,
                expected_user_revision=1, reason='并发增加')
            return 'updated'
        except ServerModelBalanceError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(credit, (one, two)))
    assert sorted(results) == ['server_model_balance_revision_conflict', 'updated']
    reopened = ServerModelBalances(ServerUsers(tmp_path))
    assert reopened.read(admin, user['user_id'])['balance'] == '5'
    assert len(users.records.list(ENTRIES)) == 1


def test_hard_bound_requires_verified_provider_endpoint_model_and_purpose():
    values = dict(provider='openai', base_url='https://api.deepseek.com/v1',
        model='deepseek-flash', purpose='generation', protocol='chat_completions')
    bound = verified_token_bound(**values)
    assert bound['context_tokens'] == 1048576 and bound['max_output_tokens'] == 393216
    for field, value in [('provider', 'proxy'), ('base_url', 'https://proxy.invalid/v1'),
                         ('base_url', 'https://api.deepseek.com.evil.invalid/v1'),
                         ('base_url', 'https://api.deepseek.com/v1?x=1'),
                         ('model', 'unknown'), ('purpose', 'asr'), ('protocol', 'search')]:
        with pytest.raises(ServerModelBalanceError, match='balance_bound_unavailable'):
            verified_token_bound(**{**values, field: value})


def test_shared_input_contract_uses_official_protocol_and_reported_billing_units():
    from decimal import Decimal
    from backend.memory_app.v2.policies.shared_metering import get
    policy = get(version='@1')
    request = {'endpoint': 'https://api.openai.com/v1/embeddings',
        'payload': {'model': 'text-embedding-3-small', 'input': ['一个问题', '一份资料']}}
    bound = policy.bound(purpose='embedding', provider='openai', base_url='https://api.openai.com/v1',
        model='text-embedding-3-small', request=request)
    price = {'unit': 'input_tokens', 'rates': {'input_per_million': '2'}}
    assert policy.reserve(bound, price) == Decimal('0.6')
    assert policy.observe(bound, {'usage': {'prompt_tokens': 12, 'total_tokens': 12}}) == {'input_tokens': 12}
    assert policy.observe(bound, {'usage': {'prompt_tokens': 12, 'total_tokens': 13}}) is None
    assert policy.charge(bound, price, None) == (None, 'usage_unavailable')
    assert policy.charge(bound, price, {'input_tokens': 300001}) == (None, 'usage_exceeds_bound')
    with pytest.raises(ValueError, match='bound_unavailable'):
        policy.bound(purpose='embedding', provider='openai', base_url='https://proxy.invalid/v1',
            model='text-embedding-3-small', request=request)
    rerank = policy.bound(purpose='rerank', provider='cohere', base_url='https://api.cohere.com/v2',
        model='rerank-v5.0-pro', request={'endpoint': 'https://api.cohere.com/v2/rerank',
            'payload': {'model': 'rerank-v5.0-pro', 'query': '问题', 'documents': ['甲', '乙'], 'max_tokens_per_doc': 4096}})
    assert rerank['limits'] == {'input_tokens': 65536}
    assert policy.observe(rerank, {'meta': {'billed_units': {'input_tokens': 45}}}) == {'input_tokens': 45}
    assert policy.observe(rerank, {'meta': {'billed_units': {'search_units': 1}}}) is None


def test_shared_search_request_contract_excludes_unbounded_native_tools_and_validates_empty_web():
    from decimal import Decimal
    from backend.memory_app.v2.policies.shared_metering import get
    from backend.video_summary.infrastructure.litellm_web_search import BraveWebSearchGateway, brave_web_results
    policy = get(version='@2')
    request = BraveWebSearchGateway.prepare_request('原始问题', count=3)
    redacted = BraveWebSearchGateway.prepare_request(r'查找 C:\Users\Example\private.txt', count=3)
    assert '[REDACTED_LOCAL_PATH]' in redacted['payload']['q']
    assert 'C:\\Users' not in redacted['payload']['q']
    options = dict(purpose='search', provider='brave', model='brave-web-search',
        base_url='https://api.search.brave.com/res/v1', request=request)
    bound = policy.bound(**options)
    price = {'unit': 'search_requests', 'rates': {'request_per_thousand': '5'}}
    assert policy.reserve(bound, price) == Decimal('0.005')
    assert policy.observe(bound, {'completed_requests': 1}) == {'requests': 1}
    assert policy.observe(bound, {'completed_requests': True}) is None
    assert policy.charge(bound, price, None) == (None, 'usage_unavailable')
    assert policy.charge(bound, price, {'requests': 1}) == (Decimal('0.005'), None)
    assert brave_web_results({'type': 'search', 'query': {'original': '原始问题'}, 'web': None}, query='原始问题') == []
    assert brave_web_results({'type': 'search', 'query': {'original': '原始问题'},
        'web': {'type': 'search', 'results': []}}, query='原始问题') == []
    for body in ({}, {'type': 'search', 'query': {'original': '其他问题'}},
                 {'type': 'search', 'query': {'original': '原始问题'}, 'web': {}}):
        with pytest.raises(ValueError, match='response_invalid'):
            brave_web_results(body, query='原始问题')
    for changes in ({'model': 'answers'}, {'base_url': 'https://proxy.invalid/res/v1'},
                    {'request': {**request, 'payload': {**request['payload'], 'offset': 1}}}):
        with pytest.raises(ValueError):
            policy.bound(**{**options, **changes})
    with pytest.raises(ValueError, match='bound_unavailable'):
        policy.bound(purpose='search', provider='openai', model='gpt-5.4-mini',
            base_url='https://api.openai.com/v1', request={'endpoint': 'https://api.openai.com/v1/responses',
                'payload': {'model': 'gpt-5.4-mini', 'max_tool_calls': 1}})


def test_asr_seconds_contract_preserves_independent_token_evidence_and_unknown_duration():
    from decimal import Decimal
    import io
    import wave
    from backend.memory_app.v2.policies.shared_metering import get
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(6400))
    policy = get(version='@3')
    bound = policy.bound(purpose='asr', provider='tokenhub-asr', model='hy-asr-3.0-preview',
        base_url='https://tokenhub.tencentmaas.com/v1', request={'method': 'POST',
            'endpoint': 'https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe',
            'payload': {'model': 'hy-asr-3.0-preview', 'voice_encode_format': 'wav',
                'data': base64.b64encode(audio.getvalue()).decode('ascii')}})
    price = {'unit': 'audio_seconds', 'rates': {'audio_per_second': '0.00022'}}
    assert policy.reserve(bound, price) == Decimal('0.396')
    response = {'status': 'completed', 'output': {'duration_ms': 200,
        'sentences': [{'begin_ms': 0, 'end_ms': 200}]}, 'usage': {'total_token': 17}}
    observed = policy.observe(bound, response)
    assert observed == {'duration_ms': 200, 'usage_total_token': 17, 'sentence_end_ms': 200}
    assert policy.charge(bound, price, observed) == (Decimal('0.000044'), None)
    assert policy.charge(bound, price, {**observed, 'duration_ms': None}) == (None, 'usage_unavailable')
    assert policy.charge(bound, price, {**observed, 'duration_ms': 100}) == (None, 'usage_invalid')
    assert policy.charge(bound, price, {**observed, 'duration_ms': 1800001}) == (None, 'usage_exceeds_bound')
    assert policy.observe(bound, {**response, 'usage': {}})['usage_total_token'] is None


def test_shared_asr_source_guards_no_wire_release_and_account_deletion(shared):
    import io
    import wave
    from pydantic import RootModel
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from backend.memory_app.v2.privacy import egress_allowed, set_private_project
    from backend.memory_app.v2.shared_asr import _AsrModelsView
    from backend.memory_app.model_costs import BalanceRecordingSink
    from backend.memory_app.v2.server_model_balance import NO_WIRE
    from backend.recognition import RecognitionConflict
    from backend.security.secrets import ServerFileSecretStore
    from backend.security.user_context import authorize_user, user_context
    from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings
    from core.storage_provider import SQLiteStructuredRecordStore
    users, registry, admin, user, member, central, balances, _, old_binding = shared
    root = users.root_for(user['user_id'])
    credential = central.access_for(user['user_id'], root).credential(old_binding)
    profile = central.create_profile(admin, name='转写', purpose='asr', provider='tokenhub-asr',
        base_url='https://tokenhub.tencentmaas.com/v1', model='hy-asr-3.0-preview',
        allowed_models=['hy-asr-3.0-preview'], enabled=True, api_key=credential.value)
    central.update_metering_prices(admin, profile['profile_id'], expected_revision=0,
        expected_profile_revision=1, models={profile['model']: {'unit': 'audio_seconds',
            'rates': {'audio_per_second': '0.00022'}}})
    central.update_allocation(admin, user['user_id'], expected_revision=1, expected_user_revision=1,
        mode='shared', profiles={'asr': profile['profile_id']})
    records = SQLiteStructuredRecordStore(root / 'recognition.sqlite3')
    models = ModelConfiguration(records, root, secrets=ServerFileSecretStore(root / 'private-secrets.json'),
        server_model_access=central.access_for(user['user_id'], root), server_model_balances=balances)
    store, _ = build_rebuild_object_store(root)
    settings = SaveCloudAsrProviderSettings(store, now='2026-11-03T00:00:00Z').execute(enabled=True, confirm_enable=True)
    binding = models.server_model_access.selection('asr')['server_model_binding']
    view = _AsrModelsView(models, store, {'binding': binding, 'settings_revision': settings.settings_revision})
    with records.begin() as tx:
        tx.put('workspace_items', 'audio-original', {'project_id': 'alpha', 'source_text': '',
            'status': 'processing', 'processing_run_id': 'audio-run'}, expected_revision=0)
        tx.commit()
    assert egress_allowed(records, view, 'alpha', 'asr') is True
    assert egress_allowed(records, view, 'alpha', 'generation') is False
    material = {'type': 'original_item', 'id': 'audio-original', 'revision': 1, 'project_id': 'alpha'}
    turn = MemoryTurn(records, view, kind='memory.link_suggest', project='alpha', key='asr-material',
        materials=[material], validate=lambda: None, purpose='asr')
    assert turn.request['privacy']['material_refs'] == [material]
    assert turn.request['privacy']['source_snapshots']
    with pytest.raises(ServerModelBalanceError, match='no_wire_proof_invalid'):
        balances.confirm_none({'attempt_id': 'self-reported', 'status': 'confirmed_none'},
            reason='server_asr_source_changed')
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(6400))
    request = {'method': 'POST', 'endpoint': 'https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe',
        'payload': {'model': profile['model'], 'voice_encode_format': 'wav',
            'data': base64.b64encode(audio.getvalue()).decode('ascii')}}
    def revoke_after_reservation(control, current):
        attempt = BalanceRecordingSink(control, balances, models.server_model_access, 'asr', view.route(),
            wire_request=request, cost_records=records).begin_model_wire_attempt()
        def guarded():
            assert balances.read(member, user['user_id'])['reserved'] == '0.396'
            set_private_project(records, 'alpha', True, 0)
            try:
                current()
            except RecognitionConflict:
                attempt.confirmed_none(error_code='server_asr_source_changed')
                raise
        attempt.invoke_wire(guarded)
    with user_context(authorize_user(users, member)):
        with pytest.raises(RecognitionConflict):
            turn.generate([], response_model=RootModel[dict], max_tokens=0, invoke=revoke_after_reservation)
    wallet = balances.read(member, user['user_id'])
    assert wallet['reserved'] == '0' and wallet['spent'] == '0'
    proof = users.records.list(NO_WIRE)[0].payload
    assert proof['status'] == 'confirmed_none' and proof['reason'] == 'server_asr_source_changed'
    receipt = turn.store.get(proof['receipt_ref'])
    assert receipt['usage'] is None and receipt['cache_metadata'] is None
    with pytest.raises(RecognitionConflict):
        turn.validate()
    SaveCloudAsrProviderSettings(store, now='2026-11-03T00:01:00Z').execute(enabled=False, confirm_enable=False)
    with pytest.raises(RecognitionConflict, match='asr_settings_changed'):
        view.route()
    models.close()
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    from backend.memory_app.v2.server_models import METERING_PRICES
    from backend.memory_app.v2.server_model_balance import METERING, RESERVATIONS
    embedding = central.create_profile(admin, name='历史向量费用', purpose='embedding', provider='openai',
        base_url='https://api.openai.com/v1', model='text-embedding-3-small',
        allowed_models=['text-embedding-3-small'], enabled=True, api_key=credential.value)
    central.update_metering_prices(admin, embedding['profile_id'], expected_revision=0,
        expected_profile_revision=1, models={embedding['model']: {'unit': 'input_tokens',
            'rates': {'input_per_million': '1'}}})
    other = users.create(admin, name='保留账号')
    other_pair = registry.exchange(registry.issue_pairing(user_id=other['user_id'], actor=admin.device_id)['code'], name='保留设备')
    other_actor = registry.authenticate(other_pair['key'])
    for selected, actor, revision in ((user, member, 2), (other, other_actor, 0)):
        central.update_allocation(admin, selected['user_id'], expected_revision=revision, expected_user_revision=1,
            mode='shared', profiles={'embedding': embedding['profile_id']})
        if selected is other:
            balances.adjust(admin, other['user_id'], amount='10', expected_revision=0,
                expected_user_revision=1, reason='保留账号费用')
        binding = central.access_for(selected['user_id'], users.root_for(selected['user_id'])).selection('embedding')['server_model_binding']
        balances.reserve(actor=actor, user_id=selected['user_id'], user_root=users.root_for(selected['user_id']),
            binding=binding, attempt_id='legacy-meter' if selected is user else 'other-meter', model=embedding['model'],
            wire_request={'endpoint': 'https://api.openai.com/v1/embeddings',
                'payload': {'model': embedding['model'], 'input': ['隔离费用记录']}})
    with users.records.begin() as tx:
        historical = tx.read(METERING, 'legacy-meter')
        tx.put(METERING, historical.object_id, {key: value for key, value in historical.payload.items() if key != 'user_id'},
            expected_revision=historical.revision)
        tx.commit()
    admin_pair = registry.exchange(registry.issue_pairing(user_id=admin.user_id, actor=admin.device_id)['code'], name='删除管理设备')
    disabled = users.update(admin, user['user_id'], expected_revision=1, disabled=True)
    app = create_server_app(layout=DeploymentLayout('server', users.root_for('local-user'), users.server_root), port=18105)
    with TestClient(app) as client:
        response = client.request('DELETE', '/api/v2/server/users/' + user['user_id'],
            headers={'Authorization': 'Bearer ' + admin_pair['key']},
            json={'expected_revision': disabled['revision'], 'confirmation': user['user_id']})
        assert response.status_code == 200, response.text
    assert not root.exists()
    assert users.records.read(METERING, 'legacy-meter') is None
    assert not users.records.list(NO_WIRE)
    assert users.records.read(RESERVATIONS, 'legacy-meter') is None
    assert users.records.read(METERING, 'other-meter') is not None
    assert users.records.read(RESERVATIONS, 'other-meter') is not None
    assert users.records.read(METERING_PRICES, embedding['profile_id']) is not None


def test_asr_wallet_uses_declared_seconds_and_holds_incomplete_or_excess_duration(shared):
    import io
    import wave
    users, _, admin, user, member, models, balances, _, old_binding = shared
    root = users.root_for(user['user_id'])
    credential = models.access_for(user['user_id'], root).credential(old_binding)
    profile = models.create_profile(admin, name='转写时长', purpose='asr', provider='tokenhub-asr',
        base_url='https://tokenhub.tencentmaas.com/v1', model='hy-asr-3.0-preview',
        allowed_models=['hy-asr-3.0-preview'], enabled=True, api_key=credential.value)
    models.update_metering_prices(admin, profile['profile_id'], expected_revision=0,
        expected_profile_revision=1, models={profile['model']: {'unit': 'audio_seconds',
            'rates': {'audio_per_second': '0.00022'}}})
    models.update_allocation(admin, user['user_id'], expected_revision=1, expected_user_revision=1,
        mode='shared', profiles={'asr': profile['profile_id']})
    binding = models.access_for(user['user_id'], root).selection('asr')['server_model_binding']
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(6400))
    request = {'method': 'POST', 'endpoint': 'https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe',
        'payload': {'model': profile['model'], 'voice_encode_format': 'wav',
            'data': base64.b64encode(audio.getvalue()).decode('ascii')}}
    for attempt in ('asr-unknown', 'asr-known', 'asr-excess'):
        held = reserve(shared, attempt, binding=binding, model=profile['model'], wire_request=request)
        assert held['reserved_amount'] == '0.396'
        observation = {'duration_ms': None if attempt == 'asr-unknown' else 1800001 if attempt == 'asr-excess' else 200,
            'usage_total_token': None, 'sentence_end_ms': 200}
        result = balances.settle(attempt, usage={}, cache_observation=None, terminal_status='succeeded',
            meter_observation=observation)
        if attempt == 'asr-known':
            assert result['status'] == 'settled' and result['charged_amount'] == '0.000044'
        else:
            assert result['status'] == 'pending_review' and result['charged_amount'] is None
    wallet = balances.read(member, user['user_id'])
    assert wallet['reserved'] == '0.792' and wallet['spent'] == '0.000044'
    assert wallet['blocked_reason'] == 'usage_exceeds_bound'
    with pytest.raises(ServerModelBalanceError, match='account_blocked'):
        reserve(shared, 'asr-blocked', binding=binding, model=profile['model'], wire_request=request)


@pytest.mark.parametrize('condition', ['missing_parent', 'wrong_owner'])
def test_account_deletion_refuses_metering_without_actual_reservation_owner(shared, condition):
    from backend.memory_app.v2.server_deletion import _check_related
    from backend.memory_app.v2.server_model_balance import METERING
    from backend.security.user_context import UserError
    users, _, _, user, _, _, _, _, _ = shared
    if condition == 'wrong_owner':
        reserve(shared, 'invalid-meter-owner')
    with users.records.begin() as tx:
        tx.put(METERING, 'invalid-meter-owner', {'user_id': 'local-user'}, expected_revision=0)
        tx.commit()
    with users.records.begin_read() as tx:
        with pytest.raises(UserError, match='user_delete_related_data'):
            _check_related(tx, user['user_id'])
    assert users.records.read(METERING, 'invalid-meter-owner') is not None
    assert users.get(user['user_id'])['user_id'] == user['user_id']


@pytest.mark.parametrize('purpose,provider,base,model', [
    ('embedding', 'openai', 'https://api.openai.com/v1', 'text-embedding-3-small'),
    ('rerank', 'cohere', 'https://api.cohere.com/v2', 'rerank-v5.0-pro'),
    ('search', 'brave', 'https://api.search.brave.com/res/v1', 'brave-web-search'),
])
def test_real_shared_consumers_refuse_missing_meter_price_before_network(shared, purpose, provider, base, model):
    from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
    from backend.memory_app.retrieval_models import configured_adapter
    from backend.memory_app.v2.memory_turn import MemoryTurn, embedding_request
    from backend.memory_app.v2.search import search_once
    from backend.security.secrets import ServerFileSecretStore
    from backend.security.user_context import authorize_user, user_context
    from core.storage_provider import SQLiteStructuredRecordStore
    users, _, admin, user, member, central, balances, _, old_binding = shared
    root = users.root_for(user['user_id'])
    credential = central.access_for(user['user_id'], root).credential(old_binding)
    profile = central.create_profile(admin, name='用途检查', purpose=purpose, provider=provider,
        base_url=base, model=model, allowed_models=[model], enabled=True, api_key=credential.value)
    central.update_allocation(admin, user['user_id'], expected_revision=1, expected_user_revision=1,
        mode='shared', profiles={purpose: profile['profile_id']})
    records = SQLiteStructuredRecordStore(root / 'recognition.sqlite3')
    models = ModelConfiguration(records, root, secrets=ServerFileSecretStore(root / 'private-secrets.json'),
        server_model_access=central.access_for(user['user_id'], root), server_model_balances=balances)
    models.update(purpose, {'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    connections, active = [], [True]
    def observe(event, arguments):
        if active[0] and event == 'socket.connect':
            connections.append(arguments[1])
    sys.addaudithook(observe)
    try:
        with user_context(authorize_user(users, member)):
            with pytest.raises(ModelConfigurationError, match='server_model_metering_price_unavailable'):
                if purpose == 'search':
                    search_once(records, models, 'alpha', '隔离预留检查', key='missing-search-price')
                else:
                    adapter = configured_adapter(models, purpose)
                    payload = {'model': model, **({'input': ['资料']} if purpose == 'embedding' else
                        {'query': '问题', 'documents': ['资料'], 'max_tokens_per_doc': 4096})}
                    embedding_request(records, models, 'alpha', [], 'missing-' + purpose + '-price',
                        lambda: None, adapter.client, {'endpoint': adapter.endpoint, 'payload': payload}, purpose=purpose)
    finally:
        active[0] = False
        models.close()
    assert connections == [] and balances.read(member, user['user_id'])['reserved'] == '0'
    turn_id = records.list('v2_memory_turn_keys')[0].object_id
    turn_store = MemoryTurn.store_for(records)
    events = turn_store.events_after(turn_id)
    dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
    assert len(dispatched) == len(terminals) == 1
    assert turn_store.get(terminals[0]['data']['receipt_ref'])['status'] == 'failed_transport'


def test_shared_embedding_metering_prices_and_wallet_are_cas_and_preserve_unknown(shared):
    from decimal import Decimal
    from backend.memory_app.v2.server_models import ServerModelError
    users, devices, admin, user, member, models, balances, old_profile, generation_binding = shared
    credential = models.access_for(user['user_id'], users.root_for(user['user_id'])).credential(generation_binding)
    profile = models.create_profile(admin, name='向量', purpose='embedding', provider='openai',
        base_url='https://api.openai.com/v1', model='text-embedding-3-small',
        allowed_models=['text-embedding-3-small'], enabled=True, api_key=credential.value)
    price = {'unit': 'input_tokens', 'rates': {'input_per_million': '2'}}
    saved = models.update_metering_prices(admin, profile['profile_id'], models={profile['model']: price},
        expected_revision=0, expected_profile_revision=profile['revision'])
    assert saved['revision'] == 1 and models.get_profile(admin, profile['profile_id'])['prices'] == {}
    with pytest.raises(ServerModelError, match='forbidden'):
        models.metering_prices(member, profile['profile_id'])
    with pytest.raises(ServerModelError, match='revision_conflict'):
        models.update_metering_prices(admin, profile['profile_id'], models={profile['model']: price},
            expected_revision=0, expected_profile_revision=profile['revision'])
    models.update_allocation(admin, user['user_id'], expected_revision=1, expected_user_revision=1,
        mode='shared', profiles={'generation': old_profile['profile_id'], 'embedding': profile['profile_id']})
    binding = models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('embedding')['server_model_binding']
    request = {'endpoint': 'https://api.openai.com/v1/embeddings',
        'payload': {'model': profile['model'], 'input': ['资料']}}
    held = reserve(shared, 'wire-embedding', binding=binding, model=profile['model'], wire_request=request)
    assert held['reserved_amount'] == '0.6'
    pending = balances.settle('wire-embedding', usage=None, cache_observation=None, terminal_status='failed_transport')
    assert pending['status'] == 'pending_review' and balances.read(member, user['user_id'])['reserved'] == '0.6'
    usage = {'input_tokens': 12, 'output_tokens': 0, 'total_tokens': 12}
    charged = balances.settle('wire-embedding', usage=usage, cache_observation=None,
        terminal_status='succeeded', meter_observation={'input_tokens': 12})
    assert charged['status'] == 'settled' and Decimal(charged['charged_amount']) == Decimal('0.000024')
    assert balances.read(member, user['user_id'])['reserved'] == '0'
    assert balances.settle('wire-embedding', usage=usage, cache_observation=None,
        terminal_status='succeeded', meter_observation={'input_tokens': 12}) == charged
    devices.revoke(member.user_id, member.device_id, expected_revision=member.revision)
    with pytest.raises(ServerModelBalanceError, match='unauthorized'):
        reserve(shared, 'wire-revoked', binding=binding, model=profile['model'], wire_request=request)


@pytest.fixture
def shared(tmp_path):
    from cryptography.fernet import Fernet
    from backend.memory_app.v2.server_models import ServerModels
    configured = {'CHRIPTMAS_SERVER_MASTER_KEY': Fernet.generate_key().decode('ascii'),
        'CHRIPTMAS_DEPLOY': 'server', 'CHRIPTMAS_APP_ROOT': str(tmp_path)}
    previous = {name: os.environ.get(name) for name in configured}
    os.environ.update(configured)
    try:
        users, devices, admin, user, member = owners(tmp_path)
        models = ServerModels(users)
        rates = {'input_per_million': '2', 'output_per_million': '8', 'cache_read_per_million': '0.04'}
        profile = models.create_profile(admin, name='服务器模型', purpose='generation', provider='openai',
            base_url='https://api.deepseek.com/v1', model='deepseek-flash',
            allowed_models=['deepseek-flash', 'deepseek-v4-pro'], enabled=True,
            prices={'deepseek-flash': rates, 'deepseek-v4-pro': rates}, api_key='synthetic-balance-value')
        models.update_allocation(admin, user['user_id'], expected_revision=0,
            expected_user_revision=1, mode='shared', profiles={'generation': profile['profile_id']})
        balances = ServerModelBalances(users, models)
        balances.adjust(admin, user['user_id'], amount='10', expected_revision=0,
            expected_user_revision=1, reason='分配金额')
        root = users.root_for(user['user_id'])
        binding = models.access_for(user['user_id'], root).selection('generation')['server_model_binding']
        yield users, devices, admin, user, member, models, balances, profile, binding
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def reserve(shared, attempt='wire-first', **changes):
    users, _, _, user, member, _, balances, _, binding = shared
    return balances.reserve(**{'actor': member, 'user_id': user['user_id'],
        'user_root': users.root_for(user['user_id']), 'binding': binding,
        'attempt_id': attempt, 'model': 'deepseek-flash', 'max_output_tokens': 100, **changes})


USAGE = {'input_tokens': 1000, 'output_tokens': 100, 'total_tokens': 1100}
CACHE = {'cache_read_input_tokens': 200, 'cache_miss_input_tokens': 800}


def test_reserve_is_durable_before_send_and_complete_usage_charges_once(shared):
    users, _, admin, user, member, _, balances, _, _ = shared
    held = reserve(shared)
    assert held['reserved_amount'] == '2.097752' and held['status'] == 'reserved'
    public = balances.read(member, user['user_id'])
    assert (public['balance'], public['reserved'], public['available']) == ('10', '2.097752', '7.902248')
    assert 'synthetic-balance-value' not in json.dumps(held)
    assert not {'rates', 'binding', 'dispatch', 'base_url'} & held.keys()
    with pytest.raises(ServerModelBalanceError, match='balance_attempt_already_reserved'):
        reserve(shared)
    paid = balances.settle('wire-first', usage=USAGE, cache_observation=CACHE, terminal_status='succeeded')
    assert paid['charged_amount'] == '0.002408' and paid['status'] == 'settled'
    assert balances.settle('wire-first', usage=USAGE, cache_observation=CACHE, terminal_status='succeeded') == paid
    assert balances.read(admin, user['user_id'])['balance'] == '9.997592'
    assert balances.read(member, user['user_id'])['reserved'] == '0'
    assert balances.read(member, user['user_id'])['spent'] == '0.002408'
    with pytest.raises(ServerModelBalanceError, match='balance_settlement_conflict'):
        balances.settle('wire-first', usage={**USAGE, 'output_tokens': 101, 'total_tokens': 1101},
            cache_observation=CACHE, terminal_status='succeeded')
    assert len(users.records.list(ENTRIES)) == 2


def test_concurrent_physical_requests_cannot_overdraw_real_sqlite_balance(shared):
    from backend.memory_app.v2.server_models import ServerModels
    users, _, admin, user, member, _, balances, _, binding = shared
    balances.adjust(admin, user['user_id'], amount='-7', expected_revision=1,
        expected_user_revision=1, reason='保留三元')
    other_users = ServerUsers(users.server_root)
    other = ServerModelBalances(other_users, ServerModels(other_users))
    def send(values):
        owner, name = values
        try:
            owner.reserve(actor=member, user_id=user['user_id'], user_root=users.root_for(user['user_id']),
                binding=binding, attempt_id=name, model='deepseek-flash', max_output_tokens=100)
            return 'reserved'
        except ServerModelBalanceError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(send, [(balances, 'wire-a'), (other, 'wire-b')]))
    assert sorted(outcomes) == ['reserved', 'server_model_balance_insufficient']
    assert balances.read(member, user['user_id'])['available'] == '0.902248'
    assert len(balances.attempts(member, user['user_id'])['items']) == 1


def test_unknown_retry_and_process_restart_keep_each_reservation(shared):
    users, _, admin, user, member, _, balances, _, _ = shared
    reserve(shared, 'wire-first', external_task_id='turn-external-accepted')
    balances.settle('wire-first', usage=None, cache_observation=None, terminal_status='failed_transport')
    reserve(shared, 'wire-second')
    balances.mark_unknown('wire-second', reason_code='process_interrupted')
    reopened = ServerModelBalances(ServerUsers(users.server_root))
    snapshot = reopened.read(member, user['user_id'])
    assert snapshot['balance'] == '10' and snapshot['reserved'] == '4.195504'
    assert snapshot['pending_count'] == 2 and snapshot['spent'] == '0'
    assert len(reopened.attempts(member, user['user_id'])['items']) == 2
    assert any(row['external_task_id'] == 'turn-external-accepted'
               for row in reopened.attempts(member, user['user_id'])['items'])
    with pytest.raises(ServerModelBalanceError, match='balance_reserved_conflict'):
        reopened.adjust(admin, user['user_id'], amount='-9', expected_revision=snapshot['revision'],
            expected_user_revision=1, reason='减少余额')


def test_partial_usage_and_missing_cache_never_release_hold_and_reconcile_preserves_observation(shared):
    _, _, admin, user, member, _, balances, _, _ = shared
    reserve(shared)
    unknown = balances.settle('wire-first', usage={'input_tokens': 1000},
        cache_observation=None, terminal_status='failed_transport')
    assert unknown['status'] == 'pending_review' and unknown['usage'] == {'input_tokens': 1000}
    unknown = balances.settle('wire-first', usage=USAGE, cache_observation=None, terminal_status='failed_transport')
    assert unknown['pending_reason'] == 'cache_unavailable' and len(unknown['observations']) == 2
    with pytest.raises(ServerModelBalanceError, match='balance_forbidden'):
        balances.reconcile(member, user['user_id'], 'wire-first', amount='0.001',
            expected_revision=unknown['revision'], expected_user_revision=1, reason='核对账单')
    settled = balances.reconcile(admin, user['user_id'], 'wire-first', amount='0.003',
        expected_revision=unknown['revision'], expected_user_revision=1, reason='管理员核对提供方账单')
    assert settled['usage'] == USAGE and settled['observations'] == unknown['observations']
    assert settled['charged_amount'] == '0.003'
    assert balances.read(member, user['user_id'])['balance'] == '9.997'
    assert balances.read(member, user['user_id'])['reserved'] == '0'
    with pytest.raises(ServerModelBalanceError, match='balance_revision_conflict'):
        balances.reconcile(admin, user['user_id'], 'wire-first', amount='0',
            expected_revision=unknown['revision'], expected_user_revision=1, reason='重复核算')


def test_usage_bound_breach_blocks_new_sends_until_admin_reconciliation(shared):
    _, _, admin, user, member, _, balances, _, _ = shared
    reserve(shared)
    excess = {'input_tokens': 1100000, 'output_tokens': 100, 'total_tokens': 1100100}
    pending = balances.settle('wire-first', usage=excess, cache_observation=None, terminal_status='succeeded')
    assert pending['usage'] == excess and pending['pending_reason'] == 'usage_exceeds_bound'
    public = balances.read(member, user['user_id'])
    assert public['blocked_reason'] == 'usage_exceeds_bound' and public['reserved'] == '2.097752'
    with pytest.raises(ServerModelBalanceError, match='balance_account_blocked'):
        reserve(shared, 'wire-other')
    balances.reconcile(admin, user['user_id'], 'wire-first', amount='12',
        expected_revision=pending['revision'], expected_user_revision=1, reason='核对异常提供方账单')
    public = balances.read(member, user['user_id'])
    assert (public['balance'], public['available'], public['deficit']) == ('-2', '0', '2')
    assert public['spent'] == '12' and public['blocked_reason'] is None
    with pytest.raises(ServerModelBalanceError, match='balance_insufficient'):
        reserve(shared, 'wire-after')


def test_central_price_rotation_keeps_frozen_charge_and_rejects_old_binding(shared):
    from backend.memory_app.v2.server_models import ServerModelError
    users, _, admin, user, member, models, balances, profile, _ = shared
    reserve(shared)
    models.update_profile(admin, profile['profile_id'], expected_revision=1, prices={
        model: {'input_per_million': '99', 'output_per_million': '99', 'cache_read_per_million': '99'}
        for model in profile['allowed_models']})
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        reserve(shared, 'wire-new')
    settled = balances.settle('wire-first', usage=USAGE, cache_observation=CACHE, terminal_status='succeeded')
    assert settled['charged_amount'] == '0.002408'
    updated = models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('generation')
    with pytest.raises(ServerModelBalanceError, match='balance_insufficient'):
        reserve(shared, 'wire-new', binding=updated['server_model_binding'])
    assert balances.read(member, user['user_id'])['balance'] == '9.997592'


def test_shared_revocation_and_independent_mode_cannot_create_holds(shared):
    from backend.memory_app.v2.server_models import ServerModelError
    users, _, admin, user, member, models, balances, _, _ = shared
    models.update_allocation(admin, user['user_id'], expected_revision=1,
        expected_user_revision=1, mode='independent', profiles={})
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        reserve(shared)
    binding = models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('generation')['server_model_binding']
    with pytest.raises(ServerModelBalanceError, match='balance_independent'):
        reserve(shared, binding=binding)
    assert balances.read(member, user['user_id'])['reserved'] == '0'


def test_missing_price_unknown_model_and_revoked_device_never_reserve(shared):
    users, devices, admin, user, member, models, balances, profile, _ = shared
    models.update_profile(admin, profile['profile_id'], expected_revision=1,
        prices={}, allowed_models=['deepseek-flash', 'unknown'])
    binding = models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('generation')['server_model_binding']
    with pytest.raises(ServerModelBalanceError, match='balance_price_unavailable'):
        reserve(shared, binding=binding)
    with pytest.raises(ServerModelBalanceError, match='balance_bound_unavailable'):
        reserve(shared, model='unknown', binding=binding)
    with pytest.raises(ServerModelBalanceError, match='balance_model_forbidden'):
        reserve(shared, model='unallocated-model', binding=binding)
    devices.revoke(user['user_id'], member.device_id, expected_revision=member.revision)
    with pytest.raises(ServerModelBalanceError, match='balance_unauthorized'):
        reserve(shared, binding=binding)
    assert balances.read(admin, user['user_id'])['reserved'] == '0'


def test_reported_zero_and_failed_transport_actual_usage_are_billed_by_frozen_rates(shared):
    _, _, _, user, member, _, balances, _, _ = shared
    reserve(shared, 'wire-zero')
    result = balances.settle('wire-zero', usage={'input_tokens': 0, 'output_tokens': 0},
        cache_observation=None, terminal_status='succeeded')
    assert result['charged_amount'] == '0' and result['status'] == 'settled'
    reserve(shared, 'wire-failure')
    failed = balances.settle('wire-failure', usage=USAGE, cache_observation=CACHE,
        terminal_status='failed_transport')
    assert failed['status'] == 'settled' and failed['charged_amount'] == '0.002408'
    assert balances.read(member, user['user_id'])['balance'] == '9.997592'


def test_concurrent_settlement_is_idempotent_across_process_owner_restart(shared):
    users, _, _, user, member, _, balances, _, _ = shared
    reserve(shared)
    other = ServerModelBalances(ServerUsers(users.server_root))
    def settle(owner):
        return owner.settle('wire-first', usage=USAGE, cache_observation=CACHE,
            terminal_status='succeeded')
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(settle, (balances, other)))
    assert results[0] == results[1]
    assert balances.read(member, user['user_id'])['spent'] == '0.002408'
    assert len(users.records.list(ENTRIES)) == 2


def test_disabled_target_and_deletion_markers_block_adjustment_or_new_send(shared):
    from backend.memory_app.v2.server_models import ServerModelError
    users, _, admin, user, _, _, balances, _, _ = shared
    users.update(admin, user['user_id'], expected_revision=1, disabled=True)
    with pytest.raises(ServerModelError, match='server_model_user_disabled'):
        reserve(shared)
    with users.records.begin() as tx:
        tx.put('server_user_deletions', user['user_id'], {'user_id': user['user_id']}, expected_revision=0)
        tx.commit()
    with pytest.raises(ServerModelBalanceError, match='balance_user_deleting'):
        balances.adjust(admin, user['user_id'], amount='1', expected_revision=1,
            expected_user_revision=2, reason='删除期间增加')


def test_unknown_cache_write_and_conflicting_observations_keep_full_hold(shared):
    _, _, _, user, member, _, balances, _, _ = shared
    reserve(shared)
    pending = balances.settle('wire-first', usage=USAGE,
        cache_observation={**CACHE, 'cache_creation_input_tokens': 10}, terminal_status='succeeded')
    assert pending['status'] == 'pending_review'
    assert pending['cache_observation']['cache_write_input_tokens'] == 10
    with pytest.raises(ServerModelBalanceError, match='balance_settlement_conflict'):
        balances.settle('wire-first', usage={'input_tokens': 1, 'output_tokens': 1},
            cache_observation={'cache_read_input_tokens': 0}, terminal_status='succeeded')
    assert balances.read(member, user['user_id'])['reserved'] == '2.097752'


def test_unknown_cache_none_values_are_preserved_without_zero_invention(shared):
    _, _, _, user, member, _, balances, _, _ = shared
    reserve(shared)
    pending = balances.settle('wire-first', usage=USAGE, cache_observation={
        'cache_read_input_tokens': None, 'cache_creation_input_tokens': None,
        'cache_miss_input_tokens': None}, terminal_status='succeeded')
    assert pending['pending_reason'] == 'cache_unavailable'
    assert pending['cache_observation'] == {'cache_read_input_tokens': None,
        'cache_write_input_tokens': None, 'uncached_input_tokens': None}
    assert pending['charged_amount'] is None
    assert balances.read(member, user['user_id'])['reserved'] == '2.097752'


def test_pending_seek_pagination_reaches_old_rows_despite_newer_consumption(shared):
    _, _, admin, user, member, _, balances, _, _ = shared
    balances.adjust(admin, user['user_id'], amount='190', expected_revision=1,
        expected_user_revision=1, reason='分配分页检查金额')
    initial = [reserve(shared, 'wire-page-' + str(index)) for index in range(55)]
    expected = sorted(initial, key=lambda row: (row['created_at'], row['attempt_id']), reverse=True)
    first = balances.attempts(member, user['user_id'], state='pending', limit=7)
    assert [row['attempt_id'] for row in first['items']] == [row['attempt_id'] for row in expected[:7]]
    assert first['has_more'] and isinstance(first['next_cursor'], str)
    reserve(shared, 'wire-newer')
    balances.settle('wire-newer', usage={'input_tokens': 0, 'output_tokens': 0},
        cache_observation=None, terminal_status='succeeded')
    page, seen = first, list(first['items'])
    while page['has_more']:
        page = balances.attempts(member, user['user_id'], state='pending', limit=7, cursor=page['next_cursor'])
        seen.extend(page['items'])
    assert page['next_cursor'] is None
    assert [row['attempt_id'] for row in seen] == [row['attempt_id'] for row in expected]
    assert len({row['attempt_id'] for row in seen}) == 55
    assert all(row['status'] != 'settled' for row in seen)


def test_cursor_is_scoped_to_user_filter_and_live_device_permissions(shared):
    _, devices, _, user, member, _, balances, _, _ = shared
    reserve(shared, 'wire-page-first')
    reserve(shared, 'wire-page-second')
    first = balances.attempts(member, user['user_id'], state='all', limit=1)
    with pytest.raises(ServerModelBalanceError, match='balance_query_invalid'):
        balances.attempts(member, user['user_id'], state='pending', limit=1, cursor=first['next_cursor'])
    with pytest.raises(ServerModelBalanceError, match='balance_forbidden'):
        balances.attempts(member, 'local-user', state='all', limit=1, cursor=first['next_cursor'])
    decoded = json.loads(base64.urlsafe_b64decode(first['next_cursor'] + '=' * (-len(first['next_cursor']) % 4)))
    assert decoded['user_id'] == user['user_id'] and decoded['state'] == 'all'
    devices.revoke(user['user_id'], member.device_id, expected_revision=member.revision)
    with pytest.raises(ServerModelBalanceError, match='balance_unauthorized'):
        balances.attempts(member, user['user_id'], state='all', limit=1, cursor=first['next_cursor'])


def test_invalid_cursor_and_page_limits_return_fixed_errors(shared):
    _, _, _, user, member, _, balances, _, _ = shared
    for cursor in ('', '$invalid', 'a' * 1025, 'AA', base64.urlsafe_b64encode(b'{}').decode('ascii').rstrip('=')):
        with pytest.raises(ServerModelBalanceError, match='balance_query_invalid'):
            balances.attempts(member, user['user_id'], cursor=cursor)
    for limit in (0, -1, 201, True, '1'):
        with pytest.raises(ServerModelBalanceError, match='balance_query_invalid'):
            balances.attempts(member, user['user_id'], limit=limit)
    assert balances.attempts(member, user['user_id']) == {'items': [], 'has_more': False, 'next_cursor': None}


def test_real_process_exit_retains_hold_and_enables_explicit_reconciliation(shared):
    users, devices, admin, user, member, _, balances, _, _ = shared
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[3] / 'src'),
        'CHRIPTMAS_BALANCE_PAIRING': devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code']}
    child = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'fixtures' / 'server_balance_child.py'),
        str(users.server_root), user['user_id'], 'wire-process-child'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    try:
        assert child.stdout.readline().strip() == 'reserved'
        assert balances.recover_interrupted(user['user_id'])['alive'] == 1
        assert balances.attempts(member, user['user_id'])['items'][0]['status'] == 'reserved'
        copied_root = users.server_root.parent / 'copied-server-root'
        shutil.copytree(users.server_root, copied_root)
        child.terminate()
        child.wait(timeout=15)
        assert balances.recover_interrupted(user['user_id'])['recovered'] == 1
        pending = balances.attempts(member, user['user_id'])['items'][0]
        assert pending['status'] == 'pending_review' and pending['pending_reason'] == 'process_interrupted'
        assert balances.read(member, user['user_id'])['reserved'] == '2.097752'
        copied = ServerModelBalances(ServerUsers(copied_root))
        assert copied.recover_interrupted(user['user_id'])['unknown'] == 1
        assert copied.attempts(member, user['user_id'])['items'][0]['status'] == 'reserved'
        assert copied.read(member, user['user_id'])['reserved'] == '2.097752'
        balances.reconcile(admin, user['user_id'], pending['attempt_id'], amount='0.001',
            expected_revision=pending['revision'], expected_user_revision=1, reason='核对退出进程账单')
        assert balances.read(member, user['user_id'])['balance'] == '9.999'
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        child.communicate(timeout=15)


def test_legacy_reservation_without_process_proof_remains_reserved(shared):
    from backend.memory_app.v2.server_model_balance import RESERVATIONS
    users, _, _, user, member, _, balances, _, _ = shared
    reserve(shared)
    with users.records.begin() as tx:
        row = tx.read(RESERVATIONS, 'wire-first')
        tx.put(RESERVATIONS, row.object_id, {key: value for key, value in row.payload.items()
            if key not in {'process_pid', 'process_created_at'}}, expected_revision=row.revision)
        tx.commit()
    assert balances.recover_interrupted(user['user_id'])['legacy'] == 1
    assert balances.attempts(member, user['user_id'])['items'][0]['status'] == 'reserved'
