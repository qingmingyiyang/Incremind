import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.signals import SignalService

AT='2026-10-06T12:00:00+00:00'
@pytest.fixture
def env(tmp_path):
    records=SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    return records,SignalService(records,now=lambda:datetime.fromisoformat(AT))

def put(records,collection,identity,payload):
    with records.begin() as tx:
        tx.put(collection,identity,payload,expected_revision=0);tx.commit()

def turn(records,identity,**changes):
    payload={'project_id':'alpha','intent':'ask','created_at':AT,'user_text':'项目预算应该怎么处理','receipt':{'ask':{'answer':'synthetic private answer','context':{'entries':[{'layer':'note','id':'doc-a','title':'Synthetic document'}]},'citations':[]}}}
    payload.update(changes);put(records,'v2_turns',identity,payload)

def test_reask_labeled_and_registered():
    from backend.memory_app.v2.policies import get,ACTIVE
    cases=json.loads((Path(__file__).parents[2]/'fixtures/signal_eval/reask.json').read_text(encoding='utf-8'))
    assert len(cases)>=30 and ACTIVE['reask']=='@1'
    result=[get('reask',version='@1')(x['first'],x['second'])['kind']==x['label'] for x in cases]
    assert sum(result)/len(result)>=.85
    validation=[result[i] for i,x in enumerate(cases) if x['split']=='validation']
    assert sum(validation)/len(validation)>=.85

def test_unused_and_missing_receipt_are_distinct(env):
    r,s=env;turn(r,'ask-a');turn(r,'ask-b',receipt={'ask':{'answer':'private'}})
    result=s.report(read_model_input=frozen_reader(r))
    assert result['unused']['objects']==[{'project_id':'alpha','layer':'note','object_id':'doc-a','sent':1,'cited':0}]
    assert result['unused']['unknown_turns']==1
    assert 'private' not in json.dumps(result)

def test_reask_scope_followup_and_window(env):
    r,s=env;turn(r,'ask-a');turn(r,'ask-b',created_at='2026-10-06T12:01:00+00:00',user_text='应该怎么处理项目预算')
    turn(r,'ask-c',created_at='2026-10-06T12:02:00+00:00',user_text='那具体步骤呢')
    turn(r,'ask-d',project_id='beta',created_at='2026-10-06T12:03:00+00:00')
    turn(r,'ask-e',created_at='2026-10-06T12:20:00+00:00')
    assert s.report()['reask']['pairs']==[{'project_id':'alpha','turn_ids':['ask-a','ask-b'],'score':pytest.approx(.888889)}]

def test_after_answer_copy_do_open(env):
    r,s=env;turn(r,'ask-a');turn(r,'do-a',intent='do',created_at='2026-10-06T12:03:00+00:00',receipt={})
    put(r,'v2_signals','copy-a',{'project_id':'alpha','kind':'copy','turn_id':'ask-a','at':AT,'by':'user','object':None})
    put(r,'v2_usage_document','doc-a',{'project_id':'alpha','events':[{'kind':'open','at':AT}],'older_count':7})
    result=s.report()['after_answer']
    assert result['turns'][0]['copy']==1 and result['turns'][0]['do']==1
    assert result['opens']==1 and result['unlocated_uses']==7

def test_dwell_missing_terminal_time_and_fade(env):
    r,s=env
    for identity in ['candidate-a','candidate-b']:
        put(r,'recognition_candidates',identity,{'scope':{'user_id':'local-user','project_id':'alpha'},'created_at':'2026-10-06T11:00:00+00:00','state':'pending','generation':{}})
    put(r,'v2_candidate_fade','candidate-a',{'faded_at':AT})
    put(r,'recognition_candidates','candidate-c',{'scope':{'user_id':'local-user','project_id':'alpha'}})
    put(r,'v2_candidate_merges','candidate-b',{'candidate_id':'candidate-c','project_id':'alpha','proposal_id':'proposal-a'})
    result=s.report()['dwell']['objects']
    assert result[0]['seconds']==3600 and result[0]['state']=='faded'
    assert result[1]['seconds'] is None and result[1]['state']=='merged'

