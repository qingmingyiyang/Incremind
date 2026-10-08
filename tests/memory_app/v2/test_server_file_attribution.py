"""Actual raw owners retain only non-secret server mutation facts."""
import json
from dataclasses import replace
from pathlib import Path
import secrets
import pytest
from backend.shared.server_resources import SharedResources,resource_context
from backend.security.file_attribution import file_attribution
from backend.security.user_context import UserAccess,user_context
from tests.memory_app.v2.test_server_jobs import setup


def test_provider_settings_profile_and_credentials_bind_admin_to_same_replaced_file(tmp_path,monkeypatch):
    from backend.providers import ProviderRegistry
    from backend.team_memory import TeamMemoryProfileStore
    from backend.video_summary.infrastructure.settings import load_settings,save_settings,load_env_settings,save_env_settings
    from backend.security.secrets import build_model_secret_store,build_secret_store
    users,admin,a,_=setup(tmp_path);root=users.root_for(a['user_id']);resources=SharedResources(tmp_path)
    resources.provider_file_attribution_factory = file_attribution
    with resource_context(resources),user_context(UserAccess(admin,a['user_id'],'admin')):
        providers=ProviderRegistry(root)
        providers.create({'provider_id':'synthetic','name':'synthetic'},fallback={'provider_id':'openai'})
        profile=TeamMemoryProfileStore(root)
        store=build_model_secret_store(root)
        credential=secrets.token_urlsafe(24)
        store.set('synthetic-model-reference',credential)
        assert build_secret_store(root)._path==store._path==root/'secrets.json'
        config=root/'config/settings.toml'
        save_settings(config,load_settings(config,root))
        save_env_settings(root,replace(load_env_settings(root),model='synthetic'))
        profile.save(enabled=False,endpoint='',service_id='',team_id='',agent_id='',user_id='',
            expected_revision=0,confirm_enable=False,secret_store=store)
        paths=[root/'library/global/providers/providers.json',profile._path,store._path]
        for path in paths:
            value=json.loads(path.read_text(encoding='utf8'))
            binding=value['server_admin_attribution']
            assert binding['by']=='admin' and binding['target_user_id']==a['user_id'] and binding['actor_user_id']=='local-user'
            assert credential not in json.dumps(binding) and set(binding)=={'by','actor_user_id','target_user_id','device_id','namespace_id','owner_id','revision'}
        for path in (config,root/'.env'):
            text=path.read_text(encoding='utf8')
            assert '# chriptmas-server-attribution:' in text and credential not in text
        with user_context(None):
            providers.update('synthetic',{'name':'normal successor'},fallback={'provider_id':'openai'})
            profile.disconnect(expected_revision=1,confirmed=True,secret_store=store)
            store.delete('synthetic-model-reference')
        assert store.get_generation('synthetic-model-reference')==2
        for path in paths:
            value=json.loads(path.read_text(encoding='utf8'))
            assert 'server_admin_attribution' not in value
            assert value['server_owner_revision']==(3 if path==store._path else 2)


def test_file_attribution_wrong_target_refuses_before_replacing_any_business_file(tmp_path):
    from backend.providers import ProviderRegistry
    from backend.team_memory import TeamMemoryProfileStore
    from backend.security.secrets import build_model_secret_store
    users,admin,a,b=setup(tmp_path);root=users.root_for(a['user_id'])
    resources = SharedResources(tmp_path)
    resources.provider_file_attribution_factory = file_attribution
    with resource_context(resources):
        providers=ProviderRegistry(root);profile=TeamMemoryProfileStore(root);store=build_model_secret_store(root)
        with user_context(UserAccess(admin,b['user_id'],'admin')):
            with pytest.raises(ValueError,match='admin_target_mismatch'):
                providers.create({'provider_id':'synthetic'},fallback={'provider_id':'openai'})
            with pytest.raises(ValueError,match='admin_target_mismatch'):
                store.set('synthetic-model-reference','synthetic fixture secret')
            with pytest.raises(ValueError,match='admin_target_mismatch'):
                profile.save(enabled=False,endpoint='',service_id='',team_id='',agent_id='',user_id='',
                    expected_revision=0,confirm_enable=False,secret_store=store)
        assert not providers._path.exists() and not profile._path.exists() and not store._path.exists()
