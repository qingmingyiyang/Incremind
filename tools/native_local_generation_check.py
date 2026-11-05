import argparse
import json
import os
from pathlib import Path
import sys


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='使用明确提供的完整本机资源检查生成能力')
    parser.add_argument('--root', required=True)
    parser.add_argument('--asset-root', required=True)
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--case', choices=('generation', 'subscription'), default='generation')
    arguments = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    if not Path(arguments.root).is_absolute() or not Path(arguments.asset_root).is_absolute():
        raise ValueError('local_generation_check_requires_absolute_paths')
    isolated = Path(arguments.root).resolve()
    assets = Path(arguments.asset_root).resolve(strict=True)
    if (isolated in (workspace, isolated.parent) or 'runtime' in {part.lower() for part in isolated.parts}
            or assets != isolated / 'models'):
        raise ValueError('local_generation_check_requires_isolated_root_and_its_models_directory')
    isolated.mkdir(parents=True, exist_ok=True)
    os.environ['CHRIPTMAS_APP_ROOT'] = str(isolated)
    os.environ['CHRIPTMAS_LOCAL_ASSET_ROOT'] = str(assets)
    os.environ['CHRIPTMAS_DEPLOY'] = 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == isolated
    sys.path.insert(0, str(workspace / 'src'))
    sys.path.insert(0, str(workspace))
    from backend.memory_app.local_generation_readiness import inspect_local_generation
    readiness = inspect_local_generation(assets / 'qwen2.5-1.5b-instruct')
    if arguments.probe_only:
        print(json.dumps(readiness.public()))
        raise SystemExit(0)
    readiness.require_available()
    if not (isolated / 'config/settings.toml').is_file():
        raise ValueError('local_generation_check_requires_isolated_settings')
    import pytest
    selected = ('subscription_reselection' if arguments.case == 'subscription'
        else 'not subscription_reselection')
    raise SystemExit(pytest.main([__file__, '-q', '-k', selected,
        '--basetemp', str(isolated / 'native-checks')]))


import pytest


@pytest.fixture(scope='session')
def native_server():
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    app = create_server_app(layout=DeploymentLayout('server', root, root), port=8765)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 1234)) as client:
        yield app, client


@pytest.fixture
def native(tmp_path, native_server):
    from backend.memory_app.local_generation_readiness import inspect_local_generation
    from backend.shared.server_resources import resource_context
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assets = Path(os.environ['CHRIPTMAS_LOCAL_ASSET_ROOT']).resolve(strict=True)
    if assets != root / 'models':
        raise ValueError('local_generation_check_asset_root_mismatch')
    inspect_local_generation(assets / 'qwen2.5-1.5b-instruct').require_available()
    resources = native_server[0].state.server_context.resources
    with resource_context(resources):
        yield tmp_path, resources


def choose_local(config, port=8001):
    return config.update_generation_mode(mode='local', local_enabled=True,
        local_base_url=f'http://127.0.0.1:{port}/local-model/v1', expected_revision=config.generation_mode()['revision'])


def api_profile(config):
    config.update('generation', {'model': 'qa-api', 'base_url': 'https://example.invalid/v1',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})


def choose_fast(config, model):
    return config.update_fast_model(model=model, expected_revision=config.fast_model()['revision'],
        expected_generation_revision=config.public()['generation']['revision'],
        expected_mode_revision=config.generation_mode()['revision'])


def test_model_config_roundtrip_keeps_api_key_and_fixed_local_profile(native):
    from tests.memory_app.test_server_local_model_identity import local_application
    from backend.memory_app.model_config import ModelConfigurationError
    folder, resources = native
    config, _, _, _ = local_application(folder, resources=resources)
    api_profile(config)
    local = choose_local(config)
    assert local['mode'] == 'local' and local['local_enabled'] and local['api_configured']
    assert config.local_generation_allowed() and config.public()['generation']['configured']
    assert not config.public()['generation']['has_api_key']
    assert config.snapshot('generation')['api_key'] == 'local-model'
    with pytest.raises(ModelConfigurationError, match='select_api_before_editing_generation'):
        config.update('generation', {'model': 'wrong', 'expected_revision': 2})
    restored = config.update_generation_mode(mode='api', local_enabled=False,
        local_base_url=local['local_base_url'], expected_revision=local['revision'])
    assert restored['mode'] == 'api' and restored['local_enabled'] is False
    assert not config.local_generation_allowed() and config.snapshot('generation')['api_key'] == 'synthetic-only'


