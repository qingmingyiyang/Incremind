import pytest
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope, RecognitionConflict


def privacy(env, identity):
    response = env.http.get(f'/api/v2/library/sources/{identity}/privacy', params={'project_id':'alpha'})
    assert response.status_code == 200, response.text
    return response.json()


def toggle(env, identity, state):
    body = {k:state[k] for k in ('source_revision','policy_revision')}
    return env.http.put(f'/api/v2/library/sources/{identity}/privacy', json={
        'project_id':'alpha', **body, 'allowed_purposes':[]})


def test_original_privacy_dual_revision_settings_and_inheritance(env):
    doc, item = add_document(env)
    insight, experience = publish(env, doc=doc)
    before = env.records.read('workspace_items', item)
    state = privacy(env,item)
    response = toggle(env,item,state)
    assert response.status_code == 200, response.text
    assert env.records.read('workspace_items',item) == before
    assert toggle(env,item,state).status_code == 409
    authority = SourceEgressService(env.records)
    snap = authority.snapshot(WorkScope('local-user','alpha'), [{'type':'recognition','id':insight.id,'revision':1}])
    with pytest.raises(RecognitionConflict): authority.require(snap,'generation')
    assert any(n['type']=='original_item' and n['id']==item for n in snap['nodes'])
    settings = env.http.get('/api/v2/settings/private-sources').json()
    assert any(r['source_id']==item for r in settings)


def test_original_direct_query_private_and_preview_revocation(env):
    doc,item = add_document(env)
    plan = env.domains.query.prepare_ask('alpha','alpha beta gamma?')
    assert plan['chosen']
    assert toggle(env,item,privacy(env,item)).status_code == 200
    with pytest.raises(RecognitionConflict): env.domains.query.validate_ask_plan(plan)
    assert env.domains.query.prepare_ask('alpha','alpha beta gamma?')['chosen'] == []


def test_json_original_privacy_survives_edit_and_recreation_is_rejected(env):
    store = env.domains.query.source_store
    body = {'id':'legacy-source','project_id':'alpha','title':'Legacy','type':'text', 'metadata':{'content_snapshot':'alpha'}}
    store.write('sources','legacy-source',body,expected_revision=0)
    assert toggle(env,'legacy-source',privacy(env,'legacy-source')).status_code == 200
    store.write('sources','legacy-source',{**body,'title':'Edited'},expected_revision=1)
    assert privacy(env,'legacy-source')['allowed_purposes'] == []
    store.delete('sources','legacy-source')
    store.write('sources','legacy-source',body,expected_revision=0)
    assert env.http.get('/api/v2/library/sources/legacy-source/privacy',params={'project_id':'alpha'}).status_code == 409
from types import SimpleNamespace
from threading import Event, Thread
from backend.memory_app.research_sources import ReadProvider, ReadControl, read_dependencies
from backend.memory_app.original_sources import resolve, original, document_roots
from backend.memory_app.research_packets import capture_research_packet, validate_research_packet
from core.ai_kernel import SQLiteAITurnStore


def original_source(env, identity='material'):
    env.domains.query.source_store.write('sources',identity, {'id':identity,'project_id':'alpha',
        'title':identity,'type':'text','metadata':{'content_snapshot':'alpha beta'}}, expected_revision=0)
    return identity


def read_request(identity='read-one'):
    return {'turn_id':'turn-research','tool_call_id':identity,'scope':{'project_id':'alpha'},
            'privacy':{'allow_remote':True}}


class SourceProvider:
    def __init__(self, env, identity, before=lambda:None): self.env,self.identity,self.before=env,identity,before
    def invoke(self, request):
        revision=self.env.domains.query.source_store.revision('sources',self.identity)
        self.before()
        return {'result':{'source_id':self.identity,'source_revision':revision,'source_refs':[],'summary':'alpha'}}


