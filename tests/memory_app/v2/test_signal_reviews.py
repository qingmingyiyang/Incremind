from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from core.storage_provider import SQLiteStructuredRecordStore
from core.document_engine import SQLiteDocumentRepository
from backend.recognition import RecognitionService, WorkScope
from backend.memory_app.v2 import policies
from backend.memory_app.v2.signals import SignalService
from backend.memory_app.v2.signal_reviews import SignalReviews, install_signal_review_routes

AT=datetime(2026,10,6,12,tzinfo=timezone.utc)
@pytest.fixture
def env(tmp_path):
    records=SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    clock=[AT]
    owner=SignalReviews(records,service=RecognitionService(records),documents=SQLiteDocumentRepository(records),now=lambda:clock[0])
    app=FastAPI();install_signal_review_routes(app,records=records,owner=owner)
    with policies.override(review='@1'),TestClient(app) as http:
        yield SimpleNamespace(records=records,owner=owner,clock=clock,http=http)

def put(records,collection,identity,payload):
    with records.begin() as tx:
        old=tx.read(collection,identity)
        tx.put(collection,identity,payload,expected_revision=old.revision if old else 0);tx.commit()

def question(env,identity,seconds=0,project='alpha',text='项目预算应该怎么处理',by='user'):
    put(env.records,'v2_turns',identity,{'project_id':project,'intent':'ask','created_at':(AT+timedelta(seconds=seconds)).isoformat(),'user_text':text,'by':by,'receipt':{'ask':{'answer':'Synthetic answer\nSecond line\nThird line','citations':[]}}})

def pair(env,project='alpha',prefix='ask'):
    question(env,prefix+'-a',project=project);question(env,prefix+'-b',1,project=project)
    env.clock[0]=AT+timedelta(seconds=2)

def listed(env,project='alpha'):
    response=env.http.get('/api/v2/library/signal-reviews',params={'project_id':project})
    assert response.status_code==200,response.text
    return response.json()['items']

def decide(env,items,project='alpha'):
    return env.http.post('/api/v2/library/signal-reviews/decide',json={'project_id':project,'items':items})

def action(item,kind='confirm'):
    return {'id':item['id'],'action':kind,'expected_revision':item['revision']}

def test_registered_policy_bounded_strength_expiry_and_dismiss():
    assert policies.ACTIVE['review']=='@1'
    with policies.override(review='@1'):
        choose=policies.get('review')
        rows=[{'id':str(i),'strength':i,'created_at':AT.isoformat(),'review_key':str(i)} for i in range(8)]
        assert [x['id'] for x in choose(rows,now=AT,dismissed={'7'})]==['6','5','4','3','2']
        assert choose(rows,now=AT+timedelta(days=14),dismissed=set())==[]
        assert choose(rows,now=AT,dismissed=set(),enabled=False)==[]

def test_reask_real_sqlite_projection_fact_whitelist_and_no_text(env):
    pair(env)
    item=listed(env)[0]
    assert set(item)=={'id','kind','title','evidence','effect','revision'}
    assert item['kind']=='reask' and item['effect']=='correction'
    assert item['evidence']['questions']==['项目预算应该怎么处理']*2
    assert item['evidence']['answer']=='Synthetic answer\nSecond line'
    result=decide(env,[action(item)])
    assert result.status_code==200 and result.json()=={'items':[{'id':item['id'],'state':'confirmed'}]}
    rows=env.records.list('v2_signal_decisions');assert len(rows)==1
    assert set(rows[0].payload)=={'review_key','kind','action','turn_ids','object','at','by'}
    assert rows[0].payload['turn_ids']==['ask-a','ask-b'] and rows[0].payload['by']=='user'
    assert 'Synthetic' not in json.dumps(rows[0].payload)
    assert listed(env)==[]

def test_dismiss_survives_projection_rebuild_without_correction(env):
    pair(env);item=listed(env)[0]
    assert decide(env,[action(item,'dismiss')]).status_code==200
    projection=env.records.read('v2_signal_reviews','alpha')
    with env.records.begin() as tx:
        tx.delete('v2_signal_reviews','alpha',expected_revision=projection.revision);tx.commit()
    assert listed(env)==[] and not env.records.list('v2_correction_events')

def test_expiry_does_not_regenerate_same_pair(env):
    pair(env);assert listed(env)
    env.clock[0]+=timedelta(days=14)
    assert listed(env)==[]
    env.clock[0]+=timedelta(days=1)
    assert listed(env)==[]

