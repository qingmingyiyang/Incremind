"""版本对照与明确指定历史成果的真实产品合同。"""
import json
import pytest

from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT, UPDATED
from tests.memory_app.v2.test_outcome_continuation_boundaries import _is_main, _continue
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_workbench_do import env as do_env


def _provider_patch(values, body):
    respond = values.models.handler

    def external_response(messages, **options):
        if _is_main(messages):
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': body}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response


def test_actual_version_comparison_returns_frozen_previous_history_not_latest_body(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    _provider_patch(values, UPDATED)
    result = wait_product(values, _continue(values, original, previous))['receipt']['do']
    assert result['state'] == 'done', result
    document = result['document_id']
    values.documents.save_user_edit(previous, markdown=CURRENT + '\n\n交付后再次修改。', expected_revision=2)
    latest = values.records.read('documents', previous)
    history = values.records.read('document_markdown', previous + '~r2')
    response = values.client.get('/api/v2/library/outcomes/' + document + '/versions',
        params={'project_id': 'project-a'})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['previous'] == {'document_id': previous, 'revision': 2, 'markdown': CURRENT}
    assert [item['document_id'] for item in payload['items']] == [previous, document]
    assert payload['items'][1]['changes'] == result['changes']
    assert values.records.read('documents', previous) == latest
    assert values.records.read('document_markdown', previous + '~r2') == history
    assert values.client.get('/api/v2/library/outcomes/' + previous + '/versions',
        params={'project_id': 'project-a'}).json()['previous'] is None


def test_actual_explicit_old_outcome_remains_eligible_while_picker_is_latest_only(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    _provider_patch(values, '第二版实施正文。\n\n')
    second = wait_product(values, _continue(values, original, previous))['receipt']['do']
    assert second['state'] == 'done', second
    document = second['document_id']
    before = values.records.read('documents', document)
    assert values.client.get('/api/v2/library/outcomes', params={'project_id': 'project-a'}).json() == {
        'items': [{'document_id': document, 'title': '补充实施记录', 'version': 2}]}
    _provider_patch(values, '明确沿旧稿写的第三版。\n\n')
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    third = wait_product(values, response.json())['receipt']['do']
    assert third['state'] == 'done', third
    lineage = values.records.read('v2_outcome_lineage', third['document_id'])
    assert lineage.payload['root_id'] == previous and lineage.payload['previous_id'] == previous
    assert lineage.payload['version'] == 3 and lineage.payload['scene'] is None
    assert third['continues'] == {'document_id': previous, 'version': 3}
    assert values.records.read('documents', document) == before
    picker = values.client.get('/api/v2/library/outcomes', params={'project_id': 'project-a'}).json()
    assert [item['document_id'] for item in picker['items']] == [third['document_id']]
    assert values.documents.markdown(previous) == BIRTH


@pytest.mark.parametrize('second_version', [False, True])
def test_actual_active_redo_freezes_its_patch_baseline_and_keeps_shared_predecessor(scenario, second_version):
    from backend.memory_app.v2.policies import override
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    first = delivered['receipt']['do']['document_id']
    target, old = first, original
    if second_version:
        _provider_patch(values, '第二版实施正文。\n\n')
        old = _continue(values, original, first)
        second = wait_product(values, old)['receipt']['do']
        assert second['state'] == 'done', second
        target = second['document_id']
    previous_lineage = values.records.read('v2_outcome_lineage', target)
    current = values.documents.markdown(target) + '\n\n用户保存的追加段落。'
    values.documents.save_user_edit(target, markdown=current, expected_revision=1)
    document_before = values.records.read('documents', target)
    history_before = values.records.read('document_markdown', target + '~r2')
    sample = values.client.get('/api/v2/workbench/turns/' + old['turn']['id'] + '/division',
        params={'project_id': 'project-a'}).json()
    _provider_patch(values, '重做补丁正文。\n\n')
    dispatches = len(values.models.calls)
    with override(continuation='@2'):
        response = values.client.post('/api/v2/workbench/turns/' + old['turn']['id'] + '/redo', json={
            'project_id': 'project-a', 'expected_revision': sample['revision']})
    assert response.status_code == 200, response.text
    result = wait_product(values, response.json())['receipt']['do']
    assert result['state'] == 'done', result
    execution = values.records.read('v2_task_executions', response.json()['turn']['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    assert frozen['outcome_input']['continuation_policy'] == '@2'
    assert frozen['outcome_input']['document_id'] == target
    assert frozen['outcome_input']['document_revision'] == 2
    assert frozen['outcome_input']['current_markdown'] == current
    assert frozen['outcome_selection']['mode'] == 'redo'
    assert frozen['outcome_selection']['previous_id'] == previous_lineage.payload['previous_id']
    assert len(values.models.calls) - dispatches == 2
    assert result['model_usage'] == {'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12}
    assert result['model_cost'] is None
    assert result['fallback_new'] is False
    assert '用户保存的追加段落。' in values.documents.markdown(result['document_id'])
    lineage = values.records.read('v2_outcome_lineage', result['document_id'])
    assert lineage.payload['root_id'] == first
    assert lineage.payload['previous_id'] == previous_lineage.payload['previous_id']
    assert lineage.payload['version'] == previous_lineage.payload['version'] + 1
    assert result['continues'] == {'document_id': target, 'version': lineage.payload['version']}
    payload = values.client.get('/api/v2/library/outcomes/' + result['document_id'] + '/versions',
        params={'project_id': 'project-a'}).json()
    assert payload['previous'] == {'document_id': target, 'revision': 2, 'markdown': current}
    event, = [row for row in values.records.list('v2_outcome_corrections') if row.payload['kind'] == 'outcome_redo']
    assert event.payload['document_id'] == target and event.payload['from_revision'] == 2
    assert event.payload['new_document_id'] == result['document_id'] and event.payload['completed_at']
    assert values.records.read('documents', target) == document_before
    assert values.records.read('document_markdown', target + '~r2') == history_before
