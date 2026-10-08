"""离线时效题使用固定标注和真实召回，不把策略控制当作生成质量。"""
from pathlib import Path

from tools.memory_eval import score_selection


def test_freshness_hit_requires_current_evidence_to_lead_not_merely_be_present():
    case = {'category': 'freshness', 'expected_ids': ['current'],
            'expected_first_id': 'current', 'expected_evidence': {'current': '120元'}}
    old = {'id': 'expired', 'excerpt': '80元'}
    current = {'id': 'current', 'excerpt': '120元'}
    assert score_selection(case, [old, current], {}, 1)['hit'] is False
    assert score_selection(case, [current, old], {}, 1)['hit'] is True
    assert score_selection(case, [], {}, 0)['hit'] is False
    assert score_selection(case, [{**current, 'excerpt': '无价格'}], {}, 1)['hit'] is False


def test_freshness_fixture_extends_the_original_corpus_without_replacing_it():
    import json
    fixture = json.loads((Path(__file__).parents[2] / 'fixtures/memory_eval/freshness.json').read_text(encoding='utf8'))
    assert fixture['base_corpus'] == 'corpus.json'
    assert not {'documents', 'insights', 'questions'} & fixture.keys()
    cases = fixture['freshness']
    assert len(cases['questions']) == 6 and len(cases['insights']) == 12
    by_id = {row['id']: row for row in cases['insights']}
    for question in cases['questions']:
        assert question['expected_first_id'] in question['expected_ids']
        current = by_id[question['expected_first_id']]
        assert current['conditions'] == ['2026年']
        assert current['project_id'] == question['project_id']
        assert question['expected_evidence'][current['id']] in current['text']