def test_off_clear_admin_and_scope_are_real_owner_gates(env):
    pair(env);item=listed(env)[0]
    signals=SignalService(env.records,now=lambda:env.clock[0])
    signals.set_enabled(False,expected_revision=0)
    assert listed(env)==[] and decide(env,[action(item)]).status_code==409
    signals.set_enabled(True,expected_revision=1)
    signals.clear(expected_revision=2)
    assert listed(env)==[] and decide(env,[action(item)]).status_code==409
    question(env,'admin-a',3,by='admin');question(env,'admin-b',4,by='admin');env.clock[0]+=timedelta(seconds=5)
    assert listed(env)==[]
    assert listed(env,'other')==[]

def test_batch_conflict_atomic_and_unknown_bad_fields(env):
    pair(env);item=listed(env)[0]
    result=decide(env,[action(item),{'id':'missing','action':'confirm','expected_revision':1}])
    assert result.status_code==409 and 'current' in result.json()
    assert not env.records.list('v2_signal_decisions') and listed(env)[0]==item
    bad=action(item);bad['text']='private'
    assert decide(env,[bad]).status_code==400
    assert decide(env,[action(item),action(item)]).status_code==400

def test_current_turn_revision_changes_reject_without_decision(env):
    pair(env);item=listed(env)[0]
    row=env.records.read('v2_turns','ask-a')
    put(env.records,'v2_turns','ask-a',{**row.payload,'user_text':'changed question'})
    assert decide(env,[action(item)]).status_code==409
    assert not env.records.list('v2_signal_decisions')
from tests.memory_app.v2.test_workbench_ask import env as ask_env, publish, add_document, ask


def real_owner(ask_env):
    from backend.memory_app.v2.signal_reviews import SignalReviews
    return SignalReviews(ask_env.records,service=ask_env.service,documents=ask_env.documents,runtime_root=ask_env.root)


def five_answers(ask_env):
    ask_env.model.numbers=[]
    for _ in range(5):
        response=ask(ask_env)
        assert response.status_code==200,response.text
    assert ask_env.model.calls==5


def test_actual_five_ask_unused_insight_confirm_original_restore(ask_env):
    from backend.memory_app.v2.recall_preferences import set_preference
    insight,_=publish(ask_env)
    five_answers(ask_env)
    owner=real_owner(ask_env)
    with policies.override(review='@1'):
        unused=[i for i in owner.current('alpha')['items'] if i['kind']=='unused']
        assert len(unused)==1
        item=unused[0]
        assert item['evidence']['sent']==5 and item['evidence']['used']==0
        assert item['evidence']['object']=={'kind':'insight','id':insight.id,'revision':1}
        original=ask_env.records.read('recognitions',insight.id)
        owner.decide('alpha',[action(item)])
        pref=ask_env.records.read('recognition_recall_preferences',insight.id)
        assert pref.payload['state']=='cooled' and pref.payload['by']=='user'
        assert ask_env.records.read('recognitions',insight.id)==original
        assert not ask_env.records.list('v2_correction_events')
        report=SignalService(ask_env.records).report(kernel_groups=owner._groups('alpha'))
        assert report['corrections']['unknown_events']==0 and report['corrections']['unknown_model_events']==0
        assert all(group['corrections']==0 for group in report['corrections']['groups'])
        set_preference(ask_env.records,WorkScope('local-user','alpha'),insight.id,
                       recognition_revision=1,preference_revision=pref.revision,state='normal')
        assert ask_env.records.read('recognition_recall_preferences',insight.id).payload['state']=='normal'
        assert not any(i['kind']=='unused' for i in owner.current('alpha')['items'])


def test_actual_document_unused_summary_note_one_owner_and_no_source_cool(ask_env):
    identity,_=add_document(ask_env)
    five_answers(ask_env)
    owner=real_owner(ask_env)
    with policies.override(review='@1'):
        items=[i for i in owner.current('alpha')['items'] if i['kind']=='unused']
        assert len(items)==1
        item=items[0];assert item['evidence']['object']['id']==identity
        original=ask_env.records.read('documents',identity)
        owner.decide('alpha',[action(item)])
        pref=ask_env.records.read('v2_document_recall',identity)
        assert pref.payload['state']=='cooled' and pref.payload['by']=='user'
        assert ask_env.records.read('documents',identity)==original
        assert all(d.payload.get('object',{}).get('kind')!='source' for d in ask_env.records.list('v2_signal_decisions') if d.payload['object'])