def test_public_mode_api_requires_selection_and_restores_remote_profile(native, native_server):
    app, client = native_server
    issued = app.state.device_registry.issue_pairing(user_id='local-user', actor='install')
    paired = client.post('/api/v2/devices/exchange', json={'code': issued['code'], 'name': 'native-check'})
    assert paired.status_code == 201
    headers = {'Authorization': 'Bearer ' + paired.json()['key']}
    body = {'model': 'qwen2.5-1.5b-instruct', 'messages': [{'role': 'user', 'content': 'local'}]}
    with client.portal.wrap_async_context_manager(app.state.server_user_pool.lease('local-user')) as child:
        config = child.state.recognition_models
        api_profile(config)
        assert client.post('/local-model/v1/chat/completions', json=body, headers=headers).status_code == 403
        chosen = client.put('/api/recognition/settings/generation-mode', json={'mode': 'local', 'local_enabled': True,
            'local_base_url': 'http://127.0.0.1:8765/local-model/v1', 'expected_revision': 0}, headers=headers)
        assert chosen.status_code == 200 and chosen.json()['mode'] == 'local'
        assert config.snapshot('generation')['api_key'] == 'local-model'
        assert client.get('/api/recognition/settings', headers=headers).json()['generation_mode']['api_configured']
        restored = client.put('/api/recognition/settings/generation-mode', json={'mode': 'api', 'local_enabled': False,
            'local_base_url': 'http://127.0.0.1:8765/local-model/v1', 'expected_revision': 1}, headers=headers)
        assert restored.status_code == 200 and config.snapshot('generation')['api_key'] == 'synthetic-only'
        assert client.post('/local-model/v1/chat/completions', json=body, headers=headers).status_code == 403


@pytest.mark.parametrize('port', [8001, 8765])
def test_selected_native_gateway_internal_identity_and_frozen_snapshot(native, port):
    from tests.memory_app.test_server_local_model_identity import local_application
    folder, resources = native
    config, auth, client, keys = local_application(folder, port, resources=resources)
    choose_local(config, port)
    assert config.snapshot('generation')['api_key'] == 'local-model'
    output, metadata = config.complete([{'role': 'user', 'content': '仅输出你好。'}], max_tokens=24)
    assert output.strip() and metadata['usage']['output_tokens'] > 0
    assert len(keys) == 1 and keys[0] != 'local-model'
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + keys[0]}).status_code == 401
    assert client.get('/local-model/v1/models', headers={'Authorization': 'Bearer ' + keys[0]}).status_code == 401
    assert auth.internal_key_for(f'http://127.0.0.1:{port}/local-model/v1') is not None
    assert config.generation_mode()['local_readiness']['status'] == 'loaded'


def test_two_members_share_one_native_engine_and_serialize_distinct_inputs(native, native_server):
    from threading import Lock, Thread, getprofile, setprofile_all_threads
    from backend.memory_app.local_model import _infer, _load
    _, resources = native
    app, client = native_server
    registry, users = app.state.device_registry, app.state.server_users
    pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='native-admin')
    admin = registry.authenticate(pair['key'])
    member = users.create(admin, name='native-second')
    other = registry.exchange(registry.issue_pairing(user_id=member['user_id'], actor=admin.device_id)['code'], name='native-member')
    keys = (pair['key'], other['key'])
    for key in keys:
        headers = {'Authorization': 'Bearer ' + key}
        current = client.get('/api/recognition/settings', headers=headers).json()['generation_mode']
        chosen = client.put('/api/recognition/settings/generation-mode', headers=headers,
            json={'mode': 'local', 'local_enabled': True, 'local_base_url': 'http://127.0.0.1:8765/local-model/v1',
                'expected_revision': current['revision']})
        assert chosen.status_code == 200, chosen.text
    cached_before = any(key[0] == 'qwen' for key in resources._models)
    live, observed, loads, results, errors, lock = [0, 0], [], [], [], [], Lock()
    def observe(frame, event, _argument):
        if frame.f_code is _load.__code__ and event == 'call':
            with lock:
                loads.append(frame.f_locals['model_dir'])
        if frame.f_code is _infer.__code__:
            with lock:
                if event == 'call':
                    live[0] += 1
                    live[1] = max(live)
                    observed.append(frame.f_locals['messages'][-1]['content'])
                elif event == 'return':
                    live[0] -= 1
    def request(key, text):
        try:
            response = client.post('/local-model/v1/chat/completions', headers={'Authorization': 'Bearer ' + key},
                json={'model': 'qwen2.5-1.5b-instruct', 'messages': [{'role': 'user', 'content': text}], 'max_tokens': 24})
            results.append(response)
        except BaseException as error:
            errors.append(error)
    previous = getprofile()
    setprofile_all_threads(observe)
    workers = [Thread(target=request, args=(key, text)) for key, text in zip(keys, ('仅输出甲。', '仅输出乙。'))]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(300)
        assert not errors and all(not worker.is_alive() for worker in workers)
        assert len(results) == 2 and all(response.status_code == 200 for response in results)
        assert all(response.json()['choices'][0]['message']['content'].strip() for response in results)
        assert set(observed) == {'仅输出甲。', '仅输出乙。'} and live == [0, 1]
        assert len(loads) == (0 if cached_before else 1)
        assert len([key for key in resources._models if key[0] == 'qwen']) == 1
        roots = [users.root_for(identity) for identity in ('local-user', member['user_id'])]
        assert roots[0] != roots[1] and all(not (root / 'data/models').exists() for root in roots)
    finally:
        setprofile_all_threads(previous)