class Turns:
    def __init__(self, capability='source.evidence.read'):
        self.capability=capability
    def get_request(self, identity): return read_request()
    def events_after(self,identity,after_sequence=0):
        return [{'type':'tool.completed','data':{'capability_id':self.capability},
            'correlation':{'tool_call_id':getattr(self,'tool_id','read-one')}}]


def test_research_read_then_privacy_flip_stops_actual_wire(env):
    identity=original_source(env)
    ReadProvider(SourceProvider(env,identity),'source.evidence.read',env.records).invoke(read_request())
    sent=[]
    inner=SimpleNamespace(begin_model_wire_attempt=lambda **kwargs:sent.append(kwargs),checkpoint=lambda:None)
    control=ReadControl(inner,env.records,Turns(),None,read_request())
    control.begin_model_wire_attempt(model_request_id='first')
    assert len(sent)==1
    assert toggle(env,identity,privacy(env,identity)).status_code == 200
    with pytest.raises(RecognitionConflict): control.begin_model_wire_attempt(model_request_id='retry')
    assert len(sent)==1


def test_research_source_revision_changed_during_read_rejected(env):
    identity=original_source(env)
    def change():
        store=env.domains.query.source_store
        body=store.read('sources',identity)
        store.write('sources',identity,{**body,'title':'Changed'},expected_revision=1)
    with pytest.raises(RecognitionConflict):
        ReadProvider(SourceProvider(env,identity,change),'source.evidence.read',env.records).invoke(read_request())
    assert env.records.list('v2_research_source_reads') == ()


def test_private_json_cannot_be_masked_by_public_document_same_id(env):
    identity=original_source(env)
    doc,_=add_document(env)
    with env.records.begin() as tx:
        existing=tx.read('documents',doc)
        tx.put('documents',identity,{**existing.payload,'id':identity},expected_revision=0)
        tx.commit()
    assert toggle(env,identity,privacy(env,identity)).status_code==200
    with pytest.raises(RecognitionConflict):
        ReadProvider(SourceProvider(env,identity),'source.evidence.read',env.records).invoke(read_request())


def test_confirmed_json_and_workspace_alias_share_privacy_keep_material_versions(env):
    doc,item=add_document(env)
    row=env.records.read('workspace_items',item)
    identity=row.payload['source_id']
    with env.records.begin() as reader:
        assert resolve(reader,WorkScope('local-user','alpha'),identity)==('original_item',item)
        assert resolve(reader,WorkScope('local-user','alpha'),identity,kind='source')==('original_source',identity)
    snapshot=SourceEgressService(env.records).snapshot(WorkScope('local-user','alpha'),[
        {'type':'original_source','id':identity,'revision':1}])
    assert {n['type'] for n in snapshot['nodes']}=={'original_source','original_item'}
    assert toggle(env,item,privacy(env,item)).status_code==200
    private=SourceEgressService(env.records).snapshot(WorkScope('local-user','alpha'),snapshot['roots'])
    with pytest.raises(RecognitionConflict): SourceEgressService(env.records).require(private,'generation')


def test_unproven_research_brief_cannot_egress(env):
    with pytest.raises(RecognitionConflict):
        validate_research_packet(env.records,WorkScope('local-user','alpha'),{'expert_brief':'unbound'},authority=SourceEgressService(env.records),remote=True)


@pytest.mark.parametrize('source_field,policy_field',[(99,0),(1,99)])
def test_original_both_revision_conflicts(env,source_field,policy_field):
    identity=original_source(env)
    response=env.http.put(f'/api/v2/library/sources/{identity}/privacy',json={
        'project_id':'alpha','source_revision':source_field,'policy_revision':policy_field,'allowed_purposes':[]})
    assert response.status_code==409
    assert privacy(env,identity)['policy_revision']==0