def test_unused_missing_actual_input_and_changed_revision_are_unknown(ask_env):
    insight,_=publish(ask_env);five_answers(ask_env)
    owner=real_owner(ask_env)
    with policies.override(review='@1'):
        assert any(i['kind']=='unused' for i in owner.current('alpha')['items'])
        ask_env.service.revise(scope=WorkScope('local-user','alpha'),recognition_id=insight.id,
            expected_revision=1,content='alpha beta gamma revised',conditions=[])
        assert not any(i['kind']=='unused' for i in owner.current('alpha')['items'])
    # Same facts without the original immutable reader never become known zero.
    isolated=SignalReviews(ask_env.records,service=ask_env.service,documents=ask_env.documents)
    with policies.override(review='@1'):
        assert not any(i['kind']=='unused' for i in isolated.current('alpha')['items'])


def test_two_instances_same_decision_only_one_fact(env):
    from concurrent.futures import ThreadPoolExecutor
    pair(env);item=listed(env)[0]
    other=SignalReviews(SQLiteStructuredRecordStore(env.records.database_path),service=env.owner.service,
                        documents=env.owner.documents,now=env.owner.now)
    def attempt(owner):
        with policies.override(review='@1'):
            try:return owner.decide('alpha',[action(item)])
            except (HTTPException, __import__('core.storage_provider',fromlist=['SQLiteUnitOfWorkConflict']).SQLiteUnitOfWorkConflict):return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(attempt,[env.owner,other]))
    assert sum(isinstance(r,dict) for r in results)==1
    assert len(env.records.list('v2_signal_decisions'))==1


def test_disable_reenable_invalidates_old_revision(env):
    pair(env);old=listed(env)[0]
    signals=SignalService(env.records)
    signals.set_enabled(False,expected_revision=0);signals.set_enabled(True,expected_revision=1)
    assert decide(env,[action(old)]).status_code==409
    assert not env.records.list('v2_signal_decisions')


def test_future_stop_contract_only_user_ask_and_cutoff(env):
    question(env,'ask-a');question(env,'admin-a',by='admin')
    signals=SignalService(env.records,now=lambda:AT)
    # Future T14.11 producer contract uses existing server-only fact writer.
    signals.record(signals.prepare({'project_id':'alpha','kind':'stop','client_id':'stop-a','turn_id':'ask-a'},server=True))
    signals.record(signals.prepare({'project_id':'alpha','kind':'stop','client_id':'stop-admin','turn_id':'admin-a'},server=True,by='admin'))
    items=listed(env)
    assert len(items)==1 and items[0]['kind']=='stop' and items[0]['evidence']['count']==1
    assert decide(env,[action(items[0])]).status_code==200
    assert env.records.list('v2_signal_decisions')[0].payload['kind']=='stop'


