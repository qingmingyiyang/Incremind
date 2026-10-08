"""已确认写法沿真实任务冻结、Main 外发和上下文读模型接线。"""
import json
from types import SimpleNamespace

import pytest

from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope
from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT, UPDATED
from tests.memory_app.v2.test_outcome_continuation_boundaries import _is_main
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_outcome_style_context import publish
from tests.memory_app.v2.test_workbench_do import env as do_env


def _styles(values):
    env = SimpleNamespace(records=values.records, service=values.state.recognition_service)
    own = publish(env, '段落要简洁。', project='project-a')
    local = publish(env, '标题采用问题式。', project='project-a', scene='访谈')
    conditional = publish(env, '适用于阶段成果。', project='project-a', conditions=['开头先列出结论。'])
    mine = publish(env, '列表使用动词开头。', project='me')
    publish(env, '表格使用三列。', project='project-a', scene='培训')
    publish(env, '标题要简短。', project='other')
    publish(env, '合成事实：本周采购预算为三百元。', project='project-a')
    private = publish(env, '用词采用合成私密规则。', project='project-a')
    source = private.source_experience_ids[0]
    SourceEgressService(values.records).set_policy(WorkScope('local-user', 'project-a'),
        'experience', source, values.records.read('recognition_experiences', source).revision, 0, [])
    forgotten = publish(env, '篇幅要控制在一页。', project='project-a')
    set_preference(values.records, WorkScope('local-user', 'project-a'), forgotten.id,
        recognition_revision=forgotten.revision, preference_revision=0, state='forgotten')
    pending_source = env.service.stage_experience(scope=WorkScope('local-user', 'project-a'), content='合成未确认来源')
    env.service.propose(scope=WorkScope('local-user', 'project-a'), content='语气要口语。',
        source_experience_ids=[pending_source])
    response = values.client.get('/api/v2/projects')
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()['items'] if row['id'] == 'project-a')
    response = values.client.patch('/api/v2/projects/project-a', json={
        'scenes': ['访谈', '培训'], 'expected_revision': row['revision']})
    assert response.status_code == 200, response.text
    return (own, local, conditional, mine)


def _start(values, *, previous=None, thread=None):
    body = {'project_id': 'project-a', 'intent': 'do', 'text': '#project-a/访谈 补充实施记录'}
    if previous is not None:
        body['continue_from'] = previous
    if thread is not None:
        body['thread_id'] = thread
    with override(style='@1'):
        response = values.client.post('/api/v2/workbench/turns', json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _capture(values, *, patch=False):
    respond, mains = values.models.handler, []

    def external_response(messages, **options):
        if _is_main(messages):
            mains.append(messages)
            if patch:
                return json.dumps({'type': 'complete', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': UPDATED}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    return mains


@pytest.mark.parametrize('continuing', [False, True])
def test_actual_main_uses_only_confirmed_scoped_style_and_saved_source_authority(scenario, continuing):
    values = scenario
    previous = thread = None
    if continuing:
        original, delivered = completed(values, summary=BIRTH)
        previous, thread = delivered['receipt']['do']['document_id'], original['thread_id']
        values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
    else:
        values.summary = BIRTH
    selected = _styles(values)
    mains = _capture(values, patch=continuing)
    started = _start(values, previous=previous, thread=thread)
    result = wait_product(values, started)['receipt']['do']
    assert result['state'] == 'done', result
    execution = values.records.read('v2_task_executions', started['turn']['id'])
    request = execution.payload['request']
    frozen = json.loads(request['input']['text'])
    style = frozen['style_input']
    assert set(style) == {'version', 'text', 'count', 'tokens', 'selected'}
    assert style['version'] == '@1' and style['count'] == 4 and 0 < style['tokens'] <= 400
    assert {row['id'] for row in style['selected']} == {row.id for row in selected}
    assert all(text not in style['text'] for text in ('三列', '私密规则', '一页', '口语', '采购预算'))
    binding = values.records.read('v2_task_styles', request['turn_id'])
    assert binding.revision == 1 and binding.payload['input_refs'] == request['input']['refs']
    assert len(mains) == 1
    assert sum(message['role'] == 'system' and message['content'] == style['text']
               for message in mains[0]) == 1
    assert all(not any(message['content'] == style['text'] for message in wire)
               for wire in values.models.calls if not _is_main(wire))
    assert len(request['privacy']['material_refs']) == len({json.dumps(ref, sort_keys=True)
               for ref in request['privacy']['material_refs']})
    for item in binding.payload['style']['items']:
        assert {'type': 'recognition', 'id': item['id'], 'revision': item['revision'],
                'project_id': item['project_id']} in request['privacy']['material_refs']
        assert item['snapshot'] in request['privacy']['source_snapshots']
    if continuing:
        assert frozen['outcome_input']['current_markdown'] == CURRENT
        assert result['continues']['document_id'] == previous
        assert values.documents.markdown(previous) == CURRENT
        assert values.documents.markdown(result['document_id']) == CURRENT.replace('用户亲改的完整段落。\n\n', UPDATED)
    parts = {part['key']: part for part in result['context']['parts']}
    assert parts['style'] == {'key': 'style', 'count': 4, 'tokens': style['tokens']}
    if continuing:
        assert parts['previous']['count'] == 1 and parts['previous']['tokens'] > 0
    else:
        assert result['continues'] is None and result['changes'] == [] and result['fallback_new'] is False
        assert 'previous' not in parts
    assert not {'text', 'basis', 'selected'} & set(parts['style'])