def test_private_project_original_inherited_cannot_cancel(env):
    from backend.memory_app.v2.privacy import set_private_project
    identity=original_source(env)
    set_private_project(env.records,'alpha',True,expected_revision=0)
    state=privacy(env,identity)
    assert state['inherited'] and state['allowed_purposes']==[]
    response=env.http.put(f'/api/v2/library/sources/{identity}/privacy',json={
        'project_id':'alpha','source_revision':1,'policy_revision':0,
        'allowed_purposes':['generation','embedding','rerank']})
    assert response.status_code==409


def test_source_json_cas_is_serialized_across_threads(env):
    identity=original_source(env)
    store=env.domains.query.source_store
    gate=Event();done=Event()
    def write():
        gate.set()
        store.write('sources',identity,{**store.read('sources',identity),'title':'Changed'},expected_revision=1)
        done.set()
    with store.locked('sources',identity):
        thread=Thread(target=write);thread.start()
        assert gate.wait(2)
        assert not done.wait(.1)
        assert store.revision('sources',identity)==1
    thread.join(3)
    assert done.is_set() and store.revision('sources',identity)==2

def completed_research(env, identity):
    from tests.memory_app.v2.research_fixture import research_request
    from tests.memory_app.v2.test_workbench_do_agents import Organization
    request=research_request('thread','alpha','Compare')
    store=SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    store.claim_turn(request)
    store.append(Organization.event(request,'turn.accepted',1),expected_sequence=0)
    provider_request={**read_request(),'turn_id':request['turn_id']}
    ReadProvider(SourceProvider(env,identity),'source.evidence.read',env.records).invoke(provider_request)
    event=Organization.event(request,'tool.completed',2)
    event['data']={'capability_id':'source.evidence.read','summary':'read'}
    event['correlation']['tool_call_id']='read-one'
    store.append(event,expected_sequence=1)
    store.append(Organization.event(request,'turn.completed',3),expected_sequence=2)
    return request


def test_durable_research_packet_blocks_revoked_original_and_forged_brief(env):
    identity=original_source(env)
    request=completed_research(env,identity)
    scope=WorkScope('local-user','alpha')
    bound=capture_research_packet(env.records,scope,request['turn_id'],'比较证据后采用方案甲',authority=SourceEgressService(env.records))
    packet={'messages':[{'role':'user','content':'以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n比较证据后采用方案甲'}],
            'research_sources':bound}
    assert validate_research_packet(env.records,scope,packet,authority=SourceEgressService(env.records),remote=True)
    with pytest.raises(RecognitionConflict):
        capture_research_packet(env.records,scope,request['turn_id'],'Forged',authority=SourceEgressService(env.records))
    assert toggle(env,identity,privacy(env,identity)).status_code==200
    with pytest.raises(RecognitionConflict): validate_research_packet(env.records,scope,packet,authority=SourceEgressService(env.records),remote=True)
    roots=validate_research_packet(env.records,scope,packet,authority=SourceEgressService(env.records),inherit=True)
    assert roots==(('original_source',identity,1),)


def test_research_document_edit_after_read_blocks_wire(env):
    doc,_=add_document(env)
    class Provider:
        def invoke(self, request):
            return {'result':{'model_input':{'untrusted_project_evidence':{'items':[
                {'kind':'document','object_id':doc,'source_refs':[]}]}},'kind':'project_skill.evidence'}}
    request=read_request()
    ReadProvider(Provider(),'project_skill.evidence.read',env.records).invoke(request)
    called=[]
    control=ReadControl(SimpleNamespace(begin_model_wire_attempt=lambda:called.append(1)),
        env.records,Turns('project_skill.evidence.read'),None,request)
    env.documents.save_user_edit(doc,expected_revision=2,markdown='Changed')
    with pytest.raises(RecognitionConflict): control.begin_model_wire_attempt()
    assert called==[]


