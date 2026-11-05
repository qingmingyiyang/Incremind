from threading import Thread

from fastapi.testclient import TestClient
from backend.shared.deployment import DeploymentLayout


def test_two_actual_user_routes_reject_missing_shared_assets_without_loading(tmp_path):
    from backend.memory_app.app import create_server_app
    app=create_server_app(layout=DeploymentLayout('server',tmp_path/'users/local-user',tmp_path))
    registry,users=app.state.device_registry,app.state.server_users
    pair=registry.exchange(registry.issue_pairing(user_id='local-user',actor='install')['code'],name='admin')
    admin=registry.authenticate(pair['key']);user=users.create(admin,name='second')
    paired=registry.exchange(registry.issue_pairing(user_id=user['user_id'],actor=admin.device_id)['code'],name='second device')
    with TestClient(app) as client:
        for key in (pair['key'],paired['key']):
            headers={'Authorization':'Bearer '+key}
            current=client.get('/api/v2/settings?project_id=inbox',headers=headers)
            assert current.status_code==200
            mode=current.json()['model']['generation_mode']
            assert mode['local_readiness']['status']=='missing_assets'
            chosen=client.put('/api/v2/settings/generation-mode',headers=headers,json={
                'mode':'local','local_enabled':True,'local_base_url':'http://127.0.0.1:8001/local-model/v1',
                'expected_revision':mode['revision']})
            assert chosen.status_code==400 and chosen.json()['detail']=='local_model_assets_missing'
        def request(key,text):
            return client.post('/local-model/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={
                'model':'qwen2.5-1.5b-instruct','messages':[{'role':'user','content':text}],'max_tokens':4})
        results=[]
        first=Thread(target=lambda:results.append(request(pair['key'],'alpha')))
        second=Thread(target=lambda:results.append(request(paired['key'],'beta')))
        first.start();second.start();first.join(15);second.join(15)
        assert not first.is_alive() and not second.is_alive()
        assert len(results)==2 and all(result.status_code==403 for result in results)
        assert all(result.json()['detail']=='local_model_disabled' for result in results)
        assert app.state.server_context.resources._models=={}
        roots=[users.root_for(identity) for identity in ('local-user',user['user_id'])]
        assert roots[0]!=roots[1]
        assert all(not (root/'data/models').exists() for root in roots)
        assert client.post('/local-model/v1/chat/completions',json={}).status_code==401
