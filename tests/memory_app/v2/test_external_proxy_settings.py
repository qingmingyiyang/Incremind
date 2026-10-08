"""独立记录开关使用原应用和真实 SQLite CAS。"""
from concurrent.futures import ThreadPoolExecutor
import importlib

import pytest

from core.storage_provider import SQLiteUnitOfWorkConflict
from tests.memory_app.v2.test_external_agent_settings import factory


DEFAULT={'revision':0,'record_conversations':{'claude':False,'codex':False}}
COLLECTION='v2_external_proxy_settings'


def read(client):
    response=client.get('/api/v2/settings')
    assert response.status_code==200
    return response.json()['external_proxy']


def test_defaults_noop_and_detached_read(factory):
    app,client=factory()
    before=client.get('/api/v2/settings').json()['external_agent']
    assert read(client)==DEFAULT
    copy=read(client); copy['record_conversations']['claude']=True
    assert read(client)==DEFAULT
    assert app.state.recognition_records.list(COLLECTION)==()
    response=client.patch('/api/v2/settings/external-proxy',json={
        'record_conversations':DEFAULT['record_conversations'],'expected_revision':0})
    assert response.status_code==200 and response.json()==DEFAULT
    assert app.state.recognition_records.list(COLLECTION)==()
    assert client.get('/api/v2/settings').json()['external_agent']==before


def test_persist_reopen_and_stale_cas_without_other_settings_changes(factory):
    app,client=factory(); old=client.get('/api/v2/settings').json()
    wanted={'record_conversations':{'claude':True,'codex':False},'expected_revision':0}
    response=client.patch('/api/v2/settings/external-proxy',json=wanted)
    assert response.status_code==200 and response.json()=={'revision':1,'record_conversations':wanted['record_conversations']}
    _,other=factory()
    assert read(other)==response.json()
    assert other.patch('/api/v2/settings/external-proxy',json=wanted).status_code==409
    current=client.get('/api/v2/settings').json()
    assert current['external_agent']==old['external_agent'] and current['model']==old['model']
    assert current['privacy']==old['privacy']
    assert app.state.recognition_records.read(COLLECTION,'default').payload=={'record_conversations':wanted['record_conversations']}


@pytest.mark.parametrize('body',[
    {}, {'record_conversations':{'claude':True,'codex':False}},
    {'record_conversations':{'claude':True,'codex':False},'expected_revision':True},
    {'record_conversations':{'claude':True,'codex':False},'expected_revision':-1},
    {'record_conversations':{'claude':1,'codex':False},'expected_revision':0},
    {'record_conversations':{'claude':True},'expected_revision':0},
    {'record_conversations':{'claude':True,'codex':False,'other':False},'expected_revision':0},
    {'record_conversations':{'claude':True,'codex':False},'expected_revision':0,'url':'synthetic'},
    {'record_conversations':{'claude':True,'codex':False},'expected_revision':0,'credential':'synthetic'},
    {'record_conversations':None,'expected_revision':0}])
def test_strict_schema_never_writes(factory,body):
    app,client=factory()
    response=client.patch('/api/v2/settings/external-proxy',json=body)
    assert response.status_code==400
    assert response.json()['detail']=='external_proxy_settings_invalid'
    assert app.state.recognition_records.list(COLLECTION)==()


def test_two_real_instances_only_one_cas_winner(factory):
    app,_=factory(); other,_=factory()
    module=importlib.import_module('backend.memory_app.v2.external_proxy_settings')
    def write(records,client):
        try:
            return module.replace_external_proxy_settings(records,{'record_conversations':{
                'claude':client=='claude','codex':client=='codex'}},expected_revision=0)
        except SQLiteUnitOfWorkConflict: return 'conflict'
    with ThreadPoolExecutor(2) as executor:
        one=executor.submit(write,app.state.recognition_records,'claude')
        two=executor.submit(write,other.state.recognition_records,'codex')
        values=[one.result(),two.result()]
    assert values.count('conflict')==1
    winner=next(value for value in values if value!='conflict')
    assert winner['revision']==1 and sum(winner['record_conversations'].values())==1
    assert module.external_proxy_settings(app.state.recognition_records)==winner