def test_json_confirmed_alias_recreation_invalidates_snapshot(env):
    doc,item=add_document(env)
    identity=env.records.read('workspace_items',item).payload['source_id']
    authority=SourceEgressService(env.records);scope=WorkScope('local-user','alpha')
    snap=authority.snapshot(scope,[{'type':'original_source','id':identity,'revision':1}])
    store=env.domains.query.source_store
    from backend.memory_app.original_sources import source_store
    body=source_store(env.records).read('sources',identity)
    store.delete('sources',identity)
    store.write('sources',identity,body,expected_revision=0)
    with pytest.raises(RecognitionConflict): authority.validate_snapshot(scope,snap)


def test_research_skill_rule_sources_cannot_be_masked_by_empty_configuration(env):
    identity=original_source(env)
    assert toggle(env,identity,privacy(env,identity)).status_code==200
    class Provider:
        def invoke(self, request):
            return {'result':{'model_input':{
                'untrusted_project_evidence':{'items':[{'kind':'current_project_skill','object_id':'skill-alpha','source_refs':[]}]},
                'current_project_skill':{'output_rules':[{'rule':'use material','source_refs':[{'source_id':identity,'locator':'text'}]}]}}}}
    with pytest.raises(RecognitionConflict):
        ReadProvider(Provider(),'project_skill.evidence.read',env.records).invoke(read_request())

@pytest.mark.parametrize('at_wire',[False,True])
def test_original_private_intake_never_sends_text(tmp_path,at_wire):
    from tests.memory_app.v2.test_intake_authorization import RemoteModel,text_item,client
    model=RemoteModel();http,records=client(tmp_path,model)
    item=text_item(http)
    def private():
        row=records.read('workspace_items',item['id'])
        SourceEgressService(records).set_policy(WorkScope('local-user','alpha'),'original_item',item['id'],row.revision,0,[])
    if at_wire: model.before_wire=private
    else: private()
    response=http.post(f"/api/workspace/v1/items/{item['id']}/process",json={'project_id':'alpha'})
    assert model.wire_calls==0
    assert response.json()['error']=='private_source_remote_blocked'

def test_retained_research_artifact_inherits_original_and_wire_uses_nested_adapter(env):
    from tests.memory_app.test_artifact_egress import _retained_artifact
    identity=original_source(env)
    request=completed_research(env,identity)
    scope=WorkScope('local-user','alpha')
    bound=capture_research_packet(env.records,scope,request['turn_id'],'比较证据后采用方案甲',authority=SourceEgressService(env.records))
    items=_retained_artifact(env.records,env.service,scope,source=False)
    with env.records.begin() as tx:
        packet=tx.read('recognition_context_packets','packet')
        saved=tx.put('recognition_context_packets','packet',{**packet.payload,
            'research_sources':bound,'messages':[{'role':'user','content':
                '以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n比较证据后采用方案甲'}]},expected_revision=packet.revision)
        artifact=tx.read('recognition_experiences',items['artifact'])
        provenance={**artifact.payload['provenance'],'source_refs':[
            {**ref,'revision':saved.revision} if ref['type']=='context_packet' else ref
            for ref in artifact.payload['provenance']['source_refs']]}
        tx.put('recognition_experiences',artifact.object_id,{**artifact.payload,'provenance':provenance},expected_revision=artifact.revision)
        tx.commit()
    class Provider:
        def invoke(self, request):
            return {'result':[{'source_refs':['crp://default/recognition_experiences/artifact'],'content':'retained'}]}
    # The new model turn reads the retained artifact through the actual service
    # snapshot; no egress or dependency service is mocked.
    request=read_request('read-two')
    ReadProvider(Provider(),'memory.recall',env.records).invoke(request)
    sent=[]
    turns=Turns('memory.recall');turns.tool_id='read-two'
    control=ReadControl(SimpleNamespace(begin_model_wire_attempt=lambda:sent.append(1)),
        env.records,turns,None,request)
    control.begin_model_wire_attempt()
    assert sent==[1]
    assert toggle(env,identity,privacy(env,identity)).status_code==200
    with pytest.raises(RecognitionConflict):control.begin_model_wire_attempt()
    snapshot=SourceEgressService(env.records).snapshot(scope,[{'type':'experience','id':'artifact','revision':2}])
    with pytest.raises(RecognitionConflict):SourceEgressService(env.records).require(snapshot,'generation')
    assert sent==[1]

