import json
from pathlib import Path

import pytest

from backend.memory_app.v2.policies import ACTIVE, get


def test_explicit_policy_registry_and_labeled_synthetic_cases():
    fixture = Path(__file__).parents[2] / 'fixtures/steer/cases.json'
    cases = json.loads(fixture.read_text(encoding='utf-8'))
    assert cases['synthetic'] is True
    assert len(cases['cases']) >= 40
    policy = get('steer', version='@1')
    for case in cases['cases']:
        assert policy(case['text'])['category'] == case['label'], case['id']
    assert ACTIVE['steer'] == '@2'
    assert ACTIVE['consolidate'] == '@3'
    assert get('steer') is get('steer', version='@2')
    assert 'stale' not in ACTIVE


def test_steer_quota_characters_followup_and_visible_tail():
    policy = get('steer', version='@1')
    assert policy.validate('字' * 2000, count=4) == '字' * 2000
    for text, count in [('字' * 2001, 0), ('', 0), ('修改', 5)]:
        with pytest.raises(ValueError):
            policy.validate(text, count=count)
    assert policy.followup('2026-11-03T00:00:00+00:00', '2026-11-03T00:10:00+00:00')
    assert not policy.followup('2026-11-03T00:00:00+00:00', '2026-11-03T00:10:01+00:00')
    assert not policy.followup('2026-11-03T00:00:00+00:00', '2026-11-02T23:59:59+00:00')
    assert policy.tail('已显示' * 101) == ('已显示' * 101)[-300:]
    assert policy('以后都用表格')['direction'] != policy('以后都不要用表格')['direction']


def test_stale_ignores_review_and_automatic_memory_state():
    policy = get('stale', version='@1')
    frozen = {'kind': 'insight', 'id': 'i1', 'project_id': 'p1', 'revision': 1, 'content': '原文'}
    current = {**frozen, 'available': True, 'revision': 2, 'reviewed': True,
               'strength': 0.2, 'auto_forgotten': True}
    assert policy(frozen,current,reference='2026-11-03T00:00:00+00:00')['reasons'] == []
    current.update(content='修改正文',manual_forgotten=True)
    assert policy(frozen,current,reference='2026-11-03T00:00:00+00:00')['reasons'] == ['edited','forgotten']
    assert policy(frozen,None,reference='2026-11-03T00:00:00+00:00')['verified'] is False
