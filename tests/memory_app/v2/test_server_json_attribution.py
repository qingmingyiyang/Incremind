"""Attribution belongs to actual JSON CAS metadata, never old business payloads."""
import json
from pathlib import Path

import pytest
from core.storage_provider import JsonObjectStore
from core.storage_provider.runtime import ObjectStoreRevisionError


def attribution(collection, object_id, revision):
    return {'by':'admin', 'actor_user_id':'local-user', 'target_user_id':'user-b',
        'device_id':'device-one', 'namespace_id':'default', 'collection':collection,
        'object_id':object_id, 'revision':revision, 'action':'write'}


def test_json_business_payload_is_unchanged_and_admin_binding_matches_actual_cas_then_clears(tmp_path):
    store = JsonObjectStore(tmp_path, mutation_attribution=attribution)
    payload = {'id':'one', 'body':'合成'}
    assert store.write('objects','one',payload,expected_revision=0) == 1
    assert store.read('objects','one') == payload
    assert store.attribution('objects','one',1) == attribution('objects','one',1)
    assert store.attribution('objects','one',2) is None
    ordinary = JsonObjectStore(tmp_path)
    assert ordinary.write('objects','one',{'id':'one','body':'本人'},expected_revision=1)==2
    assert ordinary.attribution('objects','one',2) is None
    assert json.loads(ordinary._object_paths('objects','one').meta_path.read_text()) == {'revision':2}


def test_wrong_cas_never_calls_callback_and_callback_failure_writes_no_business_or_by(tmp_path):
    calls = []
    def rejected(collection, object_id, revision):
        calls.append((collection,object_id,revision))
        raise ValueError('attribution_rejected')
    store = JsonObjectStore(tmp_path, mutation_attribution=rejected)
    with pytest.raises(ObjectStoreRevisionError):
        store.write('objects','one',{'id':'one'},expected_revision=9)
    assert calls == []
    with pytest.raises(ValueError, match='attribution_rejected'):
        store.write('objects','one',{'id':'one'},expected_revision=0)
    assert calls == [('objects','one',1)] and store.read('objects','one') is None
    assert store.attribution('objects','one',1) is None


def test_invalid_binding_is_rejected_before_payload_and_metadata_replace_failure_is_not_success(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path, mutation_attribution=lambda *args: {'by':'admin'})
    with pytest.raises(ValueError, match='mutation_attribution_invalid'):
        store.write('objects','bad',{'id':'bad'},expected_revision=0)
    assert store.read('objects','bad') is None
    from core.storage_provider import runtime
    writer = runtime._write_json_atomic
    def write(path, value):
        if path == store._object_paths('objects','one').meta_path:
            raise OSError('synthetic_meta_replace_failure')
        return writer(path,value)
    monkeypatch.setattr(runtime,'_write_json_atomic',write)
    store.mutation_attribution = attribution
    with pytest.raises(OSError, match='synthetic_meta_replace_failure'):
        store.write('objects','one',{'id':'one'},expected_revision=0)
    assert store.revision('objects','one') == 0 and store.attribution('objects','one',1) is None
    # Existing JSON authority uses two file replaces; a failure is observable
    # and may leave payload bytes. This test deliberately does not claim atomicity.
    assert store.read('objects','one') == {'id':'one'}


def test_server_callback_matches_actual_root_and_preserves_logical_delete_binding(tmp_path):
    from backend.memory_app.server_audit import json_attribution
    from backend.security.device_identity import DeviceIdentity
    from backend.security.user_context import UserAccess, user_context
    from backend.shared.server_resources import SharedResources, resource_context
    root = tmp_path/'users/user-b'
    with resource_context(SharedResources(tmp_path)):
        callback = json_attribution(root, 'default')
        store = JsonObjectStore(root/'.rebuild-data',mutation_attribution=callback)
        with user_context(UserAccess(DeviceIdentity('device-one','local-user',1),'user-b','admin')):
            store.write('objects','one',{'id':'one','status':'deleted'},expected_revision=0)
            assert store.attribution('objects','one',1)['target_user_id']=='user-b'
            wrong = JsonObjectStore(tmp_path/'users/other/.rebuild-data',mutation_attribution=json_attribution(tmp_path/'users/other','default'))
            with pytest.raises(ValueError, match='admin_target_mismatch'):
                wrong.write('objects','one',{'id':'one'},expected_revision=0)
            assert wrong.read('objects','one') is None


def test_actual_rebuild_factory_sources_write_and_wrong_cas_bind_real_user_root(tmp_path):
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.security.device_identity import DeviceIdentity
    from backend.security.user_context import UserAccess, user_context
    from backend.shared.server_resources import SharedResources, resource_context
    root = tmp_path/'users/user-b'
    with resource_context(SharedResources(tmp_path)):
        store, settings = build_rebuild_object_store(root)
        with user_context(UserAccess(DeviceIdentity('device-one','local-user',1),'user-b','admin')):
            assert store.write('sources','one',{'id':'one','title':'原件'},expected_revision=0)==1
            assert store.attribution('sources','one',1)['by']=='admin'
            with pytest.raises(ObjectStoreRevisionError):
                store.write('sources','one',{'id':'one'},expected_revision=0)
            assert store.attribution('sources','one',1)['namespace_id']==settings.namespace_id


@pytest.mark.parametrize('change', [{'extra':'private'}, {'actor_user_id':17}, {'by':'user'}, {'action':'read'}, {'device_id':None}])
def test_real_metadata_corruption_never_returns_unvalidated_attribution(tmp_path, change):
    store = JsonObjectStore(tmp_path,mutation_attribution=attribution)
    store.write('objects','one',{'id':'one'},expected_revision=0)
    path = store._object_paths('objects','one').meta_path
    metadata = json.loads(path.read_text())
    metadata['server_admin_attribution'].update(change)
    path.write_text(json.dumps(metadata),encoding='utf8')
    assert store.attribution('objects','one',1) is None