def test_actual_sqlite_failure_rolls_back_whole_batch(env):
    pair(env);question(env,'stop-turn')
    signals=SignalService(env.records,now=lambda:env.clock[0])
    signals.record(signals.prepare({'project_id':'alpha','kind':'stop','client_id':'stop-event','turn_id':'stop-turn'},server=True))
    items=listed(env);assert len(items)>=2
    import sqlite3
    with sqlite3.connect(env.records.database_path) as sql:
        sql.execute("CREATE TRIGGER synthetic_reject_stop BEFORE INSERT ON crp_structured_records WHEN NEW.collection='v2_signal_decisions' AND json_extract(NEW.payload_json,'$.kind')='stop' BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    requests=[action(next(i for i in items if i['kind']=='reask')),action(next(i for i in items if i['kind']=='stop'))]
    with pytest.raises(sqlite3.IntegrityError):env.owner.decide('alpha',requests)
    assert not env.records.list('v2_signal_decisions')

def test_dismissed_pair_is_statistical_counterexample_but_not_correction(env):
    pair(env);item=listed(env)[0]
    assert decide(env,[action(item,'dismiss')]).status_code==200
    report=SignalService(env.records).report()
    assert len(report['reask']['pairs'])==1
    assert len(report['reask']['counterexamples'])==1
    assert report['corrections']['unknown_events']==0


def test_continuous_three_questions_are_two_original_pairs(env):
    pair(env);question(env,'ask-c',2);env.clock[0]+=timedelta(seconds=1)
    items=listed(env)
    assert len(items)==2 and all(i['evidence']['count']==1 for i in items)
    projection=env.records.read('v2_signal_reviews','alpha')
    assert {tuple(i['turn_ids']) for i in projection.payload['items']}=={('ask-a','ask-b'),('ask-b','ask-c')}


def test_current_projection_no_business_text_and_max_five(env):
    for i in range(8):question(env,'question-'+str(i),i)
    env.clock[0]+=timedelta(seconds=10)
    items=listed(env)
    assert len(items)==5
    projection=env.records.read('v2_signal_reviews','alpha')
    assert '项目预算' not in json.dumps(projection.payload,ensure_ascii=False)
    assert 'Synthetic answer' not in json.dumps(projection.payload)


def test_actual_unused_cool_and_decisions_rollback_together(ask_env):
    insight,_=publish(ask_env);five_answers(ask_env)
    owner=real_owner(ask_env)
    # The bounded policy orders equal strength by evidence time. Select the
    # earliest actual ASK rather than an arbitrary UUID-ordered store row.
    turn_id=min(ask_env.records.list('v2_turns'),
        key=lambda row:(row.payload['created_at'],row.object_id)).object_id
    signals=SignalService(ask_env.records)
    signals.record(signals.prepare({'project_id':'alpha','kind':'stop','client_id':'stop-future-contract','turn_id':turn_id},server=True))
    with policies.override(review='@1'):
        items=owner.current('alpha')['items']
        requests=[action(next(i for i in items if i['kind']=='unused')),action(next(i for i in items if i['kind']=='stop'))]
        import sqlite3
        with sqlite3.connect(ask_env.records.database_path) as sql:
            sql.execute("CREATE TRIGGER synthetic_stop_failure BEFORE INSERT ON crp_structured_records WHEN NEW.collection='v2_signal_decisions' AND json_extract(NEW.payload_json,'$.kind')='stop' BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
        with pytest.raises(sqlite3.IntegrityError):owner.decide('alpha',requests)
    assert ask_env.records.read('recognition_recall_preferences',insight.id) is None
    assert not ask_env.records.list('v2_signal_decisions')


def test_window_project_future_admin_and_cleared_copy_use_original_stats(env):
    question(env,'old',-31*86400);question(env,'future',86400)
    pair(env)
    report=SignalService(env.records).report(since=AT-timedelta(days=30),until=env.clock[0],project_id='alpha')
    assert len(report['after_answer']['turns'])==2
    other=SignalService(SQLiteStructuredRecordStore(env.records.database_path),now=lambda:env.clock[0])
    item=listed(env)[0];other.clear(expected_revision=0)
    assert decide(env,[action(item)]).status_code==409
    assert not env.records.list('v2_signal_decisions')


def test_confirmed_reask_original_actual_model_policy_attribution(ask_env):
    publish(ask_env);ask_env.model.numbers=[]
    first=ask(ask_env);second=ask(ask_env)
    assert first.status_code==second.status_code==200
    owner=real_owner(ask_env)
    with policies.override(review='@1'):
        item=next(i for i in owner.current('alpha')['items'] if i['kind']=='reask')
        owner.decide('alpha',[action(item)])
    report=SignalService(ask_env.records).report(kernel_groups=owner._groups('alpha'))
    assert report['corrections']['unknown_events']==0
    assert any(i['dimension']=='model' and i['corrections']==1 and i['outputs']==2 for i in report['corrections']['groups'])
@pytest.mark.parametrize('invalid',[{'id':'missing','action':[],'expected_revision':1},{'id':'missing','action':{},'expected_revision':1},{'id':'../bad','action':'confirm','expected_revision':1},{'id':'missing','action':'confirm','expected_revision':True}])
def test_decision_public_fields_strict_400(env,invalid):
    pair(env)
    response=decide(env,[invalid])
    assert response.status_code==400
    assert not env.records.list('v2_signal_decisions')


def test_off_before_cold_history_never_creates_projection(env):
    SignalService(env.records).set_enabled(False,expected_revision=0)
    assert listed(env)==[]
    assert env.records.read('v2_signal_reviews','alpha') is None


def test_real_app_default_namespace_installs_route_under_override(monkeypatch):
    from tempfile import TemporaryDirectory, gettempdir
    from shutil import copyfile
    with TemporaryDirectory(prefix='c20-') as directory:
        root=Path(directory)
        assert root.resolve().parent == Path(gettempdir()).resolve() and root.name.startswith('c20-')
        (root/'config').mkdir()
        copyfile(Path(__file__).parents[3]/'config/settings.toml.example',root/'config/settings.toml')
        monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(root))
        monkeypatch.setenv('CHRIPTMAS_DEPLOY','desktop')
        from backend.memory_app.app import create_app
        from tests.memory_app.v2.test_workbench_ask import Model
        app=create_app(runtime_root=root,legacy_app=FastAPI(),model_configuration=Model())
        with policies.override(review='@1'),TestClient(app) as client:
            response=client.get('/api/v2/library/signal-reviews',params={'project_id':'alpha'})
            assert response.status_code==200 and response.json()=={'items':[]}
    assert not root.exists()

def test_optional_project_window_does_not_count_other_project_open(env):
    from backend.memory_app.v2.usage import UsageService
    from core.document_engine import DocumentDraft
    document=env.owner.documents.create(DocumentDraft(title='Synthetic document',document_type='note',
        markdown='Synthetic text',source_refs=({'source_id':'synthetic-source','locator':'text:0'},),project_id='beta'))
    UsageService(env.records).record_usage('document',document['id'],'beta',1,event_kind='open')
    assert SignalService(env.records).report()['after_answer']['opens']==1
    assert SignalService(env.records).report(project_id='alpha')['after_answer']['opens']==0

def test_reask_counterexamples_keep_detected_pairs_and_scoped_fact_ids(env):
    pair(env);item=listed(env)[0]
    assert decide(env,[action(item,'dismiss')]).status_code==200
    fact=env.records.list('v2_signal_decisions')[0]
    report=SignalService(env.records).report()
    assert len(report['reask']['pairs'])==1
    assert report['reask']['counterexamples']==[{'project_id':'alpha','turn_ids':['ask-a','ask-b'],'decision_id':fact.object_id}]
    assert '项目预算' not in json.dumps(report,ensure_ascii=False)
    other=SignalService(env.records).report(project_id='other')
    assert other['reask'].get('counterexamples',[])==[]

def test_old_turns_first_exposure_starts_fourteen_days_without_reopen_extension(env):
    old=-20*86400
    question(env,'old-a',old);question(env,'old-b',old+1)
    items=listed(env);assert len(items)==1
    item=items[0]
    projection=env.records.read('v2_signal_reviews','alpha')
    assert projection.payload['items'][0]['created_at']==AT.isoformat()
    assert projection.payload['items'][0]['event_at']==(AT+timedelta(seconds=old+1)).isoformat()
    signals=SignalService(env.records)
    env.clock[0]+=timedelta(days=1)
    signals.set_enabled(False,expected_revision=0);assert listed(env)==[]
    signals.set_enabled(True,expected_revision=1);assert len(listed(env))==1
    assert env.records.read('v2_signal_reviews','alpha').payload['items'][0]['created_at']==AT.isoformat()
    env.clock[0]=AT+timedelta(days=14)
    assert listed(env)==[]
    assert env.records.read('v2_turns','old-b').payload['created_at']==(AT+timedelta(seconds=old+1)).isoformat()

def test_actual_five_ask_unused_shows_latest_three_but_keeps_all_fact_ids(ask_env):
    publish(ask_env);ask_env.model.numbers=[]
    ids=[]
    for i in range(5):
        response=ask(ask_env,text=f'alpha beta gamma question {i}?')
        assert response.status_code==200,response.text
        ids.append(response.json()['turn']['id'])
    assert ask_env.model.calls==5
    owner=real_owner(ask_env)
    with policies.override(review='@1'):
        item=next(i for i in owner.current('alpha')['items'] if i['kind']=='unused')
        assert item['evidence']['questions']==[f'alpha beta gamma question {i}?' for i in range(2,5)]
        assert item['evidence']['sent']==5 and item['evidence']['used']==0
        owner.decide('alpha',[action(item)])
    fact=next(row for row in ask_env.records.list('v2_signal_decisions') if row.payload['kind']=='unused')
    assert set(fact.payload['turn_ids'])==set(ids) and len(fact.payload['turn_ids'])==5
    assert 'question' not in json.dumps(fact.payload)
