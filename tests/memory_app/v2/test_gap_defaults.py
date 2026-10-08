"""验证默认策略通过真实 ASK 与资料库接口生成待补清单。"""
from tests.memory_app.v2.test_workbench_ask import env, ask


def test_default_gap_policy_lists_real_no_match_without_model(env):
    env.model.allowed = False
    response = ask(env)
    assert response.status_code == 200, response.text
    assert response.json()['turn']['receipt']['ask']['no_match'] is True
    listed = env.http.get('/api/v2/library/gaps', params={'project_id': 'alpha'})
    assert listed.status_code == 200, listed.text
    items = listed.json()['items']
    assert len(items) == 1
    assert set(items[0]) == {'id', 'scene', 'text', 'count', 'last_at'}
    assert items[0]['text'] == 'alpha beta gamma?'
    assert items[0]['scene'] is None and items[0]['count'] == 1
    assert env.model.calls == 0
