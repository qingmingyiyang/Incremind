"""Real scoped read projection never exposes bodies or unbound admin facts."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from core.storage_provider import JsonObjectStore
from backend.security.user_context import json_attribution
from tests.memory_app.v2.test_server_dispatch import fixture,headers


def test_original_audio_and_record_by_projections_are_bound_to_current_user_revision(tmp_path):
    app,users,_,_,admin_pair,target,pair,_=fixture(tmp_path)
    pool=app.state.server_user_pool;original=pool.factory;stores={}
    def factory(root,user_id):
        child=original(root,user_id)
        store=JsonObjectStore(root/'.rebuild-data',namespace_id='default',mutation_attribution=json_attribution(root,'default'))
        audio=JsonObjectStore(root/'workspace/asr-internal',namespace_id='default',mutation_attribution=json_attribution(root,'default'))
        stores[user_id]=(store,audio)
        child.state.recognition_runtime_root=root
        child.state.workspace_domains=SimpleNamespace(confirmations=None,legacy_reviews=SimpleNamespace(object_store=store))
        @child.put('/api/test/original')
        def write():
            for owner in (store,audio):owner.write('sources','one',{'id':'one','body':'private synthetic body'},expected_revision=0)
            return {'saved':True}
        return child
    pool.factory=factory
    endpoint='/api/v2/server/attribution'
    with TestClient(app) as client:
        assert client.put('/api/test/original',headers=headers(admin_pair,target['user_id'])).status_code==200
        for authority in ('originals','audio'):
            response=client.get(endpoint,headers=headers(pair),params={'authority':authority,'collection':'sources','object_id':'one','revision':1})
            assert response.status_code==200 and response.json()['by']['target_user_id']==target['user_id']
            assert response.json()['by']['by']=='admin' and 'private synthetic body' not in response.text
            wrong=client.get(endpoint,headers=headers(pair),params={'authority':authority,'collection':'sources','object_id':'one','revision':2})
            assert wrong.status_code==409
        assert client.get(endpoint,headers=headers(pair),params={'authority':'arbitrary','collection':'sources','object_id':'one','revision':1}).status_code==422
        assert client.get(endpoint,headers=headers(pair),params={'authority':'audio','collection':'../bad','object_id':'one','revision':1}).status_code==400
        assert client.get(endpoint,headers=headers(pair,'local-user'),params={'collection':'objects','object_id':'one','revision':1}).status_code==403
        store,_=stores[target['user_id']]
        path=store._object_paths('sources','one').meta_path
        metadata=json.loads(path.read_text());metadata['server_admin_attribution']['target_user_id']='different-user'
        path.write_text(json.dumps(metadata),encoding='utf8')
        assert client.get(endpoint,headers=headers(pair),params={'authority':'originals','collection':'sources','object_id':'one','revision':1}).status_code==403