def test_correction_unknown_version_and_denominator(env):
    r,s=env;turn(r,'ask-a')
    put(r,'v2_correction_events','edit-a',{'project_id':'alpha','object_kind':'recognition','object_id':'insight-a','type':'edit','at':AT,'before':'secret-before','after':'secret-after'})
    result=s.report()['corrections']
    assert result['unknown_events']==1 and result['groups']==[]
    assert 'secret-' not in json.dumps(result)

def test_document_edits_and_unknown_version(env):
    r,s=env;put(r,'documents','doc-a',{'project_id':'alpha'})
    put(r,'document_revisions','revision-a',{'document_id':'doc-a','revision':1,'author':'system','created_at':AT,'changed_blocks':[{'block_id':'b1','block':{'block_type':'paragraph','content':'synthetic AI'}}]})
    put(r,'document_revisions','revision-b',{'document_id':'doc-a','revision':2,'author':'user','created_at':AT,'changed_blocks':[{'block_id':'b1','block':{'block_type':'paragraph','content':'synthetic human'}}]})
    assert s.report()['document_edits']['objects']==[{'project_id':'alpha','object_id':'doc-a','revision':2,'paragraphs':1,'policy':None}]

def test_stop_and_missing_steers(env):
    r,s=env;turn(r,'ask-a')
    put(r,'v2_signals','stop-a',{'project_id':'alpha','kind':'stop','turn_id':'ask-a','at':AT,'by':'user','object':None})
    assert s.report()['interruptions']['turns']==[{'project_id':'alpha','turn_id':'ask-a','stop':1,'steer':None}]

def test_disabled_clear_cutoff_and_admin(env):
    r,s=env;turn(r,'ask-a')
    put(r,'v2_turns','admin-a',{'project_id':'alpha','intent':'ask','created_at':AT,'by':'admin','receipt':{}})
    s.clear(expected_revision=0)
    assert s.report()['unused']['objects']==[]
    turn(r,'new-a',created_at='2026-10-06T12:01:00+00:00')
    assert len(s.report(read_model_input=frozen_reader(r))['unused']['objects'])==1
    s.set_enabled(False,expected_revision=1)
    assert s.report()=={}


def test_policy_hard_negatives_and_boundary():
    from backend.memory_app.v2.policies import get
    check=get('reask',version='@1')
    assert check('项目预算应该怎么处理','设备配对应该怎么处理')['kind']=='new_topic'
    assert check('预算100元够吗','预算200元够吗')['kind']=='new_topic'
    assert check('项目预算怎么处理','项目预算怎么处理',elapsed_seconds=601)['kind']=='new_topic'
    assert check('项目预算怎么处理','项目预算怎么处理',elapsed_seconds=600)['kind']=='reask'

def test_clear_during_projection_rechecks_owner(env):
    r,s=env;turn(r,'ask-a')
    other=SignalService(SQLiteStructuredRecordStore(r.database_path),now=s.now)
    original=r.list
    fired=[]
    # Instrument the reader boundary only to schedule a real second-owner CAS,
    # never substitute the projection, transactions, or stored facts.
    def interleaved(collection):
        value=original(collection)
        if collection=='v2_turns' and not fired:
            fired.append(True);other.clear(expected_revision=0)
        return value
    r.list=interleaved
    assert s.report()=={}
    assert other.report()['unused']['objects']==[]

