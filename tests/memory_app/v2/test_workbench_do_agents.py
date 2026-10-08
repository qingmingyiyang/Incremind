from types import SimpleNamespace
from threading import RLock
import time
import pytest
from tests.memory_app.v2.test_workbench_do import env

class Organization:
    def __init__(self):
        self.calls = []
        self.state = 'running'
        self._read_lock = RLock()
    def start(self, request, *, agent_turn_mode=False):
        from core.ai_kernel.contracts import validate_turn_request
        validate_turn_request(request)
        if hasattr(self,'turns'):
            self.turns.claim_turn(request)
            if not self.turns.events_after(request['turn_id']):
                self.turns.append(self.event(request,'turn.accepted',1),expected_sequence=0)
        self.calls.append(request)
        assert agent_turn_mode
        return {'main': {'turn_id': request['turn_id']}}
    @staticmethod
    def event(request,kind,sequence):
        return {'schema_version':'1.0.0','event_id':f"event-{request['turn_id']}-{sequence}",
            'turn_id':request['turn_id'],'session_id':request['session_id'],'sequence':sequence,
            'type':kind,'actor':'system','correlation':{'operation_id':request['operation_id']},
            'data':{'summary':'比较证据后采用方案甲','status':'completed' if kind=='turn.completed' else 'accepted'},
            'occurred_at':request['created_at']}
    def read(self, identity, project):
        with self._read_lock:
            if self.state=='completed' and hasattr(self,'turns'):
                events=self.turns.events_after(identity)
                if not events or events[-1]['type']!='turn.completed':
                    self.turns.append(self.event(self.turns.get_request(identity),'turn.completed',len(events)+1),
                        expected_sequence=len(events))
        return {'status': self.state, 'summary': '比较证据后采用方案甲'}
    def topology(self, identity, project):
        return [{'role': '研究员', 'state': 'done' if self.state == 'completed' else 'running'}]

def install(client, org, workflow=None):
    from backend.memory_app.v2.workbench import install_workbench_routes
    app = client.app
    from core.ai_kernel import SQLiteAITurnStore
    from backend.memory_app.original_sources import source_store
    org.turns=SQLiteAITurnStore(source_store(app.state.recognition_records).root / 'ai-turns.sqlite3')
    app.router.routes[:] = [r for r in app.router.routes if not (getattr(r, 'path', '').startswith('/api/v2/workbench') or getattr(getattr(r, 'original_router', None), 'prefix', '') == '/api/v2/workbench')]
    install_workbench_routes(app, records=app.state.recognition_records, models=app.state.recognition_models,
        documents=app.state.recognition_documents, service=app.state.recognition_service,
        workspace=app.state.workspace_domains,
        organization=org, research_reader=org.read, topology_reader=org.topology)



















def test_lazy_runtime_projection_uses_existing_dispatcher_and_scope():
    from backend.memory_app.v2 import _LazyOrganization
    from tests.memory_app.v2.research_fixture import research_request
    request = research_request('turn-a', 'project-a', '研究')
    org = Organization()
    state = SimpleNamespace(container=object())
    calls = []
    profiles = {'steward.scheduler':SimpleNamespace(organization_role='管家'),
                'expert-a':SimpleNamespace(organization_role='研究员')}
    def build():
        calls.append('build')
        state.agent_organization_runtime = org
        state.ai_turn_store = SimpleNamespace(get_request=lambda identity: request)
        state.ai_runtime = SimpleNamespace(receipt_for=lambda identity: SimpleNamespace(status='completed', current_sequence=2),
            events_after=lambda identity: [{'type':'turn.completed','sequence':2,'data':{'summary':'真实终态摘要'}}])
        state.agent_runtime_composition = SimpleNamespace(request_loader=lambda identity:request,
            profiles=profiles, coordinator=SimpleNamespace(list=lambda **kw: {'children':[
                {'profile_id':'steward.scheduler','status':'completed'},
                {'profile_id':'expert-a','status':'completed'}]}))
    state.recognition_turn_dispatcher = SimpleNamespace(_runtime=build)
    lazy = _LazyOrganization(SimpleNamespace(state=state))
    assert lazy.available()
    assert calls == ['build']
    lazy.start(request, agent_turn_mode=True)
    assert org.calls == []
    assert lazy.read('research-turn-a','project-a') == {'status':'completed','summary':'真实终态摘要'}
    assert lazy.topology('research-turn-a','project-a') == [{'role':'研究员','state':'done'}]
    with pytest.raises(RuntimeError, match='research_scope_changed'):
        lazy.read('research-turn-a','other')


def test_brief_validation_preserves_packet_and_order(monkeypatch):
    from datetime import datetime, timezone
    import backend.memory_app.context_adapter as adapter

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(adapter, 'datetime', FixedDateTime)
    from backend.memory_app.context_adapter import compile_selected, ContextSelectionError
    plain = compile_selected('project-a', [], [], '写总结', 1)
    researched = compile_selected('project-a', [], [], '写总结', 1, expert_brief='比较证据')
    assert {k:v for k,v in researched.items() if k not in {'messages', 'graph', 'token_count'}} == {k:v for k,v in plain.items() if k not in {'messages', 'graph', 'token_count'}}
    assert {k:v for k,v in researched['graph'].items() if k != 'created_at'} == {k:v for k,v in plain['graph'].items() if k != 'created_at'}
    assert researched['token_count'] > plain['token_count']
    assert researched['messages'][0] == plain['messages'][0]
    assert researched['messages'][-1] == plain['messages'][-1]
    assert researched['messages'][1]['content'] == '以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n比较证据'
    with pytest.raises(ContextSelectionError, match='expert_brief is invalid'):
        compile_selected('project-a', [], [], '写总结', 1, expert_brief='x' * 6001)
