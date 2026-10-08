from copy import deepcopy

import pytest

from backend.memory_app.v2.workbench import _ask_receipt
from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_workbench_ask import env as _env, ask
from tests.memory_app.v2.test_insight_validity import publish, supersede, JUNE

env = _env


@pytest.mark.parametrize(
    'layer,temporal,validity,historical',
    [
        ('L3', True, {'valid_from': '2026-03-12T00:00:00+08:00',
                       'valid_until': '2026-06-12T00:00:00+08:00'}, True),
        ('L3', True, {'valid_from': '2026-10-05T00:00:00+08:00',
                       'valid_until': None}, False),
        ('L3', False, {'valid_from': '2026-03-12T00:00:00+08:00',
                        'valid_until': '2026-06-12T00:00:00+08:00'}, False),
        ('L3', False, {'valid_from': '2026-10-05T00:00:00+08:00',
                        'valid_until': None}, False),
        ('L3', True, {}, False),
        ('L2', True, {'valid_from': '2026-03-12T00:00:00+08:00',
                       'valid_until': '2026-06-12T00:00:00+08:00'}, False),
    ],
)
def test_receipt_marks_only_historical_superseded_recognitions(layer, temporal, validity, historical):
    plan = {'trace': [], 'profile': {'count': 0}}
    chosen = [{'layer': layer, 'entry': {'id': 'recognition-q49-march',
                                        'document_id': 'document-q49-march'},
               'temporal': temporal, 'validity': validity}]
    result = {'answer': '合成回答[1]', 'sources': [{
        'number': 1, 'title': '春港展会在北厅办小型展',
        'excerpt': '春港展会在北厅办小型展',
        'coordinate_space': 'recognition_content_v1',
        'windows': [{'start': 0, 'end': 11}],
    }]}
    original = deepcopy((plan, chosen, result))
    receipt = _ask_receipt(plan, chosen, result, 'egress-q49')
    citation = receipt['citations'][0]
    if historical:
        assert citation.get('historical') is True
    else:
        assert 'historical' not in citation
    assert citation['n'] == 1
    assert citation['quote'] == result['sources'][0]['excerpt']
    assert citation['locator'] == {
        'coordinate_space': result['sources'][0]['coordinate_space'],
        'windows': result['sources'][0]['windows'],
    }
    assert (plan, chosen, result) == original


def test_real_workbench_preserves_temporal_mapping_through_answer_execution(env):
    old = publish(env, '春港展会在北厅办小型展')
    future = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, future)
    with override(retrieve='@3', compose='@3'):
        response = ask(env, text='春港展会在2026年三月时怎么想的？')
    assert response.status_code == 200, response.text
    saved = response.json()
    receipt = saved['turn']['receipt']['ask']
    assert [citation['id'] for citation in receipt['citations']] == [old.id]
    assert all(citation.get('historical') is True for citation in receipt['citations'])
    assert env.model.messages[-1]['content'].count('当时有效') == 1
    assert '2026-03-12' in env.model.messages[-1]['content']
    assert '2026-06-12' in env.model.messages[-1]['content']
    assert env.model.calls == 2  # Actual query expansion followed by the answer.
    history = env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha")
    assert history.status_code == 200
    assert history.json()['turns'][0]['receipt']['ask'] == receipt
    assert env.model.calls == 2