def test_actual_kernel_extract_version_model_and_dwell(env,tmp_path):
    from uuid import UUID
    from core.ai_kernel import SQLiteAITurnStore,SynchronousAIRuntime,ScopedCapabilityRegistry
    from core.ai_kernel.turn_kinds import freeze_turn_request
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    r,s=env
    identity='memory-00000000000040008000000000000001'
    store=SQLiteAITurnStore(tmp_path/'ai-turns.sqlite3')
    class Planner:
        def plan(self,request,events,capabilities,payloads,execution_control):
            control=execution_control
            ref=store.get_or_create_immutable_payload(identity,'memory-model-route-v1',{'provider':'synthetic','model':'safe-model','revision':1,'allow_remote':True,'base_url':'https://example.invalid/v1'})
            control.model_call_routed(snapshot_ref=ref,snapshot_revision='a'*64,prompt_cache_scope_identity='b'*64,provider='synthetic',model='safe-model',execution_location='remote',purpose='aux')
            control.model_call_started(provider='synthetic',model='safe-model')
            attempt=control.begin_model_wire_attempt()
            attempt.invoke_wire(lambda:attempt.succeeded(usage={'input_tokens':1,'output_tokens':1,'total_tokens':2},cache_observation=None))
            control.model_call_completed(usage={'input_tokens':1,'output_tokens':1,'total_tokens':2})
            return {'type':'complete','summary':'Synthetic completed'}
    request=freeze_turn_request('memory.propose_insights',turn_id=identity,session_id='synthetic-session',operation_id='synthetic-operation',idempotency_key=identity,project_id='alpha',created_at=AT,text='synthetic input',capabilities=[],privacy={'mode':'remote_allowed','allow_remote':True,'pii':'possible','consent_refs':['crp://default/model-settings/generation'],'retention':'session'})
    request['policy_versions']={'extract':'@3'}
    runtime=SynchronousAIRuntime(planner=Planner(),registry=ScopedCapabilityRegistry(),events=store,payloads=store,state=store)
    assert runtime.submit_turn(request).status=='completed'
    from backend.recognition import RecognitionService,WorkScope
    domain=RecognitionService(r);scope=WorkScope('local-user','alpha')
    source=domain.stage_experience(scope=scope,content='Synthetic source')
    candidate=domain.propose(scope=scope,content='Synthetic candidate',source_experience_ids=[source],
        generation={'id':str(UUID(identity.removeprefix('memory-'))),'step_version':'candidate-from-experiences-v1',
                    'model':'safe-model','configuration_revision':1,'completed_at':AT})
    domain.reject_candidate(scope=scope,candidate_id=candidate.id,expected_revision=candidate.revision,reviewer='local-user')
    groups=kernel_call_groups(tmp_path,remote_only=False,records=r)
    assert len(groups)==1
    report=s.report(kernel_groups=groups)
    assert report['dwell']['objects'][0]['policy']=='extract@3'
    assert report['dwell']['groups'][0]['known_durations']==1
    assert report['dwell']['groups'][0]['median_seconds']>=0
    assert report['corrections']['groups']==[
        {'dimension':'model','id':'safe-model','corrections':1,'outputs':1,'rate':1},
        {'dimension':'policy','id':'extract@3','corrections':1,'outputs':1,'rate':1}]

def test_correction_old_revision_cannot_adopt_current_generation(env):
    r,s=env
    put(r,'recognition_candidates','candidate-a',{'scope':{'user_id':'local-user','project_id':'alpha'},'created_at':AT,'state':'pending','generation':{'id':'00000000-0000-0000-0000-000000000001'}})
    put(r,'v2_correction_events','old-a',{'project_id':'alpha','object_kind':'candidate','object_id':'candidate-a','object_revision':0,'type':'edit','at':AT})
    groups=[{'turn_id':'memory-00000000000000000000000000000001','project_id':'alpha','request':{'policy_versions':{'extract':'@3'}},'calls':[{'model_id':'safe-model'}]}]
    report=s.report(kernel_groups=groups)
    assert report['corrections']['unknown_events']==1
    assert all(x['corrections']==0 for x in report['corrections']['groups'])

def test_recall_requires_actual_frozen_wire(env):
    r,s=env;turn(r,'ask-a')
    report=s.report()
    assert report['unused']['objects']==[]
    assert report['unused']['unknown_turns']==1


