"""默认成果选择及写法沿真实协调器生效，强制新写保留明确选择。"""
import json
from types import SimpleNamespace

from backend.memory_app.v2 import policies
from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT, UPDATED
from tests.memory_app.v2.test_outcome_redos import scenario, wait_product
from tests.memory_app.v2.test_outcome_style_context import publish
from tests.memory_app.v2.test_outcome_style_main import _capture
from tests.memory_app.v2.test_workbench_do import env as do_env


def _default_start(values, **choice):
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'intent':'do', 'text':'准备一份项目方案', **choice})
    assert response.status_code == 200, response.text
    data = response.json()
    receipt = wait_product(values, data)['receipt']['do']
    assert receipt['state'] == 'done', receipt
    request = values.records.read('v2_task_executions', data['turn']['id']).payload['request']
    return receipt, json.loads(request['input']['text'])


def test_default_first_auto_continuation_and_force_new_use_saved_writing(scenario):
    values = scenario
    writing = publish(SimpleNamespace(records=values.records,
        service=values.state.recognition_service), '开头先列出结论。', project='project-a')
    original_handler = values.models.handler
    values.summary = BIRTH
    first_mains = _capture(values)
    first, first_input = _default_start(values)
    assert first_input['outcome_input'] == {'continuation_policy':'@2', 'document_id':None}
    assert first_input['style_input']['version'] == '@1'
    assert first_input['style_input']['selected'] == [{'id':writing.id, 'revision':writing.revision}]
    assert len(first_mains) == 1 and any(message['role'] == 'system'
        and message['content'] == first_input['style_input']['text'] for message in first_mains[0])
    previous = first['document_id']
    values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    values.models.handler = original_handler
    second_mains = _capture(values, patch=True)
    second, second_input = _default_start(values)
    assert second_input['outcome_input']['continuation_policy'] == '@2'
    assert second_input['outcome_input']['current_markdown'] == CURRENT
    assert second['continues'] == {'document_id':previous, 'version':2}
    assert second_input['style_input'] == first_input['style_input']
    assert len(second_mains) == 1 and any(message['role'] == 'system'
        and message['content'] == first_input['style_input']['text'] for message in second_mains[0])
    assert values.documents.markdown(previous) == CURRENT
    assert values.documents.markdown(second['document_id']) == CURRENT.replace('用户亲改的完整段落。\n\n', UPDATED)
    lineage = values.records.read('v2_outcome_lineage', second['document_id'])
    assert lineage.payload['version'] == 2 and lineage.payload['previous_id'] == previous
    parts = {part['key']:part for part in second['context']['parts']}
    assert parts['style']['count'] == 1 and parts['previous']['count'] == 1
    values.models.handler = original_handler
    third_mains = _capture(values)
    third, third_input = _default_start(values, continue_from=None)
    assert third_input['outcome_input'] == {'continuation_policy':'@2', 'document_id':None}
    assert third['continues'] is None and len(third_mains) == 1
    assert third_input['style_input'] == first_input['style_input']
    new_lineage = values.records.read('v2_outcome_lineage', third['document_id'])
    assert new_lineage.payload['version'] == 1 and new_lineage.payload['previous_id'] is None
    assert new_lineage.payload['root_id'] == third['document_id']
    assert values.documents.markdown(previous) == CURRENT
    values.models.handler = original_handler
    redo_mains = _capture(values, patch=True)
    response = values.client.post('/api/v2/workbench/turns/' + lineage.payload['turn_id'] + '/redo', json={
        'project_id':'project-a', 'expected_revision':1})
    assert response.status_code == 200, response.text
    redo_data = response.json()
    redone = wait_product(values, redo_data)['receipt']['do']
    assert redone['state'] == 'done' and len(redo_mains) == 1
    redo_input = json.loads(values.records.read('v2_task_executions', redo_data['turn']['id']).payload['request']['input']['text'])
    assert redo_input['outcome_input']['continuation_policy'] == '@2'
    assert redo_input['style_input'] == first_input['style_input']
    assert redo_input['outcome_selection']['mode'] == 'redo'
    assert redo_input['outcome_selection']['document_id'] == second['document_id']
    redone_lineage = values.records.read('v2_outcome_lineage', redone['document_id'])
    assert redone_lineage.payload['root_id'] == previous
    assert redone_lineage.payload['version'] == 3 and redone_lineage.payload['previous_id'] == previous
    assert values.documents.markdown(previous) == CURRENT
    assert values.documents.markdown(second['document_id']) == CURRENT.replace('用户亲改的完整段落。\n\n', UPDATED)


def test_default_versions_preserve_every_original_selection_and_recipe():
    expected = {'retry':'@1', 'image_read':'@1', 'organize':'@1', 'place':'@2', 'extract':'@3',
        'route':'@1', 'scope':'@2', 'retrieve':'@3', 'rank':'@2', 'strength':'@1',
        'forget':'@1', 'enough':'@1', 'compose':'@3', 'trigger':'@2', 'consolidate':'@2',
        'handoff':'@1', 'reask':'@1', 'outcome_correction':'@1', 'review':'@1',
        'elsewhere':'@1', 'gap':'@1', 'search':'@1'}
    assert policies.ACTIVE == {**expected, 'continuation':'@2', 'style':'@1'}
    assert policies.get('continuation') is policies.get('continuation', version='@2')
    assert policies.get('style') is policies.get('style', version='@1')