def test_typed_document_rule_cannot_be_masked_by_json_source_with_same_id(env):
    doc,item=add_document(env)
    original_source(env,doc)
    assert toggle(env,item,privacy(env,item)).status_code==200
    class Provider:
        def invoke(self, request):
            return {'result':{'model_input':{'untrusted_project_evidence':{'items':[]},
                'current_project_skill':{'output_rules':[{'rule':'use document',
                    'source_refs':[{'source_id':doc,'locator':'document:summary'}]}]}}}}
    with pytest.raises(RecognitionConflict):
        ReadProvider(Provider(),'project_skill.evidence.read',env.records).invoke(read_request())


@pytest.mark.parametrize("namespace,database", [("default", "records.sqlite3"), ("recognition", "recognition.sqlite3")])
def test_original_namespace_and_transaction_privacy_are_isolated(tmp_path, namespace, database):
    from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
    from backend.memory_app.transaction_records import TransactionRecords
    records = SQLiteStructuredRecordStore(tmp_path / database)
    for name in ("default", "recognition"):
        JsonObjectStore(tmp_path / ".rebuild-data", namespace_id=name).write(
            "sources", "same-source", {"id":"same-source", "project_id":"alpha",
            "title":name, "metadata":{"content_snapshot":name}}, expected_revision=0)
    scope = WorkScope("local-user", "alpha")
    with records.begin() as tx:
        row = original(tx, scope, "original_source", "same-source")
        assert row.payload["title"] == namespace
        assert original(TransactionRecords(tx), scope, "original_source", "same-source").payload == row.payload
    authority = SourceEgressService(records)
    saved = authority.set_policy(scope, "original_source", "same-source", 1, 0, [])
    assert saved["allowed_purposes"] == []
    snapshot = authority.snapshot(scope, [{"type":"original_source", "id":"same-source", "revision":1}])
    with pytest.raises(RecognitionConflict):
        authority.require(snapshot, "generation")
    other = "recognition" if namespace == "default" else "default"
    JsonObjectStore(tmp_path / ".rebuild-data", namespace_id=other).write(
        "sources", "same-source", {"id":"same-source", "project_id":"alpha", "title":"other edited"}, expected_revision=1)
    with records.begin() as tx:
        assert original(tx, scope, "original_source", "same-source").payload["title"] == namespace
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(scope, [{"type":"original_source", "id":"same-source", "revision":1}]), "generation")


def test_workspace_segment_references_keep_the_exact_original_identity(env):
    doc, item = add_document(env)
    env.domains.query.source_store.write("sources", item,
        {"id":item, "project_id":"alpha", "title":"Independent source"}, expected_revision=0)
    scope = WorkScope("local-user", "alpha")
    refs = [{"source_id":item, "locator":"workspace://" + item},
            {"source_id":item, "locator":"text:0:2"}]
    with env.records.begin() as tx:
        roots = document_roots(tx, scope, refs)
        assert roots == (("original_item", item, tx.read("workspace_items", item).revision),)
        explicit = document_roots(tx, scope, refs + [{"source_id":item, "locator":"source://" + item}])
        assert {root[0] for root in explicit} == {"original_item", "original_source"}

        crp = document_roots(tx, scope, refs + [{"source_id":item, "locator":"crp://default/sources/" + item}])
        assert {root[0] for root in crp} == {"original_item", "original_source"}
    authority = SourceEgressService(env.records)
    authority.set_policy(scope, "original_source", item, 1, 0, [])
    roots = [{"type":"original_item", "id":item, "revision":env.records.read("workspace_items",item).revision},
             {"type":"original_source", "id":item, "revision":1}]
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(scope, roots), "generation")
    authority.require(authority.snapshot(scope, roots[:1]), "generation")