def frozen_reader(records):
    from core.ai_kernel import SQLiteAITurnStore
    store=SQLiteAITurnStore(records.database_path.parent/'ai-inputs.sqlite3')
    for row in records.list('v2_turns'):
        if row.payload.get('receipt',{}).get('ask',{}).get('context',{}).get('entries'):
            from core.ai_kernel.turn_kinds import freeze_turn_request
            request=freeze_turn_request('project.answer',turn_id=row.object_id,session_id='session-'+row.object_id,operation_id='operation-'+row.object_id,idempotency_key=row.object_id,project_id=row.payload['project_id'],created_at=row.payload['created_at'],text=row.payload['user_text'],capabilities=[],privacy={'mode':'local_only','allow_remote':False,'pii':'possible','consent_refs':[],'retention':'session'})
            store.claim_turn(request)
            store.get_or_create_immutable_payload(row.object_id,'answer-model-input-answer',{'messages':[{'role':'user','content':'资料：\n[1] Synthetic document\nSynthetic evidence\n\n问题：synthetic'}]})
    def read(identity):
        value=store.get_immutable_payload(identity,'answer-model-input-answer')
        return value[1] if value else None
    return read

from tests.memory_app.v2.test_workbench_ask import env as ask_env

def test_real_answer_wire_and_persona_projection(ask_env):
    from tests.memory_app.v2.test_workbench_ask import publish,ask
    primary,_=publish(ask_env)
    persona,_=publish(ask_env,text='alpha beta gamma my preferred tools are notebooks',project='me')
    ask_env.model.numbers=[1]
    response=ask(ask_env)
    assert response.status_code==200
    identity=response.json()['turn']['id']
    store=ask_env.http.app.state.ai_turn_store
    def frozen_input(turn_id):
        payload=store.get_immutable_payload(turn_id,'answer-model-input-answer')
        return payload[1] if payload else None
    report=SignalService(ask_env.records).report(read_model_input=frozen_input)
    entries={x['object_id']:x for x in report['unused']['objects']}
    assert entries[primary.id]['sent']==1 and entries[primary.id]['cited']==1
    assert entries[persona.id]['layer']=='persona' and entries[persona.id]['sent']==1
    assert entries[persona.id]['cited'] is None
    assert report['unused']['layers']['persona']['cited'] is None
    assert report['unused']['unknown_turns']==0
    assert ask_env.model.messages[-1]['content'] not in json.dumps(report)