def test_desktop_native_wire_keeps_local_key(native):
    from tests.memory_app.test_server_local_model_identity import local_application
    folder, resources = native
    config, _, _, keys = local_application(folder, provider=False, resources=resources)
    choose_local(config)
    output, _ = config.complete([{'role': 'user', 'content': '仅输出你好。'}], max_tokens=24)
    assert output.strip() and keys == ['local-model']


def test_native_profile_other_port_does_not_borrow_internal_key(native):
    from tests.memory_app.test_server_local_model_identity import local_application
    folder, resources = native
    config, auth, _, keys = local_application(folder, resources=resources)
    choose_local(config, 8002)
    assert auth.internal_key_for(config.snapshot('generation')['base_url']) is None
    gateway = config._generation_gateway(config.snapshot('generation'))
    assert gateway._wire_api_key() == 'local-model' and keys == []


def test_native_fast_choice_keeps_fixed_profile_and_endpoint(native):
    from backend.memory_app.model_config import ModelConfigurationError
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from pydantic import RootModel
    from tests.memory_app.test_server_local_model_identity import local_application
    folder, resources = native
    config, auth, client, keys = local_application(folder, 8765, resources=resources)
    choose_local(config, 8765)
    primary = config.records.read('recognition_model_config', 'generation')
    with pytest.raises(ModelConfigurationError, match='fast_local_model_unavailable'):
        choose_fast(config, 'other-model')
    choose_fast(config, 'qwen2.5-1.5b-instruct')
    selected = config.for_auxiliary(config.freeze_auxiliary_binding())
    memory = MemoryTurn(config.records, config, kind='memory.overview', project='inbox',
        key='native-local-fast', materials=[], validate=lambda: selected.snapshot('generation'))
    messages = [{'role': 'user', 'content': '仅输出你好。'}]
    def invoke(control, current):
        text, metadata = selected.complete(messages, max_tokens=24,
            validate_current=current, wire_attempt_sink=control)
        return RootModel[str](text), metadata
    output, metadata = memory.generate(messages, response_model=RootModel[str], max_tokens=24, invoke=invoke)
    assert output.root.strip() and metadata['model'] == 'qwen2.5-1.5b-instruct'
    event, = [event for event in memory.store.events_after(memory.turn_id) if event['type'] == 'model.completed']
    assert event['data']['model_call_purpose'] == 'aux'
    assert len(keys) == 1 and keys[0] != 'local-model'
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + keys[0]}).status_code == 401
    assert auth.internal_key_for('http://127.0.0.1:8001/local-model/v1') is None
    assert config.records.read('recognition_model_config', 'generation') == primary


def test_normal_server_factory_native_generation_uses_shared_assets_and_identity(native, native_server):
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    app, client = native_server
    with client.portal.wrap_async_context_manager(app.state.server_user_pool.lease('local-user')) as child:
            config = child.state.recognition_models
            assert config._local_models_root == app.state.server_context.resources.model_root
            choose_local(config, 8765)
            keys = []
            def wire(**request):
                keys.append(request['api_key'])
                response = client.post('/local-model/v1/chat/completions', headers={'Authorization': 'Bearer ' + request['api_key']},
                    json={'model': request['model'].removeprefix('openai/'), 'messages': request['messages'], 'max_tokens': request['max_tokens']})
                assert response.status_code == 200, response.text
                return response.json()
            config._completion_fn = wire
            output, _ = config.complete([{'role': 'user', 'content': '仅输出你好。'}], max_tokens=24)
            assert output.strip() and len(keys) == 1 and keys[0] != 'local-model'
            assert config.snapshot('generation')['api_key'] == 'local-model'
            assert client.get('/api/private', headers={'Authorization': 'Bearer ' + keys[0]}).status_code == 401
            assert not (root / 'recognition.sqlite3').exists()


def subscription_key_matches(config):
    from hmac import compare_digest
    key = config.snapshot('generation')['api_key']
    return bool(key) and compare_digest(key, config.subscriptions.token())


def test_subscription_reselection_can_restore_qualified_native_mode(native, native_server):
    from backend.memory_app.model_config import ModelConfigurationError
    app, client = native_server
    # 前提由正常界面在隔离账号完成，本检查不发起登录或构造任何凭据。
    with client.portal.wrap_async_context_manager(app.state.server_user_pool.lease('local-user')) as child:
        config = child.state.recognition_models
        auth = config.subscriptions.status()
        selection = config.subscription_selection()
        if not auth['connected'] or not auth['sharing']:
            raise ValueError('local_subscription_check_requires_real_encrypted_direct_account')
        if (not selection['model'] or selection['account_revision'] == auth['revision']
                or not config.generation_mode()['local_enabled']):
            raise ValueError('local_subscription_check_requires_actual_relogin_before_reselection')
        assert not config.local_generation_allowed() and config.generation_mode()['local_enabled']
        assert not config.public()['generation']['configured']
        with pytest.raises(ModelConfigurationError, match='selection_required'):
            config.snapshot('generation')
        config.select_subscription(model=selection['model'], expected_revision=selection['revision'])
        assert subscription_key_matches(config)
        choose_local(config)
        assert config.local_generation_allowed() and config.snapshot('generation')['model'] == 'qwen2.5-1.5b-instruct'
