"""外部任务只冻结可信绑定引用，不把请求字段当作权限。"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel import AIKernelContractError, validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request
from tests.rebuild.test_product_turn_kinds import request


def external_request(**changes):
    values = dict(template_version=2, capability_request={'mode':'execute_exact_v1',
        'capability_id':'external.task.execute',
        'arguments':{'binding_ref':'crp://session/turn-' + 'a' * 32 + '/external-task-run-v1'}})
    values.update(changes)
    value = request('project.task', **values)
    value['policy_versions'] = {'handoff':'@1'}
    return value


def test_external_task_exact_template_has_no_automatic_context_and_is_schema_valid():
    value = external_request()
    assert value['execution_policy'] == {'template_version':2,'purpose':'primary',
        'budget':{'max_steps':1,'planner_timeout_ms':1_230_000}}
    assert value['capability_policy'] == {'allowed':['external.task.execute'],
        'denied':[],'require_approval':[]}
    assert value['context_policy'] == {'include_project_skill':False,'include_memory':False,
        'include_session_history':False,'max_context_bytes':262144}
    assert validate_turn_request(value) == value
    schema = json.loads((Path(__file__).resolve().parents[2] /
        'core-contracts/ai/turn-request.schema.json').read_text(encoding='utf-8'))
    assert Draft202012Validator(schema).is_valid(value)
    assert external_request() == value


@pytest.mark.parametrize('change', ['missing','auth','preset','bool','wrongcap','extra',
    'othernamespace','emptyref','notref','purpose','step','timeout','skill','memory','history',
    'narrowcap','approval','denied'])
def test_external_task_rejects_request_or_policy_expansion(change):
    value = external_request()
    if change == 'missing':
        del value['capability_request']
    elif change in {'auth','preset','bool','extra'}:
        value['capability_request']['arguments'][change] = True
    elif change == 'wrongcap':
        value['capability_request']['capability_id'] = 'external.context.execute'
    elif change in {'othernamespace','emptyref','notref'}:
        value['capability_request']['arguments']['binding_ref'] = {
            'othernamespace':'crp://other/binding','emptyref':'crp://default/','notref':True}[change]
    elif change == 'purpose':
        value['execution_policy']['purpose'] = 'aux'
    elif change in {'step','timeout'}:
        value['execution_policy']['budget'][{'step':'max_steps','timeout':'planner_timeout_ms'}[change]] += 1
    elif change in {'skill','memory','history'}:
        value['context_policy'][{'skill':'include_project_skill','memory':'include_memory',
            'history':'include_session_history'}[change]] = True
    elif change == 'narrowcap':
        value['capability_policy']['allowed'] = []
    else:
        value['capability_policy'][{'approval':'require_approval','denied':'denied'}[change]] = ['external.task.execute']
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)


def test_old_templates_and_detachment_remain_identical():
    old = request('project.task')
    answer = request('project.answer', template_version=2)
    assert old['execution_policy']['template_version'] == 1
    assert old['execution_policy']['budget'] == {'max_steps':64,'planner_timeout_ms':600_000}
    assert all(old['context_policy'][name] for name in
        ('include_project_skill','include_memory','include_session_history'))
    assert answer['execution_policy']['budget'] == {'max_steps':2,'planner_timeout_ms':120_000}
    before = copy.deepcopy((old, answer))
    external_request()
    assert (request('project.task'), request('project.answer', template_version=2)) == before


def test_binding_request_is_detached():
    ref = 'crp://session/turn-' + 'a' * 32 + '/external-task-run-v1'
    cap = {'mode':'execute_exact_v1','capability_id':'external.task.execute',
        'arguments':{'binding_ref':ref}}
    value = external_request(capability_request=cap)
    cap['arguments']['binding_ref'] = 'changed'
    assert value['capability_request']['arguments']['binding_ref'] == ref


@pytest.mark.parametrize('change', ['missing_execution','wrong_kind'])
def test_external_execution_cannot_enter_legacy_or_other_turns(change):
    value = external_request()
    if change == 'missing_execution':
        del value['execution_policy']
    else:
        value['desired_outcome'] = 'project.answer'
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)


@pytest.mark.parametrize('ref', ['crp://default/binding',
    'crp://session/turn-' + 'b' * 32 + '/external-task-run-v1',
    'crp://session/turn-' + 'a' * 32 + '/other-kind'])
def test_external_task_binding_matches_actual_turn_and_archive_kind(ref):
    with pytest.raises(AIKernelContractError):
        external_request(capability_request={'mode':'execute_exact_v1',
            'capability_id':'external.task.execute','arguments':{'binding_ref':ref}})
