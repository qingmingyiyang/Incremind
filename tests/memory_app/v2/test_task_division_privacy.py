"""The real coordinator inherits only filtered task material into a child."""
import json

from tests.memory_app.v2.test_turn_requests import materials, freeze
from tests.backend.unit.api.test_agent_coordinator import _durable_coordinator


def test_private_material_never_enters_division_child_input(materials, tmp_path):
    records, models, descriptors = materials
    request, loaded = freeze(records, models, descriptors, 'project.task',
        text='只按给定公开资料执行。', capabilities=['agent.spawn', 'memory.recall', 'source.evidence.read'])
    assert loaded == ['public-item']
    turns, _, _, _, _, coordinator = _durable_coordinator(tmp_path)
    parent = coordinator.accept_and_register_main(request)
    child = coordinator.prepare_child(parent_turn_id=parent.run.turn_id,
        operation_id='op-private-division-child-0001', tool_call_id='tool-private-division-child-0001',
        project_id='alpha', scope=request['scope'], privacy=request['privacy'],
        arguments={'profile_id':'subagent.worker', 'task':request['input']['text']})
    stored = turns.get_request(child.run.turn_id)
    assert stored['desired_outcome'] == 'project.task'
    assert 'public-item-body' in stored['input']['text']
    assert 'private-item-body' not in json.dumps(stored)
    assert stored['privacy']['material_refs'] == request['privacy']['material_refs']
    assert set(child.run.capability_ids) <= set(request['capability_policy']['allowed'])


def test_agent_tool_provider_preserves_full_frozen_product_privacy(materials,tmp_path):
    from backend.api.agent_capabilities import AgentCapabilityProvider
    records,models,descriptors=materials
    request,_=freeze(records,models,descriptors,'project.task',text='读取分工',capabilities=['agent.list'])
    _,_,_,_,_,coordinator=_durable_coordinator(tmp_path)
    parent=coordinator.accept_and_register_main(request)
    provider=AgentCapabilityProvider(coordinator=coordinator,capability_id='agent.list')
    result=provider.invoke({'turn_id':parent.run.turn_id,'operation_id':'op-list-product-0001',
        'tool_call_id':'tool-list-product-0001','capability_id':'agent.list',
        'scope':request['scope'],'privacy':request['privacy'],'arguments':{'include_messages':False}})
    assert result['result']['children'] == []