def test_real_document_revision_organize_policy(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    add_document(ask_env)
    groups=kernel_call_groups(ask_env.root,remote_only=False,records=ask_env.records)
    assert any(g['request'].get('policy_versions',{}).get('organize')=='@1' for g in groups)
    revisions=[row for doc in ask_env.documents.list() for row in ask_env.documents.revisions(doc['id'])]
    assert any(row['author']=='system' for row in revisions)
    assert any(row['author']=='user' for row in revisions)
    result=SignalService(ask_env.records).report(kernel_groups=groups)['document_edits']['objects']
    assert result and all(row['policy']=='organize@1' for row in result), [(v['revision'],v['author'],v['created_at'],len(v['changed_blocks']),[list(c) for c in v['changed_blocks']]) for v in revisions]
    assert all(row['paragraphs']>0 and row['project_id']=='alpha' for row in result)
    correction_groups=SignalService(ask_env.records).report(kernel_groups=groups)['corrections']['groups']
    assert any(g['dimension']=='policy' and g['id']=='organize@1' and g['outputs']==1 for g in correction_groups)
    assert SignalService(ask_env.records).report(kernel_groups=groups)['document_edits']['groups']==[{'policy':'organize@1','revisions':len(result),'paragraphs':sum(row['paragraphs'] for row in result)}]


def test_admin_private_scope_and_unknown_citations(env):
    r,s=env
    put(r,'v2_private_scopes','alpha',{'private':True})
    turn(r,'private-a',receipt={'ask':{'answer':'Private answer','context':{'entries':[{'layer':'note','id':'doc-a','title':'Synthetic document'}]}}})
    turn(r,'admin-a',by='admin')
    result=s.report(read_model_input=frozen_reader(r))
    assert len(result['unused']['objects'])==1
    assert result['unused']['objects'][0]['cited'] is None
    assert result['unused']['layers']['note']['cited'] is None
    assert 'Private answer' not in json.dumps(result)

def test_domain_signal_writes_are_projected_without_changes(env):
    r,s=env;turn(r,'ask-a')
    prepared=s.prepare({'project_id':'alpha','kind':'copy','turn_id':'ask-a','client_id':'client-a'})
    s.record(prepared)
    before=r.list('v2_signals')
    assert s.report()['after_answer']['turns'][0]['copy']==1
    assert r.list('v2_signals')==before


def test_dwell_view_window_project_revision_and_published_precedence(env):
    r,s=env
    put(r,'recognition_candidates','candidate-a',{'scope':{'user_id':'local-user','project_id':'alpha'},'created_at':'2026-10-06T11:00:00+00:00','reviewed_at':AT,'state':'published','generation':{}})
    put(r,'v2_candidate_fade','candidate-a',{'faded_at':'2026-10-06T11:30:00+00:00'})
    for identity,project,at,revision in [('valid','alpha','2026-10-06T11:10:00+00:00',1),('before','alpha','2026-10-06T10:59:00+00:00',1),('after','alpha','2026-10-06T12:01:00+00:00',1),('other','beta','2026-10-06T11:10:00+00:00',1),('future','alpha','2026-10-06T11:10:00+00:00',2)]:
        put(r,'v2_signals',identity,{'project_id':project,'kind':'view','turn_id':None,'object':{'kind':'insight','id':'candidate-a','revision':revision},'at':at,'by':'user'})
    result=s.report()['dwell']['objects'][0]
    assert result['state']=='published' and result['seconds']==3600 and result['views']==1

def test_real_pending_merge_uses_original_reviewed_time(env):
    from backend.recognition import RecognitionService,WorkScope
    from backend.recognition.restructuring import RestructureProposalService
    r,s=env;domain=RecognitionService(r);scope=WorkScope('local-user','alpha')
    source=domain.stage_experience(scope=scope,content='Synthetic source')
    candidates=[domain.propose(scope=scope,content='Synthetic '+str(i),source_experience_ids=[source]) for i in range(2)]
    authority=RestructureProposalService(domain)
    snapshot=authority.capture(scope=scope,recognition_ids=[c.id for c in candidates],expected_revisions={c.id:c.revision for c in candidates},pending_output=True)
    proposal=authority.save(scope=scope,proposal_id='merge-a',snapshot=snapshot,operation='merge',outputs=[{'recognition_id':'merged-candidate','content':'Synthetic merged','conditions':[],'source_experience_ids':[source],'source_recognition_ids':[]}],reason='Synthetic merge')
    authority.review(scope=scope,proposal_id='merge-a',expected_revision=proposal['revision'],decision='approve',reviewer='local-user')
    results={x['object_id']:x for x in s.report()['dwell']['objects']}
    for candidate in candidates:
        assert results[candidate.id]['state']=='merged' and results[candidate.id]['seconds'] is not None

def test_real_unchanged_user_revision_does_not_count_paragraphs(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    add_document(ask_env)
    doc=ask_env.documents.list()[0]
    ask_env.documents.save_user_edit(doc['id'],expected_revision=doc['revision'],markdown=ask_env.documents.markdown(doc['id']))
    edits=SignalService(ask_env.records).report()['document_edits']['objects']
    assert edits and all(row['revision']!=doc['revision']+1 for row in edits)

def test_clear_does_not_count_unlocated_historical_uses(env):
    r,s=env
    put(r,'v2_usage_document','doc-a',{'project_id':'alpha','events':[{'kind':'open','at':AT}],'older_count':7})
    s.clear(expected_revision=0)
    result=s.report()['after_answer']
    assert result['opens']==0 and result['unlocated_uses'] is None

def test_mixed_typed_models_attribute_only_primary_output(env):
    from core.ai_kernel.contracts import validate_model_call_receipt
    r,s=env;turn(r,'ask-a')
    def call(identity,model,purpose):
        return validate_model_call_receipt({'schema_version':'1.0.0','receipt_id':'model-receipt-'+identity,'turn_id':'ask-a','model_request_id':identity,'status':'completed','requested_at':AT,'completed_at':AT,'duration_ms':1,'provider_id':'synthetic','model_id':model,'usage_status':'not_recorded','usage':None,'input_recorded':False,'output_recorded':False,'error_code':None,'model_call_purpose':purpose})
    groups=[{'turn_id':'ask-a','project_id':'alpha','kind':'project.answer','request':{'policy_versions':{'compose':'@3'}},'calls':[call('rewrite','fast-model','aux'),call('answer','main-model','primary')]}]
    put(r,'v2_correction_events','answer-miss',{'project_id':'alpha','type':'answer_miss','turn_id':'ask-a','at':AT})
    result=s.report(kernel_groups=groups)['corrections']['groups']
    assert [x for x in result if x['dimension']=='model']==[{'dimension':'model','id':'main-model','corrections':1,'outputs':1,'rate':1}]
    legacy={**groups[0],'calls':[{key:value for key,value in groups[0]['calls'][1].items() if key!='model_call_purpose'}]}
    result=s.report(kernel_groups=[legacy])['corrections']
    assert not any(x['dimension']=='model' for x in result['groups'])
    assert result['unknown_model_outputs']==1 and result['unknown_model_events']==1


def test_policy_exact_repeat_precedes_followup_cues():
    from backend.memory_app.v2.policies import get
    check=get('reask',version='@1')
    assert check('为什么合同审批这么慢？','为什么合同审批这么慢',elapsed_seconds=600)['kind']=='reask'
    assert check('Why is approval slow?', 'Ｗｈｙ is approval slow！')['kind']=='reask'
    assert check('为什么合同审批这么慢','为什么要增加这一审批步骤')['kind']=='followup'
    assert check('为什么合同审批这么慢','为什么合同审批这么慢',elapsed_seconds=601)['kind']=='new_topic'
    assert check('', '')['kind']=='new_topic'


def test_reask_report_obeys_selected_policy_version(env):
    from backend.memory_app.v2.policies import get, register, override, version
    r,s=env
    turn(r,'default-question-a')
    turn(r,'default-question-b',created_at='2026-10-06T12:01:00+00:00')
    assert version('reask')=='@1'
    assert get('reask') is get('reask',version='@1')
    assert s.report()['reask']['policy']=='reask@1'
    assert len(s.report()['reask']['pairs'])==1
    # An evaluation-local pure strategy exercises the registry contract;
    # the real read model and original stored facts remain in use.
    @register('reask','@9901')
    def comparison(first,second,*,elapsed_seconds=0):
        return {'kind':'new_topic','score':0.0}
    with override(reask='@9901'):
        assert s.report()['reask']=={'policy':'reask@9901','pairs':[]}
    assert s.report()['reask']['policy']=='reask@1'


def test_real_conflicting_ai_patch_does_not_change_user_edit_baseline(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    identity,_=add_document(ask_env)
    doc=ask_env.documents.read(identity)
    before=SignalService(ask_env.records).report()['document_edits']['objects']
    paragraph=next(block for block in doc['blocks'] if block['block_type']=='paragraph')
    conflicted=ask_env.documents.apply_ai_patch(identity,expected_revision=doc['revision'],
        blocks=[{**paragraph,'content':'Synthetic rejected AI proposal'}])
    assert conflicted['status']=='conflicted'
    assert ask_env.documents.markdown(identity)==ask_env.documents.markdown(identity,revision=doc['revision'])
    revisions=ask_env.documents.revisions(identity)
    assert revisions[-1]['conflict']['status']=='detected'
    saved=ask_env.documents.save_user_edit(identity,expected_revision=conflicted['revision'],markdown=ask_env.documents.markdown(identity))
    edits=SignalService(ask_env.records).report()['document_edits']['objects']
    assert edits==before
    assert sum(row['paragraphs'] for row in edits if row['revision']==saved['revision'])==0


def test_real_organize_steps_mixed_missing_policy_remains_unknown(ask_env):
    from copy import deepcopy
    from tests.memory_app.v2.test_workbench_ask import add_document
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    identity,item_id=add_document(ask_env)
    item=ask_env.records.read('workspace_items',item_id)
    ask_env.model.intake=True
    owner=OrganizeTurns(root=ask_env.root,records=ask_env.records,models=ask_env.model,
        item_id=item_id,project_id='alpha',source=item.payload['source_text'],validate_current=lambda:None)
    output,_=owner.complete([{'role':'user','content':'Synthetic second contributing stage'}],
        max_tokens=512,validate_current=lambda:None,stage='second-contribution')
    assert output
    groups=kernel_call_groups(ask_env.root,remote_only=False,records=ask_env.records)
    steps=[row for row in ask_env.records.list('workspace_organize_steps') if row.payload.get('output') and not row.payload.get('rejected')]
    identities={row.payload['turn_id'] for row in steps}
    contributing=[g for g in groups if g['turn_id'] in identities]
    assert len(contributing)>=2 and all(g['request']['policy_versions']['organize']=='@1' for g in contributing)
    # Detached original receipt projection models an older contribution with no
    # frozen version; no writer or read-model SUT is substituted.
    historical=deepcopy(groups)
    missing_id=contributing[-1]['turn_id']
    next(g for g in historical if g['turn_id']==missing_id)['request']['policy_versions'].pop('organize')
    result=SignalService(ask_env.records).report(kernel_groups=historical)['document_edits']
    assert result['objects'] and all(row['object_id']==identity and row['policy'] is None for row in result['objects'])
    assert result['groups']==[] and result['unknown_versions']==len(result['objects'])


def test_copied_incomplete_published_alias_cannot_adopt_known_generation(env):
    from backend.recognition import RecognitionService,WorkScope
    r,s=env;domain=RecognitionService(r);scope=WorkScope('local-user','alpha')
    source=domain.stage_experience(scope=scope,content='Synthetic source')
    candidate=domain.propose(scope=scope,content='Synthetic recognition',source_experience_ids=[source],
        generation={'id':'00000000-0000-4000-8000-000000000001','step_version':'candidate-from-experiences-v1',
                    'model':'safe-model','configuration_revision':1,'completed_at':AT})
    recognition=domain.publish(scope=scope,candidate_id=candidate.id,expected_revision=1,reviewer='local-user')
    assert any(row.payload['recognition_id']==recognition.id and row.payload['recognition_revision']==recognition.revision for row in r.list('recognition_versions'))
    put(r,'v2_correction_events','copied-correction',{'project_id':'alpha','object_kind':'recognition',
        'object_id':recognition.id,'object_revision':recognition.revision,'type':'edit','at':AT})
    groups=[{'turn_id':'memory-00000000000040008000000000000001','project_id':'alpha','kind':'memory.propose_insights',
        'request':{'policy_versions':{'extract':'@3'}},'calls':[]}]
    before=s.report(kernel_groups=groups)['corrections']
    assert before['unknown_events']==0 and any(row['id']=='extract@3' and row['corrections']==1 for row in before['groups'])
    # Incomplete copied/legacy fact, not a normal publish producer: the real
    # publisher cannot attach a second candidate to an existing recognition.
    alias={**r.read('recognition_candidates',candidate.id).payload,'id':'legacy-alias','generation':None}
    put(r,'recognition_candidates','legacy-alias',alias)
    result=s.report(kernel_groups=groups)['corrections']
    assert result['unknown_events']==1
    assert all(row['corrections']==0 for row in result['groups'])
