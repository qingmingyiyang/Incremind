"""Exercise applicable methods through every real organization model caller."""
from copy import deepcopy
from types import SimpleNamespace

from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_divided_do import _real_kernel_drafts
from tests.memory_app.v2.test_profile import publish
from tests.memory_app.v2.test_situation_methods import method
from tests.memory_app.v2.test_workbench_do import env


def test_main_steward_and_experts_share_frozen_methods_separate_from_profile(env):
    client, model = env
    service = client.app.state.recognition_service
    domain = SimpleNamespace(records=service.records, service=service)
    publish(domain, 'I prefer explicit verifiable conclusions.')
    one = method(domain, '先列清可验收的交付要求', ['准备方案并汇总时'], project='project-a')
    with override(retrieve='@2', compose='@2'):
        _real_kernel_drafts(env)
    assert len(model.calls) >= 5
    records = domain.records
    execution = records.list('v2_task_executions')[0]
    request = execution.payload['request']
    profile = records.read('v2_task_profiles', request['turn_id']).payload['profile']
    frozen = records.read('v2_task_methods', request['turn_id']).payload
    for messages in model.calls:
        assert messages[0] == {'role': 'system', 'content': profile['text']}
        assert messages[1] == {'role': 'system', 'content': frozen['text']}
        assert '情境补全方法：' in messages[1]['content']
        assert '先列清可验收的交付要求' in messages[1]['content']
        assert sum(message['content'].count('先列清可验收的交付要求') for message in messages) == 1
    assert one.id not in {row['id'] for row in profile['items']}
    product_turn = records.read('v2_turns', frozen['turn_id'])
    thread = client.get(f"/api/v2/workbench/threads/{product_turn.payload['thread_id']}?project_id=project-a")
    assert thread.status_code == 200, thread.text
    context = thread.json()['turns'][0]['receipt']['do']['context']
    assert context['entries'] == [{'layer': 'insight', 'id': one.id, 'title': '已发布认识',
                                   'supplemented': True, 'object_revision': 1}]
    assert {part['key']: part['count'] for part in context['parts']}['source'] == 0
    before = deepcopy(product_turn)
    url = f"/api/v2/workbench/turns/{frozen['turn_id']}/context-feedback"
    data = {
        'project_id': 'project-a', 'object_kind': 'recognition', 'object_id': one.id,
        'object_revision': 1, 'expected_revision': 0}
    assert client.post(url, json={**data, 'project_id': 'other'}).status_code == 404
    assert client.post(url, json={**data, 'object_revision': 2}).status_code == 409
    assert client.post(url, json={**data, 'object_kind': 'candidate'}).status_code == 400
    response = client.post(url, json=data)
    assert response.status_code == 200, response.text
    assert records.read('v2_turns', frozen['turn_id']) == before
    assert records.read('v2_task_methods', request['turn_id']).payload == frozen
