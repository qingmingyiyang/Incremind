"""Actual product factory imports and dispatches isolated users without ENV swaps."""
import os
from pathlib import Path
import subprocess
import sys


def environment(root, tmp_path):
    result = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'] if key in os.environ}
    result.update(CHRIPTMAS_DEPLOY='server', CHRIPTMAS_APP_ROOT=str(root),
        LOCALAPPDATA=str(tmp_path / 'local'), PYTHONPATH=str(Path('src').resolve()),
        USERPROFILE=str(tmp_path / 'profile'), HOME=str(tmp_path / 'profile'), CHRIPTMAS_COMPANION_MODE='development')
    return result


def test_server_product_import_is_lazy_and_three_users_have_distinct_records_models_and_cache(tmp_path, tmp_path_factory):
    root = tmp_path_factory.mktemp('s')
    script = '''
from pathlib import Path
import os
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = Fernet.generate_key().decode('ascii')
import backend.memory_app.app as module
assert module.app.application is None
assert not list(Path(os.environ['CHRIPTMAS_APP_ROOT']).rglob('*.sqlite3'))
from backend.shared.deployment import resolve_deployment
layout = resolve_deployment(Path(os.environ['CHRIPTMAS_APP_ROOT']))
application = module.create_server_app(layout=layout, port=8765)
assert application.state.server_user_pool.loaded_user_ids == ()
registry = application.state.device_registry
pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='admin')
headers = {'Authorization': 'Bearer '+pair['key']}
with TestClient(application, base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as client:
    users = [client.post('/api/v2/server/users',headers=headers,json={'name':name}).json() for name in ['one','two']]
    assert all(user.get('role') == 'user' for user in users)
    assert application.state.server_user_pool.loaded_user_ids == ()
    targets = ['local-user']+[user['user_id'] for user in users]
    for target in targets:
        selected={**headers,'X-Chriptmas-Target-User':target}
        assert client.get('/api/v2/settings',headers=selected).status_code == 200
        assert os.environ['CHRIPTMAS_APP_ROOT'] == str(layout.server_root)
        child = application.state.server_user_pool._children[target].application
        assert child.state.recognition_runtime_root == layout.server_root/'users'/target
        assert child.state.workspace_domains.items.records.database_path.is_relative_to(child.state.recognition_runtime_root)
        assert child.state.recognition_records.database_path.is_relative_to(child.state.recognition_runtime_root)
        assert child.state.server_context is application.state.server_context
    children=[application.state.server_user_pool._children[target].application for target in targets]
    import secrets
    credentials=[secrets.token_urlsafe(24) for _ in children]
    devices=[]
    for target in targets:
        issued=registry.issue_pairing(user_id=target,actor=registry.authenticate(pair['key']).device_id)
        devices.append(registry.exchange(issued['code'],name='synthetic user device'))
    for index,target in enumerate(targets):
        own={'Authorization':'Bearer '+devices[index]['key']}
        saved=client.put('/api/recognition/settings',headers=own,json={
            'purpose':'generation','base_url':'https://provider.example/v1','model':'synthetic-model',
            'api_key':credentials[index],'allow_remote':True,'expected_revision':0})
        assert saved.status_code==200 and saved.json()['has_api_key']
        for reader in (own,{**headers,'X-Chriptmas-Target-User':target}):
            for endpoint in ('/api/recognition/settings','/api/v2/settings'):
                value=client.get(endpoint,headers=reader)
                assert value.status_code==200
                assert all(credential not in value.text for credential in credentials)
        if index:
            denied=client.get('/api/recognition/settings',headers={**own,'X-Chriptmas-Target-User':'local-user'})
            assert denied.status_code==403
    used=[]
    for index,child in enumerate(children):
        def wire(index=index,**request):
            assert request['api_key']==credentials[index]
            used.append(index)
            return {'choices':[{'message':{'content':'synthetic answer'},'finish_reason':'stop'}],'usage':{}}
        child.state.recognition_models._completion_fn=wire
        result=child.state.recognition_models.complete([{'role':'user','content':'synthetic prompt'}])
        assert result[0]=='synthetic answer'
    assert used==[0,1,2]
    assert len({child.state.recognition_models.secrets._path for child in children})==3
    import httpx
    requests=[]
    def no_oauth(request):
        requests.append(request.method)
        return httpx.Response(502,json={'error':'synthetic_transport'})
    subscription=children[0].state.recognition_models.subscriptions
    subscription.client.close()
    subscription.client=httpx.Client(transport=httpx.MockTransport(no_oauth))
    denied=client.post('/api/v2/settings/subscriptions/login',headers=headers,json={'expected_revision':0})
    assert denied.status_code==409 and denied.json()=={'detail':'subscription_login_unavailable_server'}
    assert requests==[] and subscription._attempts=={}
    assert client.get('/api/v2/settings/subscriptions',headers=headers).status_code==200
    assert len({id(child.state.recognition_models) for child in children})==3
    assert len({id(child.state.workspace_domains.query) for child in children})==3
    auth=application.state.server_device_auth
    keys=[child.state.recognition_models._internal_local_key_provider('http://127.0.0.1:8765/local-model/v1') for child in children]
    assert len(set(keys)) == 3 and all(keys)
    assert all(child.state.recognition_models._internal_local_key_provider('http://127.0.0.1:8001/local-model/v1') is None for child in children)
assert application.state.server_user_pool.loaded_user_ids == ()
assert all(child.state.recognition_models.subscriptions.client.is_closed for child in children)
print('three-user-product-composition-ok')
'''
    result = subprocess.run([sys.executable, '-c', script], env=environment(root, tmp_path), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == 'three-user-product-composition-ok'


def test_actual_factory_local_profile_uses_shared_installed_weights_without_user_copy(tmp_path, tmp_path_factory):
    root=tmp_path_factory.mktemp('s')
    script='''
from pathlib import Path
import os
from fastapi.testclient import TestClient
root=Path(os.environ['CHRIPTMAS_APP_ROOT'])
model=root/'models/qwen2.5-1.5b-instruct/model.safetensors'
model.parent.mkdir(parents=True);model.write_bytes(b'synthetic installed asset')
from backend.memory_app.app import create_server_app
application=create_server_app(port=8765)
registry=application.state.device_registry
pair=registry.exchange(registry.issue_pairing(user_id='local-user',actor='install')['code'],name='synthetic admin')
headers={'Authorization':'Bearer '+pair['key']}
with TestClient(application,base_url='http://127.0.0.1:8765') as client:
    assert client.get('/api/recognition/settings',headers=headers).status_code==200
    child=application.state.server_user_pool._children['local-user'].application
    models=child.state.recognition_models
    assert models.generation_mode()['local_model_installed'] is True
    selected=client.put('/api/recognition/settings/generation-mode',headers=headers,json={
        'mode':'local','local_enabled':True,'local_base_url':'http://127.0.0.1:8765/local-model/v1','expected_revision':0})
    assert selected.status_code==200 and models.local_generation_allowed()
    assert not (child.state.recognition_runtime_root/'data/models/qwen2.5-1.5b-instruct').exists()
print('actual-factory-shared-local-profile-ok')
'''
    result=subprocess.run([sys.executable,'-c',script],env=environment(root,tmp_path),capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stderr
    assert result.stdout.splitlines()[-1]=='actual-factory-shared-local-profile-ok'
